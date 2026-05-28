"""Phase 3 — Pack clusters into coordinates.

See ``docs/SPEC_Schematic_Placement.md`` § "Phase 3 — Pack".

Slice 3 ships the basic packing: tier → x_mm column, within-tier
barycentric ordering of clusters, components in each cluster stacked
vertically inside the cluster's row range. Convention rules (decap below
IC, regulator input-left, etc.) land in Slice 4.

Coordinates are in mm and are intended to be passed straight to
KiCad's ``move_component``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_COLUMN_PITCH_MM = 25.4
DEFAULT_ROW_PITCH_MM = 12.7

# Origin offset so the leftmost column doesn't sit at x=0 (KiCad's sheet
# margin starts ~12.7 mm in).
_X_ORIGIN_MM = 12.7
_Y_ORIGIN_MM = 12.7


@dataclass
class PackResult:
    """Result of Phase 3 packing."""

    # ref → (x_mm, y_mm)
    positions: dict[str, tuple[float, float]] = field(default_factory=dict)
    # cluster_id → bounding box {"x", "y", "w", "h"}
    bboxes: dict[str, dict[str, float]] = field(default_factory=dict)


def pack_layout(
    *,
    members_by_cluster: dict[str, list[str]],
    cluster_tier: dict[str, int],
    tiers: dict[int, list[str]],
    cluster_edges: dict[str, dict[str, float]],
    column_pitch_mm: float = DEFAULT_COLUMN_PITCH_MM,
    row_pitch_mm: float = DEFAULT_ROW_PITCH_MM,
) -> PackResult:
    """Compute per-component coordinates and per-cluster bounding boxes.

    Within each tier we reorder clusters barycentrically (sort by mean
    row-position of incident clusters in adjacent tiers) to reduce
    edge crossings. Components inside a cluster are stacked vertically
    in the cluster's column.
    """
    if not members_by_cluster:
        return PackResult()

    # Initial within-tier ordering: deterministic (already sorted by
    # cluster_id in the TierAssignment); barycentric pass refines it.
    ordered = {t: list(cluster_ids) for t, cluster_ids in tiers.items()}

    # Single barycentric pass. Multiple passes oscillate because every
    # tier is reordered using its neighbors in *both* directions, so
    # consecutive passes can undo each other. Sugiyama-style alternating
    # forward/backward sweeps with convergence detection are out of
    # scope for v1 — one pass is sufficient to demonstrate crossing
    # reduction on the synthetic fixtures.
    ordered = _barycentric_pass(ordered, cluster_edges)

    # Assign y_index to each cluster (its row within its tier).
    cluster_y_index: dict[str, int] = {}
    for t in sorted(ordered):
        for y_idx, cid in enumerate(ordered[t]):
            cluster_y_index[cid] = y_idx

    # Compute positions. Each cluster occupies a column slice; its
    # members stack vertically starting at the cluster's y origin.
    positions: dict[str, tuple[float, float]] = {}
    bboxes: dict[str, dict[str, float]] = {}

    # First pass: figure out how many rows each tier consumes so clusters
    # don't overlap vertically. We accumulate y per tier as we lay them out.
    tier_y_cursor: dict[int, float] = dict.fromkeys(ordered, _Y_ORIGIN_MM)
    # Iterate clusters in (tier, y_index) order so y_cursor advances correctly.
    ordering: list[tuple[int, int, str]] = sorted(
        (cluster_tier[cid], cluster_y_index[cid], cid)
        for cid in members_by_cluster
    )

    for tier, _y_idx, cid in ordering:
        members = members_by_cluster[cid]
        # Place anchor first, then non-anchor members alphabetically — the
        # anchor goes at top of the cluster's column slice.
        x_mm = _X_ORIGIN_MM + tier * column_pitch_mm
        start_y = tier_y_cursor[tier]
        for i, ref in enumerate(members):
            positions[ref] = (x_mm, start_y + i * row_pitch_mm)
        # Reserve a row pitch as a gap between clusters.
        slice_height = max(len(members), 1) * row_pitch_mm + row_pitch_mm
        bboxes[cid] = {
            "x": x_mm,
            "y": start_y,
            "w": column_pitch_mm,
            "h": slice_height - row_pitch_mm,
        }
        tier_y_cursor[tier] = start_y + slice_height

    return PackResult(positions=positions, bboxes=bboxes)


# ---------------------------------------------------------------------------
# Barycentric ordering
# ---------------------------------------------------------------------------

def _barycentric_pass(
    ordered: dict[int, list[str]],
    cluster_edges: dict[str, dict[str, float]],
) -> dict[int, list[str]]:
    """One barycentric pass: reorder each tier by the mean y-position of
    its neighbors in adjacent tiers. Earlier-tier neighbors and later-tier
    neighbors both contribute.
    """
    # Build incoming/outgoing inverse for quick neighbor lookup.
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {c: list(d.keys()) for c, d in cluster_edges.items()}
    for src, dsts in cluster_edges.items():
        for dst in dsts:
            incoming.setdefault(dst, []).append(src)

    # Snapshot current y-position by cluster.
    y_of: dict[str, int] = {}
    for tier in sorted(ordered):
        for i, cid in enumerate(ordered[tier]):
            y_of[cid] = i

    new_ordered: dict[int, list[str]] = {}
    for tier in sorted(ordered):
        def baryscore(cid: str) -> tuple[float, str]:
            neigh_y: list[int] = []
            for n in incoming.get(cid, []):
                if n in y_of:
                    neigh_y.append(y_of[n])
            for n in outgoing.get(cid, []):
                if n in y_of:
                    neigh_y.append(y_of[n])
            if not neigh_y:
                # No neighbors → keep current position
                return (float(y_of[cid]), cid)
            return (sum(neigh_y) / len(neigh_y), cid)
        new_ordered[tier] = sorted(ordered[tier], key=baryscore)
    return new_ordered
