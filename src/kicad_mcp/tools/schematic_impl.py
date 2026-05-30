"""Schematic tools — wrapping kicad-sch-api for schematic manipulation.

All tools operate on a single in-memory schematic at a time.  ``load_schematic``
or ``create_schematic`` must be called first; subsequent tools operate on that
loaded instance until a different schematic is loaded.

The underlying library is kicad-sch-api (PyPI), which provides lossless
round-trip parsing of .kicad_sch files.
"""
# TODO: Migrate !r script interpolation to JSON params (see pcb_board.py for pattern)

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level schematic state
# ---------------------------------------------------------------------------

_current_schematic: Any | None = None

SCHEMATIC_GRID_MM = 1.27  # KiCad schematic grid (50 mils)


def _require_schematic() -> Any:
    """Return the current schematic or raise."""
    if _current_schematic is None:
        raise RuntimeError("No schematic loaded. Call create_schematic or load_schematic first.")
    return _current_schematic


# ---------------------------------------------------------------------------
# Module-level helpers — imported and used by the schematic router (schematic.py)
# ---------------------------------------------------------------------------

def _snap(x: float, y: float) -> tuple:
    """Round both coordinates to the nearest KiCad schematic grid step."""
    g = SCHEMATIC_GRID_MM
    return (round(round(x / g) * g, 6), round(round(y / g) * g, 6))


def _parse_unit_pin_mapping(symbol_def):
    """Parse raw KiCad symbol data to build {unit_num: [pin_numbers]} mapping.

    Multi-unit symbols (LM393, 7400, etc.) have sub-symbols named like
    ``LM393_1_1`` (unit 1, style 1), ``LM393_3_1`` (unit 3, style 1).
    Style ``_2`` variants are DeMorgan representations and are skipped.
    """
    import sexpdata as _sexpdata  # type: ignore[import-untyped]

    def _tag(elem) -> str:
        if isinstance(elem, _sexpdata.Symbol):
            return str(elem.value())
        return str(elem).strip('"')

    unit_pins: dict = {}
    raw = symbol_def.raw_kicad_data
    if not isinstance(raw, list):
        return unit_pins
    for item in raw:
        if not isinstance(item, list) or len(item) < 2:
            continue
        if _tag(item[0]) != "symbol":
            continue
        name = item[1] if isinstance(item[1], str) else str(item[1]).strip('"')
        parts = name.split("_")
        if len(parts) < 3:
            continue
        try:
            unit_num = int(parts[-2])
            style = int(parts[-1])
            if style != 1:
                continue
        except ValueError:
            logger.debug("Skipped malformed sub-symbol name %r in unit-pin mapping", name)
            continue
        pins: list = []
        for sub in item:
            if not isinstance(sub, list) or len(sub) < 2:
                continue
            if _tag(sub[0]) != "pin":
                continue
            for s in sub:
                if isinstance(s, list) and len(s) >= 2:
                    if _tag(s[0]) == "number":
                        pn = s[1] if isinstance(s[1], str) else str(s[1]).strip('"')
                        pins.append(pn)
        if pins:
            unit_pins[unit_num] = pins
    return unit_pins


def _find_component_for_pin(sch, reference: str, pin_number: str):
    """Find the component instance (unit) that owns the given pin."""
    from kicad_sch_api.library.cache import get_symbol_cache
    all_comps = [c for c in sch.components if c.reference == reference]
    if len(all_comps) <= 1:
        return sch.components.get(reference)
    cache = get_symbol_cache()
    symbol_def = cache.get_symbol(all_comps[0].lib_id)
    if symbol_def is None:
        return all_comps[0]
    unit_pins = _parse_unit_pin_mapping(symbol_def)
    for comp in all_comps:
        unit = getattr(comp, "unit", None)
        if unit is None:
            unit = getattr(getattr(comp, "_data", None), "unit", 1) or 1
        if pin_number in unit_pins.get(unit, []):
            return comp
    return all_comps[0]


def _kicad_pin_position(comp, pin_number: str):
    """Return the absolute pin-tip position as KiCad computes it."""
    import math
    from kicad_sch_api.core.types import Point
    pin = comp.get_pin(pin_number)
    if pin is None:
        return None
    angle_rad = math.radians(comp.rotation)
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    rx = pin.position.x * cos_a - pin.position.y * sin_a
    ry = pin.position.x * sin_a + pin.position.y * cos_a
    return Point(
        round(comp.position.x + rx, 3),
        round(comp.position.y - ry, 3),
    )


def _pin_wire_offset(comp, pin_number: str, distance: float = 2.54):
    """Compute (dx, dy) for a wire stub extending away from the symbol body."""
    import math
    pin = comp.get_pin(pin_number)
    if pin is None:
        return (distance, 0.0)
    away_deg = (-pin.rotation - comp.rotation + 180) % 360
    away_rad = math.radians(away_deg)
    dx = distance * math.cos(away_rad)
    dy = distance * math.sin(away_rad)
    return _snap(dx, dy)
