"""Edge-aware terminal placement: rotation, ordering, and hint normalization.

Pure-Python helpers for the PCB autoplacer's connector/terminal handling
(``_step_smart_placement`` in ``tools/pcb_pipeline.py``).  Embedded scripts that
run inside pcbnew's Python interpreter cannot ``import`` this module; they inject
:data:`EDGE_TERMINAL_HELPER` (a source string defining the same functions, sans
type annotations) into their embedded script source.  This mirrors the
``spiral_placement.SPIRAL_HELPER`` pattern; the drift test in
``tests/test_edge_terminal_placement.py`` keeps the two behaviourally identical.

**Rotation convention.** :func:`rotate_extents` and :func:`rotation_to_face`
are both derived from the *same* rotation matrix ``R(theta)`` (math-CCW applied
in KiCad's y-down screen coordinates), so they are mutually consistent by
construction.  Whether that matches pcbnew's ``SetOrientationDegrees`` sign is
the one empirical unknown — the integration golden (pad-centroid-inboard)
validates it; if it ever mirrors, flip the apply angle and the 90/270 cases of
:func:`rotate_extents` together.

**Hint validation seam** (CLAUDE.md Syntactic-Semantic Seam Rule).
:func:`normalize_hint` is the single source of truth for valid hint values;
unknown keys / out-of-set values are dropped *and reported*, never silently
substituted.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence, Tuple

# Allowed hint values — single source of truth for validation (parent-side).
_VALID_EDGES = ("top", "bottom", "left", "right", "none")
_VALID_ROTATIONS = (0, 90, 180, 270)


# ---------------------------------------------------------------------------
# Designator classification (keys off the same _ref_class the engine uses)
# ---------------------------------------------------------------------------

def is_screw_terminal_class(cls: str) -> bool:
    """True for the connector designator class that gets the edge-rotation
    heuristic.  Only ``J`` (field-wiring terminals + module headers); ``SW``
    (switches) and ``H`` (mounting holes) edge-place but never rotate."""
    return cls == "J"


def natural_ref_key(ref: str) -> Tuple[str, int]:
    """``(prefix, number)`` sort key for human/numeric ordering: ``J2 < J10``
    (not the lexical ``J10 < J2``).  A bare prefix (no digits) sorts first
    via ``-1``; any trailing suffix after the first digit run is ignored."""
    n = len(ref)
    i = 0
    while i < n and not ref[i].isdigit():
        i += 1
    prefix = ref[:i]
    j = i
    while j < n and ref[j].isdigit():
        j += 1
    num = int(ref[i:j]) if j > i else -1
    return (prefix, num)


# ---------------------------------------------------------------------------
# Pad geometry (input: list of absolute pad bounding boxes (xmin,ymin,xmax,ymax))
# ---------------------------------------------------------------------------

def pad_centroid_offset(
    pads: Sequence[Tuple[float, float, float, float]],
    origin: Tuple[float, float],
) -> Tuple[float, float]:
    """Mean of pad-box centers minus ``origin``.  Empty → ``(0, 0)``."""
    if not pads:
        return (0.0, 0.0)
    sx = 0.0
    sy = 0.0
    for (xmin, ymin, xmax, ymax) in pads:
        sx += (xmin + xmax) / 2.0
        sy += (ymin + ymax) / 2.0
    cx = sx / len(pads)
    cy = sy / len(pads)
    return (cx - origin[0], cy - origin[1])


def pad_extent(
    pads: Sequence[Tuple[float, float, float, float]],
    origin: Tuple[float, float],
) -> Tuple[float, float, float, float]:
    """Union bbox of pads as positive extents from ``origin``:
    ``(left, right, top, bot)``.  Empty → ``(0, 0, 0, 0)``."""
    if not pads:
        return (0.0, 0.0, 0.0, 0.0)
    xmin = min(p[0] for p in pads)
    ymin = min(p[1] for p in pads)
    xmax = max(p[2] for p in pads)
    ymax = max(p[3] for p in pads)
    ox, oy = origin
    return (ox - xmin, xmax - ox, oy - ymin, ymax - oy)


# ---------------------------------------------------------------------------
# Rotation (R(theta) math-CCW; rotate_extents derived from the same matrix)
# ---------------------------------------------------------------------------

def _rotate_vec(vx: float, vy: float, theta: int) -> Tuple[float, float]:
    """Rotate a vector by ``theta`` degrees to MATCH pcbnew's
    ``SetOrientationDegrees`` (empirically: pcbnew's positive rotation is the
    opposite screen direction from a naive y-down ``R(theta)``, so we apply
    ``R(-theta)``). Keeping this aligned with pcbnew means the angle
    :func:`rotation_to_face` picks, applied verbatim, actually orients the pads
    as predicted — and :func:`rotate_extents` predicts the as-placed extents."""
    a = math.radians(-theta)
    ca = math.cos(a)
    sa = math.sin(a)
    return (vx * ca - vy * sa, vx * sa + vy * ca)


def rotate_extents(
    ext: Tuple[float, float, float, float],
    theta: int,
) -> Tuple[float, float, float, float]:
    """Rotate asymmetric extents ``(L, R, T, B)`` by ``theta in {0,90,180,270}``.

    Derived by mapping the extent-box corners through the same ``R(-theta)`` used
    by :func:`_rotate_vec`, so it predicts pcbnew's as-placed extents:
        0   → (L, R, T, B)
        90  → (T, B, R, L)
        180 → (R, L, B, T)
        270 → (B, T, L, R)
    A non-orthogonal angle leaves the (axis-aligned) extents unchanged."""
    L, R, T, B = ext
    t = theta % 360
    if t == 90:
        return (T, B, R, L)
    if t == 180:
        return (R, L, B, T)
    if t == 270:
        return (B, T, L, R)
    return (L, R, T, B)


def rotation_to_face(
    vec: Tuple[float, float],
    target_normal: Tuple[float, float],
    eps: float = 0.3,
) -> int:
    """Snap to ``theta in {0,90,180,270}`` so ``R(theta)·vec`` points most toward
    ``target_normal``.

    ``vec`` is a direction in the footprint's 0° frame (pad-side for connectors
    → aim at the *inward* normal; keepout-side for tier-1 → aim at the *outward*
    normal).  Degenerate ``|vec| < eps`` → ``0`` (deterministic).  Ties resolve
    to the lowest angle (fixed iteration order + strict ``>``)."""
    vx, vy = vec
    if math.hypot(vx, vy) < eps:
        return 0
    nx, ny = target_normal
    best_theta = 0
    best_dot = -1e18
    for theta in (0, 90, 180, 270):
        rx, ry = _rotate_vec(vx, vy, theta)
        mag = math.hypot(rx, ry) or 1.0
        dot = (rx / mag) * nx + (ry / mag) * ny
        if dot > best_dot:
            best_dot = dot
            best_theta = theta
    return best_theta


# ---------------------------------------------------------------------------
# Edge normals + assignment
# ---------------------------------------------------------------------------

def inward_normal(edge: str) -> Tuple[float, float]:
    """Unit vector pointing into the board from ``edge`` (KiCad +y is DOWN)."""
    if edge == "top":
        return (0.0, 1.0)
    if edge == "bottom":
        return (0.0, -1.0)
    if edge == "left":
        return (1.0, 0.0)
    if edge == "right":
        return (-1.0, 0.0)
    return (0.0, 0.0)


def outward_normal(edge: str) -> Tuple[float, float]:
    """Unit vector pointing off the board across ``edge``."""
    ix, iy = inward_normal(edge)
    return (-ix, -iy)


def nearest_edge(
    target: Tuple[float, float],
    board_box: Tuple[float, float, float, float],
) -> str:
    """Edge whose line is nearest to ``target``.  Ties resolve in fixed order
    ``top, bottom, left, right``."""
    tx, ty = target
    xmin, ymin, xmax, ymax = board_box
    dists = (
        ("top", ty - ymin),
        ("bottom", ymax - ty),
        ("left", tx - xmin),
        ("right", xmax - tx),
    )
    best_edge = "top"
    best_d = None
    for edge, d in dists:
        if best_d is None or d < best_d:
            best_d = d
            best_edge = edge
    return best_edge


# ---------------------------------------------------------------------------
# Ordered layout along an edge (pad-anchored overhang for terminals)
# ---------------------------------------------------------------------------

def layout_along_edge(
    items: Sequence[Tuple[str, Tuple[float, float, float, float], Tuple[float, float, float, float], bool]],
    edge: str,
    board_box: Tuple[float, float, float, float],
    margin: float,
    spacing: float,
    clearance: Optional[float] = None,
    anchor: str = "start",
) -> list:
    """Lay connectors out in given order along ``edge``.

    ``items``: ``(ref, ext, pad_ext, overhang)`` where ``ext`` / ``pad_ext`` are
    the *rotated* ``(L, R, T, B)`` extents and ``overhang`` requests the body to
    cross the edge outward.  Returns ``[(ref, x, y, fits), ...]`` in order;
    ``fits`` is False if the cursor ran past the edge (caller falls back).

    Cross-axis anchoring: an ``overhang`` terminal anchors its PAD box at
    ``clearance`` inside the edge (courtyard may cross the edge outward, pads
    stay on-board); otherwise the full courtyard sits ``margin`` inside.  Along
    the edge, connectors advance by their courtyard width plus ``spacing``.

    ``anchor``: ``"start"`` (default) packs the group from the edge start (the
    historical behaviour); ``"center"`` centres the whole group within the usable
    span ``[edge_start+margin, edge_end-margin]`` — equal slack at both ends — so
    a group shorter than its edge sits balanced rather than packed at one end. A
    group that fills (or over-fills) the edge falls back to ``start`` (the
    ``max(0, …)`` clamp), and the per-item ``fits`` check is unchanged. The corner
    keepout the caller bakes into ``board_box`` is respected either way."""
    if clearance is None:
        clearance = margin
    xmin, ymin, xmax, ymax = board_box
    out = []
    if edge in ("top", "bottom"):
        edge_lo = xmin + margin
        total = (sum(it[1][0] + it[1][1] for it in items)
                 + spacing * max(0, len(items) - 1))
        cursor = edge_lo + (max(0.0, ((xmax - margin) - edge_lo - total) / 2.0)
                            if anchor == "center" else 0.0)
        for (ref, ext, pad_ext, overhang) in items:
            L, R, T, B = ext
            pL, pR, pT, pB = pad_ext
            x = cursor + L
            if edge == "top":
                y = (ymin + clearance + pT) if overhang else (ymin + margin + T)
            else:
                y = (ymax - clearance - pB) if overhang else (ymax - margin - B)
            fits = (x + R) <= (xmax - margin)
            out.append((ref, x, y, fits))
            cursor = x + R + spacing
    else:
        edge_lo = ymin + margin
        total = (sum(it[1][2] + it[1][3] for it in items)
                 + spacing * max(0, len(items) - 1))
        cursor = edge_lo + (max(0.0, ((ymax - margin) - edge_lo - total) / 2.0)
                            if anchor == "center" else 0.0)
        for (ref, ext, pad_ext, overhang) in items:
            L, R, T, B = ext
            pL, pR, pT, pB = pad_ext
            y = cursor + T
            if edge == "left":
                x = (xmin + clearance + pL) if overhang else (xmin + margin + L)
            else:
                x = (xmax - clearance - pR) if overhang else (xmax - margin - R)
            fits = (y + B) <= (ymax - margin)
            out.append((ref, x, y, fits))
            cursor = y + B + spacing
    return out


# ---------------------------------------------------------------------------
# Hint normalization (parent-side only; NOT injected into the embedded script)
# ---------------------------------------------------------------------------

def normalize_hint(raw: object) -> Tuple[dict, list]:
    """Validate one per-ref placement hint.  Returns ``(clean, warnings)``.

    ``clean`` is a subset of ``{edge, rotation, fixed}``.  Unknown keys and
    out-of-set values are dropped and reported in ``warnings`` — never raised,
    never silently substituted (the dict.get-default seam).  Booleans are
    rejected for ``rotation``/``fixed`` (``False == 0`` would slip through)."""
    warnings: list = []
    clean: dict = {}
    if not isinstance(raw, dict):
        return clean, ["hint must be a mapping, got %s" % type(raw).__name__]
    for key, val in raw.items():
        if key == "edge":
            if val in _VALID_EDGES:
                clean["edge"] = val
            else:
                warnings.append("ignored edge=%r (not one of %r)" % (val, _VALID_EDGES))
        elif key == "rotation":
            if isinstance(val, bool) or val not in _VALID_ROTATIONS:
                warnings.append(
                    "ignored rotation=%r (not one of %r)" % (val, _VALID_ROTATIONS)
                )
            else:
                clean["rotation"] = int(val)
        elif key == "fixed":
            if (
                isinstance(val, (list, tuple))
                and len(val) == 2
                and all(
                    isinstance(c, (int, float)) and not isinstance(c, bool) for c in val
                )
            ):
                clean["fixed"] = [float(val[0]), float(val[1])]
            else:
                warnings.append("ignored fixed=%r (need [x, y] numbers)" % (val,))
        else:
            warnings.append("ignored unknown hint key %r" % (key,))
    return clean, warnings


# ---------------------------------------------------------------------------
# Terminal distribution (SPEC_Multi_Edge_Terminal_Distribution.md §3-§5)
# Pure, parent-side: the SINGLE source for BOTH the board size and the per-ref
# edge assignment (passed to placement as {"edge": E} hints). Runs in the parent
# only — NOT injected into the pcbnew script — so it does not appear in
# EDGE_TERMINAL_HELPER and must not import tools.pcb_pipeline (no cycle).
# ---------------------------------------------------------------------------

_OPP_EDGE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}


def _size_from_assignment(
    interior, terminals, edge_of, primary, side_a, side_b, horizontal,
    routing_factor, padding, spacing, corner_inset_mm, corner_center_inset_mm,
    side_silk_gap_mm, cluster_wh: Optional[tuple] = None,
):
    """Board ``{width_mm,height_mm}`` for a given edge assignment (SPEC §4,
    generalized). Canonical frame: ``primary`` terminals run along the ALONG axis;
    the two side edges run along the CROSS axis. Reduces EXACTLY to
    ``_content_aware_size`` when every terminal is on ``primary`` (the regression
    lock) — verified by ``test_terminal_distribution``.

    ``cluster_wh``: optional measured ``(cluster_w, cluster_h)`` rectangle (board
    frame) from a placement pass — replaces the square-``C`` interior estimate with
    axis-specific values (SPEC_Post_Placement_Board_Refit.md §4). ``None`` ⇒ the
    square-``C`` path, byte-identical to today (the re-fit regression lock)."""
    interior_area = sum(c["w"] * c["h"] for c in interior)
    max_interior = max((max(c["w"], c["h"]) for c in interior), default=0.0)
    if cluster_wh is None:
        cluster = math.sqrt(interior_area * routing_factor) if interior_area > 0 else 0.0
        cluster = max(cluster, max_interior)
        along_cluster = cross_cluster = cluster
    else:
        # Measured rectangle → axis-specific. ALONG is the primary-run axis: board
        # WIDTH when horizontal (antenna top/bottom), HEIGHT otherwise. Keep the
        # per-axis ``max_interior`` floor so a single oversized body still fits.
        cluster_w, cluster_h = cluster_wh
        along_cluster, cross_cluster = (
            (cluster_w, cluster_h) if horizontal else (cluster_h, cluster_w)
        )
        along_cluster = max(along_cluster, max_interior)
        cross_cluster = max(cross_cluster, max_interior)

    def _on(edge):
        return [t for t in terminals if edge_of.get(t["ref"]) == edge]

    def _along(ts):
        return sum(max(t["w"], t["h"]) + spacing for t in ts)

    def _depth(ts):
        return max((min(t["w"], t["h"]) for t in ts), default=0.0)

    p_ts, a_ts, b_ts = _on(primary), _on(side_a), _on(side_b)
    sides_used = bool(a_ts) or bool(b_ts)
    # Side bands eat the ALONG axis (depth inward) + reserve an inboard silk gap so
    # side-edge legends clear the cluster. The primary band eats the CROSS axis.
    d_a = _depth(a_ts) + (side_silk_gap_mm if a_ts else 0.0)
    d_b = _depth(b_ts) + (side_silk_gap_mm if b_ts else 0.0)
    d_p = _depth(p_ts)
    # ALONG: the primary run / cluster sits BETWEEN the two side bands (so the
    # primary terminals never extend into a side band — §4.1 body-overlap guard).
    along_dim = max(along_cluster, _along(p_ts)) + d_a + d_b + 2 * padding + 2 * corner_inset_mm
    # CROSS: cluster / side runs sit ABOVE the primary band. Corner reserve is FULL
    # on this axis only when a terminal edge (a side) runs along it (RFE #1).
    cross_corner = corner_inset_mm if sides_used else corner_center_inset_mm
    cross_dim = (max(cross_cluster, _along(a_ts), _along(b_ts)) + d_p
                 + 2 * padding + 2 * cross_corner)
    w, h = (along_dim, cross_dim) if horizontal else (cross_dim, along_dim)
    return {"width_mm": math.ceil(w), "height_mm": math.ceil(h)}


def distribute_terminals(
    components,
    antenna_side: Optional[str] = None,
    *,
    mode: str = "single_edge",
    routing_factor: float = 2.5,
    padding: float = 2.0,
    spacing: float = 1.0,
    corner_inset_mm: float = 0.0,
    corner_center_inset_mm: float = 0.0,
    side_silk_gap_mm: float = 2.5,
    near_square_thresh: float = 1.35,
    cluster_wh: Optional[tuple] = None,
) -> dict:
    """Decide each field-wiring terminal's board edge AND the board size — the
    single source for sizing + placement (SPEC §3-§5).

    ``components``: ``[{"ref","w","h","is_terminal"}, …]``. ``antenna_side``: the
    edge the MCU antenna overhangs ("top"/…/None); terminals go OPPOSITE it
    (``primary``), spilling only to the perpendicular SIDE edges, never the antenna
    edge. ``mode="single_edge"`` keeps today's all-on-one-edge behaviour;
    ``"multi_edge"`` peels terminals onto the side edges to square the board.

    Returns ``{"edge_of": {ref: edge}, "size": {"width_mm","height_mm"},
    "primary_edge": edge}``."""
    interior = [c for c in components if not c.get("is_terminal")]
    terminals = sorted((c for c in components if c.get("is_terminal")),
                       key=lambda t: natural_ref_key(t.get("ref", "")))
    horizontal = (antenna_side in ("top", "bottom")) if antenna_side else True
    primary = _OPP_EDGE.get(antenna_side or "", "bottom")
    side_a, side_b = ("left", "right") if horizontal else ("top", "bottom")

    def _size(edge_of):
        return _size_from_assignment(
            interior, terminals, edge_of, primary, side_a, side_b, horizontal,
            routing_factor, padding, spacing, corner_inset_mm,
            corner_center_inset_mm, side_silk_gap_mm, cluster_wh)

    all_primary = {t["ref"]: primary for t in terminals}
    base_size = _size(all_primary)
    base = {"edge_of": all_primary, "size": base_size, "primary_edge": primary}

    # single_edge, or too few terminals to split → today's behaviour.
    if mode != "multi_edge" or len(terminals) < 2:
        return base

    # Step 2: already near-square (aspect-ratio cutoff, axis-independent) → no-op.
    bw, bh = base_size["width_mm"], base_size["height_mm"]
    lo, hi = min(bw, bh), max(bw, bh)
    if lo > 0 and hi <= lo * near_square_thresh:
        return base

    # Step 3: peel a suffix (natural-ref order) off primary; first ceil(k/2) of the
    # peeled refs → side_a, the rest → side_b (each side stays in natural order).
    # Score (max-dimension, area); argmin, ties → smaller k (and single-edge wins
    # over any multi-edge tie, since the loop only replaces on a STRICT improvement).
    n = len(terminals)
    best_edge_of, best_size = all_primary, base_size
    best_score = (hi, bw * bh)
    for k in range(1, n):
        edge_of = dict(all_primary)
        peeled = terminals[n - k:]
        half = (k + 1) // 2
        for i, t in enumerate(peeled):
            edge_of[t["ref"]] = side_a if i < half else side_b
        sz = _size(edge_of)
        score = (max(sz["width_mm"], sz["height_mm"]), sz["width_mm"] * sz["height_mm"])
        if score < best_score:
            best_score, best_edge_of, best_size = score, edge_of, sz
    return {"edge_of": best_edge_of, "size": best_size, "primary_edge": primary}
# Concatenated into the _step_smart_placement script.  The definitions below
# MUST stay behaviourally identical to the Python-side functions above (type
# annotations stripped — the embedded script has no `from __future__ import
# annotations` and may run under Python 3.9).  The drift test execs this string
# and compares outputs against the imported functions.  normalize_hint is NOT
# here: hints are validated parent-side before being passed into the script.

EDGE_TERMINAL_HELPER = """
import math as _et_math

