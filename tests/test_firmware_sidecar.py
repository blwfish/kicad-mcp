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
    # M3 (release-gate): unknown SUB-keys in placement / extra_connectors entries.
    # The header's "unknown keys REJECTED" contract previously covered only the
    # TOP-LEVEL keys; a typo'd sub-key degraded the directive silently.
    ("placement:\n  U2: {locus: remote, devcie: INMP441}\n", "unknown key"),
    ("extra_connectors:\n  - ref: J1\n    lib_id: X:Y\n    footprint: F:P\n"
     "    nets: {1: VCC}\n    desc: barrel\n", "unknown key"),
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


# --- board_id escape hatch (Phase 1a) ----------------------------------------

def test_board_id_loads_and_strips(tmp_path):
    assert load_sidecar(_write(tmp_path, "board_id: esp32dev\n")).board_id == "esp32dev"
    assert load_sidecar(_write(tmp_path, 'board_id: "  esp32dev  "\n')).board_id == "esp32dev"


def test_board_id_absent_is_none(tmp_path):
    assert load_sidecar(_write(tmp_path, "power_source: usb_c\n")).board_id is None


@pytest.mark.parametrize("body", [
    "board_id: ''\n",          # empty string
    'board_id: "   "\n',       # whitespace-only -> empty after strip
    "board_id: 42\n",          # non-string scalar
    "board_id: [a, b]\n",      # non-string sequence
])
def test_board_id_invalid_rejected(tmp_path, body):
    with pytest.raises(SidecarError, match="board_id"):
        load_sidecar(_write(tmp_path, body))


def test_sidecar_board_id_resolves_mcu_without_platformio(tmp_path):
    """No platformio.ini at all: the sidecar board_id is the escape hatch — it must
    resolve the MCU (no mcu_unknown gap) and be reported as the board source."""
    from kicad_mcp.tools.design import _op_import
    from kicad_mcp.utils.firmware.intent import load_intent
    (tmp_path / "config.h").write_text("#define I2C_SDA 21\n#define I2C_SCL 22\n")
    (tmp_path / "board.yaml").write_text("board_id: esp32dev\n")
    r = _op_import(firmware_path=str(tmp_path / "config.h"),
                   out_path=str(tmp_path / "i.yaml"))
    assert r["status"] == "ok"
    assert r["board"] == "esp32dev" and r["board_source"] == "sidecar"
    intent = load_intent(r["intent_path"])
    assert intent.mcu is not None                          # MCU resolved via the sidecar
    assert not any(g.kind == "mcu_unknown" for g in intent.gaps)


def test_sidecar_board_id_overrides_platformio_and_reaches_branch_select(tmp_path):
    """A sidecar board_id wins over platformio.ini AND reaches #if branch-selection
    (the early-plumbing point), not just MCU resolution: the S3 branch's pin is
    chosen, proving the override happened BEFORE the preprocessor ran."""
    from kicad_mcp.tools.design import _op_import
    from kicad_mcp.utils.firmware.intent import load_intent
    (tmp_path / "config.h").write_text(
        "#define I2C_SDA 21\n#define I2C_SCL 22\n"
        "#if CONFIG_IDF_TARGET_ESP32S3\n#define MIC_PIN 15\n"
        "#else\n#define MIC_PIN 4\n#endif\n")
    (tmp_path / "platformio.ini").write_text("[env:esp32dev]\nboard = esp32dev\n")
    (tmp_path / "board.yaml").write_text("board_id: esp32-s3-devkitc-1\n")
    r = _op_import(firmware_path=str(tmp_path / "config.h"),
                   out_path=str(tmp_path / "i.yaml"))
    assert r["board"] == "esp32-s3-devkitc-1" and r["board_source"] == "sidecar"
    intent = load_intent(r["intent_path"])
    mic = next((n for n in intent.nets if n.name == "MIC"), None)
    assert mic is not None
    assert any(ep.gpio == 15 for ep in mic.endpoints)      # S3 branch taken
    assert not any(ep.gpio == 4 for ep in mic.endpoints)   # not the #else


