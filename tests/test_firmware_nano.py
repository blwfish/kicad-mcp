"""Phase 2b — the first 5V MCU (Arduino Nano v3, ATmega328P). Pins the new seam:
a per-MCU supply_rail (+5V for a 5V Arduino vs the ESP32/Pico +3V3) threaded through
the power glue, plus the D{n} digital pin-name prefix and the board_match that stays
clear of the other Nano variants. End-to-end symbol resolution + routing is proven in
tests/integration/test_firmware_invariants.py over the nano_basic fixture.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_mcp.utils.firmware.cards import validate_mcu_card
from kicad_mcp.utils.firmware.intent import build_intent, resolve_mcu
from kicad_mcp.utils.firmware.mcu_pinmap import gpio_to_pin_number
from kicad_mcp.utils.firmware.parse import parse_macros, partition
from kicad_mcp.utils.firmware.templates import _supply_rail, expand_intent


def _sym(*names_numbers):
    return SimpleNamespace(pins=[SimpleNamespace(name=n, number=num)
                                 for n, num in names_numbers])


def _expand(board_id, fw="#define HX711_DOUT_PIN 2\n#define HX711_SCK_PIN 3\n"):
    return expand_intent(build_intent(partition(parse_macros(fw)),
                                      firmware_path="c.h", board_id=board_id))


# --- resolution + the board_match greediness boundary --------------------------

@pytest.mark.parametrize("board_id", ["nanoatmega328", "nanoatmega328new"])
def test_nano_board_ids_resolve(board_id):
    mi = resolve_mcu(board_id)
    assert mi is not None and mi["part"] == "Arduino-Nano-v3"
    assert mi.get("gpio_pin_prefix") == "D" and mi.get("supply_rail") == "+5V"
    assert mi["needs_3v3"] is False and mi["native_usb"] is True


@pytest.mark.parametrize("board_id", ["nano", "nanorp2040connect", "nano_every"])
def test_other_nano_variants_do_not_hijack(board_id):
    # the Nano card must NOT claim the RP2040 / Every / bare-"nano" boards (different
    # silicon + pinout). They resolve to None (honest mcu_unknown), not the AVR Nano.
    assert resolve_mcu(board_id) != {"part": "Arduino-Nano-v3"}
    r = resolve_mcu(board_id)
    assert r is None or r["part"] != "Arduino-Nano-v3"


def test_nano_card_valid_without_en_boot():
    mi = resolve_mcu("nanoatmega328")
    assert "en_pin" not in mi and "boot_pin" not in mi
    assert validate_mcu_card(dict(mi)) == []


# --- the D{n} digital pin prefix (incl. the D0/RX compound) ---------------------

def test_d_prefix_resolves_digital_pins():
    # mirror the REAL Nano symbol: D0/RX and D1/TX are compound names (tokenizer
    # splits on "/"), D2..D13 are plain. D14 is genuinely absent -> honest None.
    sym = _sym(("D0/RX", "2"), ("D1/TX", "1"), ("D2", "5"), ("D13", "16"))
    assert gpio_to_pin_number(sym, 0, "D") == "2"     # D0 inside the D0/RX compound
    assert gpio_to_pin_number(sym, 1, "D") == "1"     # D1 inside the D1/TX compound
    assert gpio_to_pin_number(sym, 2, "D") == "5"
    assert gpio_to_pin_number(sym, 13, "D") == "16"
    assert gpio_to_pin_number(sym, 14, "D") is None   # no D14 pin -> honest None
    assert gpio_to_pin_number(sym, 1, "D") != "16"    # D1 must not collide with D13


# --- supply_rail: 5V board ties to +5V; ESP32 stays +3V3 (regression) -----------

def test_supply_rail_is_per_mcu():
    nano = build_intent(partition(parse_macros("#define X 2\n")),
                        firmware_path="c.h", board_id="nanoatmega328")
    esp = build_intent(partition(parse_macros("#define X 2\n")),
                       firmware_path="c.h", board_id="esp32dev")
    assert _supply_rail(nano) == "+5V"
    assert _supply_rail(esp) == "+3V3"        # default carries the ESP32 (card omits it)


def test_nano_board_ties_everything_to_5v_no_ldo():
    ex = _expand("nanoatmega328")
    assert not any(p.type == "AMS1117" for p in ex.peripherals)      # on-board reg, no LDO
    rails = {n.name for n in ex.nets if n.kind == "power"}
    assert "+5V" in rails and "+3V3" not in rails                    # 5V logic, no +3V3 rail
    p5 = next(n for n in ex.nets if n.name == "+5V")
    assert any(e.ref == "U1" for e in p5.endpoints)                  # MCU +5V pin sources it
    assert any(e.ref != "U1" for e in p5.endpoints)                  # a peripheral consumes it


def test_esp32_still_ties_to_3v3():
    # regression: the generalization must not move the ESP32 off +3V3
    ex = _expand("esp32dev")
    rails = {n.name for n in ex.nets if n.kind == "power"}
    assert "+3V3" in rails
    assert any(p.type == "AMS1117" for p in ex.peripherals)          # ESP32 keeps its LDO


# --- card validation hardening --------------------------------------------------

def test_validate_mcu_card_rejects_non_string_supply_rail():
    mi = dict(resolve_mcu("nanoatmega328"))
    mi["supply_rail"] = 5
    assert any("supply_rail" in e for e in validate_mcu_card(mi))


@pytest.mark.parametrize("bad", [5, ""])   # non-string AND empty-string both rejected
def test_validate_mcu_card_rejects_bad_supply_rail(bad):
    mi = dict(resolve_mcu("nanoatmega328"))
    mi["supply_rail"] = bad
    assert any("supply_rail" in e for e in validate_mcu_card(mi))


def test_arduino_ide_fqbn_nano_is_mcu_unknown():
    # the Arduino-IDE FQBN form is NOT resolvable (avoids hijacking nano_every etc.);
    # the user supplies board.yaml board_id: nanoatmega328 instead. Pin the honest None.
    assert resolve_mcu("arduino:avr:nano") is None


# --- the rail generalization reaches EVERY power-emitting template (cold review) --

def test_nano_mcp23017_straps_to_5v_not_sourceless_3v3():
    # MCP23017 is detected by its I2C ADDRESS, so it fires even though the Nano's I2C
    # is on analog pins we can't yet parse. On a Nano its RESET static-tie + address
    # straps (device_config) must go to +5V — NOT a sourceless +3V3, which would hold
    # RESET low (permanent reset). The sharpest cold-review finding.
    ex = _expand("nanoatmega328", "#define MCP23017_ADDR 0x21\n")
    rails = {n.name for n in ex.nets if n.kind == "power"}
    assert "+3V3" not in rails and "+5V" in rails
    mcp = next(p.ref for p in ex.peripherals if p.type == "MCP23017")
    mcp_rails = {n.name for n in ex.nets if n.kind == "power"
                 for e in n.endpoints if e.ref == mcp}
    assert mcp_rails == {"+5V", "GND"}   # RESET + address pins on +5V, none sourceless


def test_nano_i2s_mic_ties_to_rail_with_loud_voltage_gap():
    # a 3.3V-only MEMS mic on a 5V board: tied to the board rail (so no sourceless
    # +3V3 net), but with a LOUD mic_supply_voltage gap rather than a silent over-volt.
    from kicad_mcp.utils.firmware.intent import Bus, DesignIntent, Mcu
    mi = resolve_mcu("nanoatmega328")
    it = DesignIntent(mcu=Mcu("U1", mi["part"], mi["lib_id"]),
                      buses=[Bus("MIC", "I2S_IN", {"BCLK": 2, "WS": 3, "SD": 4})])
    ex = expand_intent(it)
    rails = {n.name for n in ex.nets if n.kind == "power"}
    assert "+3V3" not in rails and "+5V" in rails
    assert any(g.kind == "mic_supply_voltage" for g in ex.gaps)


def test_esp32_i2s_mic_has_no_voltage_gap():
    # regression: a 3.3V mic on a 3V3 board is fine — no voltage gap, ties to +3V3
    from kicad_mcp.utils.firmware.intent import Bus, DesignIntent, Mcu
    mi = resolve_mcu("esp32dev")
    it = DesignIntent(mcu=Mcu("U1", mi["part"], mi["lib_id"]),
                      buses=[Bus("MIC", "I2S_IN", {"BCLK": 2, "WS": 3, "SD": 4})])
    ex = expand_intent(it)
    assert not any(g.kind == "mic_supply_voltage" for g in ex.gaps)
    assert "+3V3" in {n.name for n in ex.nets if n.kind == "power"}


# --- Arduino analog pins (A0..A7): resolved by symbol pin NAME, not a GPIO number --

def _intent(fw):
    return build_intent(partition(parse_macros(fw)),
                        firmware_path="c.h", board_id="nanoatmega328")


def test_analog_pin_resolves_to_symbol_a_pin_not_dropped():
    # #define LDR_PIN A0 -> an orphan net whose MCU endpoint is the symbol "A0" pin
    # (by name). Before analog support it was silently demoted to provenance.unparsed.
    it = _intent("#define LDR_PIN A0\n#define POT_PIN A3\n")
    eps = {n.name: n.endpoints[0] for n in it.nets}
    assert eps["LDR"].pin == "A0" and eps["LDR"].gpio is None
    assert eps["POT"].pin == "A3"
    unparsed = {u["name"] for u in it.provenance.get("unparsed", [])}
    assert "LDR_PIN" not in unparsed and "POT_PIN" not in unparsed   # no silent loss


def test_analog_and_digital_pins_coexist():
    it = _intent("#define LED_PIN 13\n#define LDR_PIN A0\n")
    by_name = {n.name: n.endpoints[0] for n in it.nets}
    assert by_name["LED"].gpio == 13 and by_name["LED"].pin is None    # digital -> gpio
    assert by_name["LDR"].pin == "A0" and by_name["LDR"].gpio is None  # analog -> pin name


def test_two_macros_on_same_analog_pin_flagged_as_conflict():
    # two firmware names on the same A-pin would short on one pad — must be flagged.
    # The GPIO conflict check skips analog (gpio None), so analog needs its own check.
    it = _intent("#define LDR_PIN A0\n#define POT_PIN A0\n")
    assert any(g.kind == "pin_conflict" and "A0" in g.detail for g in it.gaps)
