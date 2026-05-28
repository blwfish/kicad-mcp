"""``schematic_layout`` router — topology-aware schematic placement.

See ``docs/SPEC_Schematic_Placement.md``. Current slices:

  Slice 1 — operation ``suggest`` + Layer 1 (topology)
  Slice 2 — Layers 2/3/4 (pattern_recognition + LCSC + caller hints)

Operations ``apply`` and ``clear_cache`` land in Slice 5.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp_events import emit_event, event_context

from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
from kicad_mcp.utils.placement import state as placement_state
from kicad_mcp.utils.placement.labeling import label_clusters
from kicad_mcp.utils.placement.topology import cluster_components

logger = logging.getLogger(__name__)


def _lcsc_lookup(lcsc_id: str) -> dict[str, Any] | None:
    """Thin shim over ``lcsc_db.get_component`` for Layer 3.

    Returns ``None`` whenever the lookup can't run (DB missing, ToS not
    accepted, snapshot unavailable, etc.) so Layer 3 degrades silently
    to Layer 2 per spec.
    """
    try:
        from kicad_mcp.utils.lcsc_db import db_exists, get_component
        if not db_exists():
            return None
        return get_component(lcsc_id)
    except Exception:
        return None


def register_schematic_layout_tools(mcp: FastMCP) -> None:
    """Register the ``schematic_layout`` router."""

    @mcp.tool()
    def schematic_layout(
        operation: str,
        schematic_path: str | None = None,
        verbosity: str = "minimal",
        hints: dict[str, str] | None = None,
    ) -> dict:
        """Topology-aware automatic placement of schematic components.

        Operations:
          suggest — compute a placement state for the given schematic.
                    Returns Layer 1 (topology) clustering + Layers 2/3/4
                    labeling. Ranking and packing land in later slices.

        Args:
          schematic_path : Path to .kicad_sch. Required.
          verbosity      : "minimal" (default) or "full". Per spec § Verbosity.
          hints          : Optional ``{ref: label}`` override map (Layer 4).
                           ``label`` must be one of the canonical labels:
                           mcu, ldo, switching_regulator, op_amp, filter,
                           oscillator, digital_interface, sensor, connector,
                           crystal, unclassified. Unknown labels are ignored.
        """
        if operation == "suggest":
            return _op_suggest(
                schematic_path=schematic_path,
                verbosity=verbosity,
                hints=hints,
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
    hints: dict[str, str] | None,
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

        # Layers 2/3/4 — labeling. LCSC lookup is wired through
        # ``_lcsc_lookup`` so tests can monkeypatch it without dragging
        # in the LCSC subsystem.
        labels, label_events = label_clusters(
            partition.members,
            netlist,
            hints=hints,
            lcsc_lookup_fn=_lcsc_lookup,
        )
        hints_applied = sorted(
            ref for ref in (hints or {})
            if ref in partition.component_cluster
        )
        state["inputs_honored"]["hints_applied"] = hints_applied

        # Bridge results into the PlacementState skeleton.
        for cid, refs in partition.members.items():
            cl = labels[cid]
            state["clusters"][cid] = {
                "members": refs,
                "anchor": cl.anchor,
                "label": cl.label,
                "label_confidence": cl.label_confidence,
                "label_source": cl.label_source,
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

        # Surface layer-1 + layer-2 events through mcp-events.
        for ev in partition.events:
            emit_event(ev["level"], ev["code"], ev["message"], ev.get("data", {}))
        for ev in label_events:
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
