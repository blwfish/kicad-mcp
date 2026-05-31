"""Placement-locus unit tests (no KiCad) — board.yaml placement channel, the
locus-aware bus templates, glue suppression, legends, and the manifest.

Intent/expand are pure Python, so the whole arc up to (but not including) the
real-KiCad PCB build is exercised here. The PCB silk-legend step + 0-unconnected
routing are gated in tests/integration/test_firmware_pcb_pipeline.py.
"""
from pathlib import Path

import pytest

from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
from kicad_mcp.utils.firmware.parse import (
    idf_target_defines,
    parse_defines,
    partition,
    select_active_branches,
)
from kicad_mcp.utils.firmware.sidecar import (
    BoardSidecar,
    SidecarError,
    advise_unspecified_placement,
    apply_sidecar,
    load_sidecar,
    _validate,
)
from kicad_mcp.utils.firmware.templates import expand_intent

FIXTURES = Path(__file__).parent / "fixtures" / "firmware"
AUDIO = FIXTURES / "audio_s3" / "config.h"
TRACK_GEOM = FIXTURES / "track_geometry" / "config.h"


def _intent(config_h: Path):
    board = find_board_id(str(config_h))
    text = select_active_branches(config_h.read_text(), idf_target_defines(board))
    parsed = partition(parse_defines(text))
    return build_intent(parsed, firmware_path=str(config_h), board_id=board)


def _audio_with_placement(placement: dict):
    intent = _intent(AUDIO)
    apply_sidecar(intent, BoardSidecar(placement=placement))
    return expand_intent(intent)


def _types(intent):
    return [p.type for p in intent.peripherals]


def _legend(intent, ref):
    return next(L for L in intent.connector_legends if L.ref == ref)


# --- board.yaml placement validation (L5) ------------------------------------

def test_validate_accepts_well_formed_placement():
    d = {"placement": {
        "CMCA_MIC": {"locus": "remote", "device": "INMP441"},
        "CMCA_I2S": {"locus": "on_board_with_remote_io", "device": "MAX98357A",
                     "external_io": ["outp", "outn"]},
    }}
    assert _validate(d) == []


def test_validate_rejects_unknown_locus():
    errs = _validate({"placement": {"X": {"locus": "floating"}}})
    assert any("locus" in e for e in errs)


def test_validate_rejects_unknown_connector():
    errs = _validate({"placement": {"X": {"locus": "remote", "connector": "magnets"}}})
    assert any("connector" in e for e in errs)


def test_validate_remote_io_requires_external_io():
    errs = _validate({"placement": {"X": {"locus": "on_board_with_remote_io"}}})
    assert any("external_io" in e for e in errs)


def test_validate_rejects_non_list_external_io():
    errs = _validate({"placement": {"X": {"locus": "on_board_with_remote_io",
                                          "external_io": "outp"}}})
    assert any("external_io" in e for e in errs)


def test_load_sidecar_raises_on_bad_placement(tmp_path):
    p = tmp_path / "board.yaml"
    p.write_text("placement: {CMCA_MIC: {locus: bogus}}")
    with pytest.raises(SidecarError):
        load_sidecar(str(p))


# --- apply: records placements + flags unknown keys --------------------------

def test_apply_records_placement_and_projects_peripheral_locus():
    intent = _intent(TRACK_GEOM)            # has carded peripherals (U2/U3/U4)
    ref = intent.peripherals[0].ref
    apply_sidecar(intent, BoardSidecar(placement={ref: {"locus": "remote",
                                                        "device": "Remote sensor"}}))
    assert intent.placements[ref].locus == "remote"
    assert next(p for p in intent.peripherals if p.ref == ref).locus == "remote"


def test_apply_flags_unknown_placement_key():
    intent = _intent(AUDIO)
    apply_sidecar(intent, BoardSidecar(placement={"NOPE": {"locus": "remote"}}))
    assert any(g.kind == "placement_unknown_target" for g in intent.gaps)


# --- locus-aware bus templates (L4) ------------------------------------------

