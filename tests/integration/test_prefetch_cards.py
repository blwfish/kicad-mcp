"""Integration test for the maintainer-time pre-fetch CLI (Phase 8) — needs the
installed KiCad symbol libraries. Proves the whole path runs on real symbols:
enumerate → synthesize → validate → write, and that auto-generated high-confidence
cards are structurally valid with real power pins. Gated by KICAD_INTEGRATION=1.
"""
import glob
import os

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)

_SYMBOLS = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"


def test_prefetch_path_runs_low_drafts_valid_high_needs_corroboration(tmp_path):
    import sys
    sys.path.insert(0, "scripts")
    from prefetch_cards import main

    from kicad_mcp.utils.firmware.cards import validate_peripheral_card

    if not os.path.isdir(_SYMBOLS):
        pytest.skip("KiCad symbols dir not found")

    libs = ["--symbols-dir", _SYMBOLS,
            "--libraries", "Sensor_Humidity", "Sensor_Temperature"]

    # h-prefetch-corroboration: the bulk CLI supplies no per-symbol I2C address,
    # so identity is never corroborated -> nothing auto-ships 'high' (pin names
    # like SDA/SCL are not proof of an I2C device). 'high' is reserved for cards
    # a human confirms.
    high_dir = tmp_path / "high"
    high_dir.mkdir()
    assert main(libs + ["--out", str(high_dir), "--min-confidence", "high"]) == 0
    assert glob.glob(str(high_dir / "*.yaml")) == [], \
        "bulk pre-fetch must not auto-ship 'high' cards without address corroboration"

    # The enumerate -> synthesize -> validate -> write path still works at 'low':
    # it produces structurally-valid draft cards, including clean I2C drafts with
    # real power pins (the would-be-high-but-for-corroboration sensors).
    low_dir = tmp_path / "low"
    low_dir.mkdir()
    assert main(libs + ["--out", str(low_dir), "--min-confidence", "low"]) == 0
    cards = glob.glob(str(low_dir / "*.yaml"))
    assert cards, "pre-fetch produced no draft cards from temp/humidity sensors"

    clean_i2c_with_power = 0
    for f in cards:
        c = yaml.safe_load(open(f).read())
        c.pop("_draft", None)
        assert validate_peripheral_card(c) == [], f"{f} failed structural validation"
        if (c["bus"] == "I2C" and c["roles"] == {"SDA": "SDA", "SCL": "SCL"}
                and c["supply_pins"] and c["ground_pins"]):
            clean_i2c_with_power += 1
    assert clean_i2c_with_power, \
        "expected at least one clean I2C draft with power among the low cards"
