"""End-to-end firmware->PCB golden harness (the session meta-finding's fix).

A flagship pipeline tool (`build_pcb_from_schematic`) was once 100% broken on
KiCad 10 with ZERO integration coverage — test count != pipeline confidence.
This gate runs the WHOLE arc on real KiCad (9 and 10):

    config.h -> design intent -> expand templates -> generate schematic
             -> build routed PCB

and asserts **version-robust invariants** — component count, by-component-ref net
membership, and mostly-complete routing (see ``_assert_mostly_routed``) — NOT
version-fragile pin numbers (which drift between KiCad symbol-library versions).
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
TRACK_GEOM_CONFIG_H = FIXTURE / "track_geometry" / "config.h"
SIDECAR_CONFIG_H = FIXTURE / "sidecar_demo" / "config.h"

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


def _assert_mostly_routed(r4, max_unrouted):
    """Assert the board routed essentially completely, within ``max_unrouted``.

    ``incomplete_nets`` is the SES-import-MEASURED unconnected count — KiCad's
    own ratsnest, read back from the actual routed board (the source of truth).
    We never parse FreeRouter's prose log; the pipeline reports this measurement
    and best-of-N pass selection ranks each pass by re-measuring it (see
    ``_select_best_pass`` / ``_measure_ses_unconnected`` in pcb_autoroute).

    FreeRouter is a heuristic, nondeterministic router, so the count has a small
    run-to-run spread. The simple boards route to 0–1; the dense audio node has
    ~2 structurally-hard nets that no pass clears (a placement follow-up, not a
    regression). ``max_unrouted`` is set per board to observed-max + 1 margin, so
    the gate stays non-flaky on both KiCad versions while still catching a real
    routing regression (a broken board leaves far more than a couple unrouted).
    The EXACT design-correctness checks are the deterministic by-ref connectivity
    invariants each test asserts below.
    """
    assert r4["incomplete_nets"] is not None, "routing produced no measured count"
    assert r4.get("tracks", 0) > 0, "no routed copper — dead board"
    assert r4["incomplete_nets"] <= max_unrouted, (
        f"{r4['incomplete_nets']} unconnected nets exceeds the bound of "
        f"{max_unrouted} — a real routing regression, not heuristic noise"
    )


def _refs_with_pads_off_board(pcb_path):
    """Refs whose copper PADS extend outside the Edge.Cuts outline (run on real
    KiCad). The board body may overhang the edge, but pads carry copper and must
    stay on-board. This is the assertion that pins the terminal-rotation SIGN: a
    flipped 90/270 rotation drives terminal pads OFF the edge, and FreeRouter
    routes to off-board pads anyway — so routing-completeness alone does NOT
    catch it (it masked exactly this bug once)."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = '''
import pcbnew, json, sys
params = json.loads(open(sys.argv[1]).read())
b = pcbnew.LoadBoard(params["pcb_path"])
e = b.GetBoardEdgesBoundingBox()
X0, Y0 = pcbnew.ToMM(e.GetX()), pcbnew.ToMM(e.GetY())
X1, Y1 = pcbnew.ToMM(e.GetRight()), pcbnew.ToMM(e.GetBottom())
tol = 0.05
bad = []
for fp in b.GetFootprints():
    for p in fp.Pads():
        bb = p.GetBoundingBox()
        if (pcbnew.ToMM(bb.GetX()) < X0 - tol or pcbnew.ToMM(bb.GetY()) < Y0 - tol
                or pcbnew.ToMM(bb.GetRight()) > X1 + tol
                or pcbnew.ToMM(bb.GetBottom()) > Y1 + tol):
            bad.append(fp.GetReference())
            break
print(json.dumps({"off_board": sorted(set(bad))}))
'''
    res = run_pcbnew_script(script, params={"pcb_path": pcb_path}, timeout=30.0)
    return res.get("off_board", [])


def _refs_with_keepout_overhang(pcb_path):
    """Refs whose rule-area (antenna) keepout zone bbox extends OUTSIDE the
    Edge.Cuts outline. This pins spec §2: an MCU antenna keepout must overhang the
    board edge (the antenna radiates off-board, recovering interior copper). The
    tier-1 placer rotates the MCU so its keepout faces the edge's outward normal
    and seats the body flush; this asserts the overhang actually happened, not
    that we merely stopped inflating the envelope."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = '''
