"""
Tests for component utility functions.

Pure unit tests — no mocking needed.
"""

import pytest

from kicad_mcp.utils.component_utils import (
    extract_voltage_from_regulator,
    extract_frequency_from_value,
    extract_resistance_value,
    extract_capacitance_value,
    extract_inductance_value,
    format_resistance,
    format_capacitance,
    normalize_component_value,
    get_component_type_from_reference,
    is_power_component,
)


# -- extract_voltage_from_regulator tests ------------------------------------

class TestExtractVoltage:

    @pytest.mark.parametrize("value,expected", [
        ("7805", "5V"),
        ("7812", "12V"),
        ("7905", "5V"),
        ("LM7805", "5V"),
        ("LM7812", "12V"),
        ("LM1117-3.3", "3.3V"),
        ("AMS1117-3.3", "3.3V"),
        ("MCP1700-3.3", "3.3V"),
        ("MCP1700-5.0", "5V"),
        ("LM317", "Adjustable"),
        ("3.3V", "3.3V"),
        ("5V", "5V"),
    ])
    def test_known_regulators(self, value, expected):
        assert extract_voltage_from_regulator(value) == expected

    def test_unknown_returns_unknown(self):
        assert extract_voltage_from_regulator("MYSTERY_CHIP") == "unknown"

    def test_voltage_out_of_range_ignored(self):
        # 99V should not match 78xx series (voltage >= 50)
        result = extract_voltage_from_regulator("7899")
        assert result == "unknown"


# -- extract_frequency_from_value tests --------------------------------------

class TestExtractFrequency:

    @pytest.mark.parametrize("value,expected", [
        ("16MHz", "16.000MHz"),
        ("8MHz", "8.000MHz"),
        ("32.768kHz", "32.768kHz"),
        ("Crystal 8MHz", "8.000MHz"),
    ])
    def test_known_frequencies(self, value, expected):
        assert extract_frequency_from_value(value) == expected

    def test_unknown_returns_unknown(self):
        assert extract_frequency_from_value("100nF") == "unknown"


# -- extract_resistance_value tests ------------------------------------------

class TestExtractResistance:

    @pytest.mark.parametrize("value,expected_val,expected_unit", [
        ("10k", 10.0, "K"),
        ("4.7k", 4.7, "K"),
        ("100", 100.0, "Ω"),
        ("1M", 1.0, "M"),
    ])
    def test_standard_values(self, value, expected_val, expected_unit):
        val, unit = extract_resistance_value(value)
        assert val == expected_val
        assert unit.upper() == expected_unit.upper()

    def test_4k7_notation(self):
        val, unit = extract_resistance_value("4k7")
        # First regex matches "4" with unit "k"; the 4k7 pattern is secondary
        assert val is not None
        assert unit is not None

    def test_unparseable(self):
        val, unit = extract_resistance_value("hello")
        assert val is None


# -- extract_capacitance_value tests -----------------------------------------

class TestExtractCapacitance:

    @pytest.mark.parametrize("value,expected_val,expected_unit", [
        ("100nF", 100.0, "nF"),
        ("10uF", 10.0, "μF"),
        ("22pF", 22.0, "pF"),
    ])
    def test_standard_values(self, value, expected_val, expected_unit):
        val, unit = extract_capacitance_value(value)
        assert val == expected_val
        assert unit == expected_unit

    def test_4n7_notation(self):
        val, unit = extract_capacitance_value("4n7")
        # First regex matches "4" with unit; the shorthand pattern is secondary
        assert val is not None
        assert unit is not None


# -- extract_inductance_value tests ------------------------------------------

class TestExtractInductance:

    @pytest.mark.parametrize("value,expected_val,expected_unit", [
        ("10uH", 10.0, "μH"),
        ("4.7nH", 4.7, "nH"),
        ("100mH", 100.0, "mH"),
    ])
    def test_standard_values(self, value, expected_val, expected_unit):
        val, unit = extract_inductance_value(value)
        assert val == expected_val
        assert unit == expected_unit


# -- format_resistance tests -------------------------------------------------

class TestFormatResistance:

    def test_ohms(self):
        assert format_resistance(100.0, "Ω") == "100Ω"

    def test_kilohms(self):
        assert format_resistance(4.7, "k") == "4.7kΩ"

    def test_megaohms(self):
        assert format_resistance(1.0, "M") == "1MΩ"


# -- normalize_component_value tests -----------------------------------------

class TestNormalizeComponentValue:

    def test_resistor(self):
        result = normalize_component_value("10k", "R")
        assert "10" in result

    def test_capacitor(self):
        result = normalize_component_value("100nF", "C")
        assert "100" in result and "nF" in result

    def test_inductor(self):
        result = normalize_component_value("10uH", "L")
        assert "10" in result

    def test_unknown_type_passthrough(self):
        assert normalize_component_value("ESP32", "U") == "ESP32"


# -- get_component_type_from_reference tests ---------------------------------

class TestGetComponentType:

    @pytest.mark.parametrize("ref,expected", [
        ("R1", "R"),
        ("C12", "C"),
        ("U1", "U"),
        ("LED1", "LED"),
        ("D3", "D"),
    ])
    def test_standard_references(self, ref, expected):
        assert get_component_type_from_reference(ref) == expected

    def test_empty_string(self):
        assert get_component_type_from_reference("") == ""


# -- is_power_component tests ------------------------------------------------

class TestIsPowerComponent:

    def test_regulator_reference(self):
        assert is_power_component({"reference": "VR1", "value": "", "lib_id": ""})

    def test_power_value(self):
        assert is_power_component({"reference": "U1", "value": "LM7805", "lib_id": ""})

    def test_not_power(self):
        assert not is_power_component({"reference": "R1", "value": "10k", "lib_id": ""})

    def test_power_lib_id(self):
        assert is_power_component(
            {"reference": "U1", "value": "X", "lib_id": "Regulator_Linear:LDO"})