def is_screw_terminal_class(cls):
    return cls == "J"

def natural_ref_key(ref):
    n = len(ref)
    i = 0
    while i < n and not ref[i].isdigit():
        i += 1
    prefix = ref[:i]
    j = i
    while j < n and ref[j].isdigit():
        j += 1
    num = int(ref[i:j]) if j > i else -1
    return (prefix, num)

def pad_centroid_offset(pads, origin):
    if not pads:
        return (0.0, 0.0)
    sx = 0.0
    sy = 0.0
    for (xmin, ymin, xmax, ymax) in pads:
        sx += (xmin + xmax) / 2.0
        sy += (ymin + ymax) / 2.0
    cx = sx / len(pads)
    cy = sy / len(pads)
    return (cx - origin[0], cy - origin[1])

def pad_extent(pads, origin):
    if not pads:
        return (0.0, 0.0, 0.0, 0.0)
    xmin = min(p[0] for p in pads)
    ymin = min(p[1] for p in pads)
    xmax = max(p[2] for p in pads)
    ymax = max(p[3] for p in pads)
    ox, oy = origin
    return (ox - xmin, xmax - ox, oy - ymin, ymax - oy)

def _rotate_vec(vx, vy, theta):
    a = _et_math.radians(-theta)
    ca = _et_math.cos(a)
    sa = _et_math.sin(a)
    return (vx * ca - vy * sa, vx * sa + vy * ca)

