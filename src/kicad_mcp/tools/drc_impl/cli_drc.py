"""
Design Rule Check (DRC) implementation using KiCad command-line interface.
"""
import json
import logging
import os
import subprocess
import tempfile
from typing import Any, Dict

from fastmcp import Context

from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path

logger = logging.getLogger(__name__)


async def run_drc_via_cli(
    pcb_file: str, ctx: Context | None
) -> Dict[str, Any]:
    """Run DRC using KiCad command line tools.

    Args:
        pcb_file: Path to the PCB file (.kicad_pcb)
        ctx: MCP context for progress reporting

    Returns:
        Dictionary with DRC results
    """
    results: Dict[str, Any] = {
        "success": False,
        "method": "cli",
        "pcb_file": pcb_file,
    }

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "drc_report.json")

            try:
                kicad_cli = get_kicad_cli_path(required=True)
            except KiCadCLIError as e:
                logger.warning("kicad-cli not available: %s", e)
                results["error"] = str(e)
                return results
            # required=True guarantees a non-None path (else KiCadCLIError above)
            assert kicad_cli is not None

            if ctx:
                await ctx.report_progress(50, 100)
                await ctx.info("Running DRC using KiCad CLI...")

            cmd = [
                kicad_cli,
                "pcb",
                "drc",
                "--format",
                "json",
                "--output",
                output_file,
                pcb_file,
            ]

            logger.debug("Running command: %s", " ".join(cmd))
            process = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            if process.returncode != 0:
                logger.warning("DRC command failed (code %s): %s",
                               process.returncode, process.stderr)
                results["error"] = f"DRC command failed: {process.stderr}"
                return results

            if not os.path.exists(output_file):
                logger.warning("DRC report file not created")
                results["error"] = "DRC report file not created"
                return results

            with open(output_file, "r") as f:
                try:
                    drc_report = json.load(f)
                except json.JSONDecodeError:
                    logger.warning("Failed to parse DRC report JSON")
                    results["error"] = "Failed to parse DRC report JSON"
                    return results

            violations = drc_report.get("violations", [])
            violation_count = len(violations)
            logger.info("DRC completed with %d violations", violation_count)
            if ctx:
                await ctx.report_progress(70, 100)
                await ctx.info(f"DRC completed with {violation_count} violations")

            # Categorize violations by rule_id (stable) with message fallback
            error_types: dict[str, int] = {}
            for violation in violations:
                # rule_id is stable across KiCad versions; message text can change
                error_type = (
                    violation.get("rule_id")
                    or violation.get("type")
                    or violation.get("message", "Unknown")
                )
                error_types[error_type] = error_types.get(error_type, 0) + 1

            results = {
                "success": True,
                "method": "cli",
                "pcb_file": pcb_file,
                "total_violations": violation_count,
                "violation_categories": error_types,
                "violations": violations,
            }

            if ctx:
                await ctx.report_progress(90, 100)
            return results

    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.error("Error in CLI DRC: %s", e)
        results["error"] = f"Error in CLI DRC: {e}"
        return results
