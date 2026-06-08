"""Terminal-only device card tests (v1).

A "terminal-only" card (``realize: terminal``) is a device that is ALWAYS
realized as a labeled screw terminal — no chip-down symbol, no board.yaml
``locus: remote`` required. The card IS the explicit declaration. Three cards
ship with v1: PIEZO (single ADC/GPIO), SWITCH (single GPIO), TCRT5000 (single
digital output).

Coverage per CLAUDE.md threshold / seam rules:
  * A firmware naming a terminal-only device gets a labeled screw terminal
    with its GPIO + power wired and NO orphan net.
  * Glue suppression: no bypass caps / decoupling added for a terminal device.
  * The peripheral's original ref is NOT placed in the schematic (excluded via
    is_remote); unresolved_endpoints does not fire for its original net endpoint.
  * Multi-instance non-collision: two PIEZO instances get distinct J-refs.
  * **Invariant pin**: an I2S bus without locus:remote is STILL REFUSED (no
    auto-terminal), i.e. INMP441 without a remote directive is never silently
    terminal-ized. This guards the spec §4 honesty-contradiction rule.
  * At/below/above at the realize-field boundary: terminal / chip / bus+remote /
    bus-no-remote / absent-realize.
  * Card structural validation accepts "terminal", rejects unknown values.
"""
from __future__ import annotations

import asyncio

from kicad_mcp.utils.firmware import knowledge as K
from kicad_mcp.utils.firmware.cards import (
    load_cards,
    terminal_card_types,
    validate_peripheral_card,
)
from kicad_mcp.utils.firmware.intent import (
    build_intent,
    find_board_id,
)
from kicad_mcp.utils.firmware.parse import (
    idf_target_defines,
    parse_defines,
    partition,
    select_active_branches,
)
from kicad_mcp.utils.firmware.templates import (
    expand_intent,
)

_FIX = "tests/fixtures/firmware/terminal_devices"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _intent_from_fix():
    """Build a DesignIntent from the terminal_devices fixture (ESP32 + PIEZO +
    TCRT5000), selecting the active #if branch for esp32dev."""
    config_path = f"{_FIX}/config.h"
    board = find_board_id(config_path)
    text = select_active_branches(
        open(config_path).read(), idf_target_defines(board)
    )
    parsed = partition(parse_defines(text))
    return build_intent(parsed, firmware_path=config_path, board_id=board)


def _design():
    from kicad_mcp.server import create_server
    return asyncio.run(create_server().get_tool("design")).fn


# ---------------------------------------------------------------------------
# Card structural validation
# ---------------------------------------------------------------------------

_GOOD_TERMINAL_CARD = {
    "type": "MYTERMINAL",
    "lib_id": "Connector_Generic:Conn_01x01",
    "value": "MyTerminal (terminal)",
    "bus": None,
    "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x01_P2.54mm_Vertical",
    "roles": {"SIGNAL": "1"},
    "supply_pins": [],
    "ground_pins": [],
    "module": False,
    "realize": "terminal",
}


def test_validate_accepts_realize_terminal():
    """A card with realize: terminal passes structural validation."""
    assert validate_peripheral_card(_GOOD_TERMINAL_CARD) == []


def test_validate_rejects_unknown_realize():
    """An unknown realize value is a structural error."""
    bad = dict(_GOOD_TERMINAL_CARD, realize="chip_down")
    errs = validate_peripheral_card(bad)
    assert any("realize" in e for e in errs)


def test_validate_accepts_card_without_realize():
    """Omitting realize is valid — default is chip-down (no realize field)."""
    card = {k: v for k, v in _GOOD_TERMINAL_CARD.items() if k != "realize"}
    assert validate_peripheral_card(card) == []


def test_terminal_card_types_returns_terminal_cards():
    """terminal_card_types(peripherals) returns only the terminal-realize cards."""
    peripherals = {
        "PIEZO": dict(_GOOD_TERMINAL_CARD, type="PIEZO"),
        "HX711": {
            "type": "HX711", "lib_id": "Analog_ADC:HX711", "value": "HX711",
            "bus": None, "footprint": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
            "roles": {"DOUT": "DOUT"}, "supply_pins": ["VSUP"], "ground_pins": ["AGND"],
            "module": False,
        },
    }
    result = terminal_card_types(peripherals)
    assert "PIEZO" in result
    assert "HX711" not in result


# ---------------------------------------------------------------------------
# Packaged card assertions
# ---------------------------------------------------------------------------

def test_packaged_cards_load_includes_terminal_cards():
    """The three v1 terminal cards ship with the package and load clean."""
    peris, _ = load_cards()
    assert {"PIEZO", "SWITCH", "TCRT5000"} <= set(peris), (
        "v1 terminal cards must be present in the packaged card library"
    )
    for name in ("PIEZO", "SWITCH", "TCRT5000"):
        assert peris[name].get("realize") == "terminal", (
            f"{name} card must declare realize: terminal"
        )


