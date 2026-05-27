"""Spiral candidate generator and grid fallback for PCB component placement.

Pure-Python functions used by ``suggest_placement`` in ``pcb_planning.py``.
Embedded scripts that run inside pcbnew's Python interpreter cannot
``import`` from this module; they inject :data:`SPIRAL_HELPER` (a source
string defining the same functions) into their embedded script source.

**Threshold semantics:**

- ``is_within_outline(box, outline, spacing)`` — the *strict-less-than*
  check mirrors the original inline code: ``box[0] < x_min + spacing`` is
  rejected (too close or outside).  A footprint positioned exactly at
  ``x_min + spacing`` passes; ``x_min + spacing - ε`` is rejected.

- ``generate_grid_fallback`` — the ``int(... + 1)`` inset is preserved
  exactly; this keeps footprints with non-integer outline coordinates one
  integer step inside the board.
"""

from __future__ import annotations

import math
from typing import Iterator, Tuple


# ---------------------------------------------------------------------------
# Board-margin check
# ---------------------------------------------------------------------------

def is_within_outline(
    box: Tuple[float, float, float, float],
    outline: dict,
    spacing: float,
) -> bool:
    """Return True if *box* is inside the outline by at least *spacing*.

    Box is a tuple ``(x_min, y_min, x_max, y_max)``.  Outline is a dict
    with keys ``x_min_mm``, ``x_max_mm``, ``y_min_mm``, ``y_max_mm``.

    The boundary condition is strict-less-than (same as the original inline
    code):

    - ``box[0] == outline["x_min_mm"] + spacing`` → **passes** (returns True)
    - ``box[0] == outline["x_min_mm"] + spacing - ε`` → **rejected** (returns False)
    - ``spacing == 0`` → accepts any box whose edge is at or inside the outline

    This function is the single source of truth for the margin check;
    ``pcb_planning.py`` injects :data:`SPIRAL_HELPER` so the same logic runs
    inside the pcbnew subprocess.
    """
    return not (
        box[0] < outline["x_min_mm"] + spacing
        or box[2] > outline["x_max_mm"] - spacing
        or box[1] < outline["y_min_mm"] + spacing
        or box[3] > outline["y_max_mm"] - spacing
    )


# ---------------------------------------------------------------------------
# Spiral candidate generator
# ---------------------------------------------------------------------------

