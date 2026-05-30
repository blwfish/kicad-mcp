"""Tests for offline card auto-draft (Phase 7). Fully offline — symbol access is
via lightweight stubs, so the false-confidence gate (cold-review hazard) is
exercised without KiCad: a pin merely *named* like a role, a mismatch (SCL→SCK),
an uncorroborated address, and a multi-unit symbol must all yield `low`, never
`high`. A draft is returned for review, never placed.
"""
from __future__ import annotations

import pytest

from kicad_mcp.utils.firmware.autodraft import (
    draft_card,
    extract_whoami,
    identify_by_address,
    score_roles,
)


class _Pin:
    def __init__(self, number: str, name: str) -> None:
        self.number, self.name = number, name


class _Sym:
    def __init__(self, pins: list[tuple[str, str]], unit_count: int = 1) -> None:
        self.pins = [_Pin(n, nm) for n, nm in pins]
        self.unit_count = unit_count


# --- address identity ---------------------------------------------------------

@pytest.mark.parametrize("addr,expected", [
    (0x68, "MPU6050"), (0x69, "MPU6050"),
    (0x3C, "OLED"), (0x76, "BME280"), (0x40, "INA219"),
    (0x55, None),       # not a known device address
    (None, None),
])
def test_identify_by_address(addr, expected):
    assert identify_by_address(addr) == expected


# --- WHO_AM_I extraction from provenance --------------------------------------

def test_extract_whoami():
    prov = {"unparsed": [
        {"name": "MPU6050_WHO_AM_I_EXPECTED", "value": "0x68"},
        {"name": "MPU6050_REG_CONFIG", "value": "0x1A"},     # not a whoami
        {"name": "SOME_STRING", "value": '"hi"'},
    ]}
    assert extract_whoami(prov) == {"MPU6050": 0x68}


# --- role scoring (name identity only) ----------------------------------------

def test_score_roles_clean_identity():
    sym = _Sym([("1", "SDA"), ("2", "SCL"), ("3", "VDD"), ("4", "GND")])
    resolved, unresolved = score_roles(sym, {"SDA": "SDA", "SCL": "SCL"})
    assert resolved == {"SDA": "SDA", "SCL": "SCL"} and unresolved == []


def test_score_roles_mismatch_flags_unresolved():
    # MCP-style: the I2C clock pin is named SCK, so role SCL does NOT name-match
    sym = _Sym([("13", "SDA"), ("12", "SCK"), ("9", "V_{DD}")])
    resolved, unresolved = score_roles(sym, {"SDA": "SDA", "SCL": "SCL"})
    assert "SDA" in resolved and unresolved == ["SCL"]


# --- draft confidence gate ----------------------------------------------------

def test_draft_high_requires_corroboration_clean_symbol():
    # address 0x76 corroborates BME280; symbol's only signal pins are SDA/SCL
    sym = _Sym([("1", "SDA"), ("2", "SCL"), ("3", "VDD"), ("4", "GND")])
    d = draft_card(type_name="BME280", address=0x76, bus="I2C",
                   roles={"SDA": "SDA", "SCL": "SCL"}, symbol=sym,
                   lib_id="Sensor:BME280", footprint="FP:BME280")
    assert d is not None and d.confidence == "high"
    assert d.card["roles"] == {"SDA": "SDA", "SCL": "SCL"}


def test_draft_low_on_mismatch():
    sym = _Sym([("13", "SDA"), ("12", "SCK"), ("9", "V_{DD}")])
    d = draft_card(type_name="MCP23017", address=0x20, bus="I2C",
                   roles={"SDA": "SDA", "SCL": "SCL"}, symbol=sym,
                   lib_id="Interface_Expansion:MCP23017x-x-SO", footprint="FP:x")
    assert d is not None and d.confidence == "low"   # SCL unresolved


def test_draft_low_when_address_uncorroborated():
    # clean symbol, but address unknown -> only one signal, stay low
    sym = _Sym([("1", "SDA"), ("2", "SCL"), ("3", "VDD"), ("4", "GND")])
    d = draft_card(type_name="MYSENSOR", address=0x55, bus="I2C",
                   roles={"SDA": "SDA", "SCL": "SCL"}, symbol=sym,
                   lib_id="X:Y", footprint="FP:Y")
    assert d is not None and d.confidence == "low"


def test_draft_low_on_unexplained_pins():
    # extra signal pins the firmware bus can't explain -> identity uncertain
    sym = _Sym([("1", "SDA"), ("2", "SCL"), ("3", "VDD"), ("4", "GND"),
                ("5", "INT"), ("6", "FIFO_WM")])
    d = draft_card(type_name="MPU6050", address=0x68, bus="I2C",
                   roles={"SDA": "SDA", "SCL": "SCL"}, symbol=sym,
                   lib_id="Sensor_Motion:MPU-6050", footprint="FP:x")
    assert d is not None and d.confidence == "low"


def test_draft_low_multiunit():
    sym = _Sym([("1", "SDA"), ("2", "SCL"), ("3", "VDD"), ("4", "GND")], unit_count=2)
    d = draft_card(type_name="BME280", address=0x76, bus="I2C",
                   roles={"SDA": "SDA", "SCL": "SCL"}, symbol=sym,
                   lib_id="Sensor:BME280", footprint="FP:BME280")
    assert d is not None and d.confidence == "low"


def test_draft_skeleton_without_symbol_is_low():
    d = draft_card(type_name="MPU6050", address=0x68, bus="I2C",
                   roles={"SDA": "SDA", "SCL": "SCL"})
    assert d is not None and d.confidence == "low"
    assert any("no local symbol" in r for r in d.reasons)
    # honest placeholders, never a fabricated lib_id
    assert d.card["lib_id"] == "TODO:confirm"


def test_draft_none_when_nothing_to_go_on():
    assert draft_card(type_name="MYSTERY", address=None, bus=None, roles={}) is None


# --- suggest_cards design op (offline, drafts uncarded devices only) ----------

def test_suggest_cards_drafts_uncarded_not_carded(tmp_path):
    from kicad_mcp.tools.design import _op_suggest_cards

    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "config.h").write_text(
        "#define I2C_SDA_PIN 21\n"
        "#define I2C_SCL_PIN 22\n"
        "#define MPU6050_ADDR 0x68\n"        # carded -> placed, NOT drafted
        "#define BME280_ADDR 0x76\n"         # uncarded -> drafted
        "#define BME280_WHO_AM_I_EXPECTED 0x60\n"
    )
    (tmp_path / "platformio.ini").write_text("[env:esp32dev]\nboard = esp32dev\n")

    r = _op_suggest_cards(firmware_path=str(tmp_path))
    assert r["status"] == "ok"
    types = {d["card"]["type"] for d in r["drafts"]}
    assert types == {"BME280"}                       # MPU6050 is carded -> skipped
    bme = next(d for d in r["drafts"] if d["card"]["type"] == "BME280")
    assert bme["confidence"] == "low"                # no symbol -> flagged
    assert bme["card"]["bus"] == "I2C"
    assert bme["card"]["roles"] == {"SDA": "SDA", "SCL": "SCL"}
    assert any("0x76" in r_ or "BME280" in r_ for r_ in bme["reasons"])
    assert any("WHO_AM_I" in r_ for r_ in bme["reasons"])   # 0x60 hint surfaced
