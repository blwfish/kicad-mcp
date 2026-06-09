"""Gap-parity harness (Phase 1b): the two intent producers — the deterministic
parser and an AI/hand author — must CONVERGE on firmware both can express. Tests
the reusable intent_parity() comparator (it detects real differences and is
ref-independent) and demonstrates that an independently hand-authored intent
reaches parity with build_intent on the same firmware (so the published contract
is expressive enough to reproduce the deterministic output).
"""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from kicad_mcp.utils.firmware.intent import (
    DesignIntent,
    Endpoint,
    Gap,
    Mcu,
    Net,
    Peripheral,
    build_intent,
    find_board_id,
    from_dict,
    intent_parity,
    to_dict,
    validate_intent,
)
from kicad_mcp.utils.firmware.parse import parse_macros, partition

_FIX = Path(__file__).parent / "fixtures" / "firmware"


def _det(cfg: Path) -> DesignIntent:
    return build_intent(partition(parse_macros(cfg.read_text())),
                        firmware_path=str(cfg), board_id=find_board_id(str(cfg)))


@pytest.mark.parametrize("cfg", sorted(_FIX.glob("**/config.h")),
                         ids=lambda p: p.parent.name)
def test_parity_identity(cfg):
    it = _det(cfg)
    assert intent_parity(it, it) == []


def test_parity_is_ref_independent():
    # a producer may assign refs differently; parity must ignore them
    it = _det(_FIX / "config.h")
    other = copy.deepcopy(it)
    for p in other.peripherals:
        p.ref = "X" + p.ref          # rename refs (parity drops them)
    assert intent_parity(it, other) == []


def test_parity_roundtrip_via_contract():
    # an AI emitting the same structure as YAML must round-trip to parity
    it = _det(_FIX / "config.h")
    assert intent_parity(it, from_dict(to_dict(it))) == []


def test_parity_detects_real_differences():
    it = _det(_FIX / "config.h")
    kind = copy.deepcopy(it); kind.nets[0].kind = "orphan" if kind.nets[0].kind != "orphan" else "bus"
    assert intent_parity(it, kind)
    fewer = copy.deepcopy(it); fewer.peripherals = fewer.peripherals[:-1]
    assert intent_parity(it, fewer)
    gapped = copy.deepcopy(it); gapped.gaps.append(Gap("made_up_kind", "x"))
    assert intent_parity(it, gapped)
    nomcu = copy.deepcopy(it); nomcu.mcu = None
    assert intent_parity(it, nomcu)


def test_independent_authoring_reaches_parity():
    """A hand-authored intent (the AI stand-in) for a small firmware reaches parity
    with build_intent — and is itself valid. Proves the contract can reproduce the
    deterministic structure without copying its refs/ordering."""
    fw = ("#define I2C_SDA 21\n#define I2C_SCL 22\n"
          "#define HX711_DOUT_PIN 16\n#define HX711_SCK_PIN 17\n")
    det = build_intent(partition(parse_macros(fw)), firmware_path="x.h", board_id="esp32dev")

    ai = DesignIntent(
        mcu=Mcu(ref="U1", part="ESP32-WROOM-32E", lib_id="RF_Module:ESP32-WROOM-32E"),
        peripherals=[Peripheral(ref="U2", type="HX711", lib_id="Analog_ADC:HX711")],
        nets=[
            Net("I2C_SDA", "orphan", "low", [Endpoint(ref="U1", gpio=21)]),
            Net("I2C_SCL", "orphan", "low", [Endpoint(ref="U1", gpio=22)]),
            Net("HX711_DOUT", "peripheral", "high",
                [Endpoint(ref="U1", gpio=16), Endpoint(ref="U2", role="DOUT")]),
            Net("HX711_SCK", "peripheral", "high",
                [Endpoint(ref="U1", gpio=17), Endpoint(ref="U2", role="SCK")]),
        ],
        gaps=[Gap(k, "x") for k in ("power_tree", "decoupling", "pullups", "connectors",
                                    "parts", "unknown_peripheral")],
    )
    assert validate_intent(ai) == []
    assert intent_parity(det, ai) == []
