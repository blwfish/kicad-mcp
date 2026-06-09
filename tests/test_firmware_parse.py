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
    idf_target_defines,
    parse_const_decls,
    parse_defines,
    parse_macros,
    parse_pin_name,
    partition,
    select_active_branches,
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
    ("GPIO_NUM_5", 5),    # ESP-IDF idiom -> 5  (was silently dropped before)
    ("GPIO_NUM_18", 18),
    ("(4)", 4),           # parenthesized -> 4
    ("5 /* mic */", 5),   # trailing block comment stripped
    ("/* lead */ 7", 7),  # leading block comment stripped
    ("(GPIO_NUM_0)", 0),  # both: unwrap then resolve
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


# --- address_base: the single source of truth for addr-macro names -----------
# Including the multi-instance qualifier (two chips of the same type on a bus).

@pytest.mark.parametrize("name,base", [
    ("MPU6050_ADDR", "MPU6050"),
    ("MPU6050_ADDR_2", "MPU6050"),       # instance qualifier: _<n>
    ("MPU6050_ADDRESS_2", "MPU6050"),    # long form + instance
    ("BME280_ADDR0", "BME280"),          # qualifier with no underscore
    ("OLED_ADDR", "OLED"),
    ("INA219_ADDRESS", "INA219"),
    # NOT address macros:
    ("MPU6050_REG_CONFIG", None),        # the register-table seam trap
    ("BASE_ADDRESS_OFFSET", None),       # trailing non-digit after ADDRESS
    ("BUZZER_PIN", None),
])
def test_address_base(name, base):
    from kicad_mcp.utils.firmware.parse import address_base
    assert address_base(name) == base

def test_second_instance_addr_is_classified_as_address():
    # The whole reason the dual-MPU was previously lost: a strict endswith("_ADDR")
    # check skipped MPU6050_ADDR_2 entirely. It must classify as an ADDRESS.
    m = classify("MPU6050_ADDR_2", "0x69", None)
    assert m.kind is MacroKind.ADDRESS and m.address == 0x69


@pytest.mark.parametrize("name,expected", [
    ("MPU-6050", "MPU6050"),       # symbol-name dash stripped
    ("MPU6050", "MPU6050"),        # firmware-derived type unchanged
    ("bme280", "BME280"),
    ("SSD1306", "SSD1306"),
])
def test_canonical_type(name, expected):
    # one source so firmware (MPU6050_ADDR), card lookup, auto-draft and pre-fetch
    # all agree — a dashed symbol name resolves the same as the macro key.
    from kicad_mcp.utils.firmware.parse import canonical_type
    assert canonical_type(name) == expected


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


# --- role-token classifier (generalizes beyond _PIN; CMCA_* convention) ------

@pytest.mark.parametrize("name,value,gpio,role,bus,stem", [
    ("CMCA_I2S_BCLK", "15", 15, "BCLK", "I2S", "CMCA_I2S"),
    ("CMCA_I2S2_DIN", "7", 7, "DIN", "I2S", "CMCA_I2S2"),
    ("CMCA_OLED_SDA", "47", 47, "SDA", "I2C", "CMCA_OLED"),
    ("CMCA_OLED_SCL", "48", 48, "SCL", "I2C", "CMCA_OLED"),
    ("CMCA_MIC_WS", "9", 9, "WS", "I2S", "CMCA_MIC"),
    ("CMCA_MIC_SD", "10", 10, "SD", None, "CMCA_MIC"),  # ambiguous role -> bus by group
])
def test_role_token_pins(name, value, gpio, role, bus, stem):
    m = classify(name, value, None)
    assert m.kind is MacroKind.PIN and m.gpio == gpio
    assert m.signal_role == role and m.bus == bus and m.peripheral_hint == stem


