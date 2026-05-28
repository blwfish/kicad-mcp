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
    get_drc_history,
    save_drc_result,
)
from kicad_mcp.utils.file_utils import get_project_files


def _op_history(project_path: str) -> Dict[str, Any]:
    print(f"Getting DRC history for project: {project_path}")

    if not os.path.exists(project_path):
        print(f"Project not found: {project_path}")
        return {"success": False, "error": f"Project not found: {project_path}"}

    history_entries = get_drc_history(project_path)

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
        "success": True,
        "project_path": project_path,
        "history_entries": history_entries,
        "entry_count": len(history_entries),
        "trend": trend,
    }


async def _op_run(project_path: str, ctx: Context | None) -> Dict[str, Any]:
    print(f"Running DRC check for project: {project_path}")

    if not os.path.exists(project_path):
        print(f"Project not found: {project_path}")
        return {"success": False, "error": f"Project not found: {project_path}"}

    files = get_project_files(project_path)
    if "pcb" not in files:
        print("PCB file not found in project")
        return {"success": False, "error": "PCB file not found in project"}

    pcb_file = files["pcb"]
    print(f"Found PCB file: {pcb_file}")

    if ctx:
        await ctx.report_progress(10, 100)
        await ctx.info(f"Starting DRC check on {os.path.basename(pcb_file)}")

    drc_results = None

    print("Using kicad-cli for DRC")
    if ctx:
        await ctx.info("Using KiCad CLI for DRC check...")
    drc_results = await run_drc_via_cli(pcb_file, ctx)

    # Process and save results if successful
    if drc_results and drc_results.get("success", False):
        save_drc_result(project_path, drc_results)

        comparison = compare_with_previous(project_path, drc_results)
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
        "success": False,
        "error": "DRC check failed with an unknown error",
    }


def register_drc_tools(mcp: FastMCP) -> None:
    """Register the DRC domain router."""

    @mcp.tool()
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
              -> {success, violations, violation_categories, comparison?, ...}
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
              -> {success, history_entries, entry_count, trend}
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
