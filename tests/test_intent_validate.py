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

# Collection-time guard: a moved/renamed fixture dir would make sorted(glob(...))
# empty -> the parametrized corpus tests collect 0 items and "pass" vacuously. Fail
# loudly instead (mirrors the integration tier's _CORPUS guard).
_CFGS = sorted(_FIX.glob("**/config.h"))
if not _CFGS:
    raise RuntimeError(f"no firmware config.h fixtures under {_FIX} — "
                       "corpus tests would vacuously pass")


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

@pytest.mark.parametrize("cfg", _CFGS, ids=lambda p: p.parent.name)
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


@pytest.mark.parametrize("cfg", _CFGS, ids=lambda p: p.parent.name)
def test_expanded_intent_passes_validator(cfg):
    # the EXPANDED intent (template-synthesized peripherals + power/passive nets)
    # must also pass — so the new structural rules don't reject a legit post-expand
    # doc (which show_intent / a re-validate would see).
    from kicad_mcp.utils.firmware.templates import expand_intent
    it = build_intent(partition(parse_macros(cfg.read_text())),
                      firmware_path=str(cfg), board_id=find_board_id(str(cfg)))
    assert validate_intent(expand_intent(it)) == []


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


# --- cold-review fixes: bus-type/signals, modeled-net arity, dup names, gpio range, locus ---

def test_mistyped_bus_rejected():
    it = _valid()
    it.buses.append(Bus(name="MIC", type="I2S_IN", signals={"FOO": 1}))  # not BCLK/WS/SD
    assert any("form a" in e for e in validate_intent(it))


def test_well_typed_bus_passes():
    it = _valid()
    it.buses.append(Bus(name="MIC", type="I2S_IN", signals={"BCLK": 4, "WS": 5, "SD": 6}))
    assert validate_intent(it) == []


def test_modeled_net_needs_two_endpoints():
    it = _valid()
    it.nets.append(Net("SOLO", "peripheral", "high", [Endpoint(ref="U2", role="X")]))
    assert any("endpoints" in e for e in validate_intent(it))


def test_orphan_net_may_have_one_endpoint():
    it = _valid()
    it.nets.append(Net("ORPH", "orphan", "low", [Endpoint(ref="U1", gpio=5)]))
    assert validate_intent(it) == []


def test_duplicate_net_name_rejected():
    it = _valid()
    it.nets.append(Net("HX711_DOUT", "orphan", "low", [Endpoint(ref="U1", gpio=9)]))
    assert any("duplicate net" in e for e in validate_intent(it))


@pytest.mark.parametrize("gpio", [-1, 49, 999])
def test_gpio_out_of_range_rejected(gpio):
    it = _valid()
    it.nets[0].endpoints[0].gpio = gpio
    assert any("range" in e for e in validate_intent(it))


def test_bad_peripheral_locus_rejected():
    it = _valid()
    it.peripherals[0].locus = "floor"
    assert any("locus" in e for e in validate_intent(it))


def test_example_i2s_passes_validator():
    from kicad_mcp.utils.firmware.intent import example_intent_i2s
    assert validate_intent(example_intent_i2s()) == []


# --- threshold boundaries the validator must pin (the <= contract, both ends) ----

@pytest.mark.parametrize("gpio", [0, 48])   # GPIO_MIN, GPIO_MAX — both INCLUSIVE
def test_gpio_at_boundary_is_valid(gpio):
    # guards the <= vs < contract on each end: a change to strict-< would reject
    # these and only an at-boundary VALID test catches it.
    it = _valid()
    it.nets[0].endpoints[0].gpio = gpio
    assert validate_intent(it) == []


@pytest.mark.parametrize("gpio", [-1, 49, 99])
def test_bus_signal_gpio_out_of_range_rejected(gpio):
    # symmetry with endpoint gpios: an out-of-range BUS signal pin must be rejected
    # too, else expand wires a pin generate can't resolve (silent unresolved endpoint).
    it = _valid()
    it.buses.append(Bus(name="MIC", type="I2S_IN", signals={"BCLK": 4, "WS": 5, "SD": gpio}))
    assert any("range" in e for e in validate_intent(it))


def test_modeled_net_with_zero_endpoints_rejected():
    # the <2 check has two sub-cases (0 and 1); the 1-endpoint case is pinned
    # elsewhere — pin the 0-endpoint case too.
    it = _valid()
    it.nets.append(Net("EMPTY", "peripheral", "high", []))
    assert any("endpoints" in e for e in validate_intent(it))


def test_bus_signals_form_a_different_valid_type_rejected():
    # NOT garbage signals (that case is test_mistyped_bus_rejected) — signals that
    # form a real but DIFFERENT bus type than declared: {BCLK,LRC,DIN} is I2S_OUT,
    # declared here as I2S_IN (the exact in/out swap). Expanding the wrong template
    # would build the wrong device; reject it.
    it = _valid()
    it.buses.append(Bus(name="X", type="I2S_IN", signals={"BCLK": 4, "LRC": 5, "DIN": 6}))
    errs = validate_intent(it)
    assert any("form a" in e for e in errs), errs


def test_example_i2s_expands_to_one_wired_ics43434():
    # F1 regression: the published worked example must expand to EXACTLY the mic it
    # declares — one ICS-43434, fully wired (BCLK/WS/SD), no phantom SPH0645 default,
    # no spurious assumed_part gap. A spec-valid intent that builds the wrong board is
    # the exact bug class this phase exists to prevent, and validate_intent passing
    # (== []) is not enough — assert the COMPOSITION, not just structural validity.
    from kicad_mcp.utils.firmware.intent import example_intent_i2s
    from kicad_mcp.utils.firmware.templates import expand_intent
    ex = expand_intent(example_intent_i2s())
    mics = [p.type for p in ex.peripherals if p.type in ("ICS43434", "SPH0645")]
    assert mics == ["ICS43434"], mics
    mic_ref = next(p.ref for p in ex.peripherals if p.type == "ICS43434")
    signal_nets = {n.name for n in ex.nets
                   if any(e.ref == mic_ref for e in n.endpoints)
                   and n.name.startswith("MIC_")}
    assert signal_nets == {"MIC_BCLK", "MIC_WS", "MIC_SD"}, signal_nets
    assert "assumed_part" not in {g.kind for g in ex.gaps}
    assert validate_intent(ex) == []
