"""Tests for the design-intent importer + serialization."""
from __future__ import annotations

from pathlib import Path

from kicad_mcp.utils.firmware.intent import (
    SCHEMA_VERSION,
    Gap,
    Net,
    build_intent,
    find_board_id,
    from_dict,
    load_intent,
    merge,
    save_intent,
    to_dict,
)
from kicad_mcp.utils.firmware.parse import parse_defines, partition

SPEEDCAL_CONFIG = Path(
    "/Volumes/Files/claude/mr-esp32/speed-cal/firmware/include/config.h"
)

_SAMPLE = """\
#define I2C_SDA 21
#define I2C_SCL 22
#define MCP23017_ADDR 0x27
#define MCP23017_INT_PIN 13
#define HX711_DOUT_PIN 16
#define HX711_SCK_PIN 17
#define I2S_SCK_PIN 18
#define PIEZO_ADC_PIN 35
#define BAD_PIN -1
#define MCP_IODIRA 0x00
#define NUM_SENSORS 6
"""


def _intent(board="esp32dev"):
    parsed = partition(parse_defines(_SAMPLE))
    return build_intent(parsed, firmware_path="config.h", board_id=board)


def _net(intent, name):
    return next(n for n in intent.nets if n.name == name)

def _gap_kinds(intent):
    return {g.kind for g in intent.gaps}


def test_mcu_resolved():
    assert _intent().mcu.part == "ESP32-WROOM-32E"

def test_mcu_unknown_when_no_board():
    i = _intent(board=None)
    assert i.mcu is None and "mcu_unknown" in _gap_kinds(i)

def test_peripherals_materialized():
    types = {p.type for p in _intent().peripherals}
    assert types == {"HX711", "MCP23017"}  # PIEZO has no symbol -> not placed

def test_bus_net_tier():
    n = _net(_intent(), "I2C_SDA")
    assert n.kind == "bus" and n.confidence == "high"
    # MCU + the one I2C device (MCP23017 = U3 in this sample)
    assert any(e.role == "SDA" for e in n.endpoints)
    assert any(e.gpio == 21 for e in n.endpoints)

def test_peripheral_net_tier():
    n = _net(_intent(), "HX711_DOUT")
    assert n.kind == "peripheral" and n.confidence == "high"
    assert {e.role for e in n.endpoints if e.role} == {"DOUT"}

def test_orphan_net_tiers():
    i = _intent()
    assert _net(i, "I2S_SCK").kind == "orphan"     # no I2S device declared
    assert _net(i, "PIEZO_ADC").kind == "orphan"   # PIEZO has no symbol

def test_always_gaps_present():
    assert {"power_tree", "decoupling", "pullups", "connectors", "parts"} <= _gap_kinds(_intent())

def test_invalid_pin_becomes_gap_not_net():
    i = _intent()
    assert "invalid_pin" in _gap_kinds(i)
    assert all(n.name != "BAD" for n in i.nets)

def test_unknown_peripheral_gaps():
    # I2S (no device) and PIEZO (no symbol) each flag a gap.
    details = " ".join(g.detail for g in _intent().gaps if g.kind == "unknown_peripheral")
    assert "I2S" in details and "PIEZO" in details

def test_provenance_retains_unmodeled_macros():
    prov = _intent().provenance
    names = {u["name"] for u in prov["unparsed"]}
    assert "MCP_IODIRA" in names and "NUM_SENSORS" in names
    assert prov["unparsed_count"] >= 2

def test_duplicate_signal_first_wins():
    parsed = partition(parse_defines("#define FOO_PIN 5\n#define FOO_PIN 6\n"))
    i = build_intent(parsed, firmware_path="c.h", board_id="esp32dev")
    foo = [n for n in i.nets if n.name == "FOO"]
    assert len(foo) == 1
    assert foo[0].endpoints[0].gpio == 5            # first wins
    assert "duplicate_signal" in _gap_kinds(i)


# --- serialization + merge ---------------------------------------------------

