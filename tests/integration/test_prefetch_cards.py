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


def test_prefetch_generates_valid_high_cards(tmp_path):
    import sys
    sys.path.insert(0, "scripts")
    from prefetch_cards import main

    from kicad_mcp.utils.firmware.cards import validate_peripheral_card

    if not os.path.isdir(_SYMBOLS):
        pytest.skip("KiCad symbols dir not found")

    rc = main(["--symbols-dir", _SYMBOLS,
               "--libraries", "Sensor_Humidity", "Sensor_Temperature",
               "--out", str(tmp_path), "--min-confidence", "high"])
    assert rc == 0

    cards = glob.glob(str(tmp_path / "*.yaml"))
    assert cards, "pre-fetch produced no high-confidence cards from temp/humidity sensors"
    for f in cards:
        c = yaml.safe_load(open(f).read())
        c.pop("_draft", None)
        assert validate_peripheral_card(c) == [], f"{f} failed structural validation"
        # high-confidence => real I2C bus + power pins (turnkey except footprint)
        assert c["bus"] == "I2C"
        assert c["roles"] == {"SDA": "SDA", "SCL": "SCL"}
        assert c["supply_pins"] and c["ground_pins"]
