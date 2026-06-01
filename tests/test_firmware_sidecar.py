"""Tests for the board.yaml sidecar (Phase 6b) — firmware-blind facts supplied
as data. Covers load/validate edges, gap resolution by provenance, connector
materialization (new net vs joining an existing rail), and auto-detection.
"""
from __future__ import annotations

import textwrap

import pytest

from kicad_mcp.utils.firmware.intent import build_intent
from kicad_mcp.utils.firmware.parse import parse_defines, partition
from kicad_mcp.utils.firmware.sidecar import (
    BoardSidecar,
    SidecarError,
    apply_sidecar,
    find_sidecar,
    load_sidecar,
)

_FW = "#define I2C_SDA_PIN 21\n#define I2C_SCL_PIN 22\n#define MPU6050_ADDR 0x68\n"


def _intent():
    return build_intent(partition(parse_defines(_FW)),
                        firmware_path="c.h", board_id="esp32dev")


def _write(tmp_path, body):
    p = tmp_path / "board.yaml"
    p.write_text(textwrap.dedent(body))
    return str(p)


# --- load + validate ----------------------------------------------------------

def test_load_valid(tmp_path):
    sc = load_sidecar(_write(tmp_path, """\
        power_source: barrel
        board_size_mm: [90, 75]
        extra_connectors:
          - ref: J_PWR
            lib_id: Connector:Barrel_Jack
            footprint: Connector_BarrelJack:BarrelJack_Horizontal
            nets: {"1": "+5V", "2": "GND"}
    """))
    assert sc.power_source == "barrel" and sc.board_size_mm == [90, 75]
    assert sc.extra_connectors[0]["ref"] == "J_PWR"


def test_empty_sidecar_ok(tmp_path):
    assert load_sidecar(_write(tmp_path, "")) == BoardSidecar()


@pytest.mark.parametrize("body,needle", [
    ("power_source: solar\n", "power_source"),
    ("board_size_mm: [90]\n", "board_size_mm"),
    ("extra_connectors:\n  - ref: J\n    nets: {}\n", "lib_id"),      # missing lib_id
    ("extra_connectors:\n  - ref: J\n    lib_id: X:Y\n", "nets"),     # missing nets
    ("extra_connectors:\n  - ref: J\n    lib_id: X:Y\n    nets: {}\n", "nets"),  # empty nets
])
def test_malformed_raises(tmp_path, body, needle):
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, body))
    assert needle in str(e.value)


# --- apply --------------------------------------------------------------------

def test_apply_adds_connector_and_resolves_gap():
    i = _intent()
    assert any(g.kind == "connectors" and not g.resolved for g in i.gaps)
    sc = BoardSidecar(extra_connectors=[{
        "ref": "J_PWR", "lib_id": "Connector:Barrel_Jack",
        "footprint": "FP:Jack", "nets": {"1": "+5V", "2": "GND"}}])
    apply_sidecar(i, sc)
    # friendly ref "J_PWR" is normalized to a KiCad-valid ref; name kept as value
    conn = next(p for p in i.peripherals if p.type == "CONN")
    assert conn.ref == "J1" and conn.value == "J_PWR" and conn.origin == "user"
    gap = next(g for g in i.gaps if g.kind == "connectors")
    assert gap.resolved and gap.resolved_by == "board.yaml"
    assert gap.resolved_components == ["J1"]


@pytest.mark.parametrize("ref,existing,expected", [
    ("J1", set(), "J1"),                 # already valid -> unchanged
    ("J1", {"J1"}, "J2"),                # VALID ref that COLLIDES -> reallocated
    ("U2", {"U1", "U2"}, "U3"),          # collides with an imported peripheral
    ("J_PWR", set(), "J1"),              # friendly -> prefix + next free
    ("J_PWR", {"J1"}, "J2"),             # avoid collision
    ("PWR", set(), "PWR1"),              # letter prefix kept
])
def test_normalize_ref(ref, existing, expected):
    from kicad_mcp.utils.firmware.sidecar import _normalize_ref
    assert _normalize_ref(ref, set(existing)) == expected