def test_yaml_round_trip(tmp_path):
    i = _intent()
    p = tmp_path / "intent.yaml"
    save_intent(i, str(p))
    assert to_dict(load_intent(str(p))) == to_dict(i)


def test_placement_hints_round_trip(tmp_path):
    """The v5 per-ref placement_hints channel survives save/load with its nested
    directive dicts intact (the build step reads these to override placement)."""
    i = _intent()
    i.placement_hints = {
        "J6": {"edge": "none"},
        "J1": {"rotation": 90, "fixed": [10, 20]},
    }
    p = tmp_path / "intent.yaml"
    save_intent(i, str(p))
    assert load_intent(str(p)).placement_hints == i.placement_hints

def test_from_dict_to_dict_identity():
    i = _intent()
    assert to_dict(from_dict(to_dict(i))) == to_dict(i)


def test_from_dict_drops_unknown_fields():
    """from_dict promises forward-compat: a newer doc with extra fields loads,
    silently dropping the unknowns, instead of raising TypeError on cls(**d)
    (a v4 schema or a typo'd hand-edited key must not crash load_intent)."""
    doc = to_dict(_intent())
    doc["mcu"]["future_v4_field"] = 123                      # unknown key on the mcu
    if doc.get("peripherals"):
        doc["peripherals"][0]["unexpected_key"] = True
    if doc.get("nets") and doc["nets"][0].get("endpoints"):
        doc["nets"][0]["endpoints"][0]["alien"] = 9
    intent = from_dict(doc)                                  # raised TypeError before the fix
    assert "future_v4_field" not in to_dict(intent)["mcu"]   # unknown was dropped


def test_from_dict_null_factory_field_coerced_to_empty():
    """A hand-edited doc with a default_factory field (Bus.signals: dict) set to
    null must NOT override the factory with None — templates then crash on
    bus.signals.get(...). _only_fields drops the None so the factory applies."""
    doc = to_dict(_audio_intent())
    assert doc.get("buses"), "fixture must have buses with signals"
    doc["buses"][0]["signals"] = None
    intent = from_dict(doc)
    assert intent.buses[0].signals == {}            # factory default, not None


def test_from_dict_top_level_null_collections_become_empty():
    """`buses: null` (etc.) in a hand-edited doc: dict.get returns None for a
    present-but-null key, and the list comprehensions would TypeError without the
    `or []` guard. Each top-level collection must degrade to empty."""
    intent = from_dict({
        "schema_version": SCHEMA_VERSION,
        "buses": None, "peripherals": None, "nets": None,
        "gaps": None, "connector_legends": None,
        "placements": None, "placement_hints": None,
    })
    assert intent.buses == []
    assert intent.peripherals == []
    assert intent.nets == []
    assert intent.gaps == []
    assert intent.connector_legends == []
    assert intent.placements == {}
    assert intent.placement_hints == {}

def test_merge_preserves_user_items_and_resolved_gaps():
    fresh = _intent()
    existing = _intent()
    existing.nets.append(Net("USER_NET", "peripheral", "high", [], origin="user"))
    # mark the power_tree gap resolved in the existing (edited) doc
    for g in existing.gaps:
        if g.kind == "power_tree":
            g.resolved = True
    merged = merge(existing, fresh)
    assert any(n.name == "USER_NET" and n.origin == "user" for n in merged.nets)
    assert any(g.kind == "power_tree" and g.resolved for g in merged.gaps)
    # imported user-less nets still present
    assert any(n.name == "I2C_SDA" for n in merged.nets)


# --- typed buses (axis 4) ----------------------------------------------------

_AUDIO = """\
#define CMCA_I2S_BCLK 15
#define CMCA_I2S_LRC 16
#define CMCA_I2S_DIN 17
#define CMCA_I2S2_BCLK 5
#define CMCA_I2S2_LRC 6
#define CMCA_I2S2_DIN 7
#define CMCA_OLED_SDA 47
#define CMCA_OLED_SCL 48
#define CMCA_OLED_ADDR 0x3C
#define CMCA_PRESENCE_RX_PIN 5
#define CMCA_PRESENCE_TX_PIN 6
#define CMCA_MIC_BCLK 8
#define CMCA_MIC_WS 9
#define CMCA_MIC_SD 10
"""

