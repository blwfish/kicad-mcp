"""End-to-end firmware->PCB golden harness (the session meta-finding's fix).

A flagship pipeline tool (`build_pcb_from_schematic`) was once 100% broken on
KiCad 10 with ZERO integration coverage — test count != pipeline confidence.
This gate runs the WHOLE arc on real KiCad (9 and 10):

    config.h -> design intent -> expand templates -> generate schematic
             -> build routed PCB

and asserts **version-robust invariants** — component count, by-component-ref net
membership, and 0-unconnected routing — NOT version-fragile pin numbers (which
drift between KiCad symbol-library versions).
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "firmware"
CONFIG_H = FIXTURE / "config.h"
AUDIO_CONFIG_H = FIXTURE / "audio_s3" / "config.h"

_MINIMAL_PRO = {
    "board": {"design_settings": {}},
    "net_settings": {"classes": [{
        "name": "Default", "clearance": 0.2, "track_width": 0.25,
        "via_diameter": 0.6, "via_drill": 0.3, "microvia_diameter": 0.3,
        "microvia_drill": 0.1, "diff_pair_gap": 0.25, "diff_pair_width": 0.2,
    }], "meta": {"version": 3}},
    "meta": {"filename": "board.kicad_pro", "version": 1},
}


@pytest.fixture(scope="module")
def mcp_server():
    from kicad_mcp.server import create_server
    return create_server()


def _tool(mcp, name):
    return asyncio.run(mcp.get_tool(name)).fn


def test_firmware_to_routed_pcb(mcp_server, tmp_path):
    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")
    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "board.kicad_sch"
    pro = tmp_path / "board.kicad_pro"

    # 1) firmware -> design intent
    r1 = design(operation="import_firmware", firmware_path=str(CONFIG_H),
                out_path=str(intent))
    assert r1["status"] == "ok"
    assert r1["board"] == "esp32dev"
    assert r1["summary"]["mcu"] == "ESP32-WROOM-32E"
    assert {p["type"] for p in r1["summary"]["peripherals"]} == {"HX711", "MCP23017"}

    # 2) expand templates (power/glue + USB programming block)
    r2 = design(operation="expand_templates", intent_path=str(intent))
    assert r2["status"] == "ok"
    assert {"power_tree", "decoupling", "pullups"} <= set(r2["gaps_resolved"])

    # 3) generate the now-complete schematic
    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok"
    assert not r3["unresolved_endpoints"]
    assert r3["components_placed"] == 23      # 3 ICs + power tree + USB block

    # 4) build a routed PCB from it — the core gate
    pro.write_text(json.dumps(_MINIMAL_PRO))
    r4 = build(project_path=str(pro), board_width_mm=90, board_height_mm=75,
               autoroute_passes=2, export_gerbers=False)
    assert r4["status"] == "ok"
    assert r4["pads_assigned"] > 0
    assert r4["incomplete_nets"] == 0         # fully routed
    assert r4["steps"]["zones"]["zones_added"] >= 1

    # 5) golden connectivity invariants (by component REF — version-robust)
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
    nl = extract_netlist_via_cli(str(sch))
    assert nl is not None

    def refs_on(net):
        return {x["component"] for x in nl["nets"].get(net, [])}

    # MCU (U1) + LDO (U4) powered from +3V3; CP2102 (U5) VDD is an OUTPUT, so U5
    # must NOT appear on +3V3 (the landmine the templates handle).
    assert {"U1", "U4"} <= refs_on("+3V3")
    assert "U5" not in refs_on("+3V3")
    assert "U1" in refs_on("GND")
    # I2C bus joins the ESP32 (U1) and the MCP23017 (U3).
    assert {"U1", "U3"} <= refs_on("I2C_SDA")


def test_audio_s3_to_routed_pcb(mcp_server, tmp_path):
    """The SECOND board shape: an ESP32-S3 audio node (CMCA_* naming, I2S amp
    buses, #if target block). Exercises the generalized recognition +
    bus-driven templates end to end."""
    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")
    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "audio.kicad_sch"
    pro = tmp_path / "audio.kicad_pro"

    r1 = design(operation="import_firmware", firmware_path=str(AUDIO_CONFIG_H),
                out_path=str(intent))
    assert r1["status"] == "ok"
    assert r1["board"] == "esp32-s3-devkitc-1"          # multi-board= prefers S3
    assert r1["summary"]["mcu"] == "ESP32-S3-WROOM-1"

    r2 = design(operation="expand_templates", intent_path=str(intent))
    assert r2["status"] == "ok"

    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok" and not r3["unresolved_endpoints"]

    pro.write_text(json.dumps(_MINIMAL_PRO))
    r4 = build(project_path=str(pro), board_width_mm=110, board_height_mm=90,
               autoroute_passes=2, export_gerbers=False)
    assert r4["status"] == "ok"
    assert r4["pads_assigned"] > 0
    assert r4["incomplete_nets"] == 0                    # conflict-free fixture routes fully
    assert r4["steps"]["zones"]["zones_added"] >= 1

    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
    nl = extract_netlist_via_cli(str(sch))
    vals = [c.get("value") for c in nl["components"].values()]
    assert vals.count("MAX98357A") == 4                 # two stereo amp pairs
    assert vals.count("SPH0645LM4H") == 1

    def refs_on(net):
        return {x["component"] for x in nl["nets"].get(net, [])}
    assert "U1" in refs_on("+3V3")                       # S3 powered
    assert len(refs_on("I2S0_BCLK")) == 3               # MCU + 2 amps share the clock