def test_footprint_required(tmp_path):
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, """\
            extra_connectors:
              - ref: J1
                lib_id: Connector:Conn_01x02
                nets: {"1": "+5V", "2": "GND"}
        """))
    assert "footprint" in str(e.value)


@pytest.mark.parametrize("lib_id,ok", [
    ("Connector:Conn_01x02", True), ("X:Y", True),
    (":", False), ("Lib:", False), (":Sym", False), ("NoColon", False),
])
def test_lib_id_validation(tmp_path, lib_id, ok):
    body = f"""\
        extra_connectors:
          - ref: J1
            lib_id: "{lib_id}"
            footprint: FP:X
            nets: {{"1": "+5V"}}
    """
    if ok:
        load_sidecar(_write(tmp_path, body))     # no raise
    else:
        with pytest.raises(SidecarError) as e:
            load_sidecar(_write(tmp_path, body))
        assert "lib_id" in str(e.value)


def test_import_op_catches_malformed_sidecar(tmp_path):
    from kicad_mcp.tools.design import _op_import
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "config.h").write_text("#define I2C_SDA_PIN 21\n#define I2C_SCL_PIN 22\n")
    (tmp_path / "platformio.ini").write_text("[env:esp32dev]\nboard = esp32dev\n")
    (inc / "board.yaml").write_text("power_source: solar\n")   # invalid
    r = _op_import(firmware_path=str(inc / "config.h"), out_path=str(tmp_path / "i.yaml"))
    assert r["status"] == "error" and r["code"] == "invalid_sidecar"


def test_apply_joins_existing_rail_else_creates():
    i = _intent()
    # pre-seed a +5V net so the connector JOINS it instead of duplicating
    from kicad_mcp.utils.firmware.intent import Endpoint, Net
    i.nets.append(Net(name="+5V", kind="power", confidence="high",
                      endpoints=[Endpoint(ref="U1", pin="VDD")]))
    sc = BoardSidecar(extra_connectors=[{
        "ref": "J1", "lib_id": "Connector:Conn_01x02", "nets": {"1": "+5V", "2": "GND"}}])
    apply_sidecar(i, sc)
    plus5 = next(n for n in i.nets if n.name == "+5V")
    assert {e.ref for e in plus5.endpoints} == {"U1", "J1"}        # joined, not duplicated
    assert any(n.name == "GND" for n in i.nets)                    # GND created


def test_apply_records_power_and_size():
    i = _intent()
    apply_sidecar(i, BoardSidecar(power_source="usb_c", board_size_mm=[80, 60]))
    assert i.source["power_source"] == "usb_c" and i.source["board_size_mm"] == [80, 60]


def test_load_placement_hints(tmp_path):
    """A top-level placement_hints: block parses into the sidecar (stored raw;
    per-directive validation is deferred to build time / normalize_hint)."""
    sc = load_sidecar(_write(tmp_path, """\
        placement_hints:
          J6: {edge: none}
          J1: {edge: left, rotation: 90}
        """))
    assert sc.placement_hints == {
        "J6": {"edge": "none"},
        "J1": {"edge": "left", "rotation": 90},
    }


def test_apply_writes_placement_hints_into_intent():
    i = _intent()
    apply_sidecar(i, BoardSidecar(placement_hints={"J6": {"edge": "none"}}))
    assert i.placement_hints["J6"] == {"edge": "none"}


# --- auto-detection -----------------------------------------------------------

def test_find_sidecar_next_to_config(tmp_path):
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "config.h").write_text("#define X 1\n")
    assert find_sidecar(str(inc / "config.h")) is None
    (inc / "board.yaml").write_text("power_source: usb_c\n")
    assert find_sidecar(str(inc / "config.h")) == str(inc / "board.yaml")


def test_find_sidecar_one_dir_up(tmp_path):
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "config.h").write_text("#define X 1\n")
    (tmp_path / "board.yaml").write_text("power_source: usb_c\n")
    assert find_sidecar(str(inc / "config.h")) == str(tmp_path / "board.yaml")
