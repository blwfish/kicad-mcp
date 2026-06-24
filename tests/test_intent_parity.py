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

# Collection-time guard: an empty glob would make the parametrized corpus tests
# collect 0 items and "pass" vacuously. Fail loudly instead.
_CFGS = sorted(_FIX.glob("**/config.h"))
if not _CFGS:
    raise RuntimeError(f"no firmware config.h fixtures under {_FIX} — "
                       "parity corpus tests would vacuously pass")

_BASE_GAPS = [Gap(k, "") for k in ("power_tree", "decoupling", "pullups",
                                   "connectors", "parts")]


def _det(cfg: Path) -> DesignIntent:
    return build_intent(partition(parse_macros(cfg.read_text())),
                        firmware_path=str(cfg), board_id=find_board_id(str(cfg)))


@pytest.mark.parametrize("cfg", _CFGS, ids=lambda p: p.parent.name)
def test_parity_identity(cfg):
    # Compare against a DEEP COPY, not the same object: a same-object compare is
    # satisfied by `return []` and proves nothing. A deepcopy still expects [] but
    # exercises the comparator's value-equality on independent structures.
    it = _det(cfg)
    assert intent_parity(it, copy.deepcopy(it)) == []


def test_parity_is_ref_independent():
    # a producer may assign different ref NAMES; parity ignores the names (it keys
    # endpoints on the target's TYPE). Rename CONSISTENTLY (ref + its endpoints), so
    # `other` is itself a valid intent — not a dangling-ref one.
    it = _det(_FIX / "config.h")
    other = copy.deepcopy(it)
    rename = {p.ref: "X" + p.ref for p in other.peripherals}
    for p in other.peripherals:
        p.ref = rename[p.ref]
    for n in other.nets:
        for e in n.endpoints:
            e.ref = rename.get(e.ref, e.ref)   # MCU ref unchanged
    assert validate_intent(other) == []        # the rename produced a valid intent
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


def test_parity_handles_mixed_addresses_without_crash():
    # A3 regression: same-type peripherals, one addr=None one addr=int, must not raise
    g = [Gap(k, "") for k in ("power_tree", "decoupling", "pullups", "connectors", "parts")]
    a = DesignIntent(
        mcu=Mcu("U1", "ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E"),
        peripherals=[Peripheral("U2", "HX711", "x:y", address=None),
                     Peripheral("U3", "HX711", "x:y", address=1)], gaps=g)
    assert intent_parity(a, copy.deepcopy(a)) == []


def test_parity_catches_net_wired_to_wrong_chip():
    # A2: identical peripheral sets, but the net targets a DIFFERENT chip type
    g = [Gap(k, "") for k in ("power_tree", "decoupling", "pullups", "connectors", "parts")]
    m = Mcu("U1", "ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E")
    a = DesignIntent(mcu=m,
        peripherals=[Peripheral("U2", "HX711", "x:y"), Peripheral("U3", "MCP23017", "x:y")],
        nets=[Net("N", "peripheral", "high", [Endpoint("U1", gpio=16), Endpoint("U2", role="DOUT")])],
        gaps=g)
    b = DesignIntent(mcu=m,
        peripherals=[Peripheral("U2", "HX711", "x:y"), Peripheral("U3", "MCP23017", "x:y")],
        nets=[Net("N", "peripheral", "high", [Endpoint("U1", gpio=16), Endpoint("U3", role="DOUT")])],
        gaps=g)
    assert intent_parity(a, b)   # net to HX711 vs MCP23017 -> not parity


def test_parity_ignores_peripheral_lib_id_by_design():
    # documented: lib_id is a symbol-library choice, not a firmware fact -> not compared
    g = [Gap(k, "") for k in ("power_tree", "decoupling", "pullups", "connectors", "parts")]
    m = Mcu("U1", "ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E")
    a = DesignIntent(mcu=m, peripherals=[Peripheral("U2", "HX711", "Analog_ADC:HX711")], gaps=g)
    b = DesignIntent(mcu=m, peripherals=[Peripheral("U2", "HX711", "Wrong:Symbol")], gaps=g)
    assert intent_parity(a, b) == []


