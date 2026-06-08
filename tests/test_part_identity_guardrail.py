"""Part-identity guardrail (C8): the invariant the whole feature exists to hold —
a recognized device is NEVER placed as a silent substitute. Either the firmware
named it (origin "imported") or the assumption is disclosed by an assumed_part /
part_unavailable gap. No KiCad.
"""
from __future__ import annotations

import asyncio
import tempfile

from kicad_mcp.server import create_server
from kicad_mcp.utils.firmware.intent import Bus, load_intent
from kicad_mcp.utils.firmware.templates import Expansion, _decide_part

# The recognized device TYPES a template places (vs. generic R/C/HDR/CONN glue).
_DEVICE_TYPES = {"MAX98357A", "SPH0645", "ICS43434"}


# --- _decide_part: the three honest cases, directly --------------------------

def test_decide_unresolved_assumes_with_gap():
    bus = Bus(name="B", type="I2S_IN")
    ex = Expansion()
    place, origin = _decide_part(bus, "SPH0645", ex)
    assert place and origin == "template"
    assert bus.resolved_part == "SPH0645" and bus.part_is_assumption
    assert [g.kind for g in ex.gaps] == ["assumed_part"]


def test_decide_match_is_imported_no_gap():
    bus = Bus(name="B", type="I2S_OUT", resolved_part="MAX98357A",
              part_provenance="corpus")
    ex = Expansion()
    place, origin = _decide_part(bus, "MAX98357A", ex)
    assert place and origin == "imported"
    assert not bus.part_is_assumption and ex.gaps == []


def test_decide_mismatch_refuses_with_part_unavailable():
    # Firmware declared INMP441; this template only builds SPH0645 → REFUSE to
    # substitute (the bug), disclose part_unavailable, place nothing.
    bus = Bus(name="B", type="I2S_IN", resolved_part="INMP441",
              part_provenance="corpus")
    ex = Expansion()
    place, origin = _decide_part(bus, "SPH0645", ex)
    assert place is False
    assert any(g.kind == "part_unavailable" and "INMP441" in g.detail
               for g in ex.gaps)
    assert bus.resolved_part == "INMP441"   # the declared part is NOT overwritten


# --- the invariant on a real expansion ---------------------------------------

def _expand_audio() -> object:
    mcp = create_server()
    design = asyncio.run(mcp.get_tool("design")).fn
    out = tempfile.mktemp(suffix=".yaml")
    design(operation="import_firmware",
           firmware_path="tests/fixtures/firmware/audio_s3/config.h", out_path=out)
    design(operation="expand_templates", intent_path=out)
    return load_intent(out)


def test_no_recognized_device_placed_without_disclosure():
    it = _expand_audio()
    for p in it.peripherals:
        if p.type not in _DEVICE_TYPES:
            continue
        if p.origin == "imported":
            continue   # firmware named it — provenance, not a substitution
        # Placed as a non-imported (assumed) device → MUST be disclosed by a gap.
        assert any(g.kind in ("assumed_part", "part_unavailable") for g in it.gaps), (
            f"{p.ref} ({p.type}) placed as a non-imported assumption with NO "
            f"assumed_part/part_unavailable gap — a silent substitution")


def test_assumed_bus_has_a_gap_naming_it():
    it = _expand_audio()
    for bus in it.buses:
        if bus.part_is_assumption:
            assert any(g.kind == "assumed_part" and bus.name in g.detail
                       for g in it.gaps), \
                f"assumed bus {bus.name!r} not disclosed by an assumed_part gap"
