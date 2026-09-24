"""DRC router — Design Rule Check operations for KiCad PCB files.

See docs/SPEC_Tool_Consolidation.md.
"""
import logging
import os
from typing import Any, Dict, Optional

from fastmcp import FastMCP, Context

logger = logging.getLogger(__name__)

from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
from kicad_mcp.tools.pcb_drc_fix import _op_autofix
from kicad_mcp.utils.drc_history import (
    compare_with_previous,
    get_drc_history_info,
    save_drc_result,
)
from kicad_mcp.utils.file_utils import get_project_files


def _op_history(project_path: str) -> Dict[str, Any]:
    logger.debug("Getting DRC history for project: %s", project_path)

    if not os.path.exists(project_path):
        logger.warning("Project not found: %s", project_path)
        return {"status": "error", "error": f"Project not found: {project_path}"}

    history_info = get_drc_history_info(project_path)
    history_entries = history_info["entries"]

    # Calculate trend information
    trend = None
    if len(history_entries) >= 2:
        first = history_entries[-1]  # Oldest entry
        last = history_entries[0]  # Newest entry

        first_violations = first.get("total_violations", 0)
        last_violations = last.get("total_violations", 0)

        if first_violations > last_violations:
            trend = "improving"
        elif first_violations < last_violations:
            trend = "degrading"
        else:
            trend = "stable"

    return {
        "status": "ok",
        "project_path": project_path,
        "history_entries": history_entries,
        "entry_count": len(history_entries),
        "trend": trend,
        # A hardcoded 10-entry cap with no flag meant a caller couldn't tell
        # "this project has 3 DRC runs ever" from "this project has 50, only
        # the newest 10 are kept". finding #106 of the 2026-09-23 review.
        "truncated": history_info["truncated"],
    }


async def _op_run(project_path: str, ctx: Context | None) -> Dict[str, Any]:
    logger.debug("Running DRC check for project: %s", project_path)

    if not os.path.exists(project_path):
        logger.warning("Project not found: %s", project_path)
        return {"status": "error", "error": f"Project not found: {project_path}"}

    files = get_project_files(project_path)
    if "pcb" not in files:
        logger.warning("PCB file not found in project")
        return {"status": "error", "error": "PCB file not found in project"}

    pcb_file = files["pcb"]
    logger.debug("Found PCB file: %s", pcb_file)

    if ctx:
        await ctx.report_progress(10, 100)
        await ctx.info(f"Starting DRC check on {os.path.basename(pcb_file)}")

    drc_results = None

    logger.debug("Using kicad-cli for DRC")
    if ctx:
        await ctx.info("Using KiCad CLI for DRC check...")
    drc_results = await run_drc_via_cli(pcb_file, ctx)

    # Process and save results if successful
    if drc_results and drc_results.get("status") == "ok":
        # Compare against history BEFORE saving the current result -- otherwise
        # save_drc_result would already have appended drc_results as the newest
        # entry, and compare_with_previous would diff it against itself (always
        # zero change, no new/resolved categories).
        comparison = compare_with_previous(project_path, drc_results)
        save_drc_result(project_path, drc_results)

        if comparison:
            drc_results["comparison"] = comparison

            if ctx:
                if comparison["change"] < 0:
                    await ctx.info(
                        f"Great progress! You've fixed {abs(comparison['change'])} "
                        f"DRC violations since the last check."
                    )
                elif comparison["change"] > 0:
                    await ctx.info(
                        f"Found {comparison['change']} new DRC violations "
                        f"since the last check."
                    )
                else:
                    await ctx.info(
                        "No change in the number of DRC violations since the last check."
                    )

    if ctx:
        await ctx.report_progress(100, 100)

    return drc_results or {
        "status": "error",
        "error": "DRC check failed with an unknown error",
    }


def register_drc_tools(mcp: FastMCP) -> None:
    """Register the DRC domain router."""

    @mcp.tool(
        annotations={
            # `run`/`history` only read; `autofix` clears tracks/vias and
            # re-routes -- can leave the board with less routing than it
            # started with if re-autoroute fails (see
            # _op_autofix's routing_regressed handling).
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    async def drc(
        operation: str,
        ctx: Context | None,
        *,
        project_path: Optional[str] = None,
        pcb_path: Optional[str] = None,
        fix_routing: bool = True,
        fix_silkscreen: bool = True,
        fix_placement: bool = True,
        autoroute_passes: int = 2,
    ) -> Dict[str, Any]:
        """Design Rule Check operations for KiCad PCB files.

        Operations:
          run(project_path)
              -> {status, violations, violation_categories, comparison?, ...}
              Run a full DRC check on a project's PCB via kicad-cli. Saves
              the result to history so subsequent runs can show a trend.

          autofix(pcb_path, project_path="", fix_routing=True,
                  fix_silkscreen=True, fix_placement=True, autoroute_passes=2)
              -> {status, before, after, actions_taken, improvement}
              Automatically fix common DRC violations. Runs DRC, categorizes
              violations, and applies fixes in order: placement (courtyard
              overlaps), routing (clearance/crossing/shorts), silkscreen
              (silk over copper/pads), zone fill. Re-runs DRC afterward to
              verify improvement and returns a before/after comparison.

          history(project_path)
              -> {status, history_entries, entry_count, trend}
              Get the DRC check history for a KiCad project. trend is
              "improving"|"degrading"|"stable"|null (null if < 2 entries).
        """
        if operation == "run":
            if project_path is None:
                return {"error": "operation='run' requires 'project_path'"}
            return await _op_run(project_path, ctx)
        if operation == "autofix":
            if pcb_path is None:
                return {"error": "operation='autofix' requires 'pcb_path'"}
            return await _op_autofix(
                pcb_path=pcb_path,
                project_path=project_path or "",
                fix_routing=fix_routing,
                fix_silkscreen=fix_silkscreen,
                fix_placement=fix_placement,
                autoroute_passes=autoroute_passes,
            )
        if operation == "history":
            if project_path is None:
                return {"error": "operation='history' requires 'project_path'"}
            return _op_history(project_path)
        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: run|autofix|history"
            )
        }