def test_remote_i2s_mic_becomes_terminal_no_chip():
    intent = _audio_with_placement({"CMCA_MIC": {"locus": "remote", "device": "INMP441"}})
    assert "SPH0645" not in _types(intent)            # no on-board mic chip
    terms = [p for p in intent.peripherals if p.value == "INMP441"]
    assert len(terms) == 1
    t = terms[0]
    assert t.type == "TERM"
    assert t.lib_id == "Connector:Screw_Terminal_01x05"  # BCLK/WS/SD/+3V3/GND
    leg = _legend(intent, t.ref)
    assert leg.positions == ["BCLK", "WS", "SD", "+3V3", "GND"]
    assert any(g.kind == "remote_device" and "INMP441" in g.detail for g in intent.gaps)


def test_remote_uart_becomes_terminal_no_header():
    intent = _audio_with_placement({"CMCA_PRESENCE": {"locus": "remote", "device": "LD2410"}})
    terms = [p for p in intent.peripherals if p.value == "LD2410"]
    assert len(terms) == 1
    assert terms[0].lib_id == "Connector:Screw_Terminal_01x04"  # RX/TX/+3V3/GND
    assert _legend(intent, terms[0].ref).positions == ["RX", "TX", "+3V3", "GND"]


def test_on_board_with_remote_io_amps_get_screw_terminal_speakers():
    intent = _audio_with_placement({
        "CMCA_I2S": {"locus": "on_board_with_remote_io", "device": "MAX98357A",
                     "external_io": ["outp", "outn"]},
    })
    # amps stay on board
    assert _types(intent).count("MAX98357A") >= 2
    # CMCA_I2S speakers are screw terminals; the OTHER (unplaced) I2S bus stays pin header
    spk_terms = [p for p in intent.peripherals
                 if p.type == "TERM" and str(p.value).startswith("SPK0")]
    assert spk_terms and all(
        p.lib_id == "Connector:Screw_Terminal_01x02" for p in spk_terms)


def test_default_speaker_is_pin_header_not_terminal():
    intent = _audio_with_placement({})       # no placement -> default behavior
    spk = [p for p in intent.peripherals if str(p.value).startswith("SPK")]
    assert spk and all(p.type == "HDR" for p in spk)   # pin headers (back-compat)
    assert all(p.lib_id == "Connector_Generic:Conn_01x02" for p in spk)


# --- glue suppression --------------------------------------------------------

def test_remote_mic_emits_no_bypass_cap():
    """The remote mic's bypass cap lives at the device, not on this board: the
    remote build has fewer caps than the on-board build."""
    on_board = _audio_with_placement({})
    remote = _audio_with_placement({"CMCA_MIC": {"locus": "remote", "device": "INMP441"}})
    assert _types(remote).count("C") < _types(on_board).count("C")


def test_remote_peripheral_excluded_from_power_ties():
    """A carded peripheral made remote is not tied to +3V3 on the board; power is
    delivered over the terminal's tap instead."""
    intent = _intent(TRACK_GEOM)
    ref = intent.peripherals[0].ref          # an MPU6050 instance
    apply_sidecar(intent, BoardSidecar(placement={ref: {"locus": "remote",
                                                        "device": "MPU6050"}}))
    intent = expand_intent(intent)
    rail = next(n for n in intent.nets if n.name == "+3V3")
    assert ref not in {ep.ref for ep in rail.endpoints}   # not tied on-board
    # but a remote terminal exists carrying it
    assert any(g.kind == "remote_device" for g in intent.gaps)


# --- edge cases --------------------------------------------------------------

def test_remote_without_device_name_still_emits_terminal_and_legend():
    intent = _audio_with_placement({"CMCA_MIC": {"locus": "remote"}})  # no device
    terms = [p for p in intent.peripherals
             if p.type == "TERM" and p.lib_id == "Connector:Screw_Terminal_01x05"]
    assert len(terms) == 1
    leg = _legend(intent, terms[0].ref)
    assert leg.device == "I2S mic"                    # fallback identity
    assert leg.positions == ["BCLK", "WS", "SD", "+3V3", "GND"]


