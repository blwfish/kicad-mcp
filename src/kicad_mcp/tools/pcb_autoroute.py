"""PCB autorouting via FreeRouter (Specctra DSN/SES pipeline)."""

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)

# Known locations for the FreeRouter JAR
_FREEROUTER_SEARCH_PATHS = [
    # Alongside the kicad-mcp-extensions project
    "/Volumes/Files/claude/KiCAD-mcp-extensions/freerouting-2.1.0.jar",
    # User home
    os.path.expanduser("~/freerouting-2.1.0.jar"),
    os.path.expanduser("~/freerouting.jar"),
]


def _find_freerouter_jar(explicit_path: Optional[str] = None) -> Optional[str]:
    """Find the FreeRouter JAR file."""
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path

    # Check environment variable
    env_path = os.environ.get("FREEROUTER_JAR")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Search known locations
    for path in _FREEROUTER_SEARCH_PATHS:
        if os.path.isfile(path):
            return path

    # Search in PATH for 'freerouting' command
    which = shutil.which("freerouting")
    if which:
        return which

    return None


def _find_java() -> Optional[str]:
    """Find a Java 17+ runtime."""
    java = shutil.which("java")
    if java:
        return java
    # macOS: check common install locations
    if platform.system() == "Darwin":
        candidates = [
            "/usr/bin/java",
            "/Library/Java/JavaVirtualMachines/amazon-corretto-21.jdk"
            "/Contents/Home/bin/java",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


def register_pcb_autoroute_tools(mcp: FastMCP) -> None:
    """Register PCB autorouting tools."""

    @mcp.tool()
    def autoroute_pcb(
        pcb_path: str,
        freerouter_jar: str = "",
        passes: int = 1,
        remove_zones: bool = True,
    ) -> Dict[str, Any]:
        """Autoroute a PCB using FreeRouter (Specctra DSN/SES pipeline).

        Runs the full autorouting pipeline:
        1. Optionally removes copper pour zones (FreeRouter doesn't understand them)
        2. Exports the board as a Specctra DSN file
        3. Runs FreeRouter headless autorouter
        4. Imports the routed SES session file back into the PCB

        After autorouting, re-add copper zones with add_copper_zone and
        fill them with fill_zones.

        FreeRouter is non-deterministic. Set passes > 1 to run multiple
        times and keep the result with the fewest incomplete connections.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            freerouter_jar: Path to freerouting JAR file. Auto-detected if empty.
            passes: Number of autoroute attempts (best result kept). Default 1.
            remove_zones: Remove copper pour zones before routing (recommended). Default True.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        # Find FreeRouter
        jar_path = _find_freerouter_jar(freerouter_jar or None)
        if not jar_path:
            return {
                "error": (
                    "FreeRouter JAR not found. Provide freerouter_jar path, "
                    "set FREEROUTER_JAR env var, or place freerouting-2.1.0.jar "
                    "in a known location."
                )
            }

        # Find Java
        java_path = _find_java()
        if not java_path:
            return {"error": "Java runtime not found. Install Java 17+ (e.g. Amazon Corretto)."}

        pcb_dir = os.path.dirname(os.path.abspath(pcb_path))
        pcb_basename = os.path.splitext(os.path.basename(pcb_path))[0]

        # Use a temp directory for DSN/SES files to avoid polluting the project
        work_dir = tempfile.mkdtemp(prefix="kicad_autoroute_")
        dsn_path = os.path.join(work_dir, f"{pcb_basename}.dsn")
        ses_path = os.path.join(work_dir, f"{pcb_basename}.ses")

        try:
            # Step 1: Optionally remove copper zones and export DSN
            zone_removal_info = ""
            if remove_zones:
                zone_removal_info = """
# Remove copper pour zones (FreeRouter doesn't understand them)
zones_removed = 0
zones_to_remove = []
for z in board.Zones():
    if not z.GetIsRuleArea():
        zones_to_remove.append(z)
for z in zones_to_remove:
    board.Remove(z)
    zones_removed += 1
"""
            else:
                zone_removal_info = "zones_removed = 0"

            export_script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

{zone_removal_info}

# Export Specctra DSN
pcbnew.ExportSpecctraDSN(board, {dsn_path!r})

# Save board (with zones removed if applicable)
board.Save({pcb_path!r})

# Count tracks/vias before routing
tracks = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_TRACK")
vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")

print(json.dumps({{
    "status": "ok",
    "dsn_exported": True,
    "dsn_path": {dsn_path!r},
    "zones_removed": zones_removed,
    "existing_tracks": tracks,
    "existing_vias": vias,
}}))
"""
            logger.info("Exporting DSN from %s", pcb_path)
            export_result = run_pcbnew_script(export_script, timeout=30.0)

            if "error" in export_result:
                return {"error": f"DSN export failed: {export_result['error']}"}

            if not os.path.exists(dsn_path):
                return {"error": f"DSN file was not created at {dsn_path}"}

            # Step 2: Run FreeRouter (possibly multiple passes)
            best_ses = None
            best_incomplete = float("inf")
            pass_results = []

            for pass_num in range(1, passes + 1):
                pass_ses = ses_path if passes == 1 else os.path.join(
                    work_dir, f"{pcb_basename}_pass{pass_num}.ses"
                )

                logger.info("FreeRouter pass %d/%d", pass_num, passes)

                cmd = [
                    java_path, "-jar", jar_path,
                    "-de", dsn_path,
                    "-do", pass_ses,
                    "--gui.enabled=false",
                ]

                try:
                    fr_result = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        timeout=300,  # 5 minute timeout
                        cwd=work_dir,
                    )
                except subprocess.TimeoutExpired:
                    pass_results.append({
                        "pass": pass_num,
                        "status": "timeout",
                        "incomplete": float("inf"),
                    })
                    continue

                if fr_result.returncode != 0:
                    pass_results.append({
                        "pass": pass_num,
                        "status": "error",
                        "error": fr_result.stderr[:500],
                    })
                    continue

                if not os.path.exists(pass_ses):
                    pass_results.append({
                        "pass": pass_num,
                        "status": "no_output",
                    })
                    continue

                # Parse FreeRouter output for incomplete count
                incomplete = _parse_freerouter_incomplete(fr_result.stdout)
                pass_results.append({
                    "pass": pass_num,
                    "status": "ok",
                    "incomplete": incomplete,
                })

                if incomplete < best_incomplete:
                    best_incomplete = incomplete
                    best_ses = pass_ses

            if best_ses is None:
                return {
                    "error": "All FreeRouter passes failed",
                    "passes": pass_results,
                    "dsn_path": dsn_path,
                }

            # Step 3: Import SES back into PCB
            import_script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