# --- the endpoint-signature key has four dimensions (gpio, role, pin, target-type);
#     a mismatch on EACH must break parity, else a dropped key would go unnoticed ----

def _two_chip(net: Net) -> DesignIntent:
    return DesignIntent(mcu=Mcu("U1", "ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E"),
                        peripherals=[Peripheral("U2", "HX711", "x:y")],
                        nets=[net], gaps=list(_BASE_GAPS))


def test_parity_detects_endpoint_gpio_mismatch():
    a = _two_chip(Net("N", "peripheral", "high",
                      [Endpoint("U1", gpio=16), Endpoint("U2", role="DOUT")]))
    b = _two_chip(Net("N", "peripheral", "high",
                      [Endpoint("U1", gpio=17), Endpoint("U2", role="DOUT")]))
    assert intent_parity(a, b)   # same chip/role, different gpio -> not parity


def test_parity_detects_endpoint_role_mismatch():
    a = _two_chip(Net("N", "peripheral", "high",
                      [Endpoint("U1", gpio=16), Endpoint("U2", role="DOUT")]))
    b = _two_chip(Net("N", "peripheral", "high",
                      [Endpoint("U1", gpio=16), Endpoint("U2", role="SCK")]))
    assert intent_parity(a, b)   # same chip/gpio, different role -> not parity


def test_parity_compares_gap_kinds_as_a_set_not_a_multiset():
    # DOCUMENTED, intentional (F7): parity compares the SET of gap kinds, not their
    # multiplicity — one unknown_peripheral gap reaches parity with six. An AI may
    # legitimately bundle "these N are unknown" into a single disclosure, and the
    # board-affecting case (a dropped connection) is caught by the NET comparison,
    # not the gap count. Pinned so a future reader doesn't assume counts are compared
    # — switching to a Counter is then a conscious change, not an accident.
    m = Mcu("U1", "ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E")
    one = DesignIntent(mcu=m, gaps=_BASE_GAPS + [Gap("unknown_peripheral", "SENSOR")])
    six = DesignIntent(mcu=m, gaps=_BASE_GAPS + [Gap("unknown_peripheral", f"X{i}")
                                                 for i in range(6)])
    assert intent_parity(one, six) == []


def test_parity_detects_net_bus_mismatch():
    # same kind + endpoints, different bus tag: an I2C-tagged net drives i2c_pullups,
    # an untagged one does not -> the two expand to DIFFERENT boards -> not parity.
    a = _two_chip(Net("SDA", "bus", "high",
                      [Endpoint("U1", gpio=21), Endpoint("U2", role="SDA")], bus="I2C"))
    b = _two_chip(Net("SDA", "bus", "high",
                      [Endpoint("U1", gpio=21), Endpoint("U2", role="SDA")], bus=None))
    assert intent_parity(a, b)


def test_parity_surfaces_duplicate_net_names_instead_of_dropping_one():
    # M1 (release-gate): the comparator keys nets by name, so a duplicate name in
    # `other` would silently overwrite — one net (and its endpoints) vanishing from
    # the check, hiding a real difference. intent_parity does NOT require valid input
    # (validate_intent is the gate for that), so it must surface the collision.
    g = [Gap(k, "") for k in ("power_tree", "decoupling", "pullups", "connectors", "parts")]
    m = Mcu("U1", "ESP32-WROOM-32E", "RF_Module:ESP32-WROOM-32E")
    a = DesignIntent(mcu=m, peripherals=[Peripheral("U2", "HX711", "x:y")],
                     nets=[Net("N", "peripheral", "high",
                               [Endpoint("U1", gpio=16), Endpoint("U2", role="DOUT")])],
                     gaps=g)
    # `other` has TWO nets named "N"; the second would clobber the first in a dict
    dup = copy.deepcopy(a)
    dup.nets.append(Net("N", "orphan", "low", [Endpoint("U1", gpio=17)]))
    diffs = intent_parity(a, dup)
    assert any("duplicate net names" in d for d in diffs)
