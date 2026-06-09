"""Integration tier of card validation (needs KiCad symbol libraries).

The unit tier (tests/test_device_cards.py) checks structure with no KiCad. This
tier loads each card's actual symbol and asserts every pin the card references
exists on that symbol — the "verify pins against the real symbol" discipline,
enforced mechanically. A wrong pin name in a card fails here.

The set of pin references is computed by ``cards.peripheral_pin_refs`` /
``mcu_pin_refs``, driven by the ``*_PIN_FIELDS`` registry — the SAME registry the
unit-tier meta-gate (``test_card_fields_all_classified``) keeps complete. So a new
pin-bearing card field is both validated here AND can't escape classification: the
two gates can never drift (the port_pins lesson).

Gated by KICAD_INTEGRATION=1; runs on the self-hosted KiCad 9/10 matrix.
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)


def _all_cards():
    from kicad_mcp.utils.firmware.cards import (
        load_cards,
        mcu_pin_refs,
        peripheral_pin_refs,
    )
    peris, mcus = load_cards()
    items = [("peripheral", t, c, peripheral_pin_refs(c)) for t, c in peris.items()]
    items += [("mcu", c["part"], c, mcu_pin_refs(c)) for c in mcus]
    return items


@pytest.mark.parametrize("kind,name,card,pins",
                         _all_cards(),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_card_pins_exist_on_symbol(kind, name, card, pins):
    from kicad_sch_api.library.cache import get_symbol_cache

    from kicad_mcp.utils.firmware.knowledge import resolve_symbol
    from kicad_mcp.utils.firmware.mcu_pinmap import resolve_pin_token

    # Resolve via the same cross-version helper the generator uses, so a card
    # whose symbol KiCad renamed (MCP23017_SO/MCP23017x-x-SO) validates on BOTH
    # the KiCad 9 and KiCad 10 matrix legs.
    candidates = [card["lib_id"], *card.get("alt_lib_ids", [])]
    _, sym = resolve_symbol(get_symbol_cache(), card["lib_id"], card.get("alt_lib_ids", []))
    assert sym is not None, f"{kind} {name}: no symbol resolved from {candidates}"
    missing = [p for p in pins if resolve_pin_token(sym, p) is None]
    assert not missing, (
        f"{kind} {name} ({card['lib_id']}): pins not on symbol: {missing}. "
        f"Available names={[getattr(p, 'name', None) for p in (sym.pins or [])][:40]}"
    )