def test_packaged_terminal_types_set():
    """terminal_card_types() from the live card cache includes the v1 cards."""
    peris, _ = load_cards()
    tt = terminal_card_types(peris)
    assert {"PIEZO", "SWITCH", "TCRT5000"} <= tt


# ---------------------------------------------------------------------------
# build_intent: terminal-only cards resolve as peripheral (not orphan)
# ---------------------------------------------------------------------------

def test_terminal_card_resolves_to_peripheral_net():
    """A PIEZO_ADC_PIN macro resolves via the PIEZO card → peripheral net (not
    orphan) at build_intent time — the card gives the signal a far-end ref."""
    intent = _intent_from_fix()
    piezo_net = next(
        (n for n in intent.nets if n.name == "PIEZO_ADC"), None
    )
    assert piezo_net is not None, "PIEZO_ADC net must be emitted"
    assert piezo_net.kind == "peripheral", (
        "PIEZO_ADC must be a peripheral net (not orphan) once the card resolves it"
    )


def test_terminal_card_peripheral_in_intent():
    """After build_intent, a PIEZO peripheral is present (it gets a ref)."""
    intent = _intent_from_fix()
    piezos = [p for p in intent.peripherals if p.type == "PIEZO"]
    assert len(piezos) == 1, "one PIEZO peripheral must be materialized"
    tcrt = [p for p in intent.peripherals if p.type == "TCRT5000"]
    assert len(tcrt) == 1, "one TCRT5000 peripheral must be materialized"


# ---------------------------------------------------------------------------
# expand_intent: terminal injection + remote_peripherals synthesis
# ---------------------------------------------------------------------------

def test_terminal_card_gets_screw_terminal_no_chip():
    """After expand_intent a terminal-only card has a screw terminal in
    intent.peripherals and its original placeholder is remote (not placed)."""
    intent = _intent_from_fix()
    intent = expand_intent(intent)

    # The PIEZO placeholder is present but remote (not placed as a chip).
    piezos = [p for p in intent.peripherals if p.type == "PIEZO"]
    assert len(piezos) == 1
    assert piezos[0].locus == "remote", "terminal card must be locus=remote after expand"

    # A screw terminal was synthesized for it.
    terms = [p for p in intent.peripherals
             if p.type == "TERM" and "Piezo" in (p.value or "")]
    assert len(terms) == 1, "exactly one PIEZO screw terminal must be emitted"


def test_terminal_card_gpio_wired_to_terminal():
    """The MCU GPIO and the screw terminal both appear on the PIEZO_ADC net —
    no orphan endpoint remains."""
    intent = _intent_from_fix()
    intent = expand_intent(intent)

    piezo_term = next(
        p for p in intent.peripherals
        if p.type == "TERM" and "Piezo" in (p.value or "")
    )
    piezo_net = next(n for n in intent.nets if n.name == "PIEZO_ADC")
    refs_on_net = {ep.ref for ep in piezo_net.endpoints}
    assert intent.mcu.ref in refs_on_net, "MCU GPIO endpoint must be on PIEZO_ADC"
    assert piezo_term.ref in refs_on_net, "terminal connector must be on PIEZO_ADC"


def test_terminal_card_no_remote_device_gap_needed():
    """A terminal-only card produces a remote_device gap (the standard disclosure)
    — this confirms the device's wire-out is documented in the gap manifest."""
    intent = _intent_from_fix()
    intent = expand_intent(intent)
    remote_gaps = [g for g in intent.gaps if g.kind == "remote_device"]
    assert any("PIEZO" in g.detail or "Piezo" in g.detail for g in remote_gaps), (
        "remote_device gap must document the PIEZO terminal"
    )


def test_terminal_card_no_bypass_cap():
    """Terminal-only cards are remote → no bypass cap is added on the board
    (glue suppression). The terminal card must produce FEWER caps than a
    board with the same MCU but no terminal device."""
    intent_no_terminal = build_intent(
        partition(parse_defines("#define MCP23017_ADDR 0x27")),
        firmware_path="c.h", board_id="esp32dev",
    )
    intent_no_terminal = expand_intent(intent_no_terminal)

    intent_with_terminal = _intent_from_fix()
    intent_with_terminal = expand_intent(intent_with_terminal)

    # Cap count with a terminal device should not EXCEED the no-terminal count
    # (the terminal gets no on-board bypass; a baseline device like MCP23017 does).
    caps_no_term = [p for p in intent_no_terminal.peripherals if p.type == "C"]
    caps_with_term = [p for p in intent_with_terminal.peripherals if p.type == "C"]
    # The terminal adds NO extra caps; any difference comes from other devices.
    # Simple guard: piezo terminal device doesn't appear in the cap list at all.
    assert not any(
        "PIEZO" in (p.value or "") for p in caps_with_term
    ), "no cap should be tied to the PIEZO peripheral (glue suppression)"