def generate_spiral_candidates(
    tx: float,
    ty: float,
    hw: float,
    hh: float,
    outline: dict,
    spacing: float,
    n_radii: int = 39,
    step_mm: float = 2.0,
    n_angles: int = 12,
) -> Iterator[Tuple[float, float, Tuple[float, float, float, float]]]:
    """Yield ``(px, py, box)`` candidates in spiral order around ``(tx, ty)``.

    Candidates are produced by walking *n_radii* radii (``step_mm * 1..n_radii``)
    and *n_angles* equi-spaced angles per radius.  At each candidate the
    position is clamped to the board outline (same ``clamp_to_board`` logic
    used in the embedded script is NOT applied here — callers must clamp
    before calling this generator if they need clamping, exactly as the
    original inline code did).

    Only candidates that pass :func:`is_within_outline` are yielded.

    Args:
        tx: X coordinate of the target (placement anchor) in mm.
        ty: Y coordinate of the target in mm.
        hw: Half-width of the footprint to place in mm.
        hh: Half-height of the footprint to place in mm.
        outline: Dict with ``x_min_mm``, ``x_max_mm``, ``y_min_mm``, ``y_max_mm``.
        spacing: Minimum clearance to the board edge in mm.
        n_radii: Number of concentric radius rings (default 39 → range(1, 40)).
        step_mm: Spacing between successive radii in mm (default 2.0).
        n_angles: Number of candidate angles per radius (default 12 → every 30°).
    """
    for radius in [r * step_mm for r in range(1, n_radii + 1)]:
        for angle_deg in range(0, 360, 360 // n_angles):
            angle = math.radians(angle_deg)
            px = tx + radius * math.cos(angle)
            py = ty + radius * math.sin(angle)

            # Clamp to board (mirrors clamp_to_board in the embedded script)
            px = max(
                outline["x_min_mm"] + hw + spacing,
                min(px, outline["x_max_mm"] - hw - spacing),
            )
            py = max(
                outline["y_min_mm"] + hh + spacing,
                min(py, outline["y_max_mm"] - hh - spacing),
            )

            box = (px - hw, py - hh, px + hw, py + hh)

            if is_within_outline(box, outline, spacing):
                yield px, py, box


# ---------------------------------------------------------------------------
# Integer grid fallback
# ---------------------------------------------------------------------------

def generate_grid_fallback(
    hw: float,
    hh: float,
    outline: dict,
    step: int = 2,
) -> Iterator[Tuple[float, float, Tuple[float, float, float, float]]]:
    """Yield ``(gx, gy, box)`` on an integer grid covering the outline interior.

    The grid starts at ``int(outline["x_min_mm"] + hw + 1)`` — the ``+1``
    integer inset keeps footprints inside the board even when the outline
    coordinate is non-integer (e.g. ``x_min=1.3`` → grid starts at ``int(1.3
    + hw + 1) = int(hw + 2.3)``).

    Yields every integer-step ``(gx, gy)`` pair with the corresponding
    footprint bounding box.  No outline-margin filter is applied; callers use
    ``box_collides`` directly, matching the original fallback loop behavior.

    Args:
        hw: Half-width of the footprint in mm.
        hh: Half-height of the footprint in mm.
        outline: Dict with ``x_min_mm``, ``x_max_mm``, ``y_min_mm``, ``y_max_mm``.
        step: Integer step between grid points in mm (default 2).
    """
    x_start = int(outline["x_min_mm"] + hw + 1)
    x_stop = int(outline["x_max_mm"] - hw)
    y_start = int(outline["y_min_mm"] + hh + 1)
    y_stop = int(outline["y_max_mm"] - hh)

    for gx in range(x_start, x_stop, step):
        for gy in range(y_start, y_stop, step):
            box = (gx - hw, gy - hh, gx + hw, gy + hh)
            yield float(gx), float(gy), box


# ---------------------------------------------------------------------------
# Injectable source string for embedded pcbnew scripts
# ---------------------------------------------------------------------------
# Embedded scripts concatenate SPIRAL_HELPER into their source.  The
# definitions below MUST stay byte-equivalent to the Python-side functions
# above — that is the whole point of having one source of truth.

SPIRAL_HELPER = """
import math as _math

def is_within_outline(box, outline, spacing):
    return not (
        box[0] < outline["x_min_mm"] + spacing
        or box[2] > outline["x_max_mm"] - spacing
        or box[1] < outline["y_min_mm"] + spacing
        or box[3] > outline["y_max_mm"] - spacing
    )

def generate_spiral_candidates(tx, ty, hw, hh, outline, spacing,
                                 n_radii=39, step_mm=2.0, n_angles=12):
    for radius in [r * step_mm for r in range(1, n_radii + 1)]:
        for angle_deg in range(0, 360, 360 // n_angles):
            angle = _math.radians(angle_deg)
            px = tx + radius * _math.cos(angle)
            py = ty + radius * _math.sin(angle)
            px = max(
                outline["x_min_mm"] + hw + spacing,
                min(px, outline["x_max_mm"] - hw - spacing),
            )
            py = max(
                outline["y_min_mm"] + hh + spacing,
                min(py, outline["y_max_mm"] - hh - spacing),
            )
            box = (px - hw, py - hh, px + hw, py + hh)
            if is_within_outline(box, outline, spacing):
                yield px, py, box

def generate_grid_fallback(hw, hh, outline, step=2):
    x_start = int(outline["x_min_mm"] + hw + 1)
    x_stop = int(outline["x_max_mm"] - hw)
    y_start = int(outline["y_min_mm"] + hh + 1)
    y_stop = int(outline["y_max_mm"] - hh)
    for gx in range(x_start, x_stop, step):
        for gy in range(y_start, y_stop, step):
            box = (gx - hw, gy - hh, gx + hw, gy + hh)
            yield float(gx), float(gy), box
"""