def test_board_source_none_when_no_platformio_and_no_sidecar_board_id(tmp_path):
    from kicad_mcp.tools.design import _op_import
    from kicad_mcp.utils.firmware.intent import load_intent
    (tmp_path / "config.h").write_text("#define I2C_SDA 21\n#define I2C_SCL 22\n")
    r = _op_import(firmware_path=str(tmp_path / "config.h"),
                   out_path=str(tmp_path / "i.yaml"))
    assert r["board"] is None and r["board_source"] == "none"
    intent = load_intent(r["intent_path"])
    assert any(g.kind == "mcu_unknown" for g in intent.gaps)


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


# --- unknown-key rejection ----------------------------------------------------

def test_unknown_top_level_key_rejected(tmp_path):
    # A typo for a real key is a loud error, not a silently-ignored directive.
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, "board_size: [90, 75]\n"))   # missing _mm
    assert "unknown board.yaml key" in str(e.value) and "board_size" in str(e.value)


def test_known_keys_all_accepted(tmp_path):
    # Every documented key loads clean together AND populates — guards all three
    # sources (BoardSidecar fields / _KNOWN_SIDECAR_KEYS / load_sidecar) from drift.
    sc = load_sidecar(_write(tmp_path, """\
        power_source: usb_c
        board_size_mm: [90, 75]
        extra_connectors: []
        placement: {}
        placement_hints: {}
        mounting_holes: {count: 4}
        bus_part_overrides: {}
        terminal_distribution: multi_edge
        terminal_centering: true
        board_refit: true
        expander_terminals: {U3: {device: TCRT5000, ports: 6}}
    """))
    assert sc.power_source == "usb_c"
    assert sc.board_size_mm == [90, 75]
    assert sc.extra_connectors == []
    assert sc.placement == {}
    assert sc.placement_hints == {}
    assert sc.mounting_holes == {"count": 4}
    assert sc.bus_part_overrides == {}
    assert sc.terminal_distribution == "multi_edge"
    assert sc.terminal_centering is True
    assert sc.board_refit is True
    # content (not just an empty dict) exercises the 3-source guard end-to-end
    assert sc.expander_terminals["U3"]["device"] == "TCRT5000"


# --- layout flags: terminal_centering / board_refit (bool) --------------------

@pytest.mark.parametrize("key", ["terminal_centering", "board_refit"])
@pytest.mark.parametrize("bad", ["1", "[]", "abc"])   # NB: YAML 'yes'/'on' ARE bools
def test_layout_bool_flag_rejects_non_bool(tmp_path, key, bad):
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, f"{key}: {bad}\n"))
    assert f"{key} must be true/false" in str(e.value)


def test_board_refit_threads_into_intent_source():
    # The orchestration reads design_intent.source["board_refit"]; apply_sidecar must
    # populate it (and only when explicitly set, mirroring terminal_centering).
    i = _intent()
    apply_sidecar(i, BoardSidecar(board_refit=True))
    assert i.source.get("board_refit") is True
    j = _intent()
    apply_sidecar(j, BoardSidecar())   # unset → absent (default off)
    assert "board_refit" not in j.source


# --- bus_part_overrides (C7) --------------------------------------------------

def test_bus_part_override_loads(tmp_path):
    sc = load_sidecar(_write(tmp_path, """\
        bus_part_overrides:
          CMCA_MIC: {part: INMP441}
    """))
    assert sc.bus_part_overrides == {"CMCA_MIC": {"part": "INMP441"}}


@pytest.mark.parametrize("body,needle", [
    ("bus_part_overrides: 5\n", "must be a mapping"),
    ("bus_part_overrides: {B: 5}\n", "must be a mapping"),
    ("bus_part_overrides: {B: {}}\n", "empty"),
    ("bus_part_overrides: {B: {part: 7}}\n", "part must be"),
    ("bus_part_overrides: {B: {prt: INMP441}}\n", "unknown key"),   # typo'd sub-key
    # H6 (release-gate): footprint was validated but NEVER applied (silent no-op) —
    # now rejected as unknown until a real footprint-override is plumbed.
    ("bus_part_overrides: {B: {footprint: F:P}}\n", "unknown key"),
])
def test_bus_part_override_rejected(tmp_path, body, needle):
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, body))
    assert needle in str(e.value)


