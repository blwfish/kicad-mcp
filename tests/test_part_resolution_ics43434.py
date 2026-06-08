"""ICS-43434 resolution + realization — the companion to test_part_resolution_inmp441.

The INMP441 case proves the front end RECOGNIZES a named part and REFUSES to
substitute when it can't build it. This proves the other half: its in-stock
successor ICS-43434 HAS a real KiCad symbol (Sensor_Audio:ICS-43434), so the same
firmware that named INMP441 — once migrated to ICS-43434 — both resolves AND gets
the mic placed (no part_unavailable gap, no orphan I2S-input bus). No KiCad
(import + expand only).
"""
from __future__ import annotations

import asyncio

from kicad_mcp.server import create_server
from kicad_mcp.utils.firmware.intent import load_intent

_FIX = "tests/fixtures/firmware/audio_ics43434"


def _design():
    return asyncio.run(create_server().get_tool("design")).fn


def test_declared_ics43434_resolves_and_places(tmp_path):
    design = _design()
    out = str(tmp_path / "intent.yaml")
    r1 = design(operation="import_firmware",
                firmware_path=f"{_FIX}/config.h", out_path=out)
    # Firmware NAMES "ICS-43434" → canonical key "ICS43434", bound from the corpus
    # (not invented/assumed) — the hyphen is stripped by canonical_type.
    assert r1["resolved_parts"]["CMCA_MIC"] == {"part": "ICS43434", "via": "corpus"}

    r2 = design(operation="expand_templates", intent_path=out)
    # Unlike INMP441, ICS-43434 is realizable → NO part_unavailable refusal.
    assert not any(g["kind"] == "part_unavailable" and "ICS43434" in g["detail"]
                   for g in r2["gaps"]), "ICS-43434 has a symbol; it must not be refused"

    it = load_intent(out)
    mics = [p for p in it.peripherals if p.type == "ICS43434"]
    assert len(mics) == 1, "the declared ICS-43434 mic must be placed"
    assert mics[0].origin == "imported"                     # firmware-named, not assumed
    assert mics[0].lib_id == "Sensor_Audio:ICS-43434"
    # The SPH0645 default must NOT be silently substituted.
    assert not any(p.type == "SPH0645" for p in it.peripherals)
