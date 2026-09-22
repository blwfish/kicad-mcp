"""`get_usage_guidance` MCP tool.

Thin wiring of `mcp-agent-notes` (design-docs/mcp-agent-notes/SPEC.md) into
kicad-mcp's FastMCP idiom. `NOTES` is the durable, authored knowledge this
project wants any connecting assistant to know without rediscovering it live
— `SERVER_INSTRUCTIONS` in server.py renders a bounded slice of it into the
`initialize` handshake, and `get_usage_guidance` is the query-tool fallback
for clients that drop that field, plus the full-detail/topic/search surface
`instructions` deliberately can't hold (see render_instructions()'s own
docstring: it must stay small regardless of corpus size).

Distinct from the per-call escalation notes on `audit(operation="placement")`
(pcb_keepout.py) and `pcb(operation="add_trace"/"add_via")` (pcb.py) — those
are runtime, per-response signal (closer to mcp-events); this is static,
per-connection knowledge, authored ahead of time.
"""

from __future__ import annotations

from datetime import date

from fastmcp import FastMCP
from mcp_agent_notes import Note, NoteKind, Priority, find, strategy, tactics

CAPABILITY_STATEMENT = (
    'KiCad EDA — design schematics and lay out PCBs. Most tools are routers '
    'that dispatch on an `operation=` argument (e.g. '
    'pcb(operation="place_footprint"), drc(operation="autofix"), '
    'library(operation="search")); the rest are standalone '
    '(build_pcb_from_schematic, estimate_board_size, suggest_placement, '
    'panelize_pcb, get_usage_guidance).'
)

QUERY_TOOL_NAME = "get_usage_guidance"

# Dated 2026-09-20 (this migration) rather than backdated to when each rule
# was first written into AGENT-INSTRUCTIONS.md/SERVER_INSTRUCTIONS — `added`
# drives render_instructions()'s "recently added" surfacing, and these are
# newly-structured entries even though the underlying rules aren't new.
_MIGRATED = date(2026, 9, 20)

