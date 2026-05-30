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


def parse_drc_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a kicad-cli ``pcb drc --format json`` report into DRC result fields.

    Pure — takes the already-loaded JSON dict — so the field extraction is
    unit-testable without KiCad (the seam pulled out of run_drc_via_cli). Reads
    ALL THREE violation arrays kicad-cli emits — clearance/rule ``violations``,
    ``unconnected_items`` and ``schematic_parity`` — so a board with unrouted
    nets or schematic-parity errors is NOT reported as clean (review finding
    h-drc-arrays; the old code read only ``violations``). A genuinely unknown
    future top-level key still yields zero, by design.
    """
    violations = report.get("violations", [])
    unconnected = report.get("unconnected_items", [])
    parity = report.get("schematic_parity", [])

    # Categorize rule violations by rule_id (stable across versions) with
    # type/message fallback; the other two arrays are counted as their own kinds.
    categories: Dict[str, int] = {}
    for violation in violations:
        key = (
            violation.get("rule_id")
            or violation.get("type")
            or violation.get("message", "Unknown")
        )
        categories[key] = categories.get(key, 0) + 1
    if unconnected:
        categories["unconnected"] = len(unconnected)
    if parity:
        categories["schematic_parity"] = len(parity)

    return {
        "total_violations": len(violations) + len(unconnected) + len(parity),
        "violation_categories": categories,
        "violations": violations,
        "unconnected_items": unconnected,
        "schematic_parity": parity,
        "unconnected_count": len(unconnected),
        "parity_count": len(parity),
    }


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

            parsed = parse_drc_report(drc_report)
            violation_count = parsed["total_violations"]
            logger.info("DRC completed with %d violations", violation_count)
            if ctx:
                await ctx.report_progress(70, 100)
                await ctx.info(f"DRC completed with {violation_count} violations")

            results = {
                "success": True,
                "method": "cli",
                "pcb_file": pcb_file,
                **parsed,
            }

            if ctx:
                await ctx.report_progress(90, 100)
            return results

    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.error("Error in CLI DRC: %s", e)
        results["error"] = f"Error in CLI DRC: {e}"
        return results
