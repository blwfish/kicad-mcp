"""``schematic_layout`` router — topology-aware schematic placement.

See ``docs/SPEC_Schematic_Placement.md``. Slices:

  Slice 1 — operation ``suggest`` + Layer 1 (topology)
  Slice 2 — Layers 2/3/4 (pattern_recognition + LCSC + caller hints)
  Slice 3 — Phase 2 ranking + Phase 3 packing
  Slice 4 — v1 convention rules
  Slice 5 — apply + stateful cache + clear_cache
  Slice 6 — telemetry integration (record_call / record_cluster_decision
            / record_warning / attribute_pending_warnings)
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp_events import emit_event, event_context

from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
from kicad_mcp.utils.placement import cache as placement_cache
from kicad_mcp.utils.placement import state as placement_state
from kicad_mcp.utils.placement.conventions import apply_conventions
from kicad_mcp.utils.placement.labeling import (
    LABEL_SOURCE_RESOLVED,
    label_clusters,
)
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

try:
    from kicad_mcp.utils.telemetry import (
        attribute_pending_warnings,
        record_call,
        record_cluster_decision,
        record_warning,
    )
    _TELEMETRY_AVAILABLE = True
except Exception:
    _TELEMETRY_AVAILABLE = False

logger = logging.getLogger(__name__)


def _with_events(result: dict[str, Any], events: Any) -> dict[str, Any]:
    """Append ``events.to_envelope()`` onto ``result`` if any events were
    accumulated. Use this on EVERY return inside an ``event_context``
    block so early-return paths (errors) don't silently drop warnings
    (placement_state_stale, wires_will_be_stale, etc.) that were emitted
    before the failure point.
    """
    if events.has_any("warn") or events.has_any("error") or events.has_any("info"):
        result["events"] = events.to_envelope()
    return result


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

    t_start = time.monotonic()
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
        # for output. Apply later loads the full version. If the write
        # fails (disk full, read-only fs, etc.), the returned state_id
        # would be unusable — surface that to the caller via a warning
        # so they don't store it and hit state_not_found later.
        saved_id = placement_cache.save_state(state)
        cache_persisted = saved_id is not None
        if not cache_persisted:
            emit_event(
                "warn", "cache_save_failed",
                (
                    f"Placement cache write failed; state_id "
                    f"{state['state_id']!r} will not be loadable by a later "
                    "apply call. Pass the full state explicitly via the "
                    "state parameter (stateless mode) instead."
                ),
                {"state_id": state["state_id"]},
            )

        # Telemetry — Slice 6.
        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        is_fresh_state = prev_state is None
        all_events = (
            list(partition.events)
            + list(label_events)
            + list(tier_assignment.events)
        )
        _record_suggest_telemetry(
            state=state,
            netlist=netlist,
            inputs={
                "schematic_path": str(sch_path),
                "verbosity": verbosity,
                "hints": hints or {},
                "column_pitch_mm": column_pitch_mm,
                "row_pitch_mm": row_pitch_mm,
            },
            partition=partition,
            tier_assignment_cluster_tier=tier_assignment.cluster_tier,
            cluster_labels=cluster_label_map,
            convention_events=convention_events,
            layer_events=all_events,
            elapsed_ms=elapsed_ms,
            is_fresh_state=is_fresh_state,
        )

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
    t_start = time.monotonic()
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
            # Drift warning may already be in the event_context — preserve it.
            return _with_events({
                "status": "error",
                "code": "schematic_load_failed",
                "message": f"Could not load schematic: {e}",
            }, events)

        # Wire-stale detection (spec § Open Question #2, option b): emit a
        # warning if any of the components we're about to move has wires
        # attached to pin positions that will no longer match. We proceed
        # with the apply either way — caller decides whether to re-wire.
        ref_filter = set(refs) if refs else None
        target_components = cached.get("components") or {}
        if ref_filter is not None:
            target_components = {
                r: v for r, v in target_components.items() if r in ref_filter
            }
        stale_refs = _detect_stale_wires(sch, target_components)
        if stale_refs:
            emit_event(
                "warn", "wires_will_be_stale",
                (
                    f"{len(stale_refs)} component(s) being moved have wires "
                    "attached at the current pin positions; those wires will "
                    "no longer terminate on the pin after the move. Re-wire "
                    "the schematic to restore connectivity."
                ),
                {"refs": stale_refs},
            )

        applied_count = 0
        errors: list[dict[str, Any]] = []

        for ref, comp_state in (cached.get("components") or {}).items():
            if ref_filter is not None and ref not in ref_filter:
                continue
            # IMPORTANT: use components.get(ref), NOT components.filter(reference=ref).
            # kicad_sch_api's filter() silently ignores unknown kwargs and returns
            # ALL components — using it here would mutate the wrong component.
            try:
                comp = sch.components.get(ref)
            except Exception as e:
                errors.append({"ref": ref, "error": str(e)})
                continue
            if comp is None:
                errors.append({"ref": ref, "error": "component not found in schematic"})
                continue
            x = float(comp_state.get("x_mm", 0.0))
            y = float(comp_state.get("y_mm", 0.0))
            try:
                comp.position = (x, y)
                applied_count += 1
            except Exception as e:
                errors.append({"ref": ref, "error": str(e)})

        # Save the modified schematic.
        try:
            sch.save()
        except Exception as e:
            # Drift + wires_will_be_stale warnings may already be in the
            # event_context — preserve them so the caller has full context
            # for why the schematic is in an inconsistent state.
            return _with_events({
                "status": "error",
                "code": "save_failed",
                "message": f"Schematic save failed: {e}",
                "applied": applied_count,
                "errors": errors,
            }, events)

        elapsed_ms = int((time.monotonic() - t_start) * 1000)
        if _TELEMETRY_AVAILABLE:
            with contextlib.suppress(Exception):
                record_call(
                    tool_name="schematic_layout.apply",
                    schematic_hash=current_hash,
                    inputs_summary={
                        "state_id": state_id or "",
                        "refs_count": len(refs) if refs else 0,
                    },
                    output_summary={
                        "applied": applied_count,
                        "errors_count": len(errors),
                    },
                    elapsed_ms=elapsed_ms,
                    state_id=cached.get("state_id"),
                    # apply is NOT a fresh-state event — it consumes an
                    # existing cached state. Without this, record_call's
                    # default (is_fresh_state=True) would trick the telemetry
                    # sweep into abandoning pending suggest-phase warnings on
                    # this schematic before the next suggest could attribute
                    # them, permanently skewing action_rate calibration.
                    is_fresh_state=False,
                )

        # If nothing was applied but components failed, the placement did NOT
        # take effect (e.g. stale state — every ref missing from the current
        # schematic). The save above succeeds trivially (no mutations), so
        # returning status="ok" would let the caller trust a silent no-op and
        # proceed to route/validate an unchanged schematic.
        if applied_count == 0 and errors:
            return _with_events({
                "status": "error",
                "code": "no_components_applied",
                "message": (
                    f"No components were moved; all {len(errors)} target(s) "
                    "failed (stale state — re-run suggest?)."
                ),
                "applied": 0,
                "errors": errors,
            }, events)

        return _with_events({
            "status": "ok",
            "applied": applied_count,
            "errors": errors,
        }, events)


# ---------------------------------------------------------------------------
# clear_cache
# ---------------------------------------------------------------------------

def _op_clear_cache(*, schematic_path: str | None) -> dict[str, Any]:
    t_start = time.monotonic()
    count = placement_cache.clear_cache(schematic_path)
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    if _TELEMETRY_AVAILABLE:
        with contextlib.suppress(Exception):
            record_call(
                tool_name="schematic_layout.clear_cache",
                schematic_hash="",
                inputs_summary={"schematic_path": schematic_path or ""},
                output_summary={"cleared_count": count},
                elapsed_ms=elapsed_ms,
                # Cache eviction is not a fresh-state event either.
                is_fresh_state=False,
            )
    return {"status": "ok", "cleared_count": count}


# ---------------------------------------------------------------------------
# Telemetry helpers
# ---------------------------------------------------------------------------

def _record_suggest_telemetry(
    *,
    state: dict[str, Any],
    netlist: dict[str, Any],
    inputs: dict[str, Any],
    partition: ClusterPartition,
    tier_assignment_cluster_tier: dict[str, int],
    cluster_labels: dict[str, Any],
    convention_events: list[Any],
    layer_events: list[dict[str, Any]],
    elapsed_ms: int,
    is_fresh_state: bool,
) -> None:
    """Write the suggest call's telemetry: one call row + cluster
    decisions + warnings + action attribution. Swallows all errors
    so telemetry failure can't break the main suggest response."""
    if not _TELEMETRY_AVAILABLE:
        return

    try:
        schematic_hash = state.get("schematic_hash", "") or ""
        clusters = state.get("clusters") or {}
        tier_count = len({
            c.get("tier") for c in clusters.values() if c.get("tier") is not None
        })

        # Label-source breakdown
        source_breakdown: Counter[str] = Counter()
        low_confidence_count = 0
        lcsc_resolved = 0
        for cl in cluster_labels.values():
            source_breakdown[cl.label_source] += 1
            if cl.label_confidence < 0.5:
                low_confidence_count += 1
            if cl.label_source == LABEL_SOURCE_RESOLVED:
                # Count LCSC-resolved *components*, not clusters.
                pass
        # LCSC-resolved count is sourced from the *netlist* component dicts
        # (which carry the `properties` field from the kicad-cli output) —
        # the placement state's component dicts don't include properties.
        netlist_components = netlist.get("components") or {}
        for comp in netlist_components.values():
            props = (comp.get("properties") or {}) if isinstance(comp, dict) else {}
            if props.get("LCSC"):
                lcsc_resolved += 1

        warning_events = [
            e for e in layer_events if e.get("level") == "warn"
        ] + [
            {"code": cev.code, "level": cev.level}
            for cev in convention_events
            if getattr(cev, "level", "") == "warn"
        ]

        output_summary = {
            "cluster_count": len(clusters),
            "tier_count": tier_count,
            "label_source_breakdown": dict(source_breakdown),
            "low_confidence_cluster_count": low_confidence_count,
            "warnings_emitted_count": len(warning_events),
            "lcsc_resolved_component_count": lcsc_resolved,
            "verbosity": inputs.get("verbosity", "minimal"),
            "graph_modularity": round(partition.graph_modularity, 4),
        }

        call_id = record_call(
            tool_name="schematic_layout.suggest",
            schematic_hash=schematic_hash,
            inputs_summary={
                "hints_count": len(inputs.get("hints") or {}),
                "column_pitch_mm": inputs.get("column_pitch_mm"),
                "row_pitch_mm": inputs.get("row_pitch_mm"),
                "verbosity": inputs.get("verbosity"),
            },
            output_summary=output_summary,
            elapsed_ms=elapsed_ms,
            state_id=state.get("state_id"),
            is_fresh_state=is_fresh_state,
        )
        if call_id is None:
            return

        for cid, cluster in clusters.items():
            cl = cluster_labels.get(cid)
            record_cluster_decision(
                call_id=call_id,
                cluster_id=cid,
                member_count=len(cluster.get("members") or []),
                anchor_ref=cluster.get("anchor"),
                label=cluster.get("label"),
                label_confidence=cluster.get("label_confidence"),
                label_source=cluster.get("label_source", ""),
                tier=cluster.get("tier"),
                louvain_modularity=(
                    partition.graph_modularity if cl is not None else None
                ),
            )

        for ev in warning_events:
            code = ev.get("code", "unknown")
            data = ev.get("data") or {}
            refs = []
            cluster_ids = []
            if isinstance(data, dict):
                if isinstance(data.get("refs"), list):
                    refs = data["refs"]
                if isinstance(data.get("ref"), str):
                    refs = [data["ref"]]
                if isinstance(data.get("cluster_id"), str):
                    cluster_ids = [data["cluster_id"]]
                if isinstance(data.get("candidates"), list):
                    cluster_ids = cluster_ids or [data.get("cluster_id", "")]
            if not refs and not cluster_ids:
                # Warning has no target — skip (record_warning would reject).
                continue
            record_warning(
                call_id=call_id,
                warning_type=code,
                severity=ev.get("level", "warn"),
                affected_refs=refs,
                affected_cluster_ids=cluster_ids,
            )

        # Action attribution: did this call's inputs address any prior
        # warnings? Compare the raw caller payload against pending warnings
        # on the same schematic.
        attribute_pending_warnings(
            schematic_hash=schematic_hash,
            new_call_id=call_id,
            inputs=inputs,
        )
    except Exception as e:
        logger.warning("schematic_layout telemetry failed: %s", e)


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


