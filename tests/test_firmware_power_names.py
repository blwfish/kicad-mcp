"""Tests for power_names — the single source of truth for pin-name classification
(consolidated from autodraft/prefetch/sidecar/cards). The regressions that
motivated it: the old supply pattern missed VDDIO/VDDA (the names real I2C IMUs
use), so the pre-fetch never auto-synthesized them.
"""
from __future__ import annotations

import pytest

from kicad_mcp.utils.firmware.power_names import (
    RAILS,
    is_control_pin,
    is_ground_pin,
    is_nc_pin,
    is_supply_pin,
)


@pytest.mark.parametrize("name", [
    "VDD", "VCC", "VDDIO", "VDDA", "VDD_IO", "VCCIO", "V_{DD}", "AVDD", "DVDD",
    "VBAT", "VIN", "3V3", "5V", "1V8", "+3V3",
])
def test_supply_pins(name):
    assert is_supply_pin(name)


@pytest.mark.parametrize("name", ["SDA", "SCL", "INT", "GND", "NC", "OUT"])
def test_non_supply(name):
    assert not is_supply_pin(name)


@pytest.mark.parametrize("name", ["GND", "VSS", "AGND", "DGND", "GNDA", "GNDIO",
                                  "V_{SS}", "EP", "EPAD", "PGND"])
def test_ground_pins(name):
    assert is_ground_pin(name)


@pytest.mark.parametrize("name", ["INT", "INT1", "INT2", "INTA", "nINT", "DRDY",
                                  "ALERT", "OS"])
def test_control_pins(name):
    assert is_control_pin(name)


def test_address_strap_pins_are_NOT_control():
    # AD0 / A0..A2 / CS genuinely need wiring -> must stay flagged, not "explained"
    for n in ("AD0", "A0", "A1", "A2", "CS", "FSYNC", "CLKIN"):
        assert not is_control_pin(n)


@pytest.mark.parametrize("name", ["NC", "DNC", "DNI", "~", "N/C", ""])
def test_nc_pins(name):
    assert is_nc_pin(name)


def test_rails_superset():
    assert {"+3V3", "+5V", "GND", "VBUS", "VCC"} <= RAILS