def test_apply_bus_part_override_stamps_user_provenance():
    from kicad_mcp.utils.firmware.intent import Bus
    i = _intent()
    i.buses.append(Bus(name="CMCA_MIC", type="I2S_IN"))
    apply_sidecar(i, BoardSidecar(bus_part_overrides={"CMCA_MIC": {"part": "INMP441"}}))
    bus = next(b for b in i.buses if b.name == "CMCA_MIC")
    assert bus.resolved_part == "INMP441"
    assert bus.part_provenance == "user" and bus.part_is_assumption is False


def test_apply_bus_part_override_unknown_bus_gaps():
    i = _intent()
    apply_sidecar(i, BoardSidecar(bus_part_overrides={"NOPE": {"part": "INMP441"}}))
    assert any(g.kind == "bus_part_override_unknown" for g in i.gaps)


# --- mounting_holes -----------------------------------------------------------

def test_mounting_holes_absent_is_none(tmp_path):
    assert load_sidecar(_write(tmp_path, "power_source: usb_c\n")).mounting_holes is None


def test_mounting_holes_explicit_null_is_none(tmp_path):
    assert load_sidecar(_write(tmp_path, "mounting_holes: null\n")).mounting_holes is None


def test_mounting_holes_empty_mapping_loads_empty(tmp_path):
    # all keys optional → an empty mapping is valid and falls back to pipeline defaults
    assert load_sidecar(_write(tmp_path, "mounting_holes: {}\n")).mounting_holes == {}


def test_mounting_holes_loads(tmp_path):
    sc = load_sidecar(_write(tmp_path, """\
        mounting_holes:
          count: 2
          drill_mm: 2.7
          inset_mm: 4.0
          keepout_mm: 1.0
    """))
    assert sc.mounting_holes == {"count": 2, "drill_mm": 2.7,
                                 "inset_mm": 4.0, "keepout_mm": 1.0}


@pytest.mark.parametrize("body,needle", [
    ("mounting_holes: 4\n", "mounting_holes must be a mapping"),
    ("mounting_holes: {count: 3}\n", "count"),          # not in {0,2,4}
    ("mounting_holes: {count: 1}\n", "count"),          # boundary just below 2
    ("mounting_holes: {count: false}\n", "count"),      # bool slips past `in (0,2,4)`
    ("mounting_holes: {drill_mm: 0}\n", "drill_mm"),    # not positive
    ("mounting_holes: {drill_mm: -1}\n", "drill_mm"),   # negative
    ("mounting_holes: {inset_mm: true}\n", "inset_mm"),  # bool is not a number
    ("mounting_holes: {counnt: 4}\n", "unknown key"),   # typo'd sub-key not dropped
])
def test_mounting_holes_rejected(tmp_path, body, needle):
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, body))
    assert needle in str(e.value)


@pytest.mark.parametrize("count", [0, 2, 4])
def test_mounting_holes_count_accepted(tmp_path, count):
    sc = load_sidecar(_write(tmp_path, f"mounting_holes: {{count: {count}}}\n"))
    assert sc.mounting_holes == {"count": count}


def test_apply_threads_mounting_holes_into_source():
    i = _intent()
    sc = BoardSidecar(mounting_holes={"count": 4, "drill_mm": 3.2})
    apply_sidecar(i, sc)
    assert i.source["mounting_holes"] == {"count": 4, "drill_mm": 3.2}


# --- expander_terminals (v2: tap an expander's GPA/GPB pins -> terminals) ------

def test_expander_terminals_load_and_apply(tmp_path):
    sc = load_sidecar(_write(tmp_path, """\
        expander_terminals:
          U3:
            device: TCRT5000
            ports: 6
    """))
    assert "U3" in sc.expander_terminals
    intent = _intent()
    apply_sidecar(intent, sc)
    spec = intent.expander_terminals["U3"]
    assert spec.device == "TCRT5000"
    assert spec.group == "per_sensor" and spec.power == "3v3"   # defaults
    assert spec.net_prefix == "TCRT5000"                        # default = device
    # ports:int resolves to the first N pins in MCP hardware register order
    assert spec.ports == ["GPA0", "GPA1", "GPA2", "GPA3", "GPA4", "GPA5"]


