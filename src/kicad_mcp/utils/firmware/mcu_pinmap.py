"""Resolve signals to symbol pin NUMBERS.

Two pure helpers over a kicad-sch-api symbol's ``.pins`` list:

  * ``gpio_to_pin_number`` — maps an MCU GPIO number to the symbol pin whose
    name carries the ``{prefix}{n}`` token. The prefix is per-MCU data (the MCU
    card's ``gpio_pin_prefix``): ESP32 names pins ``IO21``/``RXD0/IO3``/``IO0``
    (prefix ``IO``), the RP2040/Pico names them ``GPIO21``/``GPIO26_ADC0``
    (prefix ``GPIO``). Derived from the symbol's own pin names — no hardcoded
    table — so it generalizes to any MCU whose pins follow a ``{prefix}{n}``
    convention. Returns None (caller flags) when no such pin exists, e.g. the
    Pico's GP25 on-board LED (not broken out on the module symbol) or an ESP32
    input-only ``SENSOR_VP``/``SENSOR_VN`` pin that lacks an ``IO`` token.
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


def gpio_to_pin_number(symbol: Any, gpio: int, prefix: str = "IO") -> Optional[str]:
    """Return the pin NUMBER (str) for the symbol pin naming GPIO ``gpio``.

    ``prefix`` is the MCU's GPIO pin-name token prefix (ESP32 ``IO`` -> ``IO21``;
    RP2040/Pico ``GPIO`` -> ``GPIO21``). Tokenized matching (split on
    non-alphanumerics, incl. ``_``) means ``GPIO26`` matches the compound Pico
    pin ``GPIO26_ADC0`` while ``GPIO2`` still does NOT match ``GPIO20``."""
    if symbol is None:
        return None
    want = f"{prefix}{gpio}"
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


def resolve_pin_token(symbol: Any, token: Any) -> Optional[str]:
    """Resolve a pin token to its NUMBER. A token is either a pin NAME
    (MCP23017 ``SDA``) or already a literal pin NUMBER (a header pin ``4``, a
    USB-C/QFN pad ``A4``/``29``). Name lookup first; else validate the token
    against the symbol's real pin numbers (alphanumeric, so not a digit check).

    Single source of truth for name-or-number pin resolution — consumed by the
    generator (wiring), the card validator (pin-existence), and auto-draft
    (role→pin scoring), so none re-encode the rule (CLAUDE.md Rule 3).
    """
    if symbol is None or token is None:
        return None
    num = pin_number_by_name(symbol, str(token))
    if num is not None:
        return num
    valid = {str(p.number) for p in (getattr(symbol, "pins", None) or ())}
    return str(token) if str(token) in valid else None