def test_two_remote_buses_get_distinct_collision_free_refs():
    intent = _audio_with_placement({
        "CMCA_MIC": {"locus": "remote", "device": "INMP441"},
        "CMCA_PRESENCE": {"locus": "remote", "device": "LD2410"},
    })
    refs = [p.ref for p in intent.peripherals if p.type == "TERM"]
    assert len(refs) == len(set(refs))                # no collisions
    assert {p.value for p in intent.peripherals if p.type == "TERM"} >= {"INMP441", "LD2410"}


# --- advisory (Open Decision 1) ----------------------------------------------

def test_advisory_lists_unset_field_wired_buses():
    intent = _intent(AUDIO)                  # I2S_IN/I2S_OUT/UART buses, no placement
    advise_unspecified_placement(intent)
    adv = [g for g in intent.gaps if g.kind == "placement_unspecified"]
    assert len(adv) == 1
    assert "CMCA_MIC" in adv[0].detail and "CMCA_PRESENCE" in adv[0].detail


def test_advisory_idempotent_and_skips_placed():
    intent = _intent(AUDIO)
    apply_sidecar(intent, BoardSidecar(placement={
        "CMCA_MIC": {"locus": "remote", "device": "INMP441"}}))
    advise_unspecified_placement(intent)
    advise_unspecified_placement(intent)     # second call must not stack
    adv = [g for g in intent.gaps if g.kind == "placement_unspecified"]
    assert len(adv) == 1
    assert "CMCA_MIC" not in adv[0].detail   # placed bus excluded from the nudge


def test_advisory_lists_every_field_wired_bus_type():
    """I2S_OUT must be in the nudge too (not just I2S_IN/UART) — pins the full
    _COMMONLY_REMOTE_BUS_TYPES set so dropping a type is caught."""
    intent = _intent(AUDIO)
    advise_unspecified_placement(intent)
    detail = next(g.detail for g in intent.gaps if g.kind == "placement_unspecified")
    for stem in ("CMCA_MIC", "CMCA_PRESENCE", "CMCA_I2S", "CMCA_I2S2"):
        assert stem in detail


# --- net wiring of synthesized terminals (Rule 2: survives the router) --------

def test_remote_peripheral_terminal_joins_its_signal_net():
    """A carded remote device's terminal must actually join the device's signal
    net (else the terminal is a routing island with no net)."""
    intent = _intent(TRACK_GEOM)
    ref = intent.peripherals[0].ref          # an MPU6050 on the shared I2C bus
    apply_sidecar(intent, BoardSidecar(placement={ref: {"locus": "remote",
                                                        "device": "MPU6050"}}))
    intent = expand_intent(intent)
    term = next(p for p in intent.peripherals if p.type == "TERM" and p.value == "MPU6050")
    sda = next(n for n in intent.nets if n.name == "I2C_SDA")
    assert term.ref in {ep.ref for ep in sda.endpoints}   # terminal is on the bus


def test_remote_mic_terminal_signals_reach_mcu_and_terminal():
    intent = _audio_with_placement({"CMCA_MIC": {"locus": "remote", "device": "INMP441"}})
    term = next(p for p in intent.peripherals if p.value == "INMP441")
    bclk = next(n for n in intent.nets if n.name == "MIC_BCLK")
    refs = {ep.ref for ep in bclk.endpoints}
    assert intent.mcu.ref in refs and term.ref in refs   # MCU drives -> terminal


def test_on_board_with_remote_io_speaker_net_joins_amp_and_terminal():
    intent = _audio_with_placement({
        "CMCA_I2S": {"locus": "on_board_with_remote_io", "device": "MAX98357A",
                     "external_io": ["outp", "outn"]},
    })
    spk_p = next(n for n in intent.nets if n.name == "SPK0L_P")
    refs = {ep.ref for ep in spk_p.endpoints}
    term = next(p for p in intent.peripherals
                if p.type == "TERM" and p.value == "SPK0L")
    amps = {p.ref for p in intent.peripherals if p.type == "MAX98357A"}
    assert term.ref in refs                    # terminal on the speaker net
    assert refs & amps                         # an amp output on the speaker net


# --- external_io is currently DECLARATIVE (H1: pin the invariant) -------------