import pcbnew, json, sys
params = json.loads(open(sys.argv[1]).read())
b = pcbnew.LoadBoard(params["pcb_path"])
e = b.GetBoardEdgesBoundingBox()
X0, Y0 = pcbnew.ToMM(e.GetX()), pcbnew.ToMM(e.GetY())
X1, Y1 = pcbnew.ToMM(e.GetRight()), pcbnew.ToMM(e.GetBottom())
tol = 0.05
over = []
for fp in b.GetFootprints():
    for z in fp.Zones():
        if z.GetIsRuleArea():
            zb = z.GetBoundingBox()
            if (pcbnew.ToMM(zb.GetX()) < X0 - tol or pcbnew.ToMM(zb.GetY()) < Y0 - tol
                    or pcbnew.ToMM(zb.GetRight()) > X1 + tol
                    or pcbnew.ToMM(zb.GetBottom()) > Y1 + tol):
                over.append(fp.GetReference())
                break
print(json.dumps({"overhang": sorted(set(over))}))
'''
    res = run_pcbnew_script(script, params={"pcb_path": pcb_path}, timeout=30.0)
    return res.get("overhang", [])


def _legend_label_placement(pcb_path, legend_texts, edge_refs):
    """Count how many EDGE-terminal legend labels sit INBOARD vs OUTBOARD of their
    nearest pad. Spec §6: the label must be readable beside the block (inboard,
    toward board centre), NEVER under the body (outboard, the wire-entry side).
    Scoped to ``edge_refs`` (the terminals actually edge-placed) — interior module
    headers keep the default offset and are not under a one-sided body.
    Returns ``{"checked", "outboard"}``."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = '''
import pcbnew, json, sys
p = json.loads(open(sys.argv[1]).read())
b = pcbnew.LoadBoard(p["pcb_path"])
texts = set(p["legend_texts"])
edge_refs = set(p["edge_refs"])
e = b.GetBoardEdgesBoundingBox()
cx = (e.GetX() + e.GetRight()) / 2.0
cy = (e.GetY() + e.GetBottom()) / 2.0
pads = []  # (pad_x, pad_y, term_x, term_y)
for fp in b.GetFootprints():
    if fp.GetReference() not in edge_refs:
        continue
    fpos = fp.GetPosition()
    for pad in fp.Pads():
        pp = pad.GetPosition()
        pads.append((pp.x, pp.y, fpos.x, fpos.y))
checked = 0
outboard = 0
for d in b.GetDrawings():
    if not isinstance(d, pcbnew.PCB_TEXT) or d.GetText() not in texts:
        continue
    lp = d.GetPosition()
    best = None
    bd = None
    for (px, py, tx, ty) in pads:
        dd = (lp.x - px) ** 2 + (lp.y - py) ** 2
        if bd is None or dd < bd:
            bd = dd; best = (px, py, tx, ty)
    if best is None:
        continue
    px, py, tx, ty = best
    # dot of (label - pad) with the inboard direction (terminal -> board centre)
    dot = (lp.x - px) * (cx - tx) + (lp.y - py) * (cy - ty)
    checked += 1
    if dot <= 0:
        outboard += 1
