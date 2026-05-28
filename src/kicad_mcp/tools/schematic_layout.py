"""``schematic_layout`` router — topology-aware schematic placement.

See ``docs/SPEC_Schematic_Placement.md``. Current slices:

  Slice 1 — operation ``suggest`` + Layer 1 (topology)
  Slice 2 — Layers 2/3/4 (pattern_recognition + LCSC + caller hints)
  Slice 3 — Phase 2 ranking + Phase 3 packing
  Slice 4 — v1 convention rules
  Slice 5 — apply + stateful cache + clear_cache

Telemetry integration lands in Slice 6.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp_events import emit_event, event_context

from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
from kicad_mcp.utils.placement import cache as placement_cache
from kicad_mcp.utils.placement import state as placement_state
from kicad_mcp.utils.placement.conventions import apply_conventions
from kicad_mcp.utils.placement.labeling import label_clusters
from kicad_mcp.utils.placement.pack import (
    DEFAULT_COLUMN_PITCH_MM,
    DEFAULT_ROW_PITCH_MM,
    pack_layout,
)
from kicad_mcp.utils.placement.rank import assign_tiers
from kicad_mcp.utils.placement.topology import (
    ClusterPartition,
    cluster_components,
)

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
        column_pitch_mm: float = DEFAULT_COLUMN_PITCH_MM,
        row_pitch_mm: float = DEFAULT_ROW_PITCH_MM,
        state_id: str | None = None,
        refs: list[str] | None = None,
    ) -> dict:
        """Topology-aware automatic placement of schematic components.

        Operations:
          suggest     — compute a placement state for the given schematic.
                        Returns Layer 1-4 labeling, Phase 2 tier
                        assignment, Phase 3 packing, and v1 convention
                        refinements. Auto-persists to server-side cache;
                        returns ``state_id`` for the cached state.
          apply       — apply a cached state to the schematic. Requires
                        ``state_id`` OR ``schematic_path`` to look up the
                        most-recent state. Optional ``refs`` narrows the
                        apply to a subset. Schematic drift surfaces a
                        ``placement_state_stale`` warning but doesn't fail.
          clear_cache — delete cached placement states. With no path,
                        clears all; with ``schematic_path``, clears only
                        states for that schematic.

        Args:
          schematic_path  : Path to .kicad_sch.
          verbosity       : "minimal" (default) or "full". Per spec § Verbosity.
          hints           : Optional ``{ref: label}`` override map (Layer 4).
                            Canonical labels only — unknown are ignored.
          column_pitch_mm : Horizontal spacing between tiers (default 25.4 mm).
          row_pitch_mm    : Vertical spacing between components (default 12.7 mm).
          state_id        : Cached state identifier (returned by ``suggest``).
                            For ``apply``: which state to use.
          refs            : For ``apply``: subset of refs to move. ``None`` = all.
        """
        if operation == "suggest":
            return _op_suggest(
                schematic_path=schematic_path,
                verbosity=verbosity,
                hints=hints,
                column_pitch_mm=column_pitch_mm,
                row_pitch_mm=row_pitch_mm,
            )
        if operation == "apply":
            return _op_apply(
                state_id=state_id,
                schematic_path=schematic_path,
                refs=refs,
            )
        if operation == "clear_cache":
            return _op_clear_cache(schematic_path=schematic_path)
        return {
            "status": "error",
            "code": "unknown_operation",
            "message": (
                f"Unknown operation: {operation!r}. "
                "Valid: suggest, apply, clear_cache."
            ),
        }


def _op_suggest(
    *,
    schematic_path: str | None,
    verbosity: str,
    hints: dict[str, str] | None,
    column_pitch_mm: float,
    row_pitch_mm: float,
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

        # Prefer the previous state's partition for cluster_id stability
        # so callers iterating on the same schematic see stable IDs.
        prev_state = placement_cache.find_latest_for_schematic(str(sch_path))
        prev_partition = _partition_from_state(prev_state) if prev_state else None

        partition = cluster_components(
            netlist,
            schematic_path=str(sch_path),
            previous_partition=prev_partition,
        )

        # Layers 2/3/4 — labeling.
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

        # Phase 2 — Rank clusters into tiers.
        tier_assignment = assign_tiers(netlist, partition.component_cluster)
        state["tiers"] = {str(t): cids for t, cids in tier_assignment.tiers.items()}

        # Phase 3 — Pack into coordinates.
        pack = pack_layout(
            members_by_cluster=partition.members,
            cluster_tier=tier_assignment.cluster_tier,
            tiers=tier_assignment.tiers,
            cluster_edges=tier_assignment.cluster_edges,
            column_pitch_mm=column_pitch_mm,
            row_pitch_mm=row_pitch_mm,
        )

        # Phase 3 — apply v1 convention rules (mutates pack in place).
        # Labels need to be in dict form keyed by cluster_id.
        cluster_label_map = {cid: labels[cid] for cid in partition.members}
        convention_events = apply_conventions(
            members_by_cluster=partition.members,
            cluster_labels=cluster_label_map,
            netlist=netlist,
            pack=pack,
            cluster_tier=tier_assignment.cluster_tier,
            column_pitch_mm=column_pitch_mm,
            row_pitch_mm=row_pitch_mm,
        )

        # Bridge results into the PlacementState skeleton.
        for cid, refs in partition.members.items():
            cl = labels[cid]
            state["clusters"][cid] = {
                "members": refs,
                "anchor": cl.anchor,
                "label": cl.label,
                "label_confidence": cl.label_confidence,
                "label_source": cl.label_source,
                "tier": tier_assignment.cluster_tier.get(cid),
                "bbox_mm": pack.bboxes.get(
                    cid, {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0},
                ),
            }
        for ref, cid in partition.component_cluster.items():
            x, y = pack.positions.get(ref, (0.0, 0.0))
            state["components"][ref] = {
                "x_mm": x,
                "y_mm": y,
                "rotation": 0,
                "mirror_x": False,
                "cluster_id": cid,
                "fixed_by": None,
            }

        # Surface events from every layer.
        for ev in partition.events:
            emit_event(ev["level"], ev["code"], ev["message"], ev.get("data", {}))
        for ev in label_events:
            emit_event(ev["level"], ev["code"], ev["message"], ev.get("data", {}))
        for ev in tier_assignment.events:
            emit_event(ev["level"], ev["code"], ev["message"], ev.get("data", {}))
        for cev in convention_events:
            emit_event(cev.level, cev.code, cev.message, {
                "rule": cev.rule,
                "cluster_id": cev.cluster_id,
                "refs": cev.affected_refs,
            })

        # Persist the FULL state to the server-side cache before trimming
        # for output. Apply later loads the full version.
        placement_cache.save_state(state)

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


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _op_apply(
    *,
    state_id: str | None,
    schematic_path: str | None,
    refs: list[str] | None,
) -> dict[str, Any]:
    """Apply a cached placement state to its schematic.

    Loads the cached state by ``state_id``, or — when ``state_id`` is
    None — the most-recent state for ``schematic_path``. Then walks
    ``state.components`` and moves each into place via the
    ``kicad_sch_api`` schematic object.
    """
    with event_context() as events:
        cached: dict[str, Any] | None
        if state_id:
            cached = placement_cache.load_state(state_id)
            if cached is None:
                return {
                    "status": "error",
                    "code": "state_not_found",
                    "message": (
                        f"No cached placement state for state_id {state_id!r}. "
                        "Run 'suggest' first or pass schematic_path to load "
                        "the most recent state."
                    ),
                }
        elif schematic_path:
            cached = placement_cache.find_latest_for_schematic(schematic_path)
            if cached is None:
                return {
                    "status": "error",
                    "code": "state_not_found",
                    "message": (
                        f"No cached state for schematic {schematic_path!r}. "
                        "Run 'suggest' first."
                    ),
                }
        else:
            return {
                "status": "error",
                "code": "missing_parameter",
                "message": "Either state_id or schematic_path is required.",
            }

        target_path = schematic_path or cached.get("schematic_path", "")
        if not target_path:
            return {
                "status": "error",
                "code": "missing_parameter",
                "message": "Cached state has no schematic_path; supply one explicitly.",
            }

        if not Path(target_path).exists():
            return {
                "status": "error",
                "code": "schematic_not_found",
                "message": f"Schematic file not found: {target_path}",
            }

        # Drift detection: if the schematic file has changed since suggest,
        # the cached coordinates may target stale refs. Warn — don't fail.
        current_hash = placement_state.schematic_hash(target_path)
        if cached.get("schematic_hash") and current_hash != cached.get("schematic_hash"):
            emit_event(
                "warn", "placement_state_stale",
                "Schematic file has changed since this placement was "
                "computed. Components may have been added or removed; "
                "applying the cached positions anyway.",
                {
                    "schematic_path": target_path,
                    "cached_hash": cached.get("schematic_hash", ""),
                    "current_hash": current_hash,
                },
            )

        # Load the schematic in isolation (don't touch _current_schematic).
        try:
            import kicad_sch_api as ksa  # type: ignore[import-untyped]
            sch = ksa.load_schematic(target_path)
        except Exception as e:
            return {
                "status": "error",
                "code": "schematic_load_failed",
                "message": f"Could not load schematic: {e}",
            }

        applied_count = 0
        errors: list[dict[str, Any]] = []
        ref_filter = set(refs) if refs else None

        for ref, comp_state in (cached.get("components") or {}).items():
            if ref_filter is not None and ref not in ref_filter:
                continue
            try:
                matches = list(sch.components.filter(reference=ref))
            except Exception as e:
                errors.append({"ref": ref, "error": str(e)})
                continue
            if not matches:
                errors.append({"ref": ref, "error": "component not found in schematic"})
                continue
            x = float(comp_state.get("x_mm", 0.0))
            y = float(comp_state.get("y_mm", 0.0))
            try:
                matches[0].position = (x, y)
                applied_count += 1
            except Exception as e:
                errors.append({"ref": ref, "error": str(e)})

        # Save the modified schematic.
        try:
            sch.save()
        except Exception as e:
            return {
                "status": "error",
                "code": "save_failed",
                "message": f"Schematic save failed: {e}",
                "applied": applied_count,
                "errors": errors,
            }

        result: dict[str, Any] = {
            "status": "ok",
            "applied": applied_count,
            "errors": errors,
        }
        if events.has_any("warn") or events.has_any("error"):
            result["events"] = events.to_envelope()
        return result


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------

def _op_clear_cache(*, schematic_path: str | None) -> dict[str, Any]:
    count = placement_cache.clear_cache(schematic_path)
    return {"status": "ok", "cleared_count": count}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _partition_from_state(state: dict[str, Any]) -> ClusterPartition:
    """Reconstruct a minimal ClusterPartition from a cached state for use
    as ``previous_partition`` in topology clustering. Only the members
    dict needs to be populated — that's what the remap logic consumes."""
    members: dict[str, list[str]] = {}
    for cid, cluster in (state.get("clusters") or {}).items():
        members[cid] = list(cluster.get("members") or [])
    return ClusterPartition(members=members)