def test_expander_terminals_int_and_list_equivalent(tmp_path):
    sc_i = load_sidecar(_write(tmp_path,
        "expander_terminals:\n  U3:\n    device: D\n    ports: 3\n"))
    sc_l = load_sidecar(_write(tmp_path,
        "expander_terminals:\n  U3:\n    device: D\n    ports: [GPA0, GPA1, GPA2]\n"))
    a, b = _intent(), _intent()
    apply_sidecar(a, sc_i)
    apply_sidecar(b, sc_l)
    assert a.expander_terminals["U3"].ports == b.expander_terminals["U3"].ports \
        == ["GPA0", "GPA1", "GPA2"]


@pytest.mark.parametrize("ports", [0, 16])    # at/below: 0 and the 16-pin max accept
def test_expander_terminals_ports_boundary_accepts(tmp_path, ports):
    sc = load_sidecar(_write(tmp_path,
        f"expander_terminals:\n  U3:\n    device: D\n    ports: {ports}\n"))
    intent = _intent()
    apply_sidecar(intent, sc)
    assert len(intent.expander_terminals["U3"].ports) == ports


@pytest.mark.parametrize("body,needle", [
    ("expander_terminals: 5\n", "must be a mapping"),
    ("expander_terminals:\n  U3: 5\n", "must be a mapping"),
    ("expander_terminals:\n  NOTAREF:\n    ports: 1\n", "not a KiCad ref"),
    ("expander_terminals:\n  U3:\n    device: D\n", "ports is required"),
    ("expander_terminals:\n  U3:\n    ports: true\n", "ports must be an int"),
    ("expander_terminals:\n  U3:\n    ports: -1\n", ">= 0"),
    ("expander_terminals:\n  U3:\n    ports: 17\n", "exceeds"),          # above max
    ("expander_terminals:\n  U3:\n    ports: [GPA0, GPC9]\n", "unknown port pin"),
    ("expander_terminals:\n  U3:\n    ports: [GPA0, GPA0]\n", "duplicate"),   # short

    ("expander_terminals:\n  U3:\n    ports: 1\n    group: weird\n", "group"),
    ("expander_terminals:\n  U3:\n    ports: 1\n    power: 12v\n", "power"),
    ("expander_terminals:\n  U3:\n    ports: 1\n    bogus: x\n", "unknown key"),
    ("expander_terminals:\n  U3:\n    ports: 1\n    net_prefix: ''\n", "net_prefix"),
    # two entries whose EFFECTIVE prefix (default = device) collide
    ("expander_terminals:\n  U3:\n    device: D\n    ports: 1\n"
     "  U4:\n    device: D\n    ports: 1\n", "must be unique"),
])
def test_expander_terminals_rejected(tmp_path, body, needle):
    with pytest.raises(SidecarError) as e:
        load_sidecar(_write(tmp_path, body))
    assert needle in str(e.value)


@pytest.mark.parametrize("fqbn,part", [
    ("esp32:esp32:esp32", "ESP32-WROOM-32E"),
    ("esp32:esp32:esp32s3", "ESP32-S3-WROOM-1"),
    ("esp32:esp32:esp32-s3-devkitc-1", "ESP32-S3-WROOM-1"),
])
def test_sidecar_board_id_accepts_arduino_fqbn(tmp_path, fqbn, part):
    """A board.yaml board_id may be an Arduino FQBN — it resolves the right ESP32."""
    from kicad_mcp.tools.design import _op_import
    from kicad_mcp.utils.firmware.intent import load_intent
    (tmp_path / "config.h").write_text("#define I2C_SDA 21\n#define I2C_SCL 22\n")
    (tmp_path / "board.yaml").write_text(f"board_id: {fqbn}\n")
    r = _op_import(firmware_path=str(tmp_path / "config.h"),
                   out_path=str(tmp_path / "i.yaml"))
    assert r["status"] == "ok" and r["board_source"] == "sidecar"
    intent = load_intent(r["intent_path"])
    assert intent.mcu is not None and intent.mcu.part == part