def test_external_io_listing_does_not_drop_a_speaker_side():
    """external_io is validated + required for on_board_with_remote_io but the amp
    template intrinsically crosses BOTH outp and outn. Declaring only [outp] must
    NOT drop the outn side — pin this invariant so the dead-param state is locked
    and documented (a future ref-keyed path may consume external_io; until then
    this asserts no silent surprise)."""
    intent = _audio_with_placement({
        "CMCA_I2S": {"locus": "on_board_with_remote_io", "device": "MAX98357A",
                     "external_io": ["outp"]},     # only one role listed
    })
    term = next(p for p in intent.peripherals if p.type == "TERM" and p.value == "SPK0L")
    leg = next(L for L in intent.connector_legends if L.ref == term.ref)
    assert leg.positions == ["L+", "L-"]          # BOTH sides still cross


# --- v4 schema serialization round-trip (M7) ---------------------------------

def test_v4_fields_survive_save_load(tmp_path):
    from kicad_mcp.utils.firmware.intent import load_intent, save_intent
    intent = _audio_with_placement({"CMCA_MIC": {"locus": "remote", "device": "INMP441"}})
    assert intent.connector_legends and intent.placements    # populated pre-save
    p = tmp_path / "intent.yaml"
    save_intent(intent, str(p))
    back = load_intent(str(p))
    assert back.placements["CMCA_MIC"].locus == "remote"
    assert back.placements["CMCA_MIC"].device == "INMP441"
    mic_leg = next(L for L in back.connector_legends if L.device == "INMP441")
    assert mic_leg.positions == ["BCLK", "WS", "SD", "+3V3", "GND"]


def test_round_trip_remote_peripheral_stays_consistent(tmp_path):
    """H2 regression: after save/load, the glue-suppression check (templates) and
    the placement-exclusion check (generate) must agree — both read placements,
    never the Peripheral.locus projection independently."""
    from kicad_mcp.utils.firmware.intent import is_remote, load_intent, save_intent
    from kicad_mcp.utils.firmware.templates import _peripheral_is_remote
    intent = _intent(TRACK_GEOM)
    ref = intent.peripherals[0].ref
    apply_sidecar(intent, BoardSidecar(placement={ref: {"locus": "remote",
                                                        "device": "MPU6050"}}))
    p = tmp_path / "i.yaml"
    save_intent(intent, str(p))
    back = load_intent(str(p))
    per = next(x for x in back.peripherals if x.ref == ref)
    assert is_remote(back, ref) == _peripheral_is_remote(back, per)   # never split


def test_locus_only_projection_without_placement_is_not_behaviorally_remote():
    """A hand-edited Peripheral.locus='remote' with NO placements entry must not
    silently land in the split state (placed but glue-suppressed). placements is
    the single behavioral source: both checks agree it is NOT remote."""
    from kicad_mcp.utils.firmware.intent import Peripheral, is_remote
    from kicad_mcp.utils.firmware.templates import _peripheral_is_remote
    intent = _intent(TRACK_GEOM)
    p = intent.peripherals[0]
    p.locus = "remote"                           # projection set, placements empty
    assert intent.placements == {}
    assert is_remote(intent, p.ref) is False
    assert _peripheral_is_remote(intent, p) is False


# --- bus-stem apply path (L3) ------------------------------------------------

def test_apply_bus_stem_key_records_placement_without_unknown_gap():
    intent = _intent(AUDIO)                      # buses, no carded peripherals
    apply_sidecar(intent, BoardSidecar(placement={
        "CMCA_MIC": {"locus": "remote", "device": "INMP441"}}))
    assert intent.placements["CMCA_MIC"].locus == "remote"
    assert not any(g.kind == "placement_unknown_target" for g in intent.gaps)


# --- silk-legend pipeline step: no-legends fast path (M5, no KiCad) -----------

def test_silkscreen_legends_step_noop_without_legends():
    from kicad_mcp.tools.pcb_pipeline import _step_silkscreen_legends
    res = _step_silkscreen_legends("/nonexistent.kicad_pcb", [])
    assert res["status"] == "ok" and res["labels_added"] == 0