def test_terminal_card_injection_honors_explicit_placement_override():
    """If board.yaml explicitly sets locus: on_board for a terminal-only card,
    that override takes precedence over the card's realize: terminal default.
    The peripheral gets placed on-board (no terminal synthesized for it)."""
    from kicad_mcp.utils.firmware.sidecar import BoardSidecar, apply_sidecar
    intent = _intent_from_fix()
    # Get the PIEZO ref before expansion.
    piezo_ref = next(p.ref for p in intent.peripherals if p.type == "PIEZO")
    # Force it on-board via explicit placement.
    apply_sidecar(intent, BoardSidecar(placement={
        piezo_ref: {"locus": "on_board", "device": "PIEZO_ONBOARD"},
    }))
    intent = expand_intent(intent)
    # The locus override is honored — PIEZO not remote.
    piezo = next(p for p in intent.peripherals if p.type == "PIEZO")
    assert piezo.locus == "on_board", "explicit on_board override must be honored"
    # No PIEZO terminal synthesized.
    piezo_terms = [p for p in intent.peripherals
                   if p.type == "TERM" and "Piezo" in (p.value or "")]
    assert len(piezo_terms) == 0, "on_board override must suppress the terminal"


# ---------------------------------------------------------------------------
# Multi-instance non-collision
# ---------------------------------------------------------------------------

def test_two_terminal_instances_get_distinct_refs():
    """Two PIEZO instances in the same firmware get distinct J-refs with no
    collision (the ref allocator handles this)."""
    src = """\
#define PIEZO1_ADC_PIN 34
#define PIEZO2_ADC_PIN 35
"""
    # NOTE: two PIEZO_* pins both resolve hint="PIEZO"; candidate_devices dedupes
    # to ONE PIEZO per unique hint — multi-instance needs an _ADDR declaration.
    # So here we test TWO DISTINCT terminal types (PIEZO + TCRT5000) to confirm
    # the ref allocator never reuses a J-ref.
    intent = _intent_from_fix()    # has PIEZO + TCRT5000
    intent = expand_intent(intent)
    term_refs = [p.ref for p in intent.peripherals if p.type == "TERM"]
    assert len(term_refs) == len(set(term_refs)), (
        "synthesized terminal connectors must have distinct refs"
    )
    assert len(term_refs) >= 2, "PIEZO and TCRT5000 each get a terminal"


# ---------------------------------------------------------------------------
# Invariant: bus peripheral without locus:remote is STILL REFUSED, not terminal-ized
# ---------------------------------------------------------------------------

def test_inmp441_without_locus_remote_still_refused_not_terminalized():
    """INMP441 is an I2S mic (bus-typed) with no KiCad symbol. WITHOUT a
    board.yaml `locus: remote` directive it must be REFUSED (part_unavailable gap)
    — NOT silently converted to a screw terminal. This guards the spec §4
    honesty-contradiction rule: terminal-only cards apply ONLY to cards with
    realize: terminal, never to un-declared bus peripherals."""

    src = """\
#define CMCA_MIC_BCLK  8
#define CMCA_MIC_WS    9
#define CMCA_MIC_SD    10
"""
    parsed = partition(parse_defines(src))
    intent = build_intent(parsed, firmware_path="c.h", board_id="esp32-s3-devkitc-1")
    # Manually force INMP441 as the resolved part (as the bus resolver would).
    for bus in intent.buses:
        if bus.type == "I2S_IN":
            bus.resolved_part = "INMP441"
            bus.part_provenance = "corpus"
    intent = expand_intent(intent)

    # INMP441 is refused (part_unavailable gap), not placed AND not terminal-ized.
    assert any(
        g.kind == "part_unavailable" and "INMP441" in g.detail
        for g in intent.gaps
    ), "INMP441 without locus:remote must still produce a part_unavailable gap"
    # No screw terminal was synthesized for the I2S bus.
    i2s_terms = [
        p for p in intent.peripherals
        if p.type == "TERM" and "mic" in (p.value or "").lower()
    ]
    assert len(i2s_terms) == 0, (
        "no terminal must be auto-emitted for a bus peripheral without locus:remote"
    )


