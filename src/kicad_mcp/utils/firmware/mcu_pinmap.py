"""Resolve signals to symbol pin NUMBERS.

Two pure helpers over a kicad-sch-api symbol's ``.pins`` list:

  * ``gpio_to_pin_number`` — maps an MCU GPIO number to the symbol pin whose
    name carries the ``IO{n}`` token (ESP32 names pins ``IO21``, ``RXD0/IO3``,
    ``IO0`` …). Derived from the symbol's own pin names — no hardcoded table —
    so it generalizes to any ESP32 variant whose pins follow the ``IO{n}``
    convention. Returns None (caller flags) when no such pin exists, e.g.
    input-only ``SENSOR_VP``/``SENSOR_VN`` pins that lack an ``IO`` token.
  * ``pin_number_by_name`` — exact pin-name lookup (for peripheral pins whose
    name we resolved via the knowledge ``roles`` map).

Tokenized matching (split on non-alphanumerics) prevents ``IO3`` from matching
``IO35``.
"""
from __future__ import annotations

import re
from typing import Any, Optional

_TOKEN_RE = re.compile(r"[^A-Za-z0-9]+")


def _tokens(name: str) -> list[str]:
    return [t for t in _TOKEN_RE.split(name) if t]


def gpio_to_pin_number(symbol: Any, gpio: int) -> Optional[str]:
    """Return the pin NUMBER (str) for the symbol pin naming GPIO ``gpio``."""
    if symbol is None:
        return None
    want = f"IO{gpio}"
    for pin in getattr(symbol, "pins", None) or ():
        if want in _tokens(pin.name or ""):
            return str(pin.number)
    return None


def pin_number_by_name(symbol: Any, pin_name: Optional[str]) -> Optional[str]:
    """Return the pin NUMBER (str) for the pin named exactly ``pin_name``."""
    if symbol is None or not pin_name:
        return None
    for pin in getattr(symbol, "pins", None) or ():
        if (pin.name or "") == pin_name:
            return str(pin.number)
    return None
