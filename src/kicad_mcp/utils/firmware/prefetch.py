"""Pre-fetch card synthesis (Phase 8) — the **pure** logic that turns a KiCad
symbol's pins into a candidate device card. The maintainer-time CLI
(``scripts/prefetch_cards.py``) does the symbol enumeration + file I/O around
this; keeping the logic here makes it unit-testable with no KiCad.

The idea (spec §11–12): the bundled card library is bulk-generated at *build
time* (maintainer-side; the **user** stays air-gapped) by enumerating the
locally-installed KiCad symbol libraries and auto-drafting a card per symbol from
its pin *names*. Most well-named I2C sensors role-map by identity (``SDA``→``SDA``),
so they synthesize for free; anything with an unexplained pin, no clean bus, or
missing power is downgraded to ``low`` and left for human review — so PRs reduce
to the genuinely new-or-weird residual.

Conservative by construction (the cold-review false-confidence hazard): a card is
``high`` only when it exposes a clean I2C bus, every other pin is power/ground/NC,
and it has both supply and ground. Multi-interface parts (extra SPI/CS pins) and
numbered-header modules do NOT auto-synthesize high.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from kicad_mcp.utils.firmware.autodraft import _GROUND_RE, _SUPPLY_RE, identify_by_address

# We auto-synthesize the high-confidence tier for I2C only — it's the common,
# address-identifiable case. Other buses are reported but not auto-shipped.
_I2C_ROLES = ("SDA", "SCL")
_NC_NAMES = frozenset({"NC", "~", ""})


def _type_from_symbol_name(name: str) -> str:
    """Firmware-style type key from a symbol name: ``MPU-6050`` -> ``MPU6050``
    (matches what ``parse.address_base`` yields from ``MPU6050_ADDR``)."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def _is_nc(name: str) -> bool:
    return name.strip().upper() in _NC_NAMES or name.strip().upper().startswith("NC")


def synthesize_i2c_card(
    *,
    symbol_name: str,
    lib_id: str,
    pin_names: list[str],
    footprint: Optional[str] = None,
    address: Optional[int] = None,
    unit_count: int = 1,
) -> tuple[Optional[dict[str, Any]], str, list[str]]:
    """Try to synthesize an I2C device card from a symbol's pin NAMES.

    Returns ``(card_or_None, confidence, reasons)``. ``confidence`` is
    ``high`` | ``low`` | ``skip`` (skip => card is None). Footprint is best-effort
    (symbols don't carry one) and left as ``TODO:confirm`` when unknown — the card
    is a *draft for review*, not a shipped fact.
    """
    names = {n.strip() for n in pin_names if n and n.strip()}
    reasons: list[str] = []

    if not set(_I2C_ROLES) <= names:
        return None, "skip", ["no clean I2C bus signature (need SDA + SCL by name)"]

    supply = sorted(n for n in names if _SUPPLY_RE.match(n))
    ground = sorted(n for n in names if _GROUND_RE.match(n))
    explained = set(_I2C_ROLES) | set(supply) | set(ground)
    unexplained = sorted(n for n in names if n not in explained and not _is_nc(n))

    addr_type = identify_by_address(address)
    corroborated = addr_type is not None and addr_type == _type_from_symbol_name(symbol_name)
    if addr_type:
        reasons.append(f"I2C address {hex(address or 0)} → {addr_type}"
                       + (" (corroborates symbol)" if corroborated else ""))

    confidence = "low"
    if not supply or not ground:
        reasons.append("flagged: symbol lacks an identifiable supply/ground pin")
    elif unit_count > 1:
        reasons.append("flagged: multi-unit symbol (ambiguous pin lookup)")
    elif unexplained:
        reasons.append(f"flagged: unexplained signal pins {unexplained[:8]} "
                       "(multi-interface part — confirm before shipping)")
    else:
        confidence = "high"
        reasons.append("clean I2C bus; all non-bus pins are power/ground/NC")

    card: dict[str, Any] = {
        "type": _type_from_symbol_name(symbol_name),
        "lib_id": lib_id,
        "value": symbol_name,
        "bus": "I2C",
        "footprint": footprint or "TODO:confirm",
        "roles": {"SDA": "SDA", "SCL": "SCL"},
        "supply_pins": supply,
        "ground_pins": ground,
        "module": False,
        "_draft": {"confidence": confidence, "reasons": reasons,
                   "needs_footprint": footprint is None},
    }
    return card, confidence, reasons


def top_level_symbol_names(kicad_sym_text: str) -> list[str]:
    """Extract the top-level symbol names from a ``.kicad_sym`` file's text,
    excluding the ``NAME_0_1`` / ``NAME_1_1`` graphical sub-symbols."""
    out: list[str] = []
    for m in re.finditer(r'^\s*\(symbol\s+"([^"]+)"', kicad_sym_text, re.MULTILINE):
        name = m.group(1)
        if re.search(r"_\d+_\d+$", name):
            continue
        out.append(name)
    return out
