"""`analyze_placement_telemetry` MCP tool.

Per `docs/SPEC_Feedback_Infrastructure.md`. Reads from the local telemetry DB
populated by `kicad_mcp.utils.telemetry`. Read-only; does not write.

Standalone tool (not behind a router). Telemetry is meta-operational and
belongs at the top level alongside other utility tools.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import Context, FastMCP

from kicad_mcp.utils import telemetry

logger = logging.getLogger(__name__)


_SUPPORTED_QUERIES = ("calibration_table", "convergence_stats", "system_events")


def register_telemetry_analyze_tools(mcp: FastMCP) -> None:
    """Register the `analyze_placement_telemetry` tool."""

    @mcp.tool()
    async def analyze_placement_telemetry(
        query: str,
        filters: dict[str, Any] | None = None,
        verbosity: str = "minimal",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Read telemetry data for calibration and convergence analysis.

        Args:
            query: One of `calibration_table`, `convergence_stats`, `system_events`.
            filters: Query-specific filter dict (see below). All filters optional.
            verbosity: `minimal` (default) for terse rows, `full` for additional
                provenance fields.

        Filter shapes per query:
            calibration_table: {"warning_type": str, "since": ISO-8601}
            convergence_stats: {"schematic_hash": str, "since": ISO-8601}
            system_events:     {"severity": "info"|"warn"|"error", "unseen_only": bool,
                                "since": ISO-8601, "limit": int}

        Returns: {"status": "ok", "rows": [...]} or {"status": "error", "error": "..."}.
        On a fresh install with no data, returns rows=[] (never errors).
        """
        if query not in _SUPPORTED_QUERIES:
            return {
                "status": "error",
                "error": f"Unknown query {query!r}; supported: {list(_SUPPORTED_QUERIES)}",
            }

        if verbosity not in ("minimal", "full"):
            return {
                "status": "error",
                "error": f"Unknown verbosity {verbosity!r}; supported: minimal, full",
            }

        f = filters or {}

        try:
            if query == "calibration_table":
                rows = telemetry.query_calibration_table(
                    warning_type=f.get("warning_type"),
                    since=f.get("since"),
                    include_samples=(verbosity == "full"),
                )
            elif query == "convergence_stats":
                rows = telemetry.query_convergence_stats(
                    schematic_hash=f.get("schematic_hash"),
                    since=f.get("since"),
                )
            else:  # system_events
                rows = telemetry.query_system_events(
                    severity=f.get("severity"),
                    unseen_only=bool(f.get("unseen_only", False)),
                    since=f.get("since"),
                    limit=int(f.get("limit", 100)),
                )
        except Exception as e:
            logger.warning(f"analyze_placement_telemetry({query=}) failed: {e}")
            if ctx:
                await ctx.info(f"Query {query!r} failed: {e}")
            return {"status": "error", "error": str(e)}

        result: dict[str, Any] = {"status": "ok", "rows": rows}
        if verbosity == "full":
            result["query"] = query
            result["filters_applied"] = f
            result["row_count"] = len(rows)
        return result