print(json.dumps({"checked": checked, "outboard": outboard}))
'''
    return run_pcbnew_script(script, params={"pcb_path": pcb_path,
                                             "legend_texts": list(legend_texts),
                                             "edge_refs": list(edge_refs)}, timeout=30.0)


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
    _assert_mostly_routed(r4, max_unrouted=2)
    assert r4["steps"]["zones"]["zones_added"] >= 1

    # §2 antenna overhang: the ESP32 (U1) keepout must hang off a board edge,
    # while its copper pads stay on-board. (Spec Verification log, H1.)
    pcb_path = r4.get("pcb_path") or str(pro.with_suffix(".kicad_pcb"))
    assert "U1" in _refs_with_keepout_overhang(pcb_path), (
        "ESP32 antenna keepout did not overhang the board edge — tier-1 overhang "
        "placement regressed (or the keepout was merged back into fit-extents)"
    )
    assert "U1" not in _refs_with_pads_off_board(pcb_path), (
        "ESP32 pads went off-board — overhang must keep copper on-board"
    )

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
    # best-of-6 (not 4): the §2 antenna overhang seats the MCU flush at a board
    # edge, which concentrates this dense I2S board and widens FreeRouter's
    # run-to-run spread. Placement is deterministic and routes to 1–3 on a good
    # pass; passes=6 pulls the median to ~1–2. The bound is the convention's
    # observed-max + 1 (observed max 5 over many passes=6 runs) — loosened from 4
    # to 6 to reflect the denser overhang layout, NOT to mask breakage: a broken
    # board leaves far more than 6, and exact correctness is pinned by the by-ref
    # connectivity invariants below. (Tightening this back is a placement-quality
    # follow-up: cluster the I2S amps closer to the MCU so the overhang costs less.)
    r4 = build(project_path=str(pro), board_width_mm=110, board_height_mm=90,
               autoroute_passes=6, export_gerbers=False)
    assert r4["status"] == "ok"
    assert r4["pads_assigned"] > 0
    _assert_mostly_routed(r4, max_unrouted=6)            # dense I2S board + overhang spread
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


def test_audio_remote_to_routed_pcb(mcp_server, tmp_path):
    """Placement locus on the audio node: a board.yaml declares the mic + presence
    sensor REMOTE (field-wired) and the amps on_board_with_remote_io. The board
    builds with screw terminals instead of the SPH0645/LD2410, routes ~complete,
    and the synthesized terminals carry a silk legend (the field-wiring doc).

    The on-board audio path is still gated by test_audio_s3_to_routed_pcb above —
    this adds the remote shape without losing that coverage."""
    import shutil

    from kicad_mcp.tools.pcb_silkscreen import _op_check_silkscreen_overlaps

    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")

    # Stage the audio firmware + a board.yaml in tmp (board.yaml is auto-detected
    # next to config.h; the canonical fixture stays pristine / on-board).
    fw = tmp_path / "fw"
    fw.mkdir()
    shutil.copy(AUDIO_CONFIG_H, fw / "config.h")
    shutil.copy(AUDIO_CONFIG_H.parent / "platformio.ini", fw / "platformio.ini")
    (fw / "board.yaml").write_text(
        "placement:\n"
        "  CMCA_MIC: {locus: remote, device: INMP441}\n"
        "  CMCA_PRESENCE: {locus: remote, device: LD2410}\n"
        "  CMCA_I2S: {locus: on_board_with_remote_io, device: MAX98357A, external_io: [outp, outn]}\n"
        "  CMCA_I2S2: {locus: on_board_with_remote_io, device: MAX98357A, external_io: [outp, outn]}\n"
    )

    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "ar.kicad_sch"
    pro = tmp_path / "ar.kicad_pro"

    r1 = design(operation="import_firmware", firmware_path=str(fw / "config.h"),
                out_path=str(intent))
    assert r1["status"] == "ok"
    assert r1["sidecar"] is not None

    r2 = design(operation="expand_templates", intent_path=str(intent))
    assert r2["status"] == "ok"
    # After expansion the manifest reports the field-wired devices off-board, with
    # their terminals (the terminals are synthesized during template expansion).
    assert r2["summary"].get("remote_devices")

    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok" and not r3["unresolved_endpoints"]

    pro.write_text(json.dumps(_MINIMAL_PRO))
    # §4 content-aware auto-size (board dims 0,0 = estimate). This shape is the
    # spec's worked example: interior ~45x40 -> board ~70x60, vs the 110x90 a
    # fixed size would hardcode. The estimator excludes the antenna keepout
    # (overhangs, §2) and reserves terminal-edge perimeter.
    r4 = build(project_path=str(pro), board_width_mm=0, board_height_mm=0,
               autoroute_passes=4, export_gerbers=False, intent_path=str(intent))
    assert r4["status"] == "ok"
    assert r4["pads_assigned"] > 0
    _assert_mostly_routed(r4, max_unrouted=4)
    assert r4["steps"]["zones"]["zones_added"] >= 1

    # The board auto-sized to content, well under the 110x90 a fixed build used.
    assert r4["steps"]["create_pcb"]["auto_sized"] is True
    bw, bh = r4["board_width_mm"], r4["board_height_mm"]
    assert 58 <= bw <= 88 and 48 <= bh <= 76, \
        f"content-aware size {bw}x{bh} outside the expected ~70x60 window"
    assert bw * bh < 110 * 90, "auto-size did not beat the hardcoded 110x90"
    # Everything still seated — an under-estimate would leave unplaced parts.
    assert not r4["steps"]["smart_placement"].get("failed_placements"), \
        "content-aware size left parts unplaced (under-estimate)"

    # The silk-legend step ran and labelled the synthesized terminals.
    silk = r4["steps"]["silkscreen_legends"]
    assert silk.get("labels_added", 0) > 0
    assert not silk.get("missing_refs")

    # Human-rational placement: the synthesized screw terminals were rotated to
    # orthogonal angles and laid out in natural ref order along each edge. (The
    # rotation *sign* is pinned by _assert_mostly_routed above — a wrong sign
    # points pads off-board and routing collapses; here we pin orthogonality +
    # ordering + the decision/event surfacing.)
    from kicad_mcp.utils.placement.edge_terminal import natural_ref_key
    decisions = r4["steps"]["smart_placement"]["placement_decisions"]
    rot = [d for d in decisions if d["event"] == "rotation_chosen"]
    assert rot, "no terminal rotations recorded — expected synthesized J terminals"
    assert all(d["angle"] in (0, 90, 180, 270) for d in rot), \
        f"terminal rotations must be orthogonal: {[d['angle'] for d in rot]}"
    by_edge: dict = {}
    for d in rot:
        by_edge.setdefault(d["edge"], []).append(d["ref"])
    for _edge, _refs in by_edge.items():
        assert _refs == sorted(_refs, key=natural_ref_key), \
            f"terminals on {_edge} edge out of natural order: {_refs}"

    # M1 oracle (spec §1): the synthesized MKDS terminals are oriented from the
    # WIRE_ENTRY table + the edge's OUTWARD normal — NOT the pad-centroid proxy
    # the core lesson rejects. At least one terminal resolves via wire-entry, and
    # each such angle equals the table prediction (wire-entry face points
    # off-board). This is the deterministic check that replaces "came out right
    # by luck".
    from kicad_mcp.utils.placement.edge_terminal import outward_normal, rotation_to_face
    from kicad_mcp.utils.placement.wire_entry import WIRE_ENTRY
    we_rot = [d for d in rot if d.get("source") == "wire_entry"]
    assert we_rot, "no terminal oriented from WIRE_ENTRY — did they fall back to pad-centroid?"
    mkds_vec = WIRE_ENTRY["TerminalBlock_Phoenix_MKDS-1,5-N-5.08_1xN_P5.08mm_Horizontal"]
    for d in we_rot:
        expect = rotation_to_face(mkds_vec, outward_normal(d["edge"]))
        assert d["angle"] == expect, (
            f"{d['ref']} on {d['edge']}: wire-entry angle {d['angle']} != oracle "
            f"{expect} (WIRE_ENTRY vector aimed at the outward normal)")

    # Decisions surface as an mcp-events envelope on the build response.
    assert "events" in r4, "placement events not surfaced on the response"
    assert any(e["code"] == "rotation_chosen" for e in r4["events"])

    # NO copper pad may sit off the board outline (pins the rotation sign).
    off = _refs_with_pads_off_board(r4["pcb_path"])
    assert not off, f"footprints with pads off the board outline: {off}"

    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
    nl = extract_netlist_via_cli(str(sch))
    vals = [c.get("value") for c in nl["components"].values()]
    # NO on-board mic substitute / presence header — they are field-wired terminals.
    assert "SPH0645LM4H" not in vals
    assert vals.count("INMP441") == 1 and vals.count("LD2410") == 1
    # Amps stay on board (two stereo pairs).
    assert vals.count("MAX98357A") == 4

    def refs_on(net):
        return {x["component"] for x in nl["nets"].get(net, [])}
    # The mic terminal sits on the I2S mic clock the MCU drives.
    assert len(refs_on("MIC_BCLK")) >= 2     # MCU + terminal
    assert "U1" in refs_on("+3V3")

    # NO legend label overlaps a pad (the field-wiring silk must stay readable).
    # Scope the check to the legend labels (every position on every synthesized
    # terminal), NOT all board silk — a dense board may have pre-existing refdes
    # overlaps unrelated to this feature.
    from kicad_mcp.utils.firmware.intent import load_intent
    legend_labels = {pos for L in load_intent(str(intent)).connector_legends
                     for pos in L.positions if pos}
    assert legend_labels, "expected synthesized-terminal legends"
    ov = _op_check_silkscreen_overlaps(str(pro).replace(".kicad_pro", ".kicad_pcb"))
    pad_hits = {o.get("silk_text") for o in ov.get("overlaps", [])}
    assert not (pad_hits & legend_labels), (
        f"legend label(s) overlap a pad: {sorted(pad_hits & legend_labels)}")

    # §6: every EDGE-terminal legend label sits INBOARD of its pad (beside the
    # block, toward board centre) — never outboard, under the body / wire-entry.
    edge_refs = {d["ref"] for d in rot if d.get("edge")}
    place = _legend_label_placement(r4["pcb_path"], legend_labels, edge_refs)
    assert place["checked"] > 0, "no edge-terminal legend labels matched to pads"
    assert place["outboard"] == 0, (
        f"{place['outboard']}/{place['checked']} edge-terminal legend labels placed "
        f"outboard (under the body) — §6 wants them inboard, clear of the block")


def test_track_geometry_to_routed_pcb(mcp_server, tmp_path):
    """The THIRD board shape: an I2C sensor-hub (track-geometry car). Exercises
    the generalization that matters here — MULTIPLE address-declared devices,
    INCLUDING TWO OF THE SAME TYPE (dual MPU-6050 at 0x68/0x69), sharing one I2C
    bus, plus an OLED. Devices are modeled as breakout-module headers; AD0 is
    strapped per address. The buzzer GPIO stays a flagged orphan (no driver
    template yet) — which must NOT break routing."""
    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")
    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "tg.kicad_sch"
    pro = tmp_path / "tg.kicad_pro"

    r1 = design(operation="import_firmware", firmware_path=str(TRACK_GEOM_CONFIG_H),
                out_path=str(intent))
    assert r1["status"] == "ok"
    assert r1["board"] == "esp32dev"
    assert r1["summary"]["mcu"] == "ESP32-WROOM-32E"
    # Two MPU6050 instances + one OLED, all recognized off their *_ADDR macros.
    types = [p["type"] for p in r1["summary"]["peripherals"]]
    assert types.count("MPU6050") == 2 and types.count("OLED") == 1

    r2 = design(operation="expand_templates", intent_path=str(intent))
    assert r2["status"] == "ok"
    assert {"power_tree", "decoupling", "pullups"} <= set(r2["gaps_resolved"])

    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok" and not r3["unresolved_endpoints"]

    pro.write_text(json.dumps(_MINIMAL_PRO))
    r4 = build(project_path=str(pro), board_width_mm=90, board_height_mm=75,
               autoroute_passes=2, export_gerbers=False)
    assert r4["status"] == "ok"
    assert r4["pads_assigned"] > 0
    _assert_mostly_routed(r4, max_unrouted=2)            # buzzer orphan must not break routing
    assert r4["steps"]["zones"]["zones_added"] >= 1

    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
    nl = extract_netlist_via_cli(str(sch))
    assert nl is not None
    vals = [c.get("value") for c in nl["components"].values()]
    assert vals.count("GY-521 (MPU-6050)") == 2         # dual same-type I2C device
    assert vals.count("OLED (SSD1306)") == 1

    def refs_on(net):
        return {x["component"] for x in nl["nets"].get(net, [])}
    # The shared I2C bus joins the ESP32 (U1) + both MPU6050 (U2/U3) + OLED (U4).
    assert {"U1", "U2", "U3", "U4"} <= refs_on("I2C_SDA")
    assert {"U1", "U2", "U3", "U4"} <= refs_on("I2C_SCL")
    assert "U1" in refs_on("+3V3")


def test_sidecar_to_routed_pcb(mcp_server, tmp_path):
    """Phase 6b: a board.yaml sidecar supplies a firmware-blind external power
    connector. import auto-detects it, resolves the `connectors` gap, and the
    connector places + routes to the +5V/GND rails on a real board."""
    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")
    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "sc.kicad_sch"
    pro = tmp_path / "sc.kicad_pro"

    r1 = design(operation="import_firmware", firmware_path=str(SIDECAR_CONFIG_H),
                out_path=str(intent))
    assert r1["status"] == "ok"
    assert r1["sidecar"] is not None                     # board.yaml auto-detected
    # the firmware-blind `connectors` gap is resolved BY the sidecar
    conn_gap = [g for g in r1["gaps"] if g["kind"] == "connectors"]
    assert conn_gap  # (detail still listed; resolution is on the intent doc)

    r2 = design(operation="expand_templates", intent_path=str(intent))
    assert r2["status"] == "ok"

    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok" and not r3["unresolved_endpoints"]

    pro.write_text(json.dumps(_MINIMAL_PRO))
    r4 = build(project_path=str(pro), board_width_mm=70, board_height_mm=55,
               autoroute_passes=2, export_gerbers=False)
    assert r4["status"] == "ok"
    _assert_mostly_routed(r4, max_unrouted=2)            # connector routes

    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
    nl = extract_netlist_via_cli(str(sch))
    assert nl is not None
    # the sidecar power connector (value PWR_IN) sits on the +5V and GND rails.
    # Assert by VALUE since the friendly ref "J_PWR" is normalized to J<n>.
    comps = nl["components"]

    def has_pwr_in(net):
        return any(comps.get(x["component"], {}).get("value") == "PWR_IN"
                   for x in nl["nets"].get(net, []))

    assert has_pwr_in("+5V") and has_pwr_in("GND")
