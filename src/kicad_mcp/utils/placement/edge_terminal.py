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
    """Rotate a vector by ``theta`` degrees (math-CCW in y-down coords)."""
    a = math.radians(theta)
    ca = math.cos(a)
    sa = math.sin(a)
    return (vx * ca - vy * sa, vx * sa + vy * ca)


def rotate_extents(
    ext: Tuple[float, float, float, float],
    theta: int,
) -> Tuple[float, float, float, float]:
    """Rotate asymmetric extents ``(L, R, T, B)`` by ``theta in {0,90,180,270}``.

    Derived by mapping the extent-box corners through the same ``R(theta)`` used
    by :func:`_rotate_vec`:
        0   → (L, R, T, B)
        90  → (B, T, L, R)
        180 → (R, L, B, T)
        270 → (T, B, R, L)
    A non-orthogonal angle leaves the (axis-aligned) extents unchanged."""
    L, R, T, B = ext
    t = theta % 360
    if t == 90:
        return (B, T, L, R)
    if t == 180:
        return (R, L, B, T)
    if t == 270:
        return (T, B, R, L)
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
) -> list:
    """Lay connectors out in given order along ``edge``.

    ``items``: ``(ref, ext, pad_ext, overhang)`` where ``ext`` / ``pad_ext`` are
    the *rotated* ``(L, R, T, B)`` extents and ``overhang`` requests the body to
    cross the edge outward.  Returns ``[(ref, x, y, fits), ...]`` in order;
    ``fits`` is False if the cursor ran past the edge (caller falls back).

    Cross-axis anchoring: an ``overhang`` terminal anchors its PAD box at
    ``clearance`` inside the edge (courtyard may cross the edge outward, pads
    stay on-board); otherwise the full courtyard sits ``margin`` inside.  Along
    the edge, connectors advance by their courtyard width plus ``spacing``."""
    if clearance is None:
        clearance = margin
    xmin, ymin, xmax, ymax = board_box
    out = []
    if edge in ("top", "bottom"):
        cursor = xmin + margin
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
        cursor = ymin + margin
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
# Injectable source string for embedded pcbnew scripts
# ---------------------------------------------------------------------------
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
    a = _et_math.radians(theta)
    ca = _et_math.cos(a)
    sa = _et_math.sin(a)
    return (vx * ca - vy * sa, vx * sa + vy * ca)

def rotate_extents(ext, theta):
    L, R, T, B = ext
    t = theta % 360
    if t == 90:
        return (B, T, L, R)
    if t == 180:
        return (R, L, B, T)
    if t == 270:
        return (T, B, R, L)
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

def layout_along_edge(items, edge, board_box, margin, spacing, clearance=None):
    if clearance is None:
        clearance = margin
    xmin, ymin, xmax, ymax = board_box
    out = []
    if edge in ("top", "bottom"):
        cursor = xmin + margin
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
        cursor = ymin + margin
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
