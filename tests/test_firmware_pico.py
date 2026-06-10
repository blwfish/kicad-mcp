"""Phase 2 — MCU generalization, proven on the RaspberryPi-Pico (the first
non-ESP32 MCU). Pins the four generalization seams at the unit level (no KiCad):
GPIO pin-name prefix, optional en/boot straps, on-board-regulated power sourcing,
and board->card resolution. The end-to-end symbol resolution + routing is proven
in tests/integration/test_firmware_invariants.py over the pico_basic fixture.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_mcp.utils.firmware.cards import validate_mcu_card
from kicad_mcp.utils.firmware.intent import build_intent, resolve_mcu
from kicad_mcp.utils.firmware.mcu_pinmap import gpio_to_pin_number
from kicad_mcp.utils.firmware.parse import parse_macros, partition
from kicad_mcp.utils.firmware.templates import expand_intent


def _sym(*names_numbers):
    """A fake symbol with the given (pin_name, pin_number) pairs."""
    pins = [SimpleNamespace(name=n, number=num) for n, num in names_numbers]
    return SimpleNamespace(pins=pins)


# --- board -> Pico card resolution (and the rp2040 greediness boundary) --------

@pytest.mark.parametrize("board_id", ["pico", "rpipico", "raspberry-pi-pico",
                                      "rp2040:rp2040:rpipico"])  # arduino-pico FQBN
def test_pico_board_ids_resolve(board_id):
    mi = resolve_mcu(board_id)
    assert mi is not None and mi["part"] == "RaspberryPi-Pico"
    assert mi.get("gpio_pin_prefix") == "GPIO"
    assert mi["needs_3v3"] is False and mi["native_usb"] is True


@pytest.mark.parametrize("board_id", ["rp2040", "stm32f407", "nrf52840"])
def test_unsupported_boards_stay_unknown(board_id):
    # modules-only: bare "rp2040" is NOT the Pico module -> honest mcu_unknown,
    # NOT a silent mis-resolve to the Pico card.
    assert resolve_mcu(board_id) is None


def test_pico_card_is_structurally_valid_without_en_boot():
    mi = resolve_mcu("pico")
    assert "en_pin" not in mi and "boot_pin" not in mi   # the Pico has neither
    assert validate_mcu_card(dict(mi)) == []


# --- gpio_to_pin_number: the prefix + the Pico compound/ tokenization boundary --

def test_gpio_prefix_esp32_default():
    sym = _sym(("IO21", "10"), ("RXD0/IO3", "5"))
    assert gpio_to_pin_number(sym, 21) == "10"            # default prefix "IO"
    assert gpio_to_pin_number(sym, 3) == "5"              # token inside RXD0/IO3


def test_gpio_prefix_pico_gpio():
    sym = _sym(("GPIO0", "1"), ("GPIO21", "27"), ("GPIO26_ADC0", "31"))
    assert gpio_to_pin_number(sym, 0, "GPIO") == "1"
    assert gpio_to_pin_number(sym, 21, "GPIO") == "27"
    # compound ADC pin: GPIO26 must match inside GPIO26_ADC0 (underscore is a split)
    assert gpio_to_pin_number(sym, 26, "GPIO") == "31"


def test_gpio_prefix_tokenization_no_false_substring():
    # GPIO2 must NOT match GPIO20 (tokenized equality, not substring)
    sym = _sym(("GPIO20", "26"))
    assert gpio_to_pin_number(sym, 2, "GPIO") is None
    assert gpio_to_pin_number(sym, 20, "GPIO") == "26"


def test_gpio_prefix_mismatch_returns_none():
    # an ESP32-prefix lookup against Pico-named pins resolves nothing (and v.v.)
    pico = _sym(("GPIO4", "6"))
    assert gpio_to_pin_number(pico, 4) is None            # default "IO" vs GPIO4
    assert gpio_to_pin_number(pico, 4, "GPIO") == "6"


def test_pico_non_broken_out_gpio_is_unresolved():
    # GP25 (on-board LED) is NOT a pin on the module symbol -> honest None, the
    # caller turns it into an unresolved-endpoint gap rather than mis-wiring it.
    sym = _sym(("GPIO22", "29"), ("GPIO26_ADC0", "31"))   # no GPIO25
    assert gpio_to_pin_number(sym, 25, "GPIO") is None


# --- power: needs_3v3=false sources +3V3 from the module, places NO LDO ---------

def _expand(board_id):
    fw = ("#define I2C_SDA 4\n#define I2C_SCL 5\n"
          "#define HX711_DOUT_PIN 2\n#define HX711_SCK_PIN 3\n")
    return expand_intent(build_intent(partition(parse_macros(fw)),
                                      firmware_path="c.h", board_id=board_id))


def test_pico_power_tree_sources_from_module_no_ldo():
    ex = _expand("pico")
    assert not any(p.type == "AMS1117" for p in ex.peripherals)   # no external LDO
    p3 = next(n for n in ex.nets if n.name == "+3V3")
    # the MCU (U1) is ON the +3V3 net — its 3V3 output pin sources the rail
    assert any(e.ref == "U1" for e in p3.endpoints)
    # and a peripheral consumes it (not a sourceless, MCU-only rail)
    assert any(e.ref != "U1" for e in p3.endpoints)


def test_esp32_power_tree_still_places_ldo():
    # regression guard: the ESP32 (needs_3v3=true) path is unchanged
    ex = _expand("esp32dev")
    assert any(p.type == "AMS1117" for p in ex.peripherals)


def test_pico_has_no_mcu_straps_esp32_does():
    pico_refs = {p.type for p in _expand("pico").peripherals}
    esp_nets = {n.name for n in _expand("esp32dev").nets}
    # Pico declares no en/boot -> no MCU_EN/MCU_BOOT strap nets
    assert "MCU_EN" not in {n.name for n in _expand("pico").nets}
    assert "MCU_BOOT" not in {n.name for n in _expand("pico").nets}
    # ESP32 still straps EN + boot
    assert "MCU_EN" in esp_nets and "MCU_BOOT" in esp_nets
    assert pico_refs  # sanity: the Pico board still expanded peripherals


# --- #ifdef-guarded pins: a Pico firmware must not be silently emptied -----------

def test_pico_ifdef_arch_branch_is_kept():
    # A Pico firmware that guards its pins under #ifdef ARDUINO_ARCH_RP2040 must keep
    # the block — idf_target_defines("pico") now emits the arch macro. Before the fix
    # it returned set() and select_active_branches dropped the block -> 0 pins.
    from kicad_mcp.utils.firmware.parse import (
        idf_target_defines,
        select_active_branches,
    )
    text = ("#ifdef ARDUINO_ARCH_RP2040\n#define I2C_SDA 4\n#endif\n"
            "#ifdef ESP32\n#define I2C_SDA 21\n#endif\n")
    out = select_active_branches(text, idf_target_defines("pico"))
    assert "#define I2C_SDA 4" in out          # the RP2040 block is kept
    assert "21" not in out                      # the ESP32 block is dropped


def test_idf_target_defines_pico_vs_esp32_pico():
    # boundary: a real ESP32 board whose id contains "pico" (esp32-pico-d4) must NOT
    # be classified as RP2040 — the esp32 check runs first.
    from kicad_mcp.utils.firmware.parse import idf_target_defines
    assert idf_target_defines("pico") == {"ARDUINO_ARCH_RP2040", "PICO_RP2040"}
    assert idf_target_defines("rpipico") == {"ARDUINO_ARCH_RP2040", "PICO_RP2040"}
    assert idf_target_defines("esp32-pico-d4") == {"CONFIG_IDF_TARGET_ESP32"}


# --- MCU card validation (the cold-review hardening) ----------------------------

def test_validate_mcu_card_rejects_non_string_gpio_prefix():
    mi = dict(resolve_mcu("pico"))
    mi["gpio_pin_prefix"] = 123                          # an int -> would f-string to "1234"
    assert any("gpio_pin_prefix" in e for e in validate_mcu_card(mi))


def test_validate_mcu_card_rejects_missing_required_field():
    mi = dict(resolve_mcu("pico"))
    del mi["supply_pin"]                                 # still required
    assert any("supply_pin" in e for e in validate_mcu_card(mi))


@pytest.mark.parametrize("board_id", ["pico2", "pico2_w", "rpipico2", "rp2350-custom"])
def test_pico2_rp2350_fails_closed(board_id):
    # REVERSED documented boundary: "pico2" (RP2350 / Pico 2) used to resolve to the
    # RP2040 Pico card as a pin-compatible approximation. With the chip guard the
    # chip axis is load-bearing (RP2350-guarded #if pin blocks would select wrongly),
    # so RP2350 ids now fail closed to an honest mcu_unknown. A user who WANTS the
    # pin-compatible approximation opts in explicitly via board.yaml board_id: pico
    # (an exact board_match, trusted past the guard).
    assert resolve_mcu(board_id) is None


@pytest.mark.parametrize("board_id,part", [
    ("tinypico", None),            # an ESP32 board with "pico" glued to "tiny" — the
                                   # verified silently-wrong-chip over-match; no esp32
                                   # text in the id, so honest mcu_unknown.
    ("pico_w", "RaspberryPi-Pico"),            # "pico" as a word still matches
    ("rpipicow", "RaspberryPi-Pico"),          # arduino-pico Pico W id
    ("esp32-pico-d4", "ESP32-WROOM-32E"),      # classic-ESP32 die: realized as the
                                               # WROOM module (same chip, our canonical
                                               # classic-esp32 realization)
])
def test_pico_word_boundary_resolution(board_id, part):
    mi = resolve_mcu(board_id)
    assert (mi["part"] if mi else None) == part


def test_m5stamp_pico_is_not_a_raspberry_pico():
    # cold-review catch: a real ESP32 product (M5Stamp Pico) with word-bounded
    # "pico" in its id must stay an honest mcu_unknown, not the RP2040 module.
    assert resolve_mcu("m5stamp-pico") is None
