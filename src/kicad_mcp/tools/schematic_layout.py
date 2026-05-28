"""``schematic_layout`` router — topology-aware schematic placement.

See ``docs/SPEC_Schematic_Placement.md``. Slice 1 ships ``operation="suggest"``
with Layer 1 (topology) only; later layers / phases / operations land in
subsequent slices.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp_events import emit_event, event_context

from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
from kicad_mcp.utils.placement import state as placement_state
from kicad_mcp.utils.placement.topology import cluster_components

logger = logging.getLogger(__name__)


def register_schematic_layout_tools(mcp: FastMCP) -> None:
    """Register the ``schematic_layout`` router."""

    @mcp.tool()
    def schematic_layout(
        operation: str,
        schematic_path: str | None = None,
        verbosity: str = "minimal",
    ) -> dict:
        """Topology-aware automatic placement of schematic components.

        Operations (Slice 1):
          suggest — compute a placement state for the given schematic.
                    Currently returns Layer 1 (topology) clustering only.
                    Layers 2-4 + Phases 2-3 + conventions land in later slices.

        Args:
          schematic_path : Path to .kicad_sch. Required in Slice 1.
          verbosity      : "minimal" (default) or "full". Controls output shape
                           per spec § Verbosity modes.
        """
        if operation == "suggest":
            return _op_suggest(
                schematic_path=schematic_path,
                verbosity=verbosity,
            )
        return {
            "status": "error",
            "code": "unknown_operation",
            "message": (
                f"Unknown operation: {operation!r}. Valid: suggest. "
                "(apply / clear_cache land in Slice 5.)"
            ),
        }


def _op_suggest(
    *,
    schematic_path: str | None,
    verbosity: str,
) -> dict[str, Any]:
    if not schematic_path:
        return {
            "status": "error",
            "code": "missing_parameter",
            "message": "schematic_path is required (Slice 1 has no loaded-schematic fallback).",
        }
    if verbosity not in ("minimal", "full"):
        return {
            "status": "error",
            "code": "invalid_parameter",
            "message": f"verbosity must be 'minimal' or 'full', got {verbosity!r}.",
        }

    sch_path = Path(schematic_path)
    if not sch_path.exists():
        return {
            "status": "error",
            "code": "schematic_not_found",
            "message": f"Schematic file not found: {schematic_path}",
        }

    with event_context() as events:
        netlist = extract_netlist_via_cli(str(sch_path))
        if netlist is None:
            return {
                "status": "error",
                "code": "netlist_extraction_failed",
                "message": (
                    "kicad-cli netlist export failed. Verify kicad-cli is on PATH "
                    "and the schematic is parseable."
                ),
            }

        state = placement_state.new_state(
            schematic_path=str(sch_path),
            schematic_hash=placement_state.schematic_hash(sch_path),
        )

        partition = cluster_components(netlist, schematic_path=str(sch_path))

        # Bridge Layer 1 results into the PlacementState skeleton.
        for cid, refs in partition.members.items():
            state["clusters"][cid] = {
                "members": refs,
                "anchor": None,
                "label": "unclassified",
                "label_confidence": 0.0,
                "label_source": "topology_only",
                "tier": None,
                "bbox_mm": {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
            }
        for ref, cid in partition.component_cluster.items():
            state["components"][ref] = {
                "x_mm": 0.0,
                "y_mm": 0.0,
                "rotation": 0,
                "mirror_x": False,
                "cluster_id": cid,
                "fixed_by": None,
            }

        # Emit layer-1 events through mcp-events so callers see them via
        # the standard envelope.
        for ev in partition.events:
            emit_event(ev["level"], ev["code"], ev["message"], ev.get("data", {}))

        # Sliced output: drop bbox_mm/label_source from the in-memory state
        # only for minimal mode (kept in full).
        trimmed = placement_state.trim_for_verbosity(state, verbosity)

        result: dict[str, Any] = {
            "status": "ok",
            "state": trimmed,
            "state_id": state["state_id"],
        }
        if events.has_any("warn") or events.has_any("error") or events.has_any("info"):
            result["events"] = events.to_envelope()
        return result