def _audio_intent():
    return build_intent(partition(parse_defines(_AUDIO)),
                        firmware_path="audio.h", board_id="esp32-s3-devkitc-1")

def _bus(intent, name):
    return next(b for b in intent.buses if b.name == name)

def test_buses_typed():
    i = _audio_intent()
    assert {b.name: b.type for b in i.buses} == {
        "CMCA_I2S": "I2S_OUT", "CMCA_I2S2": "I2S_OUT",
        "CMCA_OLED": "I2C", "CMCA_PRESENCE": "UART", "CMCA_MIC": "I2S_IN",
    }

def test_bus_signals_and_address():
    i = _audio_intent()
    assert _bus(i, "CMCA_I2S").signals == {"BCLK": 15, "LRC": 16, "DIN": 17}
    assert _bus(i, "CMCA_OLED").address == 0x3C    # _ADDR associated by stem
    assert _bus(i, "CMCA_MIC").signals == {"BCLK": 8, "WS": 9, "SD": 10}

def test_pin_conflict_flagged():
    # GPIO 5/6 used by both I2S bus 2 and the presence UART.
    i = _audio_intent()
    conflicts = [g.detail for g in i.gaps if g.kind == "pin_conflict"]
    assert any("GPIO5" in d for d in conflicts) and any("GPIO6" in d for d in conflicts)

def test_audio_mcu_is_s3():
    assert _audio_intent().mcu.part == "ESP32-S3-WROOM-1"

def test_speedcal_has_no_buses():
    # the WROOM sensor board's I2C/HX711 go through the peripheral path, not buses
    assert _intent().buses == []


# --- board detection on the real tree ----------------------------------------

def test_find_board_id_real_speedcal():
    if not SPEEDCAL_CONFIG.exists():
        import pytest
        pytest.skip("speed-cal fixture tree not present in this checkout")
    assert find_board_id(str(SPEEDCAL_CONFIG)) == "esp32dev"


# --- find_board_id multi-env "prefer last known board" seam (tmp_path) --------
# The rule (intent.py:133): of the declared `board =` ids, prefer the LAST one
# that resolves to a known MCU; if none are known, the LAST declared. These
# cases were only covered by an integration test under KICAD_INTEGRATION=1.

def _write_pio(tmp_path, *board_ids, with_config=True):
    """Write a platformio.ini declaring one env per board id (in order) plus a
    config.h, and return the config.h path to start the walk-up from."""
    body = "".join(
        f"[env:e{i}]\nboard = {bid}\n" for i, bid in enumerate(board_ids))
    (tmp_path / "platformio.ini").write_text(body)
    cfg = tmp_path / "config.h"
    if with_config:
        cfg.write_text("#define X 1\n")
    return cfg


def test_find_board_id_last_known_wins(tmp_path):
    # two known boards -> the LAST declared known one
    cfg = _write_pio(tmp_path, "esp32dev", "esp32-s3-devkitc-1")
    assert find_board_id(str(cfg)) == "esp32-s3-devkitc-1"


def test_find_board_id_prefers_known_over_last_declared(tmp_path):
    # known then UNKNOWN -> the known one, even though it isn't last declared
    cfg = _write_pio(tmp_path, "esp32dev", "totally-unknown-xyz")
    assert find_board_id(str(cfg)) == "esp32dev"


def test_find_board_id_none_known_falls_back_to_last_declared(tmp_path):
    # no board resolves to a known MCU -> the LAST declared id
    cfg = _write_pio(tmp_path, "esp32-c3", "totally-unknown-xyz")
    assert find_board_id(str(cfg)) == "totally-unknown-xyz"


def test_find_board_id_single_board(tmp_path):
    cfg = _write_pio(tmp_path, "esp32dev")
    assert find_board_id(str(cfg)) == "esp32dev"


def test_find_board_id_no_ini_returns_none(tmp_path):
    cfg = tmp_path / "config.h"
    cfg.write_text("#define X 1\n")
    assert find_board_id(str(cfg)) is None