def rotate_extents(ext, theta):
    L, R, T, B = ext
    t = theta % 360
    if t == 90:
        return (T, B, R, L)
    if t == 180:
        return (R, L, B, T)
    if t == 270:
        return (B, T, L, R)
    return (L, R, T, B)

def rotation_to_face(vec, target_normal, eps=0.3):
    vx, vy = vec
    if _et_math.hypot(vx, vy) < eps:
        return 0
    nx, ny = target_normal
    best_theta = 0
    best_dot = -1e18
    for theta in (0, 90, 180, 270):
        rx, ry = _rotate_vec(vx, vy, theta)
        mag = _et_math.hypot(rx, ry) or 1.0
        dot = (rx / mag) * nx + (ry / mag) * ny
        if dot > best_dot:
            best_dot = dot
            best_theta = theta
    return best_theta

def inward_normal(edge):
    if edge == "top":
        return (0.0, 1.0)
    if edge == "bottom":
        return (0.0, -1.0)
    if edge == "left":
        return (1.0, 0.0)
    if edge == "right":
        return (-1.0, 0.0)
    return (0.0, 0.0)

def outward_normal(edge):
    ix, iy = inward_normal(edge)
    return (-ix, -iy)

def nearest_edge(target, board_box):
    tx, ty = target
    xmin, ymin, xmax, ymax = board_box
    dists = (
        ("top", ty - ymin),
        ("bottom", ymax - ty),
        ("left", tx - xmin),
        ("right", xmax - tx),
    )
    best_edge = "top"
    best_d = None
    for edge, d in dists:
        if best_d is None or d < best_d:
            best_d = d
            best_edge = edge
    return best_edge