NOTES: tuple[Note, ...] = (
    Note(
        id="never-hand-route",
        added=_MIGRATED,
        priority=Priority.CRITICAL,
        kind=NoteKind.TACTIC,
        summary=(
            "Never hand-route with pcb(operation='add_trace'/'add_via') for "
            "more than a touch-up — use autoroute(operation='run') instead."
        ),
        detail=(
            "LLMs cannot compute spatial clearances reliably by hand — "
            "manually placed traces routinely short nets or violate "
            "clearances. autoroute(operation='run') wraps FreeRouter and "
            "solves routing in seconds with zero violations. add_trace/"
            "add_via exist only for minor touch-ups after autorouting."
        ),
        tags=("routing",),
        addresses=(
            "trace placement fails clearance",
            "manual routing shorts a net",
            "how do I route a board",
            "add_trace",
            "add_via",
        ),
    ),
    Note(
        id="never-guess-library-names",
        added=_MIGRATED,
        priority=Priority.CRITICAL,
        kind=NoteKind.TACTIC,
        summary=(
            "Never guess library or footprint names from training data — "
            "they change between KiCad versions. Search first with "
            "library(operation='search')."
        ),
        tags=("library",),
        addresses=(
            "symbol not found",
            "footprint not found",
            "unknown lib_id",
            "library name wrong",
        ),
    ),
    Note(
        id="no-concurrent-pcb-writes",
        added=_MIGRATED,
        priority=Priority.CRITICAL,
        kind=NoteKind.TACTIC,
        summary=(
            "Never issue two mutating calls against the same PCB file "
            "concurrently — serialize them."
        ),
        detail=(
            "PCB tools load, modify, and save the file as subprocesses. Two "
            "concurrent mutating calls on the same file will corrupt it. "
            "Serialize mutating pcb/schematic/autoroute/drc(autofix) calls "
            "against one file. Read-only calls (get_pad_positions, "
            "list_nets, list_footprints, drc(run), audit(...)) are safe to "
            "run in parallel or delegate to subagents."
        ),
        tags=("pcb",),
        addresses=(
            "PCB file corrupted",
            "board file unreadable after edit",
            "parallel PCB edits",
        ),
    ),
    Note(
        id="verify-before-finishing",
        added=_MIGRATED,
        priority=Priority.HIGH,
        kind=NoteKind.TACTIC,
        summary=(
            "Verify with both audit(operation='all') and "
            "drc(operation='run') before declaring a board done — they "
            "check different things."
        ),
        detail=(
            "audit(operation='all') checks courtyard/keepout/clearance "
            "geometry directly; drc(operation='run') runs KiCad's own DRC "
            "engine, which also catches unrouted nets and schematic-parity "
            "errors that audit doesn't see. Neither alone is a complete "
            "check."
        ),
        tags=(),
        addresses=("is the board done", "final checks before manufacturing"),
    ),
    Note(
        id="audit-placement-blind-spot",
        added=_MIGRATED,
        priority=Priority.HIGH,
        kind=NoteKind.TACTIC,
        summary=(
            "audit(operation='placement') reporting clean does not mean "
            "the board is clean — it only checks keepout/board-edge "
            "geometry. Use audit(operation='all') for pad clearances and "
            "silkscreen too."
        ),
        detail=(
            "placement checks keepout-zone overlap and board-outline "
            "overhang only, using each footprint's bounding box. It does "
            "not check pad-to-pad clearance (operation='pad_clearances') "
            "or silkscreen overlap (operation='check_silkscreen_overlaps'). "
            "This is the same class of gap as FreeCAD's check_solid "
            "reporting clean on a Part::Compound with a bad child — a "
            "check that's correct for what it covers, but narrower than "
            "its name suggests."
        ),
        tags=("audit",),
        addresses=(
            "placement passed but DRC still fails",
            "board looks clean but has violations",
            "pad clearance violation after clean audit",
        ),
    ),
    Note(
        id="workflow-overview",
        added=_MIGRATED,
        priority=Priority.HIGH,
        kind=NoteKind.STRATEGY,
        summary=(
            "Typical flow: schematic -> estimate_board_size -> pcb setup -> "
            "place footprints -> assign nets -> autoroute -> audit+drc."
        ),
        detail=(
            "schematic(operation='create') -> library(operation='search') + "
            "schematic(operation='add_component'/'connect_pins_with_labels') "
            "-> schematic(operation='save'/'validate') -> "
            "estimate_board_size -> "
            "pcb(operation='create'/'set_outline'/'set_design_rules') -> "
            "place footprints (place_footprint or suggest_placement) -> "
            "assign nets (add_net/bulk_assign_pad_nets, or "
            "build_pcb_from_schematic for a schematic-driven board) -> "
            "autoroute(operation='run') -> audit(operation='all') + "
            "drc(operation='run') -> drc(operation='autofix') for any "
            "remaining violations -> panelize_pcb for a manufacturing "
            "panel."
        ),
        tags=(),
        addresses=(
            "where do I start",
            "how do I design a PCB from scratch",
            "workflow order",
        ),
    ),
    Note(
        id="prefer-schematic-driven-pipeline",
        added=_MIGRATED,
        priority=Priority.MEDIUM,
        kind=NoteKind.TACTIC,
        summary=(
            "For a schematic-driven board, prefer build_pcb_from_schematic "
            "over manually replicating placement + net assignment + "
            "routing."
        ),
        tags=("pcb", "schematic"),
        addresses=("build PCB from schematic", "manual net assignment"),
    ),
    Note(
        id="delegate-read-only-to-subagents",
        added=_MIGRATED,
        priority=Priority.MEDIUM,
        kind=NoteKind.TACTIC,
        summary=(
            "Delegate read-only lookups (library search, pad positions, "
            "net lists, DRC/audit runs) to subagents to keep the main "
            "context lean; keep mutating PCB calls serialized in the main "
            "session."
        ),
        tags=(),
        addresses=("context window filling up", "keep conversation lean"),
    ),
    Note(
        id="firmware-design-narrow-envelope",
        added=_MIGRATED,
        priority=Priority.MEDIUM,
        kind=NoteKind.STRATEGY,
        summary=(
            "design's deterministic firmware import only covers a narrow, "
            "explicit envelope (specific MCUs/toolchains/pin styles) — "
            "outside it, author a design-intent via intent_template rather "
            "than expecting silent best-effort."
        ),
        tags=("design", "firmware"),
        addresses=(
            "firmware import failed",
            "design tool doesn't recognize my board",
            "unsupported MCU",
        ),
        location="docs/FIRMWARE_FRONTEND_SCOPE.md",
    ),
)


def register_usage_guidance_tools(mcp: FastMCP) -> None:
    """Register the `get_usage_guidance` query tool (mcp-agent-notes §8b)."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def get_usage_guidance(
        operation: str = "strategy",
        *,
        topic: str | None = None,
        problem: str | None = None,
    ) -> str:
        """Query onboarding knowledge: known issues, best practices, workflow.

        Call `get_usage_guidance()` (operation="strategy", no topic) once at
        the start of a session — costs nothing, no side effects.

        Operations:
          strategy(topic=None) -> tiered overview of high-level guidance:
              full detail for universal/always-relevant notes, one-liners
              for topic-scoped ones. strategy(topic="routing") -> full
              detail for that one topic, regardless of relevance state.
          tactics(topic=None) -> list of tactical topics available.
              tactics(topic="pcb") -> full detail for that topic.
          find(problem="...") -> notes ranked by relevance to a free-text
              problem description (both strategy and tactic notes).
        """
        if operation == "strategy":
            return strategy(NOTES, topic=topic, active_topics=frozenset())
        if operation == "tactics":
            return tactics(NOTES, topic=topic)
        if operation == "find":
            if not problem:
                return "error: operation='find' requires 'problem'"
            return find(NOTES, problem)
        return f"error: unknown operation {operation!r}; valid: strategy|tactics|find"
