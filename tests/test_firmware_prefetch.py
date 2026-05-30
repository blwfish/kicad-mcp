"""Tests for the pre-fetch card-synthesis logic (Phase 8), pure / no KiCad.
The conservative high-confidence gate is exercised at each edge: clean bus →
high; no bus → skip; unexplained/SPI pins, missing power, multi-unit → low.
"""
from __future__ import annotations

import pytest

from kicad_mcp.utils.firmware.cards import validate_peripheral_card
from kicad_mcp.utils.firmware.parse import canonical_type as _type_from_symbol_name
from kicad_mcp.utils.firmware.prefetch import (
    synthesize_i2c_card,
    top_level_symbol_names,
)


def _synth(pins, **kw):
    return synthesize_i2c_card(symbol_name=kw.pop("name", "FOO"),
                               lib_id=kw.pop("lib_id", "Sensor:FOO"),
                               pin_names=pins, **kw)


def test_type_normalization():
    assert _type_from_symbol_name("MPU-6050") == "MPU6050"
    assert _type_from_symbol_name("BME280") == "BME280"


def test_clean_i2c_is_high_and_validates():
    card, conf, _ = _synth(["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76)
    assert conf == "high"
    assert card["roles"] == {"SDA": "SDA", "SCL": "SCL"}
    assert card["supply_pins"] == ["VDD"] and card["ground_pins"] == ["GND"]
    assert card["type"] == "BME280"
    # a synthesized card must pass the same structural validator real cards do
    assert validate_peripheral_card(card) == []


def test_address_corroboration_noted():
    _, _, reasons = _synth(["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76)
    assert any("corroborates" in r for r in reasons)


def test_no_bus_is_skip():
    card, conf, _ = _synth(["OUT", "VDD", "GND"])
    assert card is None and conf == "skip"


def test_unexplained_pins_is_low():
    # extra SPI-ish pins the I2C card can't explain -> flagged
    card, conf, reasons = _synth(["SDA", "SCL", "VDD", "GND", "CSB", "SDO"])
    assert conf == "low" and card is not None
    assert any("unexplained" in r for r in reasons)


def test_missing_power_is_low():
    card, conf, _ = _synth(["SDA", "SCL", "VDD"])     # no ground pin
    assert conf == "low"


def test_multiunit_is_low():
    card, conf, _ = _synth(["SDA", "SCL", "VDD", "GND"], unit_count=2)
    assert conf == "low"


def test_nc_pins_dont_block_high():
    # NC pins don't block high — but high still needs address corroboration.
    card, conf, _ = _synth(["SDA", "SCL", "VDD", "GND", "NC", "NC"],
                           name="BME280", address=0x76)
    assert conf == "high"


def test_no_address_corroboration_is_low():
    """h-prefetch-corroboration: a clean SDA/SCL/VDD/GND symbol is NOT 'high'
    without I2C-address corroboration — a pin named SCL is not proof of an I2C
    clock. The bulk CLI always passes address=None, so this common path must
    downgrade to 'low' for human review (it graded 'high' before the fix)."""
    card, conf, reasons = _synth(["SDA", "SCL", "VDD", "GND"])   # no address
    assert conf == "low" and card is not None
    assert any("corroborat" in r.lower() for r in reasons)


def test_non_matching_address_is_low():
    # 0x68 maps to a different device than BME280 -> no corroboration -> low.
    _, conf, _ = _synth(["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x68)
    assert conf == "low"


def test_footprint_todo_when_unknown():
    card, _, _ = _synth(["SDA", "SCL", "VDD", "GND"])
    assert card["footprint"] == "TODO:confirm"
    assert card["_draft"]["needs_footprint"] is True


# --- symbol-name enumeration from .kicad_sym text ------------------------------

def test_top_level_symbol_names_filters_subsymbols():
    txt = '''
    (symbol "MPU-6050" (pin_names ...)
      (symbol "MPU-6050_0_1" ...)
      (symbol "MPU-6050_1_1" ...))
    (symbol "BME280" ...)
      (symbol "BME280_0_0" ...)
    '''
    assert top_level_symbol_names(txt) == ["MPU-6050", "BME280"]