@pytest.mark.parametrize("name,value", [
    ("CMCA_I2S_SAMPLE_RATE", "22050"),   # _RATE not a role
    ("CMCA_I2S_BITS", "16"),             # _BITS not a role, 16 is valid GPIO
    ("CMCA_DEFAULT_VOLUME", "80"),
    ("CMCA_MAX_CHANNELS", "8"),          # count in GPIO range
    ("CMCA_PANIC_BUTTON_ENABLED", "0"),  # ENABLED != EN (segment match)
    ("CMCA_PRESENCE_HOLD_MS", "10000"),
    # m-gain-roles: GAIN/ADC/DAC are config, not pins, when bare. The value
    # gate can't tell a gain-in-dB / channel index from a GPIO, so the bare
    # token must NOT promote these to pins.
    ("MAX_GAIN", "12"),
    ("AUDIO_GAIN", "3"),
    ("CMCA_AMP_GAIN_BUS0", "11"),        # was previously (wrongly) a pin
    ("AUDIO_DAC", "5"),
    ("CODEC_ADC", "7"),
])
def test_role_token_rejects_config(name, value):
    assert classify(name, value, None).kind is MacroKind.OTHER


@pytest.mark.parametrize("name,value,role,peri", [
    # An EXPLICIT _PIN/_GPIO suffix disambiguates: these ARE pins, and the
    # role is still recovered (via the legacy peripheral/role split).
    ("AMP_GAIN_PIN", "25", "GAIN", "AMP"),
    ("PIEZO_ADC_PIN", "35", "ADC", "PIEZO"),
    ("CODEC_DAC_GPIO", "26", "DAC", "CODEC"),
])
def test_suffix_disambiguates_gain_adc_dac(name, value, role, peri):
    m = classify(name, value, None)
    assert m.kind is MacroKind.PIN and m.gpio == int(value)
    assert m.signal_role == role and m.peripheral_hint == peri


def test_role_token_speedcal_regression():
    # The speed-cal _PIN/alias pins still classify identically.
    for name, value, role in [("I2C_SDA", "21", "SDA"),
                              ("HX711_DOUT_PIN", "16", "DOUT"),
                              ("MCP23017_INT_PIN", "13", "INT")]:
        m = classify(name, value, None)
        assert m.kind is MacroKind.PIN and m.signal_role == role


# --- preprocessor branch selection -------------------------------------------

_PP_TEXT = """\
#if CONFIG_IDF_TARGET_ESP32S3
#define I2S_BCLK 15
#else
#define I2S_BCLK 26
#endif
#define ALWAYS 1
"""

def test_select_branch_s3():
    out = select_active_branches(_PP_TEXT, {"CONFIG_IDF_TARGET_ESP32S3"})
    assert "15" in out and "26" not in out and "ALWAYS" in out

def test_select_branch_else():
    out = select_active_branches(_PP_TEXT, {"CONFIG_IDF_TARGET_ESP32"})
    assert "26" in out and "15" not in out

def test_select_nested_and_ifdef():
    text = "#ifdef A\n#ifndef B\nX\n#endif\nY\n#endif\nZ\n"
    assert select_active_branches(text, {"A"}) == "X\nY\nZ\n"   # B undefined -> ifndef true
    assert select_active_branches(text, {"A", "B"}) == "Y\nZ\n" # ifndef B false -> X dropped
    assert select_active_branches(text, set()) == "Z\n"          # A undefined -> all dropped

def test_unknown_condition_takes_if_branch():
    text = "#if FOO && BAR\nP\n#else\nQ\n#endif\n"
    assert select_active_branches(text, set()) == "P\n"          # complex -> take #if

def test_idf_target_defines():
    assert idf_target_defines("esp32-s3-devkitc-1") == {"CONFIG_IDF_TARGET_ESP32S3"}
    assert idf_target_defines("esp32dev") == {"CONFIG_IDF_TARGET_ESP32"}


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


# --- const / constexpr pin declarations (Arduino) ----------------------------
# A SECOND syntactic source feeding the ONE classifier (CLAUDE.md Rule 3). The
# regex finds candidates; classify() still decides pin/address/other by NAME, so
# the seam can't be load-bearing on its own.

def _one_const(line):
    decls = parse_const_decls(line)
    assert len(decls) == 1, f"expected 1 decl, got {[d.name for d in decls]}"
    return decls[0]


