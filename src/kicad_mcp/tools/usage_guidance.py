"""`get_usage_guidance` MCP tool.

A schema-visible fallback channel for the same critical rules carried in the
server's `instructions` field (see `SERVER_INSTRUCTIONS` in `server.py`). Some
MCP clients only surface `tools/list` to the model and silently drop the
`initialize.instructions` field — a callable tool reaches every client that
lists tools at all, which is all of them.

Standalone tool (not behind a router): cross-cutting meta-guidance, not a
domain operation. Static payload, no state inspection, no side effects —
costs nothing to call. Every top-level key is always present, even when a
list would otherwise be empty, so a client can distinguish "checked, nothing
to report" from a field that was simply omitted.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _usage_guidance_payload() -> dict[str, Any]:
    return {
        "avoid_these_issues": [
            {
                "issue": (
                    "Hand-routing traces with pcb(operation='add_trace'/'add_via') "
                    "instead of autorouting"
                ),
                "guidance": (
                    "LLMs cannot compute spatial clearances reliably by hand — "
                    "manually placed traces routinely short nets or violate "
                    "clearances. Use autoroute(operation='run') (wraps FreeRouter) "
                    "instead. add_trace/add_via are for minor touch-ups after "
                    "autorouting only."
                ),
                "confidence": (
                    "confirmed — stated as a mandatory rule in "
                    "AGENT-INSTRUCTIONS.md; add_trace/add_via also carry this "
                    "reminder in their own response."
                ),
            },
            {
                "issue": (
                    "Treating audit(operation='placement') reporting clean as "
                    "'the board has no placement/DRC problems'"
                ),
                "guidance": (
                    "placement only checks keepout-zone overlap and "
                    "board-outline overhang, using each footprint's bounding "
                    "box. It does not check pad-to-pad clearance "
                    "(operation='pad_clearances') or silkscreen overlap "
                    "(operation='check_silkscreen_overlaps'). Use "
                    "audit(operation='all') for a combined check before "
                    "declaring a board done."
                ),
                "confidence": (
                    "confirmed by reading _op_placement / _op_all_full in "
                    "pcb_keepout.py — placement, pad_clearances, and "
                    "check_silkscreen_overlaps are independent sub-checks; "
                    "'all' is the only operation that runs all of them."
                ),
            },
            {
                "issue": "Guessing library or footprint names from training data",
                "guidance": (
                    "KiCad library and footprint names change between "
                    "versions. Always search first: "
                    "library(operation='search', query=..., type='symbol'|'footprint')."
                ),
                "confidence": "confirmed — stated as a mandatory rule in AGENT-INSTRUCTIONS.md.",
            },
            {
                "issue": "Issuing two mutating calls against the same PCB file concurrently",
                "guidance": (
                    "PCB tools load, modify, and save the file as subprocesses. "
                    "Two concurrent mutating calls on the same file will "
                    "corrupt it. Serialize mutating pcb/schematic/autoroute/"
                    "drc(autofix) calls against one file. Read-only calls "
                    "(get_pad_positions, list_nets, list_footprints, "
                    "drc(run), audit(...)) are safe to run in parallel or "
                    "delegate to subagents."
                ),
                "confidence": "confirmed — stated as a mandatory rule in AGENT-INSTRUCTIONS.md.",
            },
        ],
        "best_practices": [
            "Follow the documented workflow order: schematic -> "
            "estimate_board_size -> pcb(create/set_outline/set_design_rules) "
            "-> place footprints (place_footprint or suggest_placement) -> "
            "assign nets (add_net/bulk_assign_pad_nets, or build_pcb_from_schematic "
            "for a schematic-driven board) -> autoroute(operation='run') -> "
            "audit(operation='all') + drc(operation='run') -> "
            "drc(operation='autofix') for any remaining violations.",
            "Verify with both audit(operation='all') and drc(operation='run') "
            "before finishing — they check different things (courtyard/keepout/"
            "clearance geometry vs. KiCad's own DRC engine, unrouted nets, and "
            "schematic parity).",
            "For a schematic-driven board, prefer build_pcb_from_schematic over "
            "manually replicating placement + net assignment + routing.",
            "Delegate read-only lookups (library search, pad positions, net "
            "lists, DRC/audit runs) to subagents to keep the main context "
            "lean; keep every mutating PCB call serialized in the main session.",
        ],
        "strategy": (
            "This server drives real KiCad tooling (pcbnew, kicad-cli, "
            "kicad-sch-api, FreeRouter) as subprocesses or in-process "
            "libraries for schematic capture and PCB layout — it is not a "
            "from-scratch geometry engine. Prefer its routers over "
            "hand-computing coordinates, guessing at KiCad file formats, or "
            "reimplementing what a router operation already does."
        ),
        "tactics": (
            "Typical flow: schematic(operation='create') -> "
            "library(operation='search') + schematic(operation='add_component'"
            "/'connect_pins_with_labels') -> schematic(operation='save'/"
            "'validate') -> estimate_board_size -> "
            "pcb(operation='create'/'set_outline'/'set_design_rules') -> "
            "place footprints -> assign nets or build_pcb_from_schematic -> "
            "autoroute(operation='run') -> audit(operation='all') + "
            "drc(operation='run') -> drc(operation='autofix') if needed -> "
            "panelize_pcb for a manufacturing panel. See AGENT-INSTRUCTIONS.md "
            "for the full workflow and TOOLS.md for the operation reference — "
            "call this tool once at the start of a session, not before every "
            "operation."
        ),
    }


def register_usage_guidance_tools(mcp: FastMCP) -> None:
    """Register the `get_usage_guidance` tool."""

    @mcp.tool()
    def get_usage_guidance() -> dict[str, Any]:
        """Read this before your first other operation in a new session.

        Returns known issues to avoid, general best practices, and a
        strategy/tactics overview for using this server well. Costs nothing
        to call, takes no arguments, has no side effects, and does not touch
        any KiCad file.
        """
        return _usage_guidance_payload()