# Pin-vs-wire endpoint tolerance: KiCad rounds positions to 3 decimal places
# (microns), so a 0.05 mm tolerance comfortably covers float jitter without
# matching unrelated nearby endpoints.
_PIN_WIRE_TOLERANCE_MM = 0.05


def _detect_stale_wires(
    sch: Any,
    target_components: dict[str, Any],
) -> list[str]:
    """Return the sorted refs whose pre-move pin positions coincide with
    existing wire endpoints. After the apply moves those components, the
    wires will no longer terminate on the pins.

    Components whose target position equals the current position are not
    treated as moves and never count as stale (regardless of wires).
    """
    import math

    # Symbol cache resolves pins from lib_id; works for both freshly-added
    # components and components loaded from disk (where comp.list_pins()
    # comes back empty under current kicad-sch-api versions).
    try:
        from kicad_sch_api.library.cache import get_symbol_cache  # type: ignore[import-untyped]
    except ImportError:
        return []

    try:
        wires = list(sch.wires.all())
    except Exception:
        return []
    if not wires:
        return []

    endpoints: list[tuple[float, float]] = []
    for w in wires:
        for p in getattr(w, "points", ()) or ():
            endpoints.append((round(p.x, 3), round(p.y, 3)))
    if not endpoints:
        return []

    sym_cache = get_symbol_cache()
    affected: set[str] = set()
    tol = _PIN_WIRE_TOLERANCE_MM
    for ref, comp_state in target_components.items():
        target_x = float(comp_state.get("x_mm", 0.0))
        target_y = float(comp_state.get("y_mm", 0.0))
        # See _op_apply: components.get(ref), not filter(reference=ref).
        try:
            comp = sch.components.get(ref)
        except Exception:
            continue
        if comp is None or not comp.position:
            continue
        # Skip components that aren't actually moving.
        if (abs(comp.position.x - target_x) <= tol
                and abs(comp.position.y - target_y) <= tol):
            continue

        # Resolve pin positions via symbol cache; same transform as
        # schematic_impl._kicad_pin_position.
        sym = sym_cache.get_symbol(comp.lib_id) if comp.lib_id else None
        pin_locals = getattr(sym, "pins", None) if sym else None
        if not pin_locals:
            continue
        angle_rad = math.radians(comp.rotation)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)
        for pin in pin_locals:
            rx = pin.position.x * cos_a - pin.position.y * sin_a
            ry = pin.position.x * sin_a + pin.position.y * cos_a
            abs_x = round(comp.position.x + rx, 3)
            abs_y = round(comp.position.y - ry, 3)
            for ex, ey in endpoints:
                if abs(abs_x - ex) <= tol and abs(abs_y - ey) <= tol:
                    affected.add(ref)
                    break
            if ref in affected:
                break
    return sorted(affected)