@pytest.mark.parametrize("line,name,gpio", [
    ("const int LED_PIN = 2;", "LED_PIN", 2),
    ("constexpr uint8_t I2C_SDA = 21;", "I2C_SDA", 21),
    ("static const int SCK_PIN = 18;", "SCK_PIN", 18),
    ("const byte BUZZER_PIN = 13;", "BUZZER_PIN", 13),
    ("static constexpr gpio_num_t MIC_SD = GPIO_NUM_15;", "MIC_SD", 15),
    ("const unsigned int RST_PIN = 4;", "RST_PIN", 4),
    ("const int LED_PIN = 2; // status led", "LED_PIN", 2),   # trailing comment past ;
])
def test_const_pin_decls_classified_as_pin(line, name, gpio):
    m = _one_const(line)
    assert m.kind is MacroKind.PIN and m.name == name and m.gpio == gpio
    assert m.pin_value_valid


def test_const_address_decl_is_address():
    m = _one_const("const uint8_t MPU6050_ADDR = 0x68;")
    assert m.kind is MacroKind.ADDRESS and m.address == 0x68


def test_const_nonpin_name_stays_other():
    # name-not-value decides: a GPIO-range value with a non-pin name is OTHER —
    # the classifier is the safety net against the const regex over-matching.
    assert _one_const("const int SAMPLE_COUNT = 16;").kind is MacroKind.OTHER
    assert _one_const("constexpr int TIMEOUT_MS = 5000;").kind is MacroKind.OTHER


def test_const_pin_named_bad_value_is_invalid_pin():
    m = _one_const("const int BUZZER_PIN = -1;")   # unused sentinel, out of range
    assert m.kind is MacroKind.PIN and not m.pin_value_valid


@pytest.mark.parametrize("line", [
    "int LED = 2;",                         # not const/constexpr -> mutable global, skip
    'const char* WIFI_SSID = "net";',       # pointer/string, skip
    'const char *HOST = "h";',              # pointer (space before *name), skip
    "const int LED_PINS[] = {2, 3, 4};",    # array, skip
    "led_pin = 2;",                         # bare assignment, no type, skip
    "// const int LED_PIN = 2;",            # commented out, skip
])
def test_const_regex_skips_non_pin_constant_forms(line):
    assert parse_const_decls(line) == []


def test_parse_macros_combines_define_and_const():
    macros = parse_macros("#define LED_PIN 2\nconst int BUZZER_PIN = 4;\n")
    assert {m.name for m in macros} == {"LED_PIN", "BUZZER_PIN"}
    assert all(m.kind is MacroKind.PIN for m in macros)


def test_parse_macros_defines_first_then_const():
    # ordering contract: #defines, then const-decls (regardless of source line order)
    assert [m.name for m in parse_macros("const int B_PIN = 4;\n#define A_PIN 2\n")] \
        == ["A_PIN", "B_PIN"]


# --- cold-review fixes: multi-declarator, block comments, esp32 variant guard ---

@pytest.mark.parametrize("line,expected", [
    # CRITICAL regression: a pin AFTER the first declarator must not be dropped.
    ("const int TIMEOUT = 5000, LED_PIN = 2;",
     [("TIMEOUT", MacroKind.OTHER, None), ("LED_PIN", MacroKind.PIN, 2)]),
    ("const int BUZZER_PIN = 4, LED_PIN = 2;",
     [("BUZZER_PIN", MacroKind.PIN, 4), ("LED_PIN", MacroKind.PIN, 2)]),
    ("constexpr uint8_t SDA_PIN = 21, SCL_PIN = 22;",
     [("SDA_PIN", MacroKind.PIN, 21), ("SCL_PIN", MacroKind.PIN, 22)]),
])
def test_const_multi_declarator_no_pin_dropped(line, expected):
    assert [(m.name, m.kind, m.gpio) for m in parse_const_decls(line)] == expected


def test_parse_macros_strips_block_comments():
    # a commented-out pinout must NOT be read as live pins (for BOTH #define + const)
    text = ("/* disabled\nconst int FAKE_PIN = 2;\n#define ALSO_FAKE_PIN 3\n*/\n"
            "const int REAL_PIN = 5;\n#define ANOTHER_PIN 6\n")
    pins = {m.name for m in parse_macros(text) if m.kind is MacroKind.PIN}
    assert pins == {"REAL_PIN", "ANOTHER_PIN"}


