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


def _final_hole_positions(pcb_path):
    """The H* mounting-hole positions as they ACTUALLY ended up on the board (mm).
    Reads the placed board, not a step's report — placement can move a footprint
    after a step records its intended spot (the bug this guards: holes that the
    create step put at the corners were silently spiral-placed into the interior)."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = (
        "import pcbnew, json, sys\n"
        "b = pcbnew.LoadBoard(json.loads(open(sys.argv[1]).read())['pcb'])\n"
        "out = {}\n"
        "for fp in b.GetFootprints():\n"
        "    r = fp.GetReference()\n"
        "    if r and r[0] == 'H' and r[1:].isdigit():\n"
        "        p = fp.GetPosition()\n"
        "        out[r] = [round(pcbnew.ToMM(p.x), 2), round(pcbnew.ToMM(p.y), 2)]\n"
        "print(json.dumps(out))\n"
    )
    return run_pcbnew_script(script, params={"pcb": pcb_path}, timeout=60.0)


def _terminal_hole_overlaps(pcb_path):
    """Pairs of (terminal J*, mounting hole H*) whose courtyards OVERLAP — i.e. a
    terminal sitting on a hole. Empty = clear. Uses GetBoundingBox(False, False)
    (body+courtyard, no text bloat) for an AABB test. Guards the bug where a
    field terminal was forced onto a corner hole because the keepout was
    routing-only and the layout didn't reserve the corner."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = (
        "import pcbnew, json, sys\n"
        "b = pcbnew.LoadBoard(json.loads(open(sys.argv[1]).read())['pcb'])\n"
        "def box(fp):\n"
        "    bb = fp.GetBoundingBox(False, False)\n"
        "    return (bb.GetX(), bb.GetY(), bb.GetRight(), bb.GetBottom())\n"
        "J = {f.GetReference(): box(f) for f in b.GetFootprints()\n"
        "     if f.GetReference().startswith('J')}\n"
        "H = {f.GetReference(): box(f) for f in b.GetFootprints()\n"
        "     if f.GetReference()[:1] == 'H' and f.GetReference()[1:].isdigit()}\n"
        "out = []\n"
        "for jr, (jx0, jy0, jx1, jy1) in J.items():\n"
        "    for hr, (hx0, hy0, hx1, hy1) in H.items():\n"
        "        if jx0 <= hx1 and jx1 >= hx0 and jy0 <= hy1 and jy1 >= hy0:\n"
        "            out.append([jr, hr])\n"
        "print(json.dumps({'overlaps': out}))\n"   # object, not bare array (bridge needs {})
    )
    return run_pcbnew_script(script, params={"pcb": pcb_path}, timeout=60.0)["overlaps"]


