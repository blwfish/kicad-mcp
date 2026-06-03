"""
Tests for circuit pattern recognition functions.

Pure unit tests — no mocking needed, no KiCad installation required.
Focuses on: regex alternation order, specific-vs-generic shadowing,
double-classification guard, edge/empty cases.
"""

import pytest

from kicad_mcp.utils.pattern_recognition import (
    identify_amplifiers,
    identify_digital_interfaces,
    identify_filters,
    identify_microcontrollers,
    identify_oscillators,
    identify_power_supplies,
    identify_sensor_interfaces,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _comp(value="", lib_id=""):
    return {"value": value, "lib_id": lib_id}


def _result_types(results):
    return [r.get("type") for r in results]


def _result_subtypes(results):
    return [r.get("subtype") for r in results]


def _result_models(results):
    return [r.get("model") for r in results]


# ===========================================================================
# identify_power_supplies
# ===========================================================================

class TestIdentifyPowerSupplies:

    def test_empty_components(self):
        result = identify_power_supplies({}, {})
        assert result == []

    def test_empty_nets_with_component(self):
        # nets can be empty — should not crash
        components = {"U1": _comp("LM7805")}
        result = identify_power_supplies(components, {})
        assert len(result) == 1

    def test_missing_value_key(self):
        # Component dict has no 'value' key — should not raise
        components = {"U1": {"lib_id": "Device:R"}}
        result = identify_power_supplies(components, {})
        assert result == []

    def test_missing_lib_id_key(self):
        # Component dict has no 'lib_id' key — should not raise
        components = {"U1": {"value": "LM7805"}}
        result = identify_power_supplies(components, {})
        assert len(result) == 1
        assert result[0]["type"] == "linear_regulator"

    def test_empty_string_value(self):
        components = {"U1": _comp("", "")}
        result = identify_power_supplies(components, {})
        assert result == []

    # --- 78xx / 79xx linear regulators ---

    @pytest.mark.parametrize("value,expected_subtype", [
        ("7805", "78xx"),
        ("LM7805", "78xx"),
        ("MC7812", "78xx"),
        ("7905", "79xx"),
        ("LM7905", "79xx"),
    ])
    def test_linear_regulator_subtypes(self, value, expected_subtype):
        components = {"U1": _comp(value)}
        result = identify_power_supplies(components, {})
        assert any(r["subtype"] == expected_subtype for r in result), \
            f"Expected subtype {expected_subtype!r} for value {value!r}, got {result}"

    def test_lm7905_not_double_classified_78xx_and_79xx(self):
        # LM7905 matches BOTH r"78\d\d" (substring "7905") via the 79xx family
        # AND is the 79xx family itself. The break ensures first-match-wins.
        # Critically: a component should NOT appear as both 78xx and 79xx.
        components = {"U1": _comp("LM7905")}
        result = identify_power_supplies(components, {})
        refs = [r["main_component"] for r in result]
        assert refs.count("U1") == 1, \
            f"LM7905 should appear exactly once, got refs: {refs}"

    def test_lm7805_not_also_switching_regulator(self):
        # LM7805 matches the 78xx pattern (linear) AND LM\d{4} (switching/buck).
        # classified_refs guard should prevent U1 from being reported as both.
        # Need an inductor component to trigger the switching-regulator path.
        components = {
            "U1": _comp("LM7805"),
            "L1": _comp("10uH"),
        }
        result = identify_power_supplies(components, {})
        linear_matches = [r for r in result if r["type"] == "linear_regulator" and r["main_component"] == "U1"]
        switching_matches = [r for r in result if r["type"] == "switching_regulator" and r["main_component"] == "U1"]
        assert len(linear_matches) == 1
        assert len(switching_matches) == 0, \
            "LM7805 already classified as linear — should not also appear as switching"

    def test_switching_regulator_buck_first_match(self):
        # LM2596 is a buck converter. The loose LDO pattern `LM\d{3}` used to match
        # "LM259" inside "LM2596" and (linear-runs-first) claim it as a linear LDO,
        # so it appeared 0 times in switching. With the prefix anchored it now
        # correctly classifies as exactly one buck switching regulator, and must
        # NOT also appear as a linear regulator.
        components = {
            "U2": _comp("LM2596"),
            "L1": _comp("68uH"),
        }
        result = identify_power_supplies(components, {})
        u2_switching = [r for r in result
                        if r["type"] == "switching_regulator" and r["main_component"] == "U2"]
        u2_linear = [r for r in result
                     if r["type"] == "linear_regulator" and r["main_component"] == "U2"]
        assert len(u2_switching) == 1, f"LM2596 must be one buck entry, got: {u2_switching}"
        assert u2_switching[0]["subtype"] == "buck"
        assert u2_linear == [], f"LM2596 must NOT be classified linear, got: {u2_linear}"

    def test_lm317_three_digit_ldo_still_linear(self):
        # Guard the prefix-anchor fix doesn't break real 3-digit LM LDOs: LM317
        # must still classify as a linear regulator.
        result = identify_power_supplies({"U1": _comp("LM317")}, {})
        u1_linear = [r for r in result
                     if r["type"] == "linear_regulator" and r["main_component"] == "U1"]
        assert len(u1_linear) == 1 and u1_linear[0]["subtype"] == "LDO"


# ===========================================================================
# identify_amplifiers
# ===========================================================================

class TestIdentifyAmplifiers:

    def test_empty_components(self):
        assert identify_amplifiers({}, {}) == []

    def test_missing_value_key(self):
        # Should not raise KeyError
        result = identify_amplifiers({"U1": {"lib_id": "Amplifier_Operational:TL072"}}, {})
        # lib_id match falls through to unknown subtype
        assert len(result) == 1
        assert result[0]["subtype"] == "unknown"

    def test_audio_opamp_ne5534(self):
        components = {"U1": _comp("NE5534")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "audio"

    def test_audio_opamp_tl071(self):
        # TL071 is in the audio list; TL082 is in general_purpose
        # Confirms the specific-vs-generic ordering
        components = {"U1": _comp("TL071")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "audio"

    def test_general_purpose_opamp_lm358(self):
        components = {"U1": _comp("LM358")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "general_purpose"

    def test_instrumentation_amp_ina128(self):
        # INA128 is an instrumentation amp.  opamp_patterns now includes
        # INA\d+ in the outer filter, so the inner instrumentation branch
        # is reachable (it was dead code before — fixed alongside the
        # AD\d{3,} widening for AD8221/AD8429).
        components = {"U1": _comp("INA128")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "instrumentation"
        assert result[0]["value"] == "INA128"

    def test_instrumentation_amp_ad8221(self):
        # AD8221 is 4-digit AD (not 3-digit AD\d{3}); the widened AD\d{3,}
        # outer pattern now catches it.  Inner branch lists AD8221 explicitly.
        components = {"U1": _comp("AD8221")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "instrumentation"

    def test_tl072_is_audio(self):
        # TL072 is listed in the audio op-amp branch (line 133: TL072).
        # The comment at line 143 says "TL072 removed" from general_purpose,
        # meaning TL072 was MOVED to audio (intentional — it's a popular
        # audio op-amp). Pin: TL072 → audio.
        components = {"U1": _comp("TL072")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "audio"

    def test_tl082_is_general_purpose(self):
        # TL082 is the general-purpose variant; TL072 is audio.
        components = {"U1": _comp("TL082")}
        result = identify_amplifiers(components, {})
        assert len(result) == 1
        assert result[0]["subtype"] == "general_purpose"

    def test_opamp_not_double_appended_via_two_patterns(self):
        # opamp_patterns has two entries; break should prevent double-appending
        components = {"U1": _comp("LM358")}
        result = identify_amplifiers(components, {})
        entries = [r for r in result if r.get("component") == "U1"]
        assert len(entries) == 1, f"U1 should appear exactly once, got: {entries}"

    def test_audio_amplifier_ic_lm386(self):
        components = {"U1": _comp("LM386")}
        result = identify_amplifiers(components, {})
        audio_ics = [r for r in result if r["type"] == "audio_amplifier_ic"]
        assert len(audio_ics) == 1

    def test_bjt_amplifier_with_biasing(self):
        components = {
            "Q1": {"value": "2N3904", "lib_id": "Device:BJT_NPN"},
            "R1": _comp("10k"),
        }
        nets = {
            "BASE_NET": [
                {"component": "Q1", "pin": "1"},
                {"component": "R1", "pin": "1"},
            ]
        }
        result = identify_amplifiers(components, nets)
        bjt = [r for r in result if r["type"] == "transistor_amplifier" and r["subtype"] == "BJT"]
        assert len(bjt) == 1

    def test_bjt_without_biasing_not_reported(self):
        # BJT with no resistors in its nets — not an amplifier
        components = {"Q1": {"value": "2N3904", "lib_id": "Device:BJT_NPN"}}
        nets = {"COLLECTOR_NET": [{"component": "Q1", "pin": "1"}]}
        result = identify_amplifiers(components, nets)
        bjt = [r for r in result if r["type"] == "transistor_amplifier"]
        assert len(bjt) == 0


# ===========================================================================
# identify_filters
# ===========================================================================

class TestIdentifyFilters:

    def test_empty_components(self):
        assert identify_filters({}, {}) == []

    def test_rc_low_pass_detected(self):
        components = {
            "R1": _comp("10k"),
            "C1": _comp("100nF"),
        }
        nets = {
            "MID_NODE": [
                {"component": "R1", "pin": "2"},
                {"component": "C1", "pin": "1"},
            ],
            "GND": [{"component": "C1", "pin": "2"}],
        }
        result = identify_filters(components, nets)
        rc = [r for r in result if r["type"] == "passive_filter" and r["subtype"] == "rc_low_pass"]
        assert len(rc) >= 1

    def test_cap_not_to_gnd_not_low_pass(self):
        # C1 is connected to VCC, not GND — not a low-pass filter
        components = {
            "R1": _comp("10k"),
            "C1": _comp("100nF"),
        }
        nets = {
            "MID_NODE": [
                {"component": "R1", "pin": "2"},
                {"component": "C1", "pin": "1"},
            ],
            "VCC": [{"component": "C1", "pin": "2"}],
        }
        result = identify_filters(components, nets)
        rc = [r for r in result if r.get("subtype") == "rc_low_pass"]
        assert len(rc) == 0

    # Crystal_filter classification removed — identify_oscillators is the
    # single source of truth for crystals (see TestCrystalNotDoubleClassified
    # below).

    def test_active_filter_opamp_with_rc_feedback(self):
        components = {
            "U1": _comp("TL082"),
            "R1": _comp("10k"),
            "C1": _comp("10nF"),
        }
        nets = {
            "FEEDBACK": [
                {"component": "U1", "pin": "2"},
                {"component": "R1", "pin": "1"},
                {"component": "C1", "pin": "1"},
            ],
        }
        result = identify_filters(components, nets)
        active = [r for r in result if r["type"] == "active_filter"]
        assert len(active) >= 1


# ===========================================================================
# identify_oscillators
# ===========================================================================

class TestIdentifyOscillators:

    def test_empty_components(self):
        assert identify_oscillators({}, {}) == []

    def test_crystal_y_prefix(self):
        components = {"Y1": _comp("16MHz")}
        result = identify_oscillators(components, {})
        xtal = [r for r in result if r["type"] == "crystal_oscillator"]
        assert len(xtal) == 1
        assert xtal[0]["component"] == "Y1"

    def test_crystal_x_prefix(self):
        components = {"X1": _comp("32.768kHz")}
        result = identify_oscillators(components, {})
        xtal = [r for r in result if r["type"] == "crystal_oscillator"]
        assert len(xtal) == 1

    def test_crystal_has_load_caps(self):
        components = {
            "Y1": _comp("8MHz"),
            "C1": _comp("22pF"),
        }
        nets = {
            "XTAL_IN": [
                {"component": "Y1", "pin": "1"},
                {"component": "C1", "pin": "1"},
            ]
        }
        result = identify_oscillators(components, nets)
        xtal = [r for r in result if r["type"] == "crystal_oscillator"]
        assert xtal[0]["has_load_capacitors"] is True

    def test_crystal_no_load_caps(self):
        components = {"Y1": _comp("16MHz")}
        nets = {"XTAL_IN": [{"component": "Y1", "pin": "1"}]}
        result = identify_oscillators(components, nets)
        xtal = [r for r in result if r["type"] == "crystal_oscillator"]
        assert xtal[0]["has_load_capacitors"] is False

    def test_555_timer_rc_oscillator(self):
        components = {"U1": _comp("NE555")}
        result = identify_oscillators(components, {})
        rc = [r for r in result if r["type"] == "rc_oscillator"]
        assert len(rc) == 1
        assert rc[0]["subtype"] == "555_timer"

    @pytest.mark.parametrize("value", ["NE555", "LM555", "ICM7555", "TLC555"])
    def test_555_variants(self, value):
        components = {"U1": _comp(value)}
        result = identify_oscillators(components, {})
        rc = [r for r in result if r["type"] == "rc_oscillator"]
        assert len(rc) == 1

    def test_oscillator_ic_by_value(self):
        components = {"U1": _comp("OSC 16MHz")}
        result = identify_oscillators(components, {})
        osc_ic = [r for r in result if r["type"] == "oscillator_ic"]
        assert len(osc_ic) >= 1

    def test_oscillator_ic_by_lib_id(self):
        components = {"U1": _comp("16MHz", lib_id="Oscillator:SG-531P")}
        result = identify_oscillators(components, {})
        osc_ic = [r for r in result if r["type"] == "oscillator_ic"]
        assert len(osc_ic) >= 1

    def test_crystal_osc_module_classified_once(self):
        # SMD crystal-oscillator module: lib_id contains BOTH "CRYSTAL" and "OSC"
        # (and ref starts with X). The crystal branch and oscillator-IC branch are
        # elif, so it must be classified exactly ONCE — separate `if`s would
        # double-count it with conflicting types.
        components = {"X1": _comp("16MHz", lib_id="Oscillator:SG8002_Crystal_OSC_SMD")}
        result = identify_oscillators(components, {})
        assert len(result) == 1, f"dual-match part double-classified: {result}"
        assert result[0]["component"] == "X1"
        # ref-prefix crystal branch wins (it's first).
        assert result[0]["type"] == "crystal_oscillator"


# ===========================================================================
# identify_digital_interfaces
# ===========================================================================

class TestIdentifyDigitalInterfaces:

    def test_empty(self):
        assert identify_digital_interfaces({}, {}) == []

    def test_i2c_scl_sda(self):
        nets = {"SCL": [], "SDA": []}
        result = identify_digital_interfaces({}, nets)
        types = _result_types(result)
        assert "i2c_interface" in types

    def test_spi_signals(self):
        nets = {"MOSI": [], "MISO": [], "SCK": [], "SS": []}
        result = identify_digital_interfaces({}, nets)
        types = _result_types(result)
        assert "spi_interface" in types

    def test_uart_signals(self):
        nets = {"TX": [], "RX": []}
        result = identify_digital_interfaces({}, nets)
        types = _result_types(result)
        assert "uart_interface" in types

    def test_usb_by_net_name(self):
        nets = {"USB_DP": [], "USB_DM": []}
        result = identify_digital_interfaces({}, nets)
        types = _result_types(result)
        assert "usb_interface" in types

    def test_usb_by_ic_value_ft232(self):
        components = {"U1": _comp("FT232RL")}
        result = identify_digital_interfaces(components, {})
        types = _result_types(result)
        assert "usb_interface" in types

    def test_usb_by_ic_value_ch340(self):
        components = {"U1": _comp("CH340G")}
        result = identify_digital_interfaces(components, {})
        assert "usb_interface" in _result_types(result)

    def test_ethernet_by_phy_w5500(self):
        components = {"U1": _comp("W5500")}
        result = identify_digital_interfaces(components, {})
        assert "ethernet_interface" in _result_types(result)

    def test_ethernet_by_phy_rtl8211f(self):
        # RTL8211F is an Ethernet PHY — should fire ethernet_interface
        components = {"U1": _comp("RTL8211F")}
        result = identify_digital_interfaces(components, {})
        assert "ethernet_interface" in _result_types(result)

    def test_muscle_clk_no_false_i2c(self):
        # Token-split: MUSCLE_CLK should NOT match I2C (no SCL token)
        nets = {"MUSCLE_CLK": []}
        result = identify_digital_interfaces({}, nets)
        assert "i2c_interface" not in _result_types(result)

    def test_bass_ctrl_no_false_spi(self):
        # BASS_CTRL should NOT match SPI SS token
        nets = {"BASS_CTRL": []}
        result = identify_digital_interfaces({}, nets)
        assert "spi_interface" not in _result_types(result)

    def test_mrxd_no_false_uart(self):
        # MRXD should NOT match UART RX token (MRXD != RX)
        nets = {"MRXD": []}
        result = identify_digital_interfaces({}, nets)
        assert "uart_interface" not in _result_types(result)


# ===========================================================================
# identify_sensor_interfaces — the recently patched function
# ===========================================================================

class TestIdentifySensorInterfaces:

    def test_empty(self):
        assert identify_sensor_interfaces({}, {}) == []

    def test_missing_value_key(self):
        # Should not raise KeyError
        result = identify_sensor_interfaces({"U1": {"lib_id": "Sensor:LM35"}}, {})
        assert isinstance(result, list)

    def test_empty_string_value(self):
        result = identify_sensor_interfaces({"U1": _comp("", "")}, {})
        assert result == []

    # --- Specific model detection ---

    def test_ds18b20_specific_branch(self):
        components = {"U1": _comp("DS18B20")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "temperature_sensor"
        assert entry["model"] == "DS18B20"
        assert entry["interface"] == "1-Wire"
        assert entry["range"] == "-55C to +125C"

    def test_lm35_specific_branch(self):
        components = {"U1": _comp("LM35")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "temperature_sensor"
        assert entry["model"] == "LM35"
        assert entry["interface"] == "Analog"

    def test_bme280_multi_sensor(self):
        components = {"U1": _comp("BME280")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "multi_sensor"
        assert "humidity" in entry["measures"]
        assert "temperature" in entry["measures"]
        assert entry["interface"] == "I2C/SPI"

    def test_bmp280_multi_sensor_no_humidity(self):
        # BMP280 does NOT have humidity — check measures list
        components = {"U1": _comp("BMP280")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "multi_sensor"
        assert "humidity" not in entry["measures"]

    def test_mpu6050_specific_branch(self):
        components = {"U1": _comp("MPU6050")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "motion_sensor"
        assert entry["model"] == "MPU6050"
        assert "accelerometer" in entry["measures"]
        assert "gyroscope" in entry["measures"]

    def test_apds9960_optical_sensor(self):
        components = {"U1": _comp("APDS9960")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "optical_sensor"
        assert entry["model"] == "APDS9960"
        assert "gesture" in entry["measures"]

    def test_ads1115_classified_as_adc(self):
        # ADS\d+ was removed from sensor_patterns["voltage"] so the
        # ADS-family routes through the "ADC" key, which has the
        # specific ADS1115 branch with resolution + channel count.
        components = {"U1": _comp("ADS1115")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "analog_interface"
        assert entry["model"] == "ADS1115"
        assert entry["resolution"] == "16-bit"
        assert entry["channels"] == 4
        assert entry["interface"] == "I2C"

    def test_hx711_adc(self):
        components = {"U1": _comp("HX711")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1
        entry = result[0]
        assert entry["type"] == "analog_interface"
        assert entry["model"] == "HX711"
        assert entry["resolution"] == "24-bit"

    # --- Regex alternation order: BME280 appears in BOTH temperature AND humidity ---

    def test_bme280_classified_once_not_twice(self):
        # BME280 matches sensor_patterns["temperature"] AND
        # sensor_patterns["humidity"]. The break + classified_refs guard
        # should produce exactly ONE entry, not two.
        components = {"U1": _comp("BME280")}
        result = identify_sensor_interfaces(components, {})
        assert len(result) == 1, \
            f"BME280 should be classified exactly once, got {len(result)} entries: {result}"

    def test_dht22_classified_once_not_twice(self):
        # DHT22 appears in both temperature AND humidity patterns
        components = {"U1": _comp("DHT22")}
        result = identify_sensor_interfaces(components, {})
        entries_for_u1 = [r for r in result if r.get("component") == "U1"]
        assert len(entries_for_u1) == 1, \
            f"DHT22 should appear once, got: {entries_for_u1}"

    def test_dht11_classified_once_not_twice(self):
        components = {"U1": _comp("DHT11")}
        result = identify_sensor_interfaces(components, {})
        entries_for_u1 = [r for r in result if r.get("component") == "U1"]
        assert len(entries_for_u1) == 1

    def test_si7021_classified_once(self):
        # SI7021 matches both temperature and humidity patterns
        components = {"U1": _comp("SI7021")}
        result = identify_sensor_interfaces(components, {})
        entries_for_u1 = [r for r in result if r.get("component") == "U1"]
        assert len(entries_for_u1) == 1

    # --- BME280 temperature-pattern fires FIRST (before humidity) ---

    def test_bme280_temperature_pattern_fires_first(self):
        # temperature is the first key in sensor_patterns; humidity is second.
        # BME280 should be classified via temperature → multi_sensor, not via
        # humidity → generic humidity_sensor.
        components = {"U1": _comp("BME280")}
        result = identify_sensor_interfaces(components, {})
        assert result[0]["type"] == "multi_sensor", \
            f"Expected multi_sensor (via temperature branch), got {result[0]['type']}"

    # --- double-classification guard: the recently fixed bug ---

    def test_rt_prefix_with_lm35_value_classified_once(self):
        # ref "RT1" would be picked up by the thermistor-prefix pass (startswith("RT"))
        # AND the value "LM35" matches sensor_patterns["temperature"].
        # With the fix, classified_refs prevents the thermistor pass from
        # double-classifying it. Exactly one entry, type=temperature_sensor.
        components = {"RT1": _comp("LM35")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "RT1"]
        assert len(entries) == 1, \
            f"RT1/LM35 should appear exactly once, got: {entries}"
        assert entries[0]["type"] == "temperature_sensor"
        assert entries[0].get("model") == "LM35"  # value-pattern branch, not thermistor

    def test_th_prefix_with_sensor_value_classified_once(self):
        # ref "TH1", value "DS18B20" — TH prefix would fire thermistor, but
        # value-pattern fires first and marks it classified.
        components = {"TH1": _comp("DS18B20")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "TH1"]
        assert len(entries) == 1
        assert entries[0]["type"] == "temperature_sensor"
        assert entries[0].get("model") == "DS18B20"

    def test_pd_prefix_with_sensor_value_classified_once(self):
        # ref "PD1", value "APDS9960" — PD prefix would fire photosensor,
        # but value-pattern fires first.
        components = {"PD1": _comp("APDS9960")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "PD1"]
        assert len(entries) == 1
        assert entries[0]["type"] == "optical_sensor"
        assert entries[0].get("model") == "APDS9960"

    def test_rv_prefix_with_sensor_value_classified_once(self):
        # ref "RV1", value "LM35" — RV prefix would fire potentiometer
        # but value-pattern fires first.
        components = {"RV1": _comp("LM35")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "RV1"]
        assert len(entries) == 1
        # Should be temperature_sensor via value-pattern, NOT potentiometer
        assert entries[0]["type"] == "temperature_sensor"

    # --- Prefix-test traps ---

    def test_rt_prefix_pure_thermistor_when_no_sensor_value(self):
        # ref "RT1", value "10k" — no sensor pattern match, so falls through
        # to thermistor-prefix pass. Should be thermistor.
        components = {"RT1": _comp("10k")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "RT1"]
        assert len(entries) == 1
        assert entries[0]["type"] == "temperature_sensor"
        assert entries[0].get("subtype") == "thermistor"

    def test_rtl8211f_not_misclassified_as_thermistor(self):
        # RTL8211F is an Ethernet PHY, not a thermistor.  The thermistor
        # designator pass now requires ^(RT|TH)\d+$ — pure digit suffix —
        # so refs like "RTL8211F" are correctly excluded.
        components = {"RTL8211F": _comp("RTL8211F")}
        result = identify_sensor_interfaces(components, {})
        rt_entries = [r for r in result if r.get("component") == "RTL8211F"]
        assert rt_entries == []

    def test_potentiometer_rv_prefix(self):
        components = {"RV1": _comp("10k")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "RV1"]
        assert len(entries) == 1
        assert entries[0]["type"] == "position_sensor"
        assert entries[0]["subtype"] == "potentiometer"

    def test_photosensor_ldr_prefix(self):
        components = {"LDR1": _comp("GL5537")}
        result = identify_sensor_interfaces(components, {})
        entries = [r for r in result if r.get("component") == "LDR1"]
        assert len(entries) == 1
        assert entries[0]["type"] == "optical_sensor"
        assert entries[0]["subtype"] == "photosensor"


# ===========================================================================
# identify_microcontrollers
# ===========================================================================

class TestIdentifyMicrocontrollers:

    def test_empty(self):
        assert identify_microcontrollers({}) == []

    def test_missing_value_key(self):
        result = identify_microcontrollers({"U1": {"lib_id": "MCU_Microchip_ATmega:ATmega328P"}})
        assert isinstance(result, list)

    def test_empty_string_value(self):
        result = identify_microcontrollers({"U1": _comp("", "")})
        assert result == []

    def test_atmega328p_not_double_classified_as_dev_board(self):
        # dev_board_patterns["Arduino"] uses (?<!AT)MEGA so the "MEGA"
        # substring in "ATMEGA328P" no longer fires the Arduino board match.
        # ATmega328P should be classified ONCE as a microcontroller.
        components = {"U1": _comp("ATmega328P")}
        result = identify_microcontrollers(components)
        mcu = [r for r in result if r.get("type") == "microcontroller"]
        dev = [r for r in result if r.get("type") == "development_board"]
        assert len(mcu) == 1
        assert mcu[0]["family"] == "AVR"
        assert mcu[0]["model"] == "ATmega328P"
        assert "Arduino" in mcu[0].get("common_usage", "")
        assert dev == []

    def test_atmega32u4_not_double_classified_as_dev_board(self):
        # Same negative-lookbehind fix protects ATmega32U4.
        components = {"U1": _comp("ATmega32U4")}
        result = identify_microcontrollers(components)
        mcu = [r for r in result if r.get("type") == "microcontroller"]
        dev = [r for r in result if r.get("type") == "development_board"]
        assert len(mcu) == 1
        assert mcu[0]["model"] == "ATmega32U4"
        assert dev == []

    def test_arduino_mega_still_classified(self):
        # The negative lookbehind must NOT block legitimate Arduino Mega
        # boards.  "MEGA2560" has no AT prefix → (?<!AT)MEGA still matches.
        components = {"U1": _comp("Arduino MEGA 2560")}
        result = identify_microcontrollers(components)
        dev = [r for r in result if r.get("type") == "development_board"]
        assert len(dev) == 1
        assert dev[0]["board_type"] == "Arduino"

    def test_esp32_specific(self):
        components = {"U1": _comp("ESP32")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        entry = result[0]
        assert entry["model"] == "ESP32"
        assert "Wi-Fi" in entry.get("features", "")

    def test_esp8266_specific(self):
        components = {"U1": _comp("ESP8266")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        assert result[0]["model"] == "ESP8266"

    def test_esp32_not_also_dev_board_nodemcu_pattern(self):
        # An ESP32 component that does NOT match dev-board patterns should
        # only appear once (from the MCU patterns), not also as a dev_board.
        components = {"U1": _comp("ESP32")}
        result = identify_microcontrollers(components)
        dev_board_entries = [r for r in result if r.get("type") == "development_board"]
        mcu_entries = [r for r in result if r.get("type") == "microcontroller"]
        assert len(mcu_entries) == 1
        assert len(dev_board_entries) == 0

    def test_stm32f103_specific(self):
        components = {"U1": _comp("STM32F103C8")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        entry = result[0]
        assert entry["family"] == "STM32"
        assert "STM32F103" in entry["model"]

    def test_rp2040_specific(self):
        components = {"U1": _comp("RP2040")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        entry = result[0]
        assert entry["model"] == "RP2040"
        assert entry["family"] == "RP2040"

    def test_attiny85_generic_avr(self):
        # ATtiny85 matches AVR pattern but has no specific branch — falls to generic
        components = {"U1": _comp("ATtiny85")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        entry = result[0]
        assert entry.get("family") == "AVR"

    def test_pic18f4550_specific(self):
        components = {"U1": _comp("PIC18F4550")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        entry = result[0]
        assert entry["family"] == "PIC"
        assert "PIC18F4550" in entry["model"].upper()

    def test_msp430g2553(self):
        components = {"U1": _comp("MSP430G2553")}
        result = identify_microcontrollers(components)
        assert len(result) == 1
        entry = result[0]
        assert entry["family"] == "MSP430"
        assert "MSP430G2553" in entry["model"].upper()

    def test_arduino_dev_board(self):
        components = {"U1": _comp("Arduino Uno")}
        result = identify_microcontrollers(components)
        dev = [r for r in result if r.get("type") == "development_board"]
        assert len(dev) >= 1
        assert dev[0]["board_type"] == "Arduino"

    def test_mcu_not_double_classified_across_patterns(self):
        # ESP32 matches "ESP" family pattern AND "ARM Cortex" (ESP32 docs mention
        # dual-core Xtensa, not ARM — confirm it only fires ESP, not ARM Cortex)
        components = {"U1": _comp("ESP32")}
        result = identify_microcontrollers(components)
        mcu_entries = [r for r in result if r.get("type") == "microcontroller"]
        assert len(mcu_entries) == 1, \
            f"ESP32 should match exactly one MCU family, got: {mcu_entries}"

    def test_pico_dev_board_raspberry_pi_pattern(self):
        # "PICO" matches the dev_board Raspberry Pi pattern — check it fires
        components = {"U1": _comp("RPICO")}
        result = identify_microcontrollers(components)
        assert any(r.get("type") in ("development_board", "microcontroller") for r in result)


# ===========================================================================
# Pre-flight fixes for placement-project calibration honesty (2026-05-27).
# Each class below pins a specific bug the v0.10.0 prep didn't address.
# ===========================================================================


def _net(pins):
    """Build a synthetic net entry: list of {component, pin}.

    Used by the test classes below that exercise net-topology fixes (RC
    pull-up false positive, digital interface component-ref collection).
    """
    return [{"component": ref, "pin": p} for ref, p in pins]


class TestSensorMCPCollision:
    """identify_sensor_interfaces previously matched any MCP\\d+ as
    voltage_sensor, sweeping up op-amps, GPIO expanders, USB-UARTs."""

    def test_mcp6002_opamp_not_voltage_sensor(self):
        components = {"U1": _comp(value="MCP6002")}
        sensors = identify_sensor_interfaces(components, {})
        types = {s.get("type") for s in sensors}
        assert "voltage_sensor" not in types

    def test_mcp23017_gpio_not_voltage_sensor(self):
        components = {"U1": _comp(value="MCP23017")}
        sensors = identify_sensor_interfaces(components, {})
        types = {s.get("type") for s in sensors}
        assert "voltage_sensor" not in types

    def test_mcp2200_usb_not_voltage_sensor(self):
        components = {"U1": _comp(value="MCP2200")}
        sensors = identify_sensor_interfaces(components, {})
        types = {s.get("type") for s in sensors}
        assert "voltage_sensor" not in types

    def test_mcp9808_temp_not_voltage_sensor(self):
        """MCP9808 is a temp sensor; voltage_sensor would shadow temperature_sensor."""
        components = {"U1": _comp(value="MCP9808")}
        sensors = identify_sensor_interfaces(components, {})
        types = {s.get("type") for s in sensors}
        assert "temperature_sensor" in types
        assert "voltage_sensor" not in types

    def test_ina226_still_classified(self):
        components = {"U1": _comp(value="INA226")}
        sensors = identify_sensor_interfaces(components, {})
        types = {s.get("type") for s in sensors}
        assert "current_sensor" in types or "voltage_sensor" in types

    def test_max_current_regex_narrowed(self):
        """Bare MAX\\d+ used to classify any Maxim part as a current sensor."""
        for non_current in ("MAX17048", "MAX98357A", "MAX7219", "MAX232"):
            sensors = identify_sensor_interfaces({"U1": _comp(value=non_current)}, {})
            assert "current_sensor" not in {s.get("type") for s in sensors}, non_current
        # a real current-sense part is still classified (MAX9928; MAX4xxx parts
        # collide with the broad 'light' MAX4\\d+ regex — a separate overlap)
        sensors = identify_sensor_interfaces({"U1": _comp(value="MAX9928")}, {})
        assert "current_sensor" in {s.get("type") for s in sensors}

    def test_mcp3204_still_classified_as_adc(self):
        """ADC path is unchanged. Emits type='analog_interface'."""
        components = {"U1": _comp(value="MCP3204")}
        sensors = identify_sensor_interfaces(components, {})
        types = {s.get("type") for s in sensors}
        assert "analog_interface" in types


class TestOpAmpMCPOverMatch:
    """identify_amplifiers previously matched MCP\\d{3} which caught
    MCP9808 (temp), MCP1525 (vref), MCP4725 (DAC), MCP3221 (ADC).
    Now uses MCP6\\d{2,3} which is the actual MCP op-amp family."""

    def test_mcp6002_still_classified_as_opamp(self):
        components = {"U1": _comp(value="MCP6002", lib_id="Amplifier_Operational:MCP6002")}
        amps = identify_amplifiers(components, {})
        values = {a.get("value") for a in amps}
        assert "MCP6002" in values

    def test_mcp9808_temp_not_opamp(self):
        components = {"U1": _comp(value="MCP9808")}
        amps = identify_amplifiers(components, {})
        values = {a.get("value") for a in amps}
        assert "MCP9808" not in values

    def test_mcp1525_vref_not_opamp(self):
        components = {"U1": _comp(value="MCP1525")}
        amps = identify_amplifiers(components, {})
        values = {a.get("value") for a in amps}
        assert "MCP1525" not in values

    def test_mcp4725_dac_not_opamp(self):
        components = {"U1": _comp(value="MCP4725")}
        amps = identify_amplifiers(components, {})
        values = {a.get("value") for a in amps}
        assert "MCP4725" not in values

    def test_mcp3221_adc_not_opamp(self):
        components = {"U1": _comp(value="MCP3221")}
        amps = identify_amplifiers(components, {})
        values = {a.get("value") for a in amps}
        assert "MCP3221" not in values


class TestRCFilterPullupFalsePositive:
    """A pull-up R between VCC and SDA + decoupling cap on VCC was
    misclassified as rc_low_pass. Now skipped via is_power_net guard."""

    def test_i2c_pullup_not_rc_filter(self):
        components = {
            "R1": _comp(value="4.7k"),
            "C1": _comp(value="100nF"),
            "U1": _comp(value="MCP23017"),
        }
        nets = {
            "VCC": _net([("R1", "1"), ("C1", "1"), ("U1", "20")]),
            "SDA": _net([("R1", "2"), ("U1", "12")]),
            "GND": _net([("C1", "2"), ("U1", "10")]),
        }
        results = identify_filters(components, nets)
        types = [(f.get("type"), f.get("subtype")) for f in results]
        assert ("passive_filter", "rc_low_pass") not in types

    def test_pullup_to_3v3_not_rc_filter(self):
        components = {
            "R1": _comp(value="10k"),
            "C1": _comp(value="100nF"),
            "U1": _comp(value="MCU"),
        }
        nets = {
            "+3V3": _net([("R1", "1"), ("C1", "1"), ("U1", "1")]),
            "SDA": _net([("R1", "2"), ("U1", "2")]),
            "GND": _net([("C1", "2"), ("U1", "3")]),
        }
        results = identify_filters(components, nets)
        assert all(f.get("subtype") != "rc_low_pass" for f in results)

    def test_real_rc_low_pass_still_detected(self):
        """R between two non-power nets, C to GND. Genuine low-pass."""
        components = {
            "R1": _comp(value="1k"),
            "C1": _comp(value="100nF"),
        }
        nets = {
            "SIGNAL_IN": _net([("R1", "1")]),
            "SIGNAL_OUT": _net([("R1", "2"), ("C1", "1")]),
            "GND": _net([("C1", "2")]),
        }
        results = identify_filters(components, nets)
        types = [(f.get("type"), f.get("subtype")) for f in results]
        assert ("passive_filter", "rc_low_pass") in types


class TestCrystalNotDoubleClassified:
    """identify_filters used to also emit crystal_filter for any Y*/X*
    ref or CRYSTAL/XTAL lib_id. identify_oscillators classifies them too.
    Removed from filters; oscillator-side stays."""

    def test_crystal_not_in_filter_output(self):
        components = {"Y1": _comp(value="16MHz", lib_id="Device:Crystal")}
        nets = {"XTAL1": _net([("Y1", "1")]), "XTAL2": _net([("Y1", "2")])}
        results = identify_filters(components, nets)
        types = {f.get("type") for f in results}
        assert "crystal_filter" not in types

    def test_crystal_still_in_oscillator_output(self):
        components = {"Y1": _comp(value="16MHz", lib_id="Device:Crystal")}
        nets = {"XTAL1": _net([("Y1", "1")]), "XTAL2": _net([("Y1", "2")])}
        results = identify_oscillators(components, nets)
        types = {f.get("type") for f in results}
        assert "crystal_oscillator" in types

    def test_ceramic_filter_still_detected(self):
        components = {"FL1": _comp(value="455kHz", lib_id="Custom:Ceramic_Filter")}
        results = identify_filters(components, {})
        types = {f.get("type") for f in results}
        assert "ceramic_filter" in types


class TestDigitalInterfacesComponents:
    """Each interface entry now has a `components` list (refs touching the
    matched signal nets, plus the IC by ref for value-based detection)."""

    def test_i2c_interface_includes_component_refs(self):
        components = {
            "U1": _comp(value="MCU"),
            "U2": _comp(value="MCP23017"),
            "R1": _comp(value="4.7k"),
            "R2": _comp(value="4.7k"),
        }
        nets = {
            "SCL": _net([("U1", "1"), ("U2", "12"), ("R1", "1")]),
            "SDA": _net([("U1", "2"), ("U2", "13"), ("R2", "1")]),
            "+3V3": _net([("R1", "2"), ("R2", "2")]),
        }
        interfaces = identify_digital_interfaces(components, nets)
        i2c = next(i for i in interfaces if i["type"] == "i2c_interface")
        assert set(i2c["components"]) == {"U1", "U2", "R1", "R2"}

    def test_spi_interface_includes_component_refs(self):
        components = {"U1": _comp(value="MCU"), "U2": _comp(value="W25Q64")}
        nets = {
            "MOSI": _net([("U1", "1"), ("U2", "5")]),
            "MISO": _net([("U1", "2"), ("U2", "2")]),
            "SCK": _net([("U1", "3"), ("U2", "6")]),
            "SPI_CS": _net([("U1", "4"), ("U2", "1")]),
        }
        interfaces = identify_digital_interfaces(components, nets)
        spi = next(i for i in interfaces if i["type"] == "spi_interface")
        assert set(spi["components"]) == {"U1", "U2"}

    def test_usb_ic_detected_by_value_appears_in_components(self):
        """USB IC detected by value (no USB nets) — must appear in components."""
        components = {"U1": _comp(value="FT232RL"), "U2": _comp(value="MCU")}
        nets = {}
        interfaces = identify_digital_interfaces(components, nets)
        usb = next(i for i in interfaces if i["type"] == "usb_interface")
        assert "U1" in usb["components"]


class TestMCP6PatternBoundaries:
    """Boundary coverage for the narrowed op-amp pattern (MCP6\\d{2,3})."""

    def test_mcp602_three_digit_form_matches(self):
        components = {"U1": _comp(value="MCP602")}
        amps = identify_amplifiers(components, {})
        assert any(a.get("value") == "MCP602" for a in amps)

    def test_mcp6001_four_digit_form_matches(self):
        components = {"U1": _comp(value="MCP6001")}
        amps = identify_amplifiers(components, {})
        assert any(a.get("value") == "MCP6001" for a in amps)

    def test_mcp7383_non_opamp_family_does_not_match(self):
        """Boundary: MCP7xxx (not an op-amp family) must NOT match the narrowed pattern."""
        components = {"U1": _comp(value="MCP7383")}
        amps = identify_amplifiers(components, {})
        assert not any(a.get("value") == "MCP7383" for a in amps)


def test_opamp_family_re_shared_and_matches_4digit_ad_and_ina():
    """The op-amp family pattern is one constant shared by identify_amplifiers and
    identify_filters (the filters copy had drifted to AD\\d{3} with no INA, so an
    AD8221/INA active filter went undetected)."""
    import re
    from kicad_mcp.utils.pattern_recognition import _OPAMP_FAMILY_RE
    for part in ("AD8221", "AD8429", "AD8620", "INA128", "INA826", "MCP6002", "TL072", "LM358"):
        assert re.search(_OPAMP_FAMILY_RE, part, re.IGNORECASE), part
    for nonopamp in ("MCP9808", "MCP4725", "MCP3221", "MCP1525"):   # narrowing holds
        assert not re.search(_OPAMP_FAMILY_RE, nonopamp, re.IGNORECASE), nonopamp
