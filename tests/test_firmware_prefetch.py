"""Tests for the pre-fetch card-synthesis logic (Phase 8), pure / no KiCad.
The conservative high-confidence gate is exercised at each edge: clean bus →
high; no bus → skip; unexplained/SPI pins, missing power, multi-unit → low.
"""
from __future__ import annotations


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


def test_pin_type_corroboration_does_not_block_a_real_i2c_bus():
    """Regression: adding pin_types corroboration must not regress the
    existing clean-bus-is-high case when SDA/SCL carry plausible I2C
    electrical types (input/bidirectional, as seen on a real KiCad 10
    symbol -- Sensor_Motion:MPU-6050 has SCL=input, SDA=bidirectional)."""
    card, conf, _ = _synth(
        ["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76,
        pin_types={"SDA": "bidirectional", "SCL": "input", "VDD": "power_in", "GND": "power_in"},
    )
    assert conf == "high"


def test_scl_typed_as_power_pin_is_flagged_not_high():
    """Regression: role classification was 100% name-regex based -- a pin
    named SCL is not itself proof of an I2C clock. A symbol where "SCL"
    is electrically typed as a power pin is a real miswiring/misnaming red
    flag that pin-name matching alone could never catch."""
    card, conf, reasons = _synth(
        ["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76,
        pin_types={"SDA": "bidirectional", "SCL": "power_in", "VDD": "power_in", "GND": "power_in"},
    )
    assert conf == "low"
    assert any("SCL" in r and "misnamed" in r for r in reasons)


def test_sda_typed_as_no_connect_is_flagged_not_high():
    card, conf, reasons = _synth(
        ["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76,
        pin_types={"SDA": "no_connect", "SCL": "input", "VDD": "power_in", "GND": "power_in"},
    )
    assert conf == "low"
    assert any("SDA" in r for r in reasons)


def test_pin_types_omitted_falls_back_to_name_only_unchanged():
    """No pin_types passed at all (the historical call shape, still used by
    any caller that hasn't been updated) must behave exactly as before --
    this is purely additive corroboration, opt-in via an optional param."""
    card, conf, _ = _synth(["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76)
    assert conf == "high"


def test_pin_types_missing_entries_for_bus_pins_is_harmless():
    """A caller that supplies pin_types for SOME pins but not the I2C bus
    pins themselves (e.g. an incomplete extraction) must not spuriously
    flag anything -- absence of type data is not itself suspicious."""
    card, conf, _ = _synth(
        ["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76,
        pin_types={"VDD": "power_in", "GND": "power_in"},
    )
    assert conf == "high"


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


def test_footprint_used_when_symbol_provides_one():
    """End-to-end: synthesize_i2c_card already accepted a footprint kwarg --
    this pins that a caller-provided value is actually used, not overridden
    by the TODO default."""
    card, _, _ = _synth(["SDA", "SCL", "VDD", "GND"],
                        footprint="Sensor_Motion:InvenSense_QFN-24_4x4mm_P0.5mm")
    assert card["footprint"] == "Sensor_Motion:InvenSense_QFN-24_4x4mm_P0.5mm"
    assert card["_draft"]["needs_footprint"] is False


# --- symbol_footprint: extracting the pre-assigned Footprint property ----------

class _FakeSymbol:
    """Duck-types kicad_sch_api's SymbolDefinition.raw_kicad_data shape --
    a list of S-expression items, each itself a list whose first element is
    a sexpdata.Symbol tag."""
    def __init__(self, raw_kicad_data):
        self.raw_kicad_data = raw_kicad_data


def _property_item(key, value):
    import sexpdata
    return [sexpdata.Symbol("property"), key, value]


def test_symbol_footprint_extracts_real_value():
    """Regression: the module's own earlier assumption ("symbols don't
    carry a footprint") was wrong -- verified against a real KiCad 10
    symbol (Sensor_Motion:MPU-6050, one of prefetch's own DEFAULT_LIBRARIES),
    which DOES carry a pre-assigned Footprint property."""
    from kicad_mcp.utils.firmware.prefetch import symbol_footprint
    sym = _FakeSymbol([
        _property_item("Reference", "U"),
        _property_item("Value", "MPU-6050"),
        _property_item("Footprint", "Sensor_Motion:InvenSense_QFN-24_4x4mm_P0.5mm"),
        _property_item("Datasheet", "https://example.com/mpu6050.pdf"),
    ])
    assert symbol_footprint(sym) == "Sensor_Motion:InvenSense_QFN-24_4x4mm_P0.5mm"


def test_symbol_footprint_none_when_absent():
    from kicad_mcp.utils.firmware.prefetch import symbol_footprint
    sym = _FakeSymbol([
        _property_item("Reference", "R"),
        _property_item("Value", "R"),
    ])
    assert symbol_footprint(sym) is None


def test_symbol_footprint_none_when_property_present_but_empty():
    """A generic symbol (e.g. Device:R) has a Footprint property that
    exists but is an empty string -- must be treated as "no footprint",
    not as a legitimate empty value."""
    from kicad_mcp.utils.firmware.prefetch import symbol_footprint
    sym = _FakeSymbol([_property_item("Footprint", "")])
    assert symbol_footprint(sym) is None


def test_symbol_footprint_none_when_raw_data_missing():
    from kicad_mcp.utils.firmware.prefetch import symbol_footprint
    assert symbol_footprint(_FakeSymbol(None)) is None
    assert symbol_footprint(object()) is None


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


# --- scripts/prefetch_cards.py::_pin_types (extraction, not synthesis logic) ---

class _FakePinType:
    def __init__(self, value):
        self.value = value


class _FakePin:
    def __init__(self, name, pin_type_value):
        self.name = name
        self.pin_type = _FakePinType(pin_type_value)


class _FakePinSymbol:
    def __init__(self, pins):
        self.pins = pins


def test_pin_types_extracts_electrical_type_by_name():
    import sys
    sys.path.insert(0, "scripts")
    from prefetch_cards import _pin_types
    sym = _FakePinSymbol([
        _FakePin("SDA", "bidirectional"),
        _FakePin("SCL", "input"),
        _FakePin("VDD", "power_in"),
    ])
    assert _pin_types(sym) == {"SDA": "bidirectional", "SCL": "input", "VDD": "power_in"}


def test_pin_types_empty_when_no_pins():
    import sys
    sys.path.insert(0, "scripts")
    from prefetch_cards import _pin_types
    assert _pin_types(_FakePinSymbol([])) == {}
    assert _pin_types(object()) == {}


def test_pin_types_skips_unnamed_pins():
    import sys
    sys.path.insert(0, "scripts")
    from prefetch_cards import _pin_types
    sym = _FakePinSymbol([_FakePin("", "passive"), _FakePin("SDA", "bidirectional")])
    assert _pin_types(sym) == {"SDA": "bidirectional"}


# --- synthesize_i2c_card: blank-pin visibility + unexplained-pin truncation --

class TestBlankPinVisibility:
    """Regression: pins with an empty/whitespace-only name were silently
    filtered out of the classification set with no count anywhere -- a
    badly-formed symbol (many blank pin names) was indistinguishable from a
    clean one with a couple of genuine NC pins. finding #74 of the
    2026-09-23 full review."""

    def test_blank_pin_names_noted_in_reasons(self):
        card, conf, reasons = _synth(
            ["SDA", "SCL", "VDD", "GND", "", "  "], name="BME280", address=0x76,
        )
        assert conf == "high"
        assert any("2 pin(s) have no name" in r for r in reasons)

    def test_no_blank_pin_note_when_all_named(self):
        _, _, reasons = _synth(["SDA", "SCL", "VDD", "GND"], name="BME280", address=0x76)
        assert not any("have no name" in r for r in reasons)

    def test_skip_path_does_not_mention_blank_pins(self):
        # No I2C bus signature at all -> skip, before blank-pin accounting
        # would even run; its own more relevant reason should be the only one.
        _, conf, reasons = _synth(["OUT", "VDD", "GND", ""])
        assert conf == "skip"
        assert not any("have no name" in r for r in reasons)


class TestUnexplainedPinsTruncationNote:
    """Regression: unexplained[:8] silently capped the pins listed in the
    reasons text with no indication more existed. finding #72 of the
    2026-09-23 full review."""

    def test_more_than_eight_unexplained_pins_notes_the_overflow(self):
        extra_pins = [f"EXTRA{i}" for i in range(10)]
        _, conf, reasons = _synth(["SDA", "SCL", "VDD", "GND"] + extra_pins)
        assert conf == "low"
        assert any("+2 more" in r for r in reasons)

    def test_eight_or_fewer_unexplained_pins_no_overflow_note(self):
        extra_pins = [f"EXTRA{i}" for i in range(3)]
        _, conf, reasons = _synth(["SDA", "SCL", "VDD", "GND"] + extra_pins)
        assert conf == "low"
        assert not any("more)" in r for r in reasons)