@pytest.mark.parametrize("fqbn", [
    "esp32:esp32:esp32c3", "esp32:esp32:esp32c6", "esp32:esp32:esp32s2",
    "esp32:esp32:esp32h2", "esp32:esp32:esp32p4",   # cold review: totally diff silicon
    "esp32:esp32:esp32c2",                           # unknown variant -> fail closed
])
def test_sidecar_board_id_unsupported_esp32_variant_fails_loud(tmp_path, fqbn):
    """C3/C6/S2/H2/P4 (and unknown variants like C2) contain 'esp32' as a substring
    but are UNSUPPORTED chips — they must fail loud (mcu_unknown), NOT silently
    resolve to classic ESP32. The dangerous case of the substring-match seam; the
    IDF-target guard (with the fail-closed variant check) catches it."""
    from kicad_mcp.tools.design import _op_import
    from kicad_mcp.utils.firmware.intent import load_intent
    (tmp_path / "config.h").write_text("#define I2C_SDA 21\n#define I2C_SCL 22\n")
    (tmp_path / "board.yaml").write_text(f"board_id: {fqbn}\n")
    r = _op_import(firmware_path=str(tmp_path / "config.h"),
                   out_path=str(tmp_path / "i.yaml"))
    intent = load_intent(r["intent_path"])
    assert intent.mcu is None
    assert any(g.kind == "mcu_unknown" for g in intent.gaps)


# --- single-source-of-truth parity pins (/audit-syntactic-seams findings) --------

def test_expander_power_vocab_matches_template_rail_map():
    # sidecar validates power against _EXPANDER_POWER; templates realize it via
    # _RAIL_FOR_POWER — two hand-typed copies of one closed set. Drift currently
    # fails LOUD (bracket lookup), but only by accident; pin the parity.
    from kicad_mcp.utils.firmware.sidecar import _EXPANDER_POWER
    from kicad_mcp.utils.firmware.templates import _RAIL_FOR_POWER
    assert set(_RAIL_FOR_POWER) == _EXPANDER_POWER


def test_commonly_remote_bus_types_are_real_bus_types():
    # the advisory set hand-copies members of intent._BUS_TYPES; a renamed bus
    # type would silently stop the placement nudge from firing.
    from kicad_mcp.utils.firmware.intent import _BUS_TYPES
    from kicad_mcp.utils.firmware.sidecar import _COMMONLY_REMOTE_BUS_TYPES
    assert _COMMONLY_REMOTE_BUS_TYPES <= _BUS_TYPES


# --- /audit-tests findings: pin every key of the numeric-validation loop --------
# Mutation run showed a tuple-member-deletion mutant (drop "keepout_mm" from the
# loop) survived the suite — only drill_mm pinned the shared loop, leaving the
# other keys' membership unprotected. One rejection test per key kills it.

@pytest.mark.parametrize("key", ["drill_mm", "inset_mm", "keepout_mm"])
def test_mounting_holes_each_numeric_key_rejects_nonpositive(tmp_path, key):
    with pytest.raises(SidecarError, match=key):
        load_sidecar(_write(tmp_path, f"mounting_holes: {{{key}: -1}}\n"))
    with pytest.raises(SidecarError, match=key):
        load_sidecar(_write(tmp_path, f"mounting_holes: {{{key}: 0}}\n"))


def test_mounting_holes_count_float_equality_accepted(tmp_path):
    # PINNED CONTRACT: `2.0 in (0, 2, 4)` is True by numeric equality, so a YAML
    # float count is accepted. Harmless downstream (count is used arithmetically);
    # pinned so a future strictness change is a conscious choice, not a surprise.
    sc = load_sidecar(_write(tmp_path, "mounting_holes: {count: 2.0}\n"))
    assert sc.mounting_holes == {"count": 2.0}


def test_mounting_holes_tiny_drill_accepted_here(tmp_path):
    # PINNED CONTRACT: this layer checks only >0 — manufacturability range sanity
    # is the pipeline's job (see docstring). 0.001mm passes validation.
    assert load_sidecar(_write(tmp_path,
        "mounting_holes: {drill_mm: 0.001}\n")).mounting_holes == {"drill_mm": 0.001}