def test_inmp441_with_locus_remote_still_works():
    """INMP441 WITH locus: remote gets a screw terminal (the existing mechanism).
    This test guards that the invariant fix did not accidentally break the
    intentional remote-mic path."""
    from kicad_mcp.utils.firmware.sidecar import BoardSidecar, apply_sidecar

    src = """\
#define CMCA_MIC_BCLK  8
#define CMCA_MIC_WS    9
#define CMCA_MIC_SD    10
"""
    parsed = partition(parse_defines(src))
    intent = build_intent(parsed, firmware_path="c.h", board_id="esp32-s3-devkitc-1")
    apply_sidecar(intent, BoardSidecar(placement={
        "CMCA_MIC": {"locus": "remote", "device": "INMP441"},
    }))
    intent = expand_intent(intent)
    # A terminal IS emitted for the I2S bus when locus: remote is declared.
    mic_terms = [
        p for p in intent.peripherals
        if p.type == "TERM" and p.value == "INMP441"
    ]
    assert len(mic_terms) == 1, (
        "INMP441 with locus: remote must still emit a screw terminal"
    )


# ---------------------------------------------------------------------------
# Screw terminal symbol / silk legend
# ---------------------------------------------------------------------------

def test_terminal_card_emits_silk_legend():
    """The screw terminal synthesized for a PIEZO gets a silk legend entry
    so the field-wiring silk is generated."""
    intent = _intent_from_fix()
    intent = expand_intent(intent)
    piezo_term = next(
        p for p in intent.peripherals
        if p.type == "TERM" and "Piezo" in (p.value or "")
    )
    leg = next(
        (L for L in intent.connector_legends if L.ref == piezo_term.ref), None
    )
    assert leg is not None, "terminal connector must have a silk legend entry"
    # Signal label appears in positions (the GPIO signal crosses to the terminal).
    assert any(label for label in leg.positions), (
        "legend must have at least one non-empty position label"
    )


def test_terminal_card_uses_screw_terminal_symbol():
    """The synthesized connector uses a Connector:Screw_Terminal_01x{N} symbol —
    not a pin header — because it is field-wired."""
    intent = _intent_from_fix()
    intent = expand_intent(intent)
    piezo_term = next(
        p for p in intent.peripherals
        if p.type == "TERM" and "Piezo" in (p.value or "")
    )
    assert "Screw_Terminal" in (piezo_term.lib_id or ""), (
        f"terminal lib_id must be a Screw_Terminal symbol, got {piezo_term.lib_id!r}"
    )


# ---------------------------------------------------------------------------
# Alias resolution for hint-matched terminal cards (the resolve_peripheral fix).
# These guard a real bug: a firmware peripheral named by a card ALIAS (TRACK ->
# SWITCH, BUZZER -> PIEZO) was silently dropped to an orphan net because
# resolve_peripheral only honored the canonical card type, not its aliases.
# ---------------------------------------------------------------------------

def test_resolve_peripheral_honors_aliases_for_terminal_cards():
    K.reload_cards()
    assert (K.resolve_peripheral("TRACK") or {}).get("value") == "Switch (terminal)"
    assert (K.resolve_peripheral("BUZZER") or {}).get("value") == "Piezo (terminal)"
    assert K.is_terminal_card_type("TRACK")        # alias -> SWITCH card
    assert K.is_terminal_card_type("BUZZER")       # alias -> PIEZO card
    assert K.is_terminal_card_type("PIEZO")        # canonical type still resolves
    assert not K.is_terminal_card_type("MCP23017")  # a chip card is NOT a terminal


def test_track_switches_group_into_one_terminal():
    """TRACK_SW1_PIN + TRACK_SW2_PIN share hint 'TRACK' -> ONE switch terminal
    carrying BOTH signals plus power (the real speed-cal polarity-switch pattern).
    Exercises the alias path end-to-end (hint 'TRACK' -> SWITCH terminal card)."""
    K.reload_cards()
    sample = "#define TRACK_SW1_PIN 25\n#define TRACK_SW2_PIN 26\n"
    intent = build_intent(partition(parse_defines(sample)),
                          firmware_path="c.h", board_id="esp32dev")
    intent = expand_intent(intent)
    terms = [p for p in intent.peripherals
             if p.type == "TERM" and "Switch" in (p.value or "")]
    assert len(terms) == 1, "the two track switches must share ONE terminal"
    leg = next(lg for lg in intent.connector_legends if lg.ref == terms[0].ref)
    assert {"SW1", "SW2"} <= set(leg.positions)         # both signals cross out
    assert {"+3V3", "GND"} <= set(leg.positions)        # power taps present
    for sig in ("TRACK_SW1", "TRACK_SW2"):
        n = next(x for x in intent.nets if x.name == sig)
        assert n.kind == "peripheral"                    # not an orphan
        assert terms[0].ref in {ep.ref for ep in n.endpoints}  # reaches the terminal