def test_const_inside_if_branch_is_selected():
    # branch-select strips the inactive branch first, so parse_macros sees one MIC_PIN
    from kicad_mcp.utils.firmware.parse import select_active_branches
    text = ("#if CONFIG_IDF_TARGET_ESP32S3\nconst int MIC_PIN = 15;\n"
            "#else\nconst int MIC_PIN = 4;\n#endif\n")
    s3 = parse_macros(select_active_branches(text, idf_target_defines("esp32-s3-devkitc-1")))
    classic = parse_macros(select_active_branches(text, idf_target_defines("esp32dev")))
    assert [m.gpio for m in s3 if m.name == "MIC_PIN"] == [15]
    assert [m.gpio for m in classic if m.name == "MIC_PIN"] == [4]


@pytest.mark.parametrize("board_id,target", [
    ("esp32dev", "CONFIG_IDF_TARGET_ESP32"),
    ("esp32", "CONFIG_IDF_TARGET_ESP32"),
    ("esp32-pico-d4", "CONFIG_IDF_TARGET_ESP32"),     # classic die name, not a variant
    ("esp32-cam", "CONFIG_IDF_TARGET_ESP32"),         # classic board, "cam" != family+digit
    ("esp32-s3-devkitc-1", "CONFIG_IDF_TARGET_ESP32S3"),
    ("esp32:esp32:esp32h2", "CONFIG_IDF_TARGET_ESP32H2"),
    ("esp32:esp32:esp32p4", "CONFIG_IDF_TARGET_ESP32P4"),
    # Arduino Nano ESP32 is an S3 — its id hides the chip behind a trailing "esp32".
    # Must classify as S3 so the resolve guard refuses the classic WROOM-32E card.
    ("arduino_nano_esp32", "CONFIG_IDF_TARGET_ESP32S3"),
])
def test_idf_target_known(board_id, target):
    assert idf_target_defines(board_id) == {target}


def test_arduino_nano_esp32_does_not_resolve_to_classic_wroom():
    # regression: "arduino_nano_esp32" contains "esp32" and used to mis-resolve to the
    # classic ESP32-WROOM-32E (wrong pinout — it's an S3 board). No Nano-ESP32 card
    # exists, so the honest outcome is mcu_unknown, NOT the wrong ESP32.
    from kicad_mcp.utils.firmware.intent import resolve_mcu
    assert resolve_mcu("arduino_nano_esp32") is None
    assert resolve_mcu("esp32dev") is not None   # the classic path still works


@pytest.mark.parametrize("board_id", [
    "esp32:esp32:esp32c2", "esp32c5", "esp32-s4", "esp32h4",
])
def test_idf_target_unknown_variant_fails_closed(board_id):
    # an unrecognized esp32 variant must NOT alias to classic ESP32 (empty set ->
    # resolve_mcu guard returns mcu_unknown, not a silently-wrong board)
    assert idf_target_defines(board_id) == set()


# --- Arduino analog pins: value "A0" names a symbol pin, not a GPIO number -------

@pytest.mark.parametrize("name,value,analog", [
    ("LDR_PIN", "A0", "A0"),       # analog pin -> carried as analog_pin
    ("SENSOR_PIN", "A7", "A7"),
    ("X_PIN", "A10", "A10"),       # Mega-range; the symbol validates it downstream
    ("X_PIN", "A00", "A0"),        # normalized via int()
    ("X_PIN", "0xA0", None),       # hex int 160 -> a (bad) GPIO, NOT an analog pin
    ("X_PIN", "A", None),          # no digit -> not an analog pin
    ("MODE", "A1", None),          # not pin-named -> OTHER (the name gate protects)
])
def test_analog_pin_classification(name, value, analog):
    m = classify(name, value, None)
    assert m.analog_pin == analog
    if analog is not None:
        assert m.kind is MacroKind.PIN and m.gpio is None and m.pin_value_valid