# Import Specctra SES
pcbnew.ImportSpecctraSES(board, {best_ses!r})
board.Save({pcb_path!r})

# Count results
tracks = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_TRACK")
vias = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_VIA")

# Count nets with unrouted connections
netinfo = board.GetNetInfo()
net_count = netinfo.GetNetCount()

print(json.dumps({{
    "status": "ok",
    "ses_imported": True,
    "tracks": tracks,
    "vias": vias,
    "net_count": net_count,
}}))
"""
            logger.info("Importing SES into %s", pcb_path)
            import_result = run_pcbnew_script(import_script, timeout=30.0)

            if "error" in import_result:
                return {"error": f"SES import failed: {import_result['error']}"}

            return {
                "status": "ok",
                "pcb_path": pcb_path,
                "freerouter_jar": jar_path,
                "zones_removed": export_result.get("zones_removed", 0),
                "tracks_before": export_result.get("existing_tracks", 0),
                "vias_before": export_result.get("existing_vias", 0),
                "tracks_after": import_result.get("tracks", 0),
                "vias_after": import_result.get("vias", 0),
                "net_count": import_result.get("net_count", 0),
                "passes_run": len(pass_results),
                "best_incomplete": best_incomplete if best_incomplete != float("inf") else None,
                "pass_results": pass_results,
                "note": (
                    "Copper zones were removed before routing. "
                    "Re-add them with add_copper_zone + fill_zones."
                    if remove_zones and export_result.get("zones_removed", 0) > 0
                    else "Routing complete."
                ),
            }

        finally:
            # Clean up temp files
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass


def _parse_freerouter_incomplete(stdout: str) -> int:
    """Parse FreeRouter stdout to find the number of incomplete connections.

    FreeRouter prints lines like:
        "0 connections not found"
        "3 connections not found"
    """
    import re

    for line in reversed(stdout.split("\n")):
        m = re.search(r"(\d+)\s+connections?\s+not\s+found", line, re.IGNORECASE)
        if m:
            return int(m.group(1))

    # Also check for "x incomplete" pattern
    for line in reversed(stdout.split("\n")):
        m = re.search(r"(\d+)\s+incomplete", line, re.IGNORECASE)
        if m:
            return int(m.group(1))

    return 0  # Assume success if no indication of failure
