"""PCB panelization via KiKit CLI."""

import logging
import os
import platform
import shutil
import subprocess
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def _find_kikit() -> Optional[str]:
    """Find the KiKit CLI binary."""
    system = platform.system()

    if system == "Darwin":
        candidates = [
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/3.9/bin/kikit",
        ]
    elif system == "Linux":
        candidates = ["/usr/local/bin/kikit"]
    elif system == "Windows":
        candidates = [r"C:\Program Files\KiCad\bin\Scripts\kikit.exe"]
    else:
        candidates = []

    for path in candidates:
        if os.path.isfile(path):
            return path

    # Fall back to PATH
    which = shutil.which("kikit")
    if which:
        return which

    return None


def register_pcb_panelize_tools(mcp: FastMCP) -> None:
    """Register PCB panelization tools."""

    @mcp.tool()
    def panelize_pcb(
        pcb_path: str,
        output_path: str = "",
        rows: int = 2,
        cols: int = 5,
        space: float = 2.0,
        cut_type: str = "vcuts",
        framing: str = "railstb",
        rail_width: float = 5.0,
        tooling: str = "3hole",
        fiducials: str = "3fid",
        mill_radius: float = 1.0,
        preset: str = "",
    ) -> Dict[str, Any]:
        """Panelize a PCB into a manufacturing panel using KiKit.

        Creates a panel with multiple copies of the input board arranged in
        a grid, with configurable cut lines, framing, tooling holes, and
        fiducials. The output is a new .kicad_pcb file.

        Args:
            pcb_path: Path to the input .kicad_pcb file.
            output_path: Output panel PCB path. Default: input name with
                         "-panel" suffix.
            rows: Number of rows in the grid (default 2).
            cols: Number of columns in the grid (default 5).
            space: Spacing between boards in mm (default 2).
            cut_type: Cut type: "vcuts" or "mousebites" (default "vcuts").
            framing: Framing type: "none", "railstb", "railslr", or "frame"
                     (default "railstb").
            rail_width: Rail/frame width in mm (default 5).
            tooling: Tooling holes: "none", "3hole", or "4hole"
                     (default "3hole").
            fiducials: Fiducial marks: "none", "3fid", or "4fid"
                       (default "3fid").
            mill_radius: Mill radius in mm for post-processing (default 1).
            preset: Path to a KiKit preset JSON file. When provided,
                    overrides all other layout parameters.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        kikit_bin = _find_kikit()
        if not kikit_bin:
            return {
                "error": (
                    "KiKit CLI not found. Install with: "
                    "pip install kikit (into KiCad's Python on macOS)"
                )
            }

        # Determine output path
        if not output_path:
            base, ext = os.path.splitext(pcb_path)
            output_path = f"{base}-panel{ext}"

        # Build CLI command
        cmd = [kikit_bin, "panelize"]

        if preset and os.path.exists(preset):
            cmd.extend(["--preset", preset])
        else:
            # Layout
            cmd.extend([
                "--layout",
                f"grid; rows: {rows}; cols: {cols}; space: {space}mm",
            ])

            # Tabs — vcuts don't need tabs, mousebites do
            if cut_type == "mousebites":
                cmd.extend(["--tabs", "fixed; width: 3mm; vcount: 1"])
            else:
                cmd.extend(["--tabs", "none"])

            # Cuts
            if cut_type == "mousebites":
                cmd.extend([
                    "--cuts",
                    f"mousebites; drill: 0.5mm; spacing: 0.8mm; offset: -0.1mm",
                ])
            else:
                cmd.extend(["--cuts", "vcuts"])

            # Framing
            if framing != "none":
                cmd.extend([
                    "--framing",
                    f"{framing}; width: {rail_width}mm",
                ])
            else:
                cmd.extend(["--framing", "none"])

            # Tooling
            if tooling != "none":
                cmd.extend([
                    "--tooling",
                    f"{tooling}; hoffset: 2.5mm; voffset: 2.5mm; size: 1.152mm",
                ])
            else:
                cmd.extend(["--tooling", "none"])

            # Fiducials
            if fiducials != "none":
                cmd.extend([
                    "--fiducials",
                    f"{fiducials}; hoffset: 5mm; voffset: 2.5mm; "
                    f"coppersize: 2mm; opening: 1mm",
                ])
            else:
                cmd.extend(["--fiducials", "none"])

            # Post-processing
            cmd.extend(["--post", f"millradius: {mill_radius}mm"])

        # Input and output
        cmd.extend([pcb_path, output_path])

        logger.info("Running KiKit: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return {"error": "KiKit panelization timed out after 60s"}

        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip()
            return {"error": f"KiKit failed: {error_msg[:2000]}"}

        if not os.path.exists(output_path):
            return {"error": f"Panel file was not created at {output_path}"}

        # Read back panel dimensions using pcbnew
        info_script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({output_path!r})

# Get board outline bounding box
bbox = board.GetBoardEdgesBoundingBox()
width_mm = round(pcbnew.ToMM(bbox.GetWidth()), 2)
height_mm = round(pcbnew.ToMM(bbox.GetHeight()), 2)

# Count footprints and nets
fp_count = len(board.GetFootprints())
net_count = board.GetNetInfo().GetNetCount()
track_count = sum(1 for t in board.GetTracks() if t.GetClass() == "PCB_TRACK")

print(json.dumps({{
    "width_mm": width_mm,
    "height_mm": height_mm,
    "footprint_count": fp_count,
    "net_count": net_count,
    "track_count": track_count,
}}))
"""
        try:
            panel_info = run_pcbnew_script(info_script, timeout=15.0)
        except Exception as e:
            logger.warning("Could not read panel info: %s", e)
            panel_info = {}

        return {
            "status": "ok",
            "input_pcb": pcb_path,
            "output_pcb": output_path,
            "grid": f"{cols}x{rows}",
            "cut_type": cut_type,
            "framing": framing,
            "width_mm": panel_info.get("width_mm"),
            "height_mm": panel_info.get("height_mm"),
            "footprint_count": panel_info.get("footprint_count"),
            "track_count": panel_info.get("track_count"),
        }
