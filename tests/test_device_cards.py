"""Tests for the device-card layer (loader, structural validator, strap math,
resolution parity). No KiCad needed — pin-existence against real symbols is the
integration tier (tests/integration/test_device_cards.py).

Boundary-focused per CLAUDE.md: the strap math and the validator's accept/reject
edges are pinned at/below/above, and the migration's equivalence is frozen by a
snapshot of the 6 entries' exact field values.
"""
from __future__ import annotations

import textwrap

import pytest

from kicad_mcp.utils.firmware import knowledge as K
from kicad_mcp.utils.firmware.cards import (
    CardError,
    compute_address_straps,
    load_cards,
    validate_mcu_card,
    validate_peripheral_card,
)


# --- compute_address_straps: single source, reproduces both legacy fns --------

_MCP_STRAP = {"pin_bits": ["A0", "A1", "A2"], "base": 0x20,
              "rail_set": "+3V3", "rail_clear": "GND"}
_MPU_STRAP = {"pin_bits": ["5"], "base": 0x68, "rail_set": "+3V3", "rail_clear": "GND"}


@pytest.mark.parametrize("addr,expected", [
    (0x20, [("A0", "GND"), ("A1", "GND"), ("A2", "GND")]),        # base
    (0x27, [("A0", "+3V3"), ("A1", "+3V3"), ("A2", "+3V3")]),     # max
    (0x24, [("A0", "GND"), ("A1", "GND"), ("A2", "+3V3")]),       # mixed
    (0x21, [("A0", "+3V3"), ("A1", "GND"), ("A2", "GND")]),       # lsb only
    (0x1F, None),                                                 # one below base
    (0x28, None),                                                 # one above max
])
def test_strap_mcp_3bit(addr, expected):
    assert compute_address_straps(_MCP_STRAP, addr) == expected


@pytest.mark.parametrize("addr,expected", [
    (0x68, [("5", "GND")]),     # base
    (0x69, [("5", "+3V3")]),    # base + 1 (max for 1 bit)
    (0x67, None),               # one below
    (0x6A, None),               # one above
])
def test_strap_mpu_1bit(addr, expected):
    assert compute_address_straps(_MPU_STRAP, addr) == expected


def test_strap_matches_backcompat_wrappers():
    # the knowledge.py wrappers must reproduce the helper exactly
    for a in (0x20, 0x24, 0x27, 0x1F, 0x28):
        assert K.mcp23017_address_straps(a) == compute_address_straps(_MCP_STRAP, a)
    for a in (0x68, 0x69, 0x67, 0x6A):
        helper = compute_address_straps(_MPU_STRAP, a)
        assert K.mpu6050_ad0_strap(a) == (helper[0] if helper else None)


# --- structural validation ----------------------------------------------------

_GOOD_PERIPHERAL = {
    "type": "FOO", "lib_id": "Lib:Foo", "value": "Foo", "bus": "I2C",
    "footprint": "FP:Foo", "roles": {"SDA": "4"}, "supply_pins": ["2"],
    "ground_pins": ["1"], "module": True,
}
_GOOD_MCU = {
    "part": "FOO-MCU", "lib_id": "Lib:Foo", "value": "Foo", "footprint": "FP:Foo",
    "board_match": ["foo"], "needs_3v3": True, "supply_pin": "VDD",
    "ground_pin": "GND", "en_pin": "EN", "boot_pin": "IO0",
    "uart_rx_pin": "RX", "uart_tx_pin": "TX", "native_usb": False,
}


def test_good_cards_validate_clean():
    assert validate_peripheral_card(_GOOD_PERIPHERAL) == []
    assert validate_mcu_card(_GOOD_MCU) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.pop("lib_id"), "missing required"),
    (lambda c: c.update(lib_id="NoColon"), "lib_id"),
    (lambda c: c.update(bus="SDIO"), "bus"),
    (lambda c: c.update(module="yes"), "module must be a bool"),
])
def test_bad_peripheral_rejected(mutate, needle):
    card = dict(_GOOD_PERIPHERAL)
    mutate(card)
    errs = validate_peripheral_card(card)
    assert errs and any(needle in e for e in errs)


@pytest.mark.parametrize("strap,ok", [
    ({"pin_bits": ["A0"], "base": 0x20}, True),
    ({"pin_bits": [], "base": 0x20}, False),          # empty pin_bits
    ({"pin_bits": ["A0"], "base": "0x20"}, False),    # base not int
    ({"pin_bits": ["A0"], "base": 1, "rail_set": "+9V"}, False),  # bad rail
])
def test_address_strap_validation(strap, ok):
    card = dict(_GOOD_PERIPHERAL, config={"address_strap": strap})
    errs = validate_peripheral_card(card)
    assert (errs == []) is ok


# --- packaged library loads + the migrated 6 entries are present --------------

