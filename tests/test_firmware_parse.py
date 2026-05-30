"""Boundary tests for the firmware #define classifier.

The dominant bug family here is the syntactic-semantic seam (CLAUDE.md Rule 3):
a value in GPIO range that ISN'T a pin, a hex value that ISN'T an address. These
tests pin the false-positive guards as hard as the happy path, per the
threshold-boundary rule (at/below/above, ambiguous inputs).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.firmware.parse import (
    GPIO_MAX,
    GPIO_MIN,
    MacroKind,
    _as_int,
    classify,
    parse_defines,
    parse_pin_name,
    partition,
)

SPEEDCAL_CONFIG = Path(
    "/Volumes/Files/claude/mr-esp32/speed-cal/firmware/include/config.h"
)


# --- _as_int: the value validator --------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("21", 21),
    ("0x27", 0x27),       # 39
    ("400000", 400000),
    ("-1", -1),
    ("16U", 16),
    ("0x13", 0x13),
    ("87.1f", None),      # float -> not an int
    ('"SpeedCal"', None), # string
    ("'a'", None),        # char
    ("A | B", None),      # expression (has space)
    ("", None),
])
def test_as_int(raw, expected):
    assert _as_int(raw) == expected


# --- the seam: GPIO-range integers that are NOT pins -------------------------

@pytest.mark.parametrize("name,value", [
    ("NUM_SENSORS", "6"),       # a count, in GPIO range
    ("MAX_SENSORS", "16"),      # a count, in GPIO range
    ("AUDIO_DMA_BUF_COUNT", "4"),
    ("AUDIO_DMA_BUF_LEN", "1024"),
])
def test_count_in_gpio_range_is_not_a_pin(name, value):
    m = classify(name, value, None)
    assert m.kind is MacroKind.OTHER, f"{name} must not classify as PIN"


# --- the seam: hex values that are NOT addresses (the register table) --------

@pytest.mark.parametrize("name,value", [
    ("MCP_IODIRA", "0x00"),
    ("MCP_GPIOB", "0x13"),
    ("MCP_IOCON", "0x0A"),
])
def test_register_hex_is_not_an_address(name, value):
    m = classify(name, value, None)
    assert m.kind is MacroKind.OTHER, f"{name} (a register) must not be ADDRESS"


# --- real pins classify correctly --------------------------------------------

@pytest.mark.parametrize("name,value,gpio,bus,role,peri", [
    ("I2C_SDA", "21", 21, "I2C", "SDA", None),
    ("I2C_SCL", "22", 22, "I2C", "SCL", None),
    ("HX711_DOUT_PIN", "16", 16, None, "DOUT", "HX711"),
    ("HX711_SCK_PIN", "17", 17, None, "SCK", "HX711"),
    ("MCP23017_INT_PIN", "13", 13, None, "INT", "MCP23017"),
    ("PIEZO_ADC_PIN", "35", 35, None, "ADC", "PIEZO"),
    ("I2S_SCK_PIN", "18", 18, "I2S", "SCK", None),
    ("I2S_WS_PIN", "19", 19, "I2S", "WS", None),
])
def test_real_pins(name, value, gpio, bus, role, peri):
    m = classify(name, value, None)
    assert m.kind is MacroKind.PIN
    assert m.gpio == gpio and m.pin_value_valid
    assert m.bus == bus and m.signal_role == role and m.peripheral_hint == peri


# --- addresses ----------------------------------------------------------------

def test_address_hex():
    m = classify("MCP23017_ADDR", "0x27", None)
    assert m.kind is MacroKind.ADDRESS and m.address == 0x27

def test_address_with_non_int_value_is_other():
    m = classify("SOME_ADDR", '"0x27"', None)
    assert m.kind is MacroKind.OTHER and "address" in m.note


# --- GPIO range boundaries (at / below / above) ------------------------------

@pytest.mark.parametrize("value,valid", [
    (str(GPIO_MIN), True),       # 0 — valid
    (str(GPIO_MAX), True),       # 48 — valid
    (str(GPIO_MAX + 1), False),  # 49 — above range
    ("-1", False),               # common "unused" sentinel
])
def test_gpio_boundaries(value, valid):
    m = classify("FOO_PIN", value, None)
    assert m.kind is MacroKind.PIN          # name says pin -> still PIN kind
    assert m.pin_value_valid is valid
    if not valid:
        assert m.note  # flagged, not silently accepted


# --- non-pin / non-int names --------------------------------------------------

def test_i2c_freq_is_not_a_pin():
    # I2C prefix but not a pin name (no _PIN, not a bare alias).
    m = classify("I2C_FREQ", "400000", None)
    assert m.kind is MacroKind.OTHER

def test_string_define_is_other():
    m = classify("WIFI_AP_SSID", '"SpeedCal"', None)
    assert m.kind is MacroKind.OTHER

def test_pin_named_but_float_value_is_other_with_note():
    m = classify("WEIRD_PIN", "1.5f", None)
    assert m.kind is MacroKind.OTHER and "pin" in m.note

def test_function_like_macro():
    m = classify("MIN", "((a) < (b) ? (a) : (b))", "(a,b)")
    assert m.kind is MacroKind.FUNCTION

def test_value_less_define_is_empty():
    m = classify("CONFIG_H", "", None)
    assert m.kind is MacroKind.EMPTY


# --- parse_pin_name -----------------------------------------------------------

@pytest.mark.parametrize("name,expected", [
    ("I2C_SDA", (None, "SDA", "I2C")),
    ("I2S_SCK_PIN", (None, "SCK", "I2S")),
    ("HX711_DOUT_PIN", ("HX711", "DOUT", None)),
    ("MCP23017_INT_PIN", ("MCP23017", "INT", None)),
    ("BUZZER_PIN", ("BUZZER", None, None)),
])
def test_parse_pin_name(name, expected):
    assert parse_pin_name(name) == expected


# --- full-text parse + duplicate handling ------------------------------------

def test_parse_preserves_duplicates_in_order():
    text = "#define X_PIN 5\n#define X_PIN 6\n"
    pins = [m for m in parse_defines(text) if m.kind is MacroKind.PIN]
    assert [m.gpio for m in pins] == [5, 6]   # both retained; first-wins is downstream

def test_line_numbers_and_comments():
    text = "// header\n#define HX711_DOUT_PIN 16  // Data out from HX711\n"
    macros = parse_defines(text)
    assert len(macros) == 1
    assert macros[0].line_no == 2
    assert macros[0].comment == "Data out from HX711"


# --- integration: the REAL speed-cal config.h --------------------------------

@pytest.mark.skipif(not SPEEDCAL_CONFIG.exists(), reason="speed-cal config.h not present")
def test_real_speedcal_config():
    pf = partition(parse_defines(SPEEDCAL_CONFIG.read_text()))
    pin_names = {m.name for m in pf.pins}
    # The actual hardware pins must all be recovered.
    assert {
        "I2C_SDA", "I2C_SCL", "MCP23017_INT_PIN", "HX711_DOUT_PIN",
        "HX711_SCK_PIN", "PIEZO_ADC_PIN", "I2S_SCK_PIN", "I2S_WS_PIN",
        "I2S_SD_PIN",
    } <= pin_names
    # The register table and counts must NOT have leaked into pins.
    assert not any(m.name.startswith("MCP_") for m in pf.pins)
    assert "NUM_SENSORS" not in pin_names and "MAX_SENSORS" not in pin_names
    # The one declared I2C address is recovered.
    assert any(m.name == "MCP23017_ADDR" and m.address == 0x27 for m in pf.addresses)
    # I2C_FREQ is config, not a pin.
    assert "I2C_FREQ" not in pin_names
