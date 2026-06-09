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
    sym = _sym(("D0/RX", "2"), ("D2", "5"), ("D13", "16"))
    assert gpio_to_pin_number(sym, 0, "D") == "2"     # D0 inside the D0/RX compound
    assert gpio_to_pin_number(sym, 2, "D") == "5"
    assert gpio_to_pin_number(sym, 13, "D") == "16"
    assert gpio_to_pin_number(sym, 1, "D") is None    # no D1 pin -> honest None


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