def test_packaged_cards_load():
    peris, mcus = load_cards()
    assert set(peris) >= {"MCP23017", "HX711", "MPU6050", "OLED"}
    assert {m["part"] for m in mcus} >= {"ESP32-WROOM-32E", "ESP32-S3-WROOM-1"}


# Frozen snapshot — the migration must reproduce the old literals EXACTLY.
_EXPECTED = {
    "MCP23017": {
        "lib_id": "Interface_Expansion:MCP23017x-x-SO", "value": "MCP23017",
        "bus": "I2C", "footprint": "Package_SO:SOIC-28W_7.5x17.9mm_P1.27mm",
        "roles": {"SDA": "SDA", "SCL": "SCK", "INT": "INTA", "INTA": "INTA"},
        "supply_pins": ["V_{DD}"], "ground_pins": ["V_{SS}"], "module": False,
    },
    "HX711": {
        "lib_id": "Analog_ADC:HX711", "value": "HX711", "bus": None,
        "footprint": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
        "roles": {"DOUT": "DOUT", "SCK": "PD_SCK", "PD_SCK": "PD_SCK"},
        "supply_pins": ["VSUP", "AVDD", "DVDD"], "ground_pins": ["AGND"],
        "module": False,
    },
    "MPU6050": {
        "lib_id": "Connector_Generic:Conn_01x05", "value": "GY-521 (MPU-6050)",
        "bus": "I2C",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
        "roles": {"SDA": "4", "SCL": "3"}, "supply_pins": ["2"],
        "ground_pins": ["1"], "module": True,
    },
    "OLED": {
        "lib_id": "Connector_Generic:Conn_01x04", "value": "OLED (SSD1306)",
        "bus": "I2C",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        "roles": {"SDA": "4", "SCL": "3"}, "supply_pins": ["2"],
        "ground_pins": ["1"], "module": True,
    },
}


@pytest.mark.parametrize("ptype", sorted(_EXPECTED))
def test_resolution_parity(ptype):
    info = K.resolve_peripheral(ptype)
    assert info is not None
    for k, v in _EXPECTED[ptype].items():
        assert info[k] == v, f"{ptype}.{k}"


def test_backcompat_constants_match_cards():
    # the mirror constants must equal the card data (so they can't drift)
    mcp = K.resolve_peripheral("MCP23017")
    mpu = K.resolve_peripheral("MPU6050")
    assert K.MCP23017_RESET_PIN == mcp["config"]["static_ties"][0]["pin"]
    assert K.MPU6050_AD0_PIN == mpu["config"]["address_strap"]["pin_bits"][0]


# --- MCU resolution (longest board_match, no hand-ordered precedence) ---------

@pytest.mark.parametrize("board,part", [
    ("esp32dev", "ESP32-WROOM-32E"),
    ("esp32", "ESP32-WROOM-32E"),
    ("esp32-s3-devkitc-1", "ESP32-S3-WROOM-1"),
    ("esp32-s3", "ESP32-S3-WROOM-1"),
    ("esp32s3", "ESP32-S3-WROOM-1"),
    ("esp32-s3-wroom", "ESP32-S3-WROOM-1"),   # substring: longest 'esp32-s3' wins
    ("esp32doit-devkit-v1", "ESP32-WROOM-32E"),  # fuzzy classic esp32, IDF agrees
    # h-resolve-mcu: C3/C6/S2 with no dedicated card must NOT fuzzy-match the
    # classic WROOM-32E via the bare 'esp32' substring — stay unknown.
    ("esp32-c3-devkitm-1", None),
    ("esp32-c6-devkitc-1", None),
    ("esp32-s2-saola-1", None),
    ("nonsense", None),
])
def test_resolve_mcu(board, part):
    info = K.resolve_mcu(board)
    assert (info["part"] if info else None) == part


# --- loader precedence + malformed handling -----------------------------------

def test_override_dir_takes_precedence(tmp_path):
    (tmp_path / "mpu_override.yaml").write_text(textwrap.dedent("""\
        type: MPU6050
        lib_id: Lib:Custom
        value: custom
        bus: I2C
        footprint: FP:Custom
        roles: {SDA: "4", SCL: "3"}
        supply_pins: ["2"]
        ground_pins: ["1"]
        module: true
    """))
    peris, _ = load_cards(extra_dirs=[str(tmp_path)])
    assert peris["MPU6050"]["lib_id"] == "Lib:Custom"   # override wins


def test_malformed_card_raises(tmp_path):
    (tmp_path / "bad.yaml").write_text("type: BAD\nlib_id: no_colon\n")
    with pytest.raises(CardError):
        load_cards(extra_dirs=[str(tmp_path)])


def test_neither_peripheral_nor_mcu_raises(tmp_path):
    (tmp_path / "huh.yaml").write_text("foo: bar\n")
    with pytest.raises(CardError):
        load_cards(extra_dirs=[str(tmp_path)])