def layout_along_edge(items, edge, board_box, margin, spacing, clearance=None, anchor="start"):
    if clearance is None:
        clearance = margin
    xmin, ymin, xmax, ymax = board_box
    out = []
    if edge in ("top", "bottom"):
        edge_lo = xmin + margin
        total = (sum(it[1][0] + it[1][1] for it in items)
                 + spacing * max(0, len(items) - 1))
        cursor = edge_lo + (max(0.0, ((xmax - margin) - edge_lo - total) / 2.0)
                            if anchor == "center" else 0.0)
        for (ref, ext, pad_ext, overhang) in items:
            L, R, T, B = ext
            pL, pR, pT, pB = pad_ext
            x = cursor + L
            if edge == "top":
                y = (ymin + clearance + pT) if overhang else (ymin + margin + T)
            else:
                y = (ymax - clearance - pB) if overhang else (ymax - margin - B)
            fits = (x + R) <= (xmax - margin)
            out.append((ref, x, y, fits))
            cursor = x + R + spacing
    else:
        edge_lo = ymin + margin
        total = (sum(it[1][2] + it[1][3] for it in items)
                 + spacing * max(0, len(items) - 1))
        cursor = edge_lo + (max(0.0, ((ymax - margin) - edge_lo - total) / 2.0)
                            if anchor == "center" else 0.0)
        for (ref, ext, pad_ext, overhang) in items:
            L, R, T, B = ext
            pL, pR, pT, pB = pad_ext
            y = cursor + T
            if edge == "left":
                x = (xmin + clearance + pL) if overhang else (xmin + margin + L)
            else:
                x = (xmax - clearance - pR) if overhang else (xmax - margin - R)
            fits = (y + B) <= (ymax - margin)
            out.append((ref, x, y, fits))
            cursor = y + B + spacing
    return out
"""