def test_find_board_id_ini_with_no_board_line_returns_none(tmp_path):
    (tmp_path / "platformio.ini").write_text("[env:e0]\nframework = arduino\n")
    cfg = tmp_path / "config.h"
    cfg.write_text("#define X 1\n")
    assert find_board_id(str(cfg)) is None


def _nested_cfg(tmp_path, depth):
    """platformio.ini at tmp_path, config.h `depth` directories below it.
    The walk-up checks config.h's parent dir then 5 ancestors (range(6)), so
    the ini is reachable iff depth <= 5."""
    _write_pio(tmp_path, "esp32dev", with_config=False)
    d = tmp_path
    for i in range(depth):
        d = d / f"d{i}"
    d.mkdir(parents=True, exist_ok=True)
    cfg = d / "config.h"
    cfg.write_text("#define X 1\n")
    return cfg


def test_find_board_id_walks_up_from_nested_file(tmp_path):
    assert find_board_id(str(_nested_cfg(tmp_path, 2))) == "esp32dev"


def test_find_board_id_walk_up_at_bound_is_found(tmp_path):
    # depth 5 = the deepest reachable within the 6-iteration bound
    assert find_board_id(str(_nested_cfg(tmp_path, 5))) == "esp32dev"


def test_find_board_id_walk_up_just_past_bound_not_found(tmp_path):
    # depth 6 = one past the bound; the ini exists but must NOT be found
    assert find_board_id(str(_nested_cfg(tmp_path, 6))) is None


# --- I2C sensor-hub: multiple address-declared devices, incl. same-type pair --
# The track-geometry generalization: two MPU-6050 at 0x68/0x69 + an OLED at 0x3C,
# all on one bare I2C bus (bare I2C_SDA/SCL pins + *_ADDR device declarations).

_HUB = """\
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
#define MPU6050_ADDR 0x68
#define MPU6050_ADDR_2 0x69
#define OLED_ADDR 0x3C
#define BUZZER_PIN 25
#define MPU6050_REG_PWR_MGMT_1 0x6B
"""


def _hub_intent():
    return build_intent(partition(parse_defines(_HUB)),
                        firmware_path="config.h", board_id="esp32dev")


def test_hub_two_mpu_instances_plus_oled():
    peris = _hub_intent().peripherals
    mpus = [p for p in peris if p.type == "MPU6050"]
    oled = [p for p in peris if p.type == "OLED"]
    # two distinct MPU instances at distinct addresses, distinct refs
    assert len(mpus) == 2 and len(oled) == 1
    assert {p.address for p in mpus} == {0x68, 0x69}
    assert len({p.ref for p in mpus}) == 2
    # deterministic order: lower address gets the lower ref
    by_ref = sorted(mpus, key=lambda p: p.ref)
    assert by_ref[0].address == 0x68 and by_ref[1].address == 0x69


def test_hub_all_devices_share_one_i2c_bus():
    i = _hub_intent()
    sda = _net(i, "I2C_SDA")
    refs = {e.ref for e in sda.endpoints}
    # MCU + both MPU + OLED all sit on the shared SDA line
    dev_refs = {p.ref for p in i.peripherals if p.type in ("MPU6050", "OLED")}
    assert dev_refs <= refs and i.mcu.ref in refs
    assert sda.kind == "bus"


def test_hub_register_macro_is_not_a_device():
    # MPU6050_REG_PWR_MGMT_1 (0x6B) must NOT become an address/peripheral.
    types = {p.type for p in _hub_intent().peripherals}
    assert "MPU6050_REG_PWR_MGMT_1" not in types and "MPU6050_REG" not in types


def test_hub_buzzer_stays_flagged_orphan():
    i = _hub_intent()
    assert _net(i, "BUZZER").kind == "orphan"
    assert any(g.kind == "unknown_peripheral" and "BUZZER" in g.detail for g in i.gaps)


def test_hub_module_assumption_flagged():
    assert any(g.kind == "module_assumption" for g in _hub_intent().gaps)
