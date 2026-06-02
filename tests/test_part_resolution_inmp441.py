"""Part-resolution feature test, using the EOL INMP441 as the case study.

The feature's job on a firmware-declared part that can't be built: recognize it,
REFUSE to silently substitute, disclose it, and let the user pick a current
replacement via board.yaml. No KiCad (import + expand only).
"""
from __future__ import annotations

import asyncio
import shutil

from kicad_mcp.server import create_server
from kicad_mcp.utils.firmware.intent import load_intent

_FIX = "tests/fixtures/firmware/audio_inmp441"


def _design():
    return asyncio.run(create_server().get_tool("design")).fn


def _stage(tmp_path, board_yaml: str | None = None):
    """Copy the INMP441 fixture into tmp (so a board.yaml can sit beside it)."""
    fw = tmp_path / "fw"
    fw.mkdir()
    shutil.copy(f"{_FIX}/config.h", fw / "config.h")
    shutil.copy(f"{_FIX}/platformio.ini", fw / "platformio.ini")
    if board_yaml is not None:
        (fw / "board.yaml").write_text(board_yaml)
    return str(fw / "config.h")


def test_declared_inmp441_resolves_then_refuses_substitution(tmp_path):
    design = _design()
    cfg = _stage(tmp_path)
    out = str(tmp_path / "intent.yaml")
    r1 = design(operation="import_firmware", firmware_path=cfg, out_path=out)
    # The firmware NAMES INMP441 → bound from the corpus (not invented/assumed).
    assert r1["resolved_parts"]["CMCA_MIC"] == {"part": "INMP441", "via": "corpus"}

    r2 = design(operation="expand_templates", intent_path=out)
    # The template builds SPH0645; it must NOT silently substitute it for the
    # declared INMP441 — instead disclose part_unavailable, place no mic.
    assert any(g["kind"] == "part_unavailable" and "INMP441" in g["detail"]
               for g in r2["gaps"])
    it = load_intent(out)
    assert not any(p.type == "SPH0645" for p in it.peripherals), \
        "SPH0645 was silently substituted for the declared INMP441"


def test_board_yaml_override_substitutes_current_replacement(tmp_path):
    # The feature's handling: declare a current replacement for the EOL part. The
    # user owns the choice (provenance "user"); the firmware naming is overridden.
    design = _design()
    cfg = _stage(tmp_path, "bus_part_overrides:\n  CMCA_MIC: {part: SPH0645}\n")
    out = str(tmp_path / "intent.yaml")
    r1 = design(operation="import_firmware", firmware_path=cfg, out_path=out)
    assert r1["resolved_parts"]["CMCA_MIC"] == {"part": "SPH0645", "via": "user"}

    design(operation="expand_templates", intent_path=out)
    it = load_intent(out)
    mics = [p for p in it.peripherals if p.type == "SPH0645"]
    assert mics and mics[0].origin == "imported"   # user-chosen, placed, not assumed
    mic_bus = next(b for b in it.buses if b.name == "CMCA_MIC")
    assert mic_bus.resolved_part == "SPH0645" and mic_bus.part_provenance == "user"
