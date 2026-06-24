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
    normalize_component_value,
    get_component_type_from_reference,
    is_power_component,
)


# -- extract_voltage_from_regulator tests ------------------------------------

class TestExtractVoltage:

    @pytest.mark.parametrize("value,expected", [
        ("7805", "5V"),
        ("7812", "12V"),
        ("7905", "-5V"),  # 79xx is the negative-voltage family
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

    # -- Threshold-boundary coverage for `if voltage < 50` ------------------
    # The 78xx/79xx branch caps the matched voltage at 50V (anything ≥50V is
    # rejected as not a regulator part).  The round-number test_known_regulators
    # set never touches the threshold; these do.
    @pytest.mark.parametrize("value,expected", [
        ("7849", "49V"),   # just below threshold -- accepted as a 49V part
        ("7850", "unknown"),  # exactly at threshold -- rejected (strict <)
        ("7851", "unknown"),  # just above threshold -- rejected
    ])
    def test_voltage_threshold_50v_78xx(self, value, expected):
        assert extract_voltage_from_regulator(value) == expected

    @pytest.mark.parametrize("value,expected", [
        ("7949", "-49V"),  # 79xx is negative -- sign preserved
        ("7950", "unknown"),
        ("7951", "unknown"),
    ])
    def test_voltage_threshold_50v_79xx(self, value, expected):
        assert extract_voltage_from_regulator(value) == expected

    # -- Generic "(\d+)V" threshold: `0 < voltage < 50` ---------------------
    @pytest.mark.parametrize("value,expected", [
        ("0V", "unknown"),    # threshold low: strict >0 -- rejected
        ("1V", "1V"),
        ("49V", "49V"),
        ("50V", "unknown"),   # threshold high: strict <50 -- rejected
        ("100V", "unknown"),
    ])
    def test_generic_voltage_thresholds(self, value, expected):
        assert extract_voltage_from_regulator(value) == expected


# -- Real-bug regression tests for extract_voltage_from_regulator ------------
# These cases come from KiCad library symbols, not the function's docstring
# examples.  Each one exercises a path that round-number tests never reach.

class TestExtractVoltageRealBugs:
    """Inputs drawn from real KiCad libraries that the existing tests miss."""

    def test_lm7905_is_negative(self):
        """LM7905 is the -5V regulator -- the regulator dict entry is "-5V".

        The 78xx/79xx numeric branch catches "7905" and returns "5V" before
        the dict lookup runs, silently stripping the sign.  Consumers
        (BOM rendering, schematic labels) get the wrong polarity.
        """
        assert extract_voltage_from_regulator("LM7905") == "-5V"

    def test_lm7912_is_negative(self):
        """LM7912 is the -12V regulator."""
        assert extract_voltage_from_regulator("LM7912") == "-12V"

    def test_7800_returns_unknown_not_0v(self):
        """'7800' is not a real part -- the 78xx branch should reject it.

        The current implementation extracts '00', evaluates `0 < 50`, and
        returns '0V'.  A 0V regulator is nonsensical; this is the same kind
        of off-by-one as the upper threshold check.
        """
        assert extract_voltage_from_regulator("7800") == "unknown"

    def test_negative_voltage_string_preserves_sign(self):
        """A description containing '-3V' must not silently strip the sign. The
        signed pattern `-(\\d+\\.?\\d*)V` is listed before the unsigned one so the
        minus survives. Pin the EXACT value: `startswith("-") or "unknown"` would
        pass even if the signed pattern were deleted (falling through to unknown)."""
        assert extract_voltage_from_regulator("REG -3V output") == "-3V"


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

    # -- Threshold coverage for the `>=1000` unit-promotion cascade ---------
    # extract_frequency_from_value promotes Hz->kHz, kHz->MHz, MHz->GHz when
    # the numeric value is >= 1000.  These boundaries are not covered by the
    # round-number tests above; a regression that flipped >= to > would slip
    # through the existing suite.
    @pytest.mark.parametrize("value,expected", [
        ("999Hz", "999.000Hz"),    # just below Hz->kHz threshold
        ("1000Hz", "1.000kHz"),    # exactly at threshold -- promote
        ("1001Hz", "1.001kHz"),    # just above threshold
    ])
    def test_frequency_hz_to_khz_threshold(self, value, expected):
        assert extract_frequency_from_value(value) == expected

    @pytest.mark.parametrize("value,expected", [
        ("999kHz", "999.000kHz"),  # just below kHz->MHz threshold
        ("1000kHz", "1.000MHz"),   # exactly at threshold -- promote
        ("1001kHz", "1.001MHz"),
    ])
    def test_frequency_khz_to_mhz_threshold(self, value, expected):
        assert extract_frequency_from_value(value) == expected

    @pytest.mark.parametrize("value,expected", [
        ("999MHz", "999.000MHz"),  # just below MHz->GHz threshold
        ("1000MHz", "1.000GHz"),   # exactly at threshold -- promote
        ("1001MHz", "1.001GHz"),
    ])
    def test_frequency_mhz_to_ghz_threshold(self, value, expected):
        assert extract_frequency_from_value(value) == expected

    # Real-world crystal frequencies pulled from KiCad symbol library
    # (Device:Crystal_GND2_Small etc) -- separate from the docstring examples.
    @pytest.mark.parametrize("value,expected", [
        ("11.0592MHz", "11.059MHz"),  # MCU UART baud-rate crystal
        ("4.194304MHz", "4.194MHz"),  # RTC binary divisor crystal
        ("3.579545MHz", "3.580MHz"),  # NTSC colour-burst crystal
    ])
    def test_frequency_real_crystal_values(self, value, expected):
        assert extract_frequency_from_value(value) == expected


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
        """'4k7' is European shorthand for 4.7k -- the decimal point is replaced
        by the unit letter.  The first regex (\\d+\\.?\\d*)([kKmMrR]?) greedily
        matches '4' + 'k' and returns before the dedicated '4k7' branch runs,
        same shadow pattern as the LM7905 bug.  Consumer-visible effect: a 4.7k
        pull-up gets normalized to 4k, off by 700 ohms.
        """
        val, unit = extract_resistance_value("4k7")
        assert val == 4.7
        assert unit.lower() == "k"

    @pytest.mark.parametrize("value,expected_val", [
        ("4k7", 4.7),
        ("2k2", 2.2),
        ("1k5", 1.5),
        ("4K7", 4.7),  # uppercase K should also work
        ("2M2", 2.2),  # megaohm shorthand
    ])
    def test_european_shorthand_notation(self, value, expected_val):
        """European resistor shorthand: 4k7 == 4.7k, 2k2 == 2.2k, etc."""
        val, _unit = extract_resistance_value(value)
        assert val == expected_val, f"{value!r} should parse to {expected_val}, got {val}"

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
        """'4n7' is European shorthand for 4.7nF.  Same shadow as the 4k7 bug:
        the first regex (\\d+\\.?\\d*)([pPnNuU]+) greedily matches '4'+'n' and
        returns (4, 'nF') before the '4n7' branch runs.  Consumer-visible
        effect: a 4.7nF decoupling cap reads as 4nF -- off by 700pF, which
        moves real filter corner frequencies.
        """
        val, unit = extract_capacitance_value("4n7")
        assert val == 4.7
        assert unit == "nF"

    @pytest.mark.parametrize("value,expected_val,expected_unit", [
        ("4n7", 4.7, "nF"),
        ("2u2", 2.2, "μF"),
        ("1p5", 1.5, "pF"),
        ("3n3", 3.3, "nF"),
    ])
    def test_european_shorthand_notation(self, value, expected_val, expected_unit):
        """European cap shorthand: 4n7 == 4.7nF, 2u2 == 2.2uF, etc."""
        val, unit = extract_capacitance_value(value)
        assert val == expected_val, f"{value!r} should parse to {expected_val}, got {val}"
        assert unit == expected_unit, f"{value!r} should have unit {expected_unit}, got {unit}"


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
