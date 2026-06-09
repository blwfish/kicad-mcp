"""validate_intent — the structural + honesty gate for a DesignIntent however it
was produced (deterministic parser, an AI reading non-C firmware, a hand edit).

The keystone property: validate_intent(build_intent(...)) == [] — the validator's
bar is EXACTLY the deterministic path's guarantees. The rest pin each rejection
(CLAUDE.md threshold rule: the ambiguous/dishonest input is the bug that matters).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.firmware.intent import (
    Bus,
    DesignIntent,
    Endpoint,
    Gap,
    Mcu,
    Net,
    Peripheral,
    build_intent,
    find_board_id,
    validate_intent,
)
from kicad_mcp.utils.firmware.parse import parse_macros, partition

_FIX = Path(__file__).parent / "fixtures" / "firmware"
_ALWAYS = ("power_tree", "decoupling", "pullups", "connectors", "parts")


def _valid() -> DesignIntent:
    """A minimal, honest, hand-authored intent — the shape an AI producer emits."""
    return DesignIntent(
        mcu=Mcu(ref="U1", part="ESP32-WROOM-32E", lib_id="RF_Module:ESP32-WROOM-32E"),
        peripherals=[Peripheral(ref="U2", type="HX711", lib_id="Amplifier_Audio:HX711")],
        nets=[Net(name="HX711_DOUT", kind="peripheral", confidence="high",
                  endpoints=[Endpoint(ref="U1", gpio=16), Endpoint(ref="U2", role="DOUT")])],
        gaps=[Gap(k, "x") for k in _ALWAYS],
    )


def test_valid_intent_passes():
    assert validate_intent(_valid()) == []


# --- keystone: the deterministic producer ALWAYS passes the validator ---------

@pytest.mark.parametrize("cfg", sorted(_FIX.glob("**/config.h")),
                         ids=lambda p: p.parent.name)
def test_build_intent_output_passes_validator(cfg):
    it = build_intent(partition(parse_macros(cfg.read_text())),
                      firmware_path=str(cfg), board_id=find_board_id(str(cfg)))
    assert validate_intent(it) == []


def test_unknown_board_intent_passes_validator():
    # mcu None + an mcu_unknown gap is HONEST -> valid
    it = build_intent(partition(parse_macros("#define I2C_SDA 21\n")),
                      firmware_path="c.h", board_id="rp2040")
    assert it.mcu is None
    assert validate_intent(it) == []


# --- structural rejections ----------------------------------------------------

def test_bad_net_kind_rejected():
    it = _valid()
    it.nets[0].kind = "Bus"   # wrong case — not in NET_KINDS
    assert any("kind" in e for e in validate_intent(it))


def test_bad_confidence_rejected():
    it = _valid()
    it.nets[0].confidence = "medium"
    assert any("confidence" in e for e in validate_intent(it))


def test_dangling_endpoint_ref_rejected():
    it = _valid()
    it.nets[0].endpoints.append(Endpoint(ref="U99", role="X"))
    assert any("U99" in e for e in validate_intent(it))


def test_peripheral_without_lib_id_rejected():
    it = _valid()
    it.peripherals[0].lib_id = None
    assert any("lib_id" in e for e in validate_intent(it))


def test_mcu_without_lib_id_rejected():
    it = _valid()
    it.mcu.lib_id = ""
    assert any("mcu" in e for e in validate_intent(it))


def test_duplicate_ref_rejected():
    it = _valid()
    it.peripherals.append(Peripheral(ref="U2", type="OLED", lib_id="x:y"))
    assert any("duplicate" in e for e in validate_intent(it))


def test_bad_bus_type_rejected():
    it = _valid()
    it.buses.append(Bus(name="FOO", type="CAN", signals={"TX": 1}))
    assert any("CAN" in e or "type" in e for e in validate_intent(it))


@pytest.mark.parametrize("gpio", ["21", 3.3, True])   # str, float, bool: none is a pin number
def test_non_int_gpio_rejected(gpio):
    it = _valid()
    it.nets[0].endpoints[0].gpio = gpio
    assert any("gpio" in e for e in validate_intent(it))


def test_bus_signal_non_int_rejected():
    it = _valid()
    it.buses.append(Bus(name="MIC", type="I2S_IN", signals={"BCLK": "5"}))
    assert any("gpio" in e for e in validate_intent(it))


# --- honesty rejections -------------------------------------------------------

@pytest.mark.parametrize("drop", _ALWAYS)
def test_missing_always_gap_rejected(drop):
    it = _valid()
    it.gaps = [g for g in it.gaps if g.kind != drop]
    assert any(drop in e for e in validate_intent(it))


def test_no_mcu_without_mcu_unknown_rejected():
    it = _valid()
    it.mcu = None
    it.nets = []   # avoid dangling-ref noise; isolate the honesty check
    assert any("mcu_unknown" in e for e in validate_intent(it))


def test_mcu_with_mcu_unknown_is_contradictory():
    it = _valid()
    it.gaps.append(Gap("mcu_unknown", "x"))
    assert any("contradictory" in e for e in validate_intent(it))


def test_example_intent_passes_validator():
    # the published template's example must itself be valid (an AI copying it starts clean)
    from kicad_mcp.utils.firmware.intent import example_intent
    assert validate_intent(example_intent()) == []


def test_contract_value_sets_match_validator_constants():
    from kicad_mcp.utils.firmware.intent import NET_KINDS, contract_value_sets
    c = contract_value_sets()
    assert set(c["net_kind"]) == set(NET_KINDS)
    assert set(c["required_gaps"]) == set(_ALWAYS)
    assert "I2C" in c["bus_type"] and "I2S_IN" in c["bus_type"]