def _terminal_centers(pcb_path):
    """Each field-terminal J*'s courtyard centre (mm) AND the board centre — for the
    terminal-centering gate (a centred edge group's mid-point sits near the board
    mid-point on that edge's along-axis)."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = (
        "import pcbnew, json, sys\n"
        "b = pcbnew.LoadBoard(json.loads(open(sys.argv[1]).read())['pcb'])\n"
        "be = b.GetBoardEdgesBoundingBox()\n"
        "out = {'board': [pcbnew.ToMM((be.GetLeft()+be.GetRight())//2),\n"
        "                 pcbnew.ToMM((be.GetTop()+be.GetBottom())//2)], 'J': {}}\n"
        "for f in b.GetFootprints():\n"
        "    r = f.GetReference()\n"
        "    if r.startswith('J'):\n"
        "        bb = f.GetBoundingBox(False, False)\n"
        "        out['J'][r] = [pcbnew.ToMM((bb.GetLeft()+bb.GetRight())//2),\n"
        "                       pcbnew.ToMM((bb.GetTop()+bb.GetBottom())//2)]\n"
        "print(json.dumps(out))\n"
    )
    return run_pcbnew_script(script, params={"pcb": pcb_path}, timeout=60.0)


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
    if not hasattr(fp, 'Zones'):     # match the production guard (older pcbnew API)
        continue
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


def _legend_label_placement(pcb_path, legend_texts, edge_of):
    """Check the silk legends + refdes of EDGE terminals. ``edge_of`` maps each
    edge-terminal ref -> its edge (top/bottom/left/right). Spec §6: per-position
    labels must be readable beside the block (inboard), NEVER under the body; and
    the refdes must be a clean callout beyond the block on the inboard side.
    Returns counts of {checked, outboard, under_block, refdes_misplaced}."""
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    script = '''
import pcbnew, json, sys
p = json.loads(open(sys.argv[1]).read())
b = pcbnew.LoadBoard(p["pcb_path"])
texts = set(p["legend_texts"])
edge_of = p["edge_of"]
edge_refs = set(edge_of)
# Inboard (inward-normal) unit step per edge, KiCad +Y down.
INB = {"top": (0, 1), "bottom": (0, -1), "left": (1, 0), "right": (-1, 0)}
e = b.GetBoardEdgesBoundingBox()
cx = (e.GetX() + e.GetRight()) / 2.0
cy = (e.GetY() + e.GetBottom()) / 2.0
pads = []      # (pad_x, pad_y, term_x, term_y)
bodies = []    # (x0, y0, x1, y1) body envelopes of the edge terminals (the BLOCKS)
terms = []     # (edge, body, refdes_x, refdes_y) for the refdes check
for fp in b.GetFootprints():
    if fp.GetReference() not in edge_refs:
        continue
    fpos = fp.GetPosition()
    for pad in fp.Pads():
        pp = pad.GetPosition()
        pads.append((pp.x, pp.y, fpos.x, fpos.y))
    bb = fp.GetBoundingBox(False, False)
    body = (bb.GetX(), bb.GetY(), bb.GetRight(), bb.GetBottom())
    bodies.append(body)
    rp = fp.Reference().GetPosition()
    terms.append((edge_of[fp.GetReference()], body, rp.x, rp.y))
# Reference designator must be a clean callout BEYOND the block on the terminal's
# INBOARD side (away from its edge) — not shoved to the side at pad level (the
# wide-terminal bug the silk auto-fixer caused).
refdes_misplaced = 0
for (edge, (x0, y0, x1, y1), rdx, rdy) in terms:
    ix, iy = INB.get(edge, (0, 0))
    if iy < 0:    ok = rdy < y0    # inboard up (bottom edge)
    elif iy > 0:  ok = rdy > y1    # inboard down (top edge)
    elif ix > 0:  ok = rdx > x1    # inboard right (left edge)
    elif ix < 0:  ok = rdx < x0    # inboard left (right edge)
    else:         ok = True
    if not ok:
        refdes_misplaced += 1
checked = 0
outboard = 0
under_block = 0
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
    # A label whose CENTRE lands inside any terminal's body envelope is hidden
    # UNDER the plastic block (the §6 failure the inboard-of-pad check missed).
    if any(x0 <= lp.x <= x1 and y0 <= lp.y <= y1 for (x0, y0, x1, y1) in bodies):
        under_block += 1
print(json.dumps({"checked": checked, "outboard": outboard, "under_block": under_block,
                  "refdes_misplaced": refdes_misplaced}))
'''
    return run_pcbnew_script(script, params={"pcb_path": pcb_path,
                                             "legend_texts": list(legend_texts),
                                             "edge_of": dict(edge_of)}, timeout=30.0)


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
    # add_mounting_holes=False: this explicit size was calibrated pre-Phase-5 and
    # is tight; the test gates nets + routing, not holes (holes-on is gated by the
    # roomy audio_s3 and the auto-sized audio-remote boards).
    r4 = build(project_path=str(pro), board_width_mm=90, board_height_mm=75,
               autoroute_passes=2, export_gerbers=False, add_mounting_holes=False)
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

    # Part resolution (C4-C7): the firmware NAMES MAX98357A in a bus comment, so
    # both I2S_OUT amp buses bind to it from the corpus (not invented). The mic
    # bus names no part → a disclosed assumption, never a silent substitution.
    rp = r1["resolved_parts"]
    i2s_out = {v["part"] for k, v in rp.items() if v["via"] == "corpus"}
    assert "MAX98357A" in i2s_out, f"MAX98357A not resolved from corpus: {rp}"
    assert all(v["via"] == "corpus" for k, v in rp.items()
               if v["part"] == "MAX98357A")

    r2 = design(operation="expand_templates", intent_path=str(intent))
    assert r2["status"] == "ok"
    # The unnamed mic is realized as a DISCLOSED assumption at expand time, never a
    # silent substitution — and expand surfaces the gap so the user sees it.
    assert any(g["kind"] == "assumed_part" for g in r2["gaps"]), \
        "the unnamed mic should surface a disclosed assumed_part gap"

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
    # §4 content-aware auto-size (board dims 0,0 = estimate). The human-rational
    # layout puts ALL field-wiring terminals along ONE edge (opposite the antenna,
    # §1), so the board is wide+short (a letterbox: ~115x58) — the width seats the
    # 6 terminals end-to-end, the height is cluster + one terminal band. Smaller in
    # AREA than the 110x90 a fixed build hardcoded, with no dead middle band.
    r4 = build(project_path=str(pro), board_width_mm=0, board_height_mm=0,
               autoroute_passes=4, export_gerbers=False, intent_path=str(intent))
    assert r4["status"] == "ok"
    assert r4["pads_assigned"] > 0
    _assert_mostly_routed(r4, max_unrouted=4)
    assert r4["steps"]["zones"]["zones_added"] >= 1

    # Auto-sized, content-aware (a wide letterbox), and beats 110x90 on area.
    assert r4["steps"]["create_pcb"]["auto_sized"] is True
    bw, bh = r4["board_width_mm"], r4["board_height_mm"]
    assert 95 <= bw <= 140 and 45 <= bh <= 75, \
        f"content-aware size {bw}x{bh} outside the expected wide-letterbox window"
    assert bw > bh, "terminals share one edge -> board should be wider than tall"
    assert bw * bh < 110 * 90, "auto-size did not beat the hardcoded 110x90 on area"
    # Everything still seated — an under-estimate would leave unplaced parts.
    assert not r4["steps"]["smart_placement"].get("failed_placements"), \
        "content-aware size left parts unplaced (under-estimate)"

    # §3 mounting holes (Phase 5): 4 corner M3 fixtures that ACTUALLY ended up at
    # the corners on the placed+routed board (read the board, not the step report —
    # the placer used to silently move fixed-hint holes into the interior). The
    # board still routes within bound above, so the corner keepouts didn't break it.
    assert r4["steps"]["mounting_holes"]["holes_added"] == 4
    hp = _final_hole_positions(r4["pcb_path"])
    assert set(hp) == {"H1", "H2", "H3", "H4"}
    for ref, (x, y) in hp.items():
        near_x = x < 7.0 or x > bw - 7.0
        near_y = y < 7.0 or y > bh - 7.0
        assert near_x and near_y, \
            f"{ref} at ({x},{y}) is not near a corner of {bw}x{bh} (moved off-corner)"
    # And no field terminal sits ON a corner hole (courtyards must not overlap).
    overlaps = _terminal_hole_overlaps(r4["pcb_path"])
    assert overlaps == [], f"terminal(s) overlap mounting hole(s): {overlaps}"

    # §1 RFI: ALL field-wiring terminals share the SINGLE edge opposite the antenna
    # (the MCU antenna overhangs the top, so terminals are on the bottom) — none
    # buried interior, none on the antenna edge.
    _term_edges = {d["edge"] for d in r4["steps"]["smart_placement"]["placement_decisions"]
                   if d["event"] == "rotation_chosen"}
    assert _term_edges == {"bottom"}, \
        f"field-wiring terminals not all on the antenna-opposite edge: {_term_edges}"

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
    edge_of = {d["ref"]: d["edge"] for d in rot if d.get("edge")}
    place = _legend_label_placement(r4["pcb_path"], legend_labels, edge_of)
    assert place["checked"] > 0, "no edge-terminal legend labels matched to pads"
    assert place["outboard"] == 0, (
        f"{place['outboard']}/{place['checked']} edge-terminal legend labels placed "
        f"outboard (under the body) — §6 wants them inboard, clear of the block")
    # …and clear of the BLOCK body, not just the pad: a label inside the terminal's
    # body envelope is hidden UNDER the plastic (the plastic foot extends ~3mm past
    # the pads inboard). This pins the fix for a bug the inboard-of-pad check missed.
    assert place["under_block"] == 0, (
        f"{place['under_block']}/{place['checked']} legend labels sit UNDER a terminal "
        f"block (hidden under the plastic) — §6 wants them on exposed board")
    # Reference designators are clean callouts beyond the block, not shoved to the
    # side at pad level (the wide-terminal J5/J7 bug the silk auto-fixer caused).
    assert place["refdes_misplaced"] == 0, (
        f"{place['refdes_misplaced']} terminal refdes misplaced (not a clear callout "
        f"beyond the block on the inboard side)")


def test_audio_remote_multi_edge_distribution(mcp_server, tmp_path):
    """board.yaml ``terminal_distribution: multi_edge`` spreads the field terminals
    across the antenna-opposite edge AND the two side edges (vs the single-edge
    default the test above guards) → a squarer, smaller board. Pins the real-board
    invariants: terminals on ≤3 edges (never the antenna), oriented wire-entry
    OUTWARD on the side edges too, side-edge legends clear of pads, still routes,
    and no antenna_frame_mismatch. See SPEC_Multi_Edge_Terminal_Distribution.md."""
    import shutil

    from kicad_mcp.utils.firmware.intent import load_intent
    from kicad_mcp.utils.placement.edge_terminal import (
        natural_ref_key, outward_normal, rotation_to_face)
    from kicad_mcp.utils.placement.wire_entry import WIRE_ENTRY
    from kicad_mcp.tools.pcb_silkscreen import _op_check_silkscreen_overlaps

    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")

    fw = tmp_path / "fw"
    fw.mkdir()
    shutil.copy(AUDIO_CONFIG_H, fw / "config.h")
    shutil.copy(AUDIO_CONFIG_H.parent / "platformio.ini", fw / "platformio.ini")
    (fw / "board.yaml").write_text(
        "terminal_distribution: multi_edge\n"
        "placement:\n"
        "  CMCA_MIC: {locus: remote, device: INMP441}\n"
        "  CMCA_PRESENCE: {locus: remote, device: LD2410}\n"
        "  CMCA_I2S: {locus: on_board_with_remote_io, device: MAX98357A, external_io: [outp, outn]}\n"
        "  CMCA_I2S2: {locus: on_board_with_remote_io, device: MAX98357A, external_io: [outp, outn]}\n"
    )
    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "ar.kicad_sch"
    pro = tmp_path / "ar.kicad_pro"

    assert design(operation="import_firmware", firmware_path=str(fw / "config.h"),
                  out_path=str(intent))["status"] == "ok"
    assert design(operation="expand_templates", intent_path=str(intent))["status"] == "ok"
    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok" and not r3["unresolved_endpoints"]

    pro.write_text(json.dumps(_MINIMAL_PRO))
    r4 = build(project_path=str(pro), board_width_mm=0, board_height_mm=0,
               autoroute_passes=4, export_gerbers=False, intent_path=str(intent))
    assert r4["status"] == "ok"
    _assert_mostly_routed(r4, max_unrouted=4)

    bw, bh = r4["board_width_mm"], r4["board_height_mm"]
    # Squarer than the single-edge letterbox the sibling test pins (~129x65, AR≈2.0).
    assert max(bw, bh) / min(bw, bh) < 1.6, f"multi-edge board not squarer: {bw}x{bh}"
    assert bw * bh < 129 * 65, f"multi-edge area {bw*bh} not smaller than single-edge"

    rot = [d for d in r4["steps"]["smart_placement"]["placement_decisions"]
           if d["event"] == "rotation_chosen"]
    edges = {d["edge"] for d in rot}
    # Spread across opposite + side edges; NEVER the antenna edge (top here).
    assert edges <= {"bottom", "left", "right"} and "top" not in edges
    assert len(edges) >= 2, f"multi_edge did not spread terminals: {edges}"
    # Per-edge natural ref order still holds (the layout gate).
    by_edge: dict = {}
    for d in rot:
        by_edge.setdefault(d["edge"], []).append(d["ref"])
    for e, refs in by_edge.items():
        assert refs == sorted(refs, key=natural_ref_key), f"{e} out of order: {refs}"
    # Wire-entry faces OUTWARD on EVERY used edge — incl. the new side edges
    # (the A2 assumption: rotation_to_face works for left/right, not just top/bottom).
    mkds = WIRE_ENTRY["TerminalBlock_Phoenix_MKDS-1,5-N-5.08_1xN_P5.08mm_Horizontal"]
    for d in (x for x in rot if x.get("source") == "wire_entry"):
        assert d["angle"] == rotation_to_face(mkds, outward_normal(d["edge"])), \
            f"{d['ref']} on {d['edge']}: wire-entry not aimed outward"
    # No copper pad off the board (rotation-sign guard on the side edges too).
    assert not _refs_with_pads_off_board(r4["pcb_path"]), "pads off the board outline"

    # Side-edge legends render clear of every pad (A1 — the crowding that drove the
    # original single-edge decision; the side_silk_gap must actually clear them).
    legend_labels = {p for L in load_intent(str(intent)).connector_legends
                     for p in L.positions if p}
    ov = _op_check_silkscreen_overlaps(str(pro).replace(".kicad_pro", ".kicad_pcb"))
    pad_hits = {o.get("silk_text") for o in ov.get("overlaps", [])}
    assert not (pad_hits & legend_labels), \
        f"legend label(s) overlap a pad: {sorted(pad_hits & legend_labels)}"

    # The frame guard stayed silent (parent/script antenna edge agree, A4).
    assert not any("antenna_frame_mismatch" in w for w in (r4.get("warnings") or []))


def test_audio_remote_terminal_centering(mcp_server, tmp_path):
    """board.yaml ``terminal_centering: true`` centres each edge's field-terminal
    group within its edge instead of packing it at one end (Part B of
    SPEC_Post_Placement_Board_Refit.md). Layout balance only — the board size is
    unchanged. Combined here with multi_edge so all three used edges are exercised.
    Pins: each used edge's terminal group is centred (group mid ≈ board mid on that
    edge's axis), and the placement is still valid (on-edge, ordered, oriented,
    pads on-board, silk clear, routes)."""
    import shutil

    from kicad_mcp.utils.placement.edge_terminal import natural_ref_key
    from kicad_mcp.tools.pcb_silkscreen import _op_check_silkscreen_overlaps

    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")

    fw = tmp_path / "fw"
    fw.mkdir()
    shutil.copy(AUDIO_CONFIG_H, fw / "config.h")
    shutil.copy(AUDIO_CONFIG_H.parent / "platformio.ini", fw / "platformio.ini")
    (fw / "board.yaml").write_text(
        "terminal_distribution: multi_edge\n"
        "terminal_centering: true\n"
        "placement:\n"
        "  CMCA_MIC: {locus: remote, device: INMP441}\n"
        "  CMCA_PRESENCE: {locus: remote, device: LD2410}\n"
        "  CMCA_I2S: {locus: on_board_with_remote_io, device: MAX98357A, external_io: [outp, outn]}\n"
        "  CMCA_I2S2: {locus: on_board_with_remote_io, device: MAX98357A, external_io: [outp, outn]}\n"
    )
    intent = tmp_path / "intent.yaml"
    sch = tmp_path / "ar.kicad_sch"
    pro = tmp_path / "ar.kicad_pro"

    assert design(operation="import_firmware", firmware_path=str(fw / "config.h"),
                  out_path=str(intent))["status"] == "ok"
    assert design(operation="expand_templates", intent_path=str(intent))["status"] == "ok"
    r3 = design(operation="generate_schematic", intent_path=str(intent),
                schematic_path=str(sch))
    assert r3["status"] == "ok" and not r3["unresolved_endpoints"]

    pro.write_text(json.dumps(_MINIMAL_PRO))
    r4 = build(project_path=str(pro), board_width_mm=0, board_height_mm=0,
               autoroute_passes=4, export_gerbers=False, intent_path=str(intent))
    assert r4["status"] == "ok"
    _assert_mostly_routed(r4, max_unrouted=4)

    rot = [d for d in r4["steps"]["smart_placement"]["placement_decisions"]
           if d["event"] == "rotation_chosen"]
    edge_of = {d["ref"]: d["edge"] for d in rot}
    assert set(edge_of.values()) <= {"bottom", "left", "right"}

    # Each used edge's terminal group is CENTRED: the midpoint of the group's
    # span (along that edge's axis) sits near the board midpoint. A start-packed
    # group would sit well off-centre toward one end.
    cen = _terminal_centers(r4["pcb_path"])
    bcx, bcy = cen["board"]
    by_edge: dict = {}
    for ref, edge in edge_of.items():
        by_edge.setdefault(edge, []).append(ref)
    for edge, refs in by_edge.items():
        horiz = edge in ("top", "bottom")
        coords = sorted(cen["J"][r][0 if horiz else 1] for r in refs)
        group_mid = (coords[0] + coords[-1]) / 2.0
        board_mid = bcx if horiz else bcy
        assert abs(group_mid - board_mid) < 6.0, \
            f"{edge} group not centred: mid {group_mid:.1f} vs board {board_mid:.1f}"
        # order still ascending along the edge
        ordered = sorted(refs, key=natural_ref_key)
        assert [cen["J"][r][0 if horiz else 1] for r in ordered] == \
            sorted(cen["J"][r][0 if horiz else 1] for r in ordered)

    # Still a valid placement: pads on-board, silk clear, no failed placements.
    assert not _refs_with_pads_off_board(r4["pcb_path"])
    assert not r4["steps"]["smart_placement"].get("failed_placements")
    ov = _op_check_silkscreen_overlaps(str(pro).replace(".kicad_pro", ".kicad_pcb"))
    assert not {o.get("silk_text") for o in ov.get("overlaps", [])} & {"SCL", "SDA", "BCLK"}


def test_approval_gate_audio_remote(mcp_server, tmp_path):
    """Phase 7 approval gate: build with approved=False returns a proposal of the
    PLACED (unrouted) board — real terminal edges, holes, a tweakable board.yaml,
    and (best-effort) a render — WITHOUT the expensive autoroute. Cheap (no route),
    so it gates the gate on real KiCad without the 3-4 min FreeRouter pass."""
    import shutil

    import yaml

    from kicad_mcp.utils.firmware.sidecar import load_sidecar

    design = _tool(mcp_server, "design")
    build = _tool(mcp_server, "build_pcb_from_schematic")

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
    design(operation="import_firmware", firmware_path=str(fw / "config.h"), out_path=str(intent))
    design(operation="expand_templates", intent_path=str(intent))
    design(operation="generate_schematic", intent_path=str(intent), schematic_path=str(sch))
    pro.write_text(json.dumps(_MINIMAL_PRO))

    r = build(project_path=str(pro), board_width_mm=0, board_height_mm=0,
              export_gerbers=False, intent_path=str(intent), approved=False)

    assert r["status"] == "pending_approval"
    assert "autoroute" not in r["steps"]          # the expensive pass was skipped
    prop = r["proposal"]
    assert prop["antenna_edge"] == "top"          # S3 antenna overhangs the top
    assert prop["terminal_table"], "no terminals in the proposal"
    assert {t["edge"] for t in prop["terminal_table"]} == {"bottom"}   # antenna-opposite
    assert len(prop["mounting_holes"]) == 4       # default corner holes proposed
    # And they ACTUALLY sit at the corners on the placed board (not just reported):
    bw, bh = prop["board_size_mm"]
    hp = _final_hole_positions(r["pcb_path"])
    assert set(hp) == {"H1", "H2", "H3", "H4"}
    for ref, (x, y) in hp.items():
        assert (x < 6.0 or x > bw - 6.0) and (y < 6.0 or y > bh - 6.0), \
            f"{ref} at ({x},{y}) not at a corner of {bw}x{bh}"
    # the proposed board.yaml is valid and round-trips through the sidecar loader
    yp = tmp_path / "proposed.yaml"
    yp.write_text(yaml.dump(prop["proposed_board_yaml"]))
    sc = load_sidecar(str(yp))
    assert sc.board_size_mm is not None and sc.mounting_holes is not None


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
    # best-of-6 (not 2/4): this dense I2C board routes to 0–2 (a buzzer-orphan net +
    # the odd structural one) but FreeRouter's tail occasionally leaves a 3rd —
    # best-of-2 flaked, then best-of-4 still hit the tail once on CI's KiCad 9.
    # Placement is unchanged (the silk step runs AFTER routing), so this is pure
    # router nondeterminism: more passes keeps the tight bound non-flaky rather
    # than loosening it (matches the dense audio_s3 board, also best-of-6).
    # add_mounting_holes=False: explicit pre-Phase-5 size, tight; gates nets +
    # routing (holes-on gated by audio_s3 + audio-remote).
    r4 = build(project_path=str(pro), board_width_mm=90, board_height_mm=75,
               autoroute_passes=6, export_gerbers=False, add_mounting_holes=False)
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
    # add_mounting_holes=False: explicit pre-Phase-5 size, tight; gates the sidecar
    # connector route, not holes (holes-on gated by audio_s3 + audio-remote).
    r4 = build(project_path=str(pro), board_width_mm=70, board_height_mm=55,
               autoroute_passes=2, export_gerbers=False, add_mounting_holes=False)
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
