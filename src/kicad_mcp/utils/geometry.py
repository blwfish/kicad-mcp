"""Canonical 2D geometry primitives for axis-aligned rectangles.

Two representations are supported:
- **Dict-format**: ``{"x_min_mm": ..., "y_min_mm": ..., "x_max_mm": ..., "y_max_mm": ...}``
- **Tuple-format**: ``(x_min, y_min, x_max, y_max)``

Functions are unit-agnostic — they compare whatever numeric values are passed.
Dict variants conventionally hold mm; tuple variants are used both for mm
coordinates (placement engines) and KiCad internal nanometer coordinates
(pcbnew bounding boxes).

**Non-strict semantics throughout.** Touching edges and coincident boundaries
are treated as the violating case:

- Two rects sharing an edge → ``rects_overlap`` returns ``True``
- A rect with an edge coincident with the outer → ``rect_inside`` returns ``True``

This matches KiCad's DRC engine, which flags any gap below ``min_clearance`` as
a violation (and zero gap is always below any positive clearance).  Our audit
flags the same geometry KiCad would, eliminating the false-negative gap where
our tool reported a placement clean but DRC then rejected it.

Embedded scripts that run inside pcbnew's Python interpreter cannot
``import`` from this module; they inject :data:`GEOMETRY_HELPER` (a source
string defining the same functions) into the embedded script source.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Dict-format primitives
# ---------------------------------------------------------------------------

def rects_overlap(a: dict, b: dict) -> bool:
    """Non-strict overlap test on dict-format rects.

    Two rects sharing an edge return ``True`` (touching counts as overlap).
    Matches KiCad DRC's notion of "any contact is a clearance violation."
    """
    return (a["x_min_mm"] <= b["x_max_mm"] and a["x_max_mm"] >= b["x_min_mm"] and
            a["y_min_mm"] <= b["y_max_mm"] and a["y_max_mm"] >= b["y_min_mm"])


def rect_inside(inner: dict, outer: dict) -> bool:
    """Non-strict containment on dict-format rects.

    ``inner`` is inside ``outer`` if every edge of ``inner`` is at or inside
    the corresponding edge of ``outer``.  Touching the boundary counts as
    inside.
    """
    return (inner["x_min_mm"] >= outer["x_min_mm"] and inner["x_max_mm"] <= outer["x_max_mm"] and
            inner["y_min_mm"] >= outer["y_min_mm"] and inner["y_max_mm"] <= outer["y_max_mm"])


def overlap_area(a: dict, b: dict) -> float:
    """Area of overlap between two dict-format rects, clipped to ``>= 0``.

    Returns ``0.0`` when rects do not overlap or only touch.  Sign of overlap
    (i.e. the touching vs embedded distinction) is carried by
    :func:`signed_gap_mm`, not this function.
    """
    dx = max(0.0, min(a["x_max_mm"], b["x_max_mm"]) - max(a["x_min_mm"], b["x_min_mm"]))
    dy = max(0.0, min(a["y_max_mm"], b["y_max_mm"]) - max(a["y_min_mm"], b["y_min_mm"]))
    return round(dx * dy, 2)


def signed_gap_mm(a: dict, b: dict) -> float:
    """Signed orthogonal gap between two dict-format rects.

    Returns:
        - ``> 0`` — rects are separated; value is the binding-axis distance
        - ``== 0`` — rects touch on at least one edge
        - ``< 0`` — rects overlap; value is the negation of penetration depth
          on the less-embedded axis (smallest move to separate)

    Sign carries semantic content: callers that lose the sign cannot
    distinguish "touching" (``0``) from "embedded by 2 mm" (``-2.0``).
    """
    gap_x = max(a["x_min_mm"] - b["x_max_mm"], b["x_min_mm"] - a["x_max_mm"])
    gap_y = max(a["y_min_mm"] - b["y_max_mm"], b["y_min_mm"] - a["y_max_mm"])
    if gap_x >= 0 and gap_y >= 0:
        # Separated on both axes — the smaller axis gap is the clearance
        return min(gap_x, gap_y)
    if gap_x >= 0 or gap_y >= 0:
        # Separated on one axis only — that axis gap is the clearance
        return max(gap_x, gap_y)
    # Overlapping on both axes — the less-negative gap is the penetration
    # depth on the easier-to-fix axis (smallest move to separate)
    return max(gap_x, gap_y)


# ---------------------------------------------------------------------------
# Tuple-format primitives
# ---------------------------------------------------------------------------

def aabb_overlap(a: tuple, b: tuple) -> bool:
    """Non-strict overlap test on tuple-format rects ``(x_min, y_min, x_max, y_max)``.

    Two rects sharing an edge return ``True``.
    """
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def aabb_inside(inner: tuple, outer: tuple) -> bool:
    """Non-strict containment on tuple-format rects.

    Touching the boundary counts as inside.
    """
    return (inner[0] >= outer[0] and inner[2] <= outer[2] and
            inner[1] >= outer[1] and inner[3] <= outer[3])


# ---------------------------------------------------------------------------
# Injectable source for embedded scripts
# ---------------------------------------------------------------------------
# Embedded scripts run inside pcbnew's Python and cannot `import` from this
# module; they string-concatenate GEOMETRY_HELPER into their source to get
# the same primitives.  The definitions below MUST stay byte-equivalent to
# the Python-side functions above — that's the whole point of having one
# source of truth.

GEOMETRY_HELPER = """
def aabb_overlap(a, b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]

def aabb_inside(inner, outer):
    return (inner[0] >= outer[0] and inner[2] <= outer[2] and
            inner[1] >= outer[1] and inner[3] <= outer[3])

def rects_overlap(a, b):
    return (a["x_min_mm"] <= b["x_max_mm"] and a["x_max_mm"] >= b["x_min_mm"] and
            a["y_min_mm"] <= b["y_max_mm"] and a["y_max_mm"] >= b["y_min_mm"])

def rect_inside(inner, outer):
    return (inner["x_min_mm"] >= outer["x_min_mm"] and inner["x_max_mm"] <= outer["x_max_mm"] and
            inner["y_min_mm"] >= outer["y_min_mm"] and inner["y_max_mm"] <= outer["y_max_mm"])

def overlap_area(a, b):
    dx = max(0.0, min(a["x_max_mm"], b["x_max_mm"]) - max(a["x_min_mm"], b["x_min_mm"]))
    dy = max(0.0, min(a["y_max_mm"], b["y_max_mm"]) - max(a["y_min_mm"], b["y_min_mm"]))
    return round(dx * dy, 2)

def signed_gap_mm(a, b):
    gap_x = max(a["x_min_mm"] - b["x_max_mm"], b["x_min_mm"] - a["x_max_mm"])
    gap_y = max(a["y_min_mm"] - b["y_max_mm"], b["y_min_mm"] - a["y_max_mm"])
    if gap_x >= 0 and gap_y >= 0:
        return min(gap_x, gap_y)
    if gap_x >= 0 or gap_y >= 0:
        return max(gap_x, gap_y)
    return max(gap_x, gap_y)
"""
