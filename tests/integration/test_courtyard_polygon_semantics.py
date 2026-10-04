"""Courtyard semantics against the REAL consumer (pcbnew + KiCad DRC).

Two invariants, both found the hard way (issues #150/#151):

1. ``courtyards_collide`` agrees with KiCad's own ``courtyards_overlap`` DRC at
   the boundaries (0.1 mm overlap / touching / gap / opposite sides), on every
   KiCad version in the CI matrix.  The unit tests only pin the helper's own
   decisions with fakes; this pins that the predicate means what DRC means.

2. ``drc(operation="autofix")`` triggered by ONE genuine courtyard overlap must
   not shove parts that merely sit inside the bounding box of a non-rectangular
   courtyard.  The ESP32 module's courtyard is a T-shaped polygon; its bbox
   swallows the notches beside the body.  Before the fix, one real overlap
   elsewhere made the nudge move ~15 footprints (U1 by ~22 mm) and took
   ``courtyards_overlap`` from 1 to 12.
"""

import asyncio
import json
import os

import pytest

from kicad_mcp.utils.keepout_helpers import COURTYARD_COLLIDE_HELPER

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)

_MINIMAL_PRO = {
    "board": {"design_settings": {}},
    "net_settings": {
        "classes": [{"name": "Default", "clearance": 0.2, "track_width": 0.25,
                     "via_diameter": 0.6, "via_drill": 0.3}],
        "meta": {"version": 3},
    },
    "meta": {"filename": "board.kicad_pro", "version": 1},
}

_LIB_PREAMBLE = """
import pcbnew, json, sys, os
params = json.loads(open(sys.argv[1]).read())
_fp_root = os.environ["KICAD_APP_PATH"] + "/Contents/SharedSupport/footprints/"

def load_fp(lib, name, ref):
    fp = pcbnew.FootprintLoad(_fp_root + lib + ".pretty", name)
    assert fp is not None, "footprint not found: %s:%s" % (lib, name)
    fp.SetReference(ref)
    return fp

def mm(v):
    return pcbnew.FromMM(v)
"""


def _run(script, **params):
    from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
    return run_pcbnew_script(script, params=params, timeout=120.0)


def _drc_categories(pcb_path):
    from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
    res = asyncio.run(run_drc_via_cli(pcb_path, ctx=None))
    assert res["status"] == "ok", res
    return res.get("violation_categories", {})


def _write_pro(pcb_path):
    pro = str(pcb_path).replace(".kicad_pcb", ".kicad_pro")
    with open(pro, "w") as f:
        json.dump(_MINIMAL_PRO, f)
    return pro


# ---------------------------------------------------------------------------
# 1. predicate == DRC at the boundaries
# ---------------------------------------------------------------------------

_PAIR_SCRIPT = _LIB_PREAMBLE + COURTYARD_COLLIDE_HELPER + """
b = pcbnew.CreateEmptyBoard()
for ref in ("R1", "R2"):
    fp = load_fp("Resistor_SMD", "R_0603_1608Metric", ref)
    fp.SetPosition(pcbnew.VECTOR2I(mm(20), mm(20)))
    b.Add(fp)
b.Save(params["dst"])

# courtyard caches only exist on a LOADED board, which is what the nudge sees
b = pcbnew.LoadBoard(params["dst"])
f = {x.GetReference(): x for x in b.GetFootprints()}
pa = f["R1"].GetCourtyard(pcbnew.F_CrtYd); pc = f["R2"].GetCourtyard(pcbnew.F_CrtYd)
dx = (pa.BBox().GetRight() - pc.BBox().GetX()) + params["delta_nm"]
f["R2"].SetPosition(pcbnew.VECTOR2I(mm(20) + dx, mm(20)))
if params["flip_r2"]:
    f["R2"].Flip(f["R2"].GetPosition(), True)
b.Save(params["dst"])

b = pcbnew.LoadBoard(params["dst"])
f = {x.GetReference(): x for x in b.GetFootprints()}
pa = f["R1"].GetCourtyard(pcbnew.F_CrtYd); pc = f["R2"].GetCourtyard(pcbnew.F_CrtYd)
print(json.dumps({"collide": courtyards_collide(f["R1"], f["R2"]),
                  "gap_nm": int(pc.BBox().GetX() - pa.BBox().GetRight())}))
"""


@pytest.mark.parametrize("delta_nm, flip_r2, expected", [
    (-100_000, False, True),    # overlap 0.1 mm  -> DRC flags it
    (-10_000,  False, False),   # overlap 10 um   -> below DRC's tolerance
    (-1,       False, False),   # overlap 1 nm
    (0,        False, False),   # touching: CLEAR in DRC (unlike the bbox nudge)
    (1,        False, False),   # gap 1 nm
    (10_000,   False, False),   # gap 0.01 mm
    (-100_000, True,  False),   # R2 on the BACK: DRC never compares sides
])
def test_courtyards_collide_matches_drc(tmp_path, delta_nm, flip_r2, expected):
    pcb = str(tmp_path / "pair.kicad_pcb")
    res = _run(_PAIR_SCRIPT, dst=pcb, delta_nm=delta_nm, flip_r2=flip_r2)
    _write_pro(pcb)
    drc_flags = "courtyards_overlap" in _drc_categories(pcb)
    assert drc_flags is expected, f"fixture drifted: DRC says {drc_flags} (gap {res['gap_nm']} nm)"
    assert res["collide"] is expected, "courtyards_collide disagrees with KiCad DRC"


# ---------------------------------------------------------------------------
# 2. drc(autofix) leaves non-colliding parts alone when one genuine overlap exists
# ---------------------------------------------------------------------------

_BOARD_SCRIPT = _LIB_PREAMBLE + COURTYARD_COLLIDE_HELPER + """
b = pcbnew.CreateEmptyBoard()
# a real outline so the nudge's containment logic has a board to respect
for (x0, y0, x1, y1) in ((0, 0, 150, 0), (150, 0, 150, 100), (150, 100, 0, 100), (0, 100, 0, 0)):
    seg = pcbnew.PCB_SHAPE(b); seg.SetShape(pcbnew.SHAPE_T_SEGMENT); seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetStart(pcbnew.VECTOR2I(mm(x0), mm(y0))); seg.SetEnd(pcbnew.VECTOR2I(mm(x1), mm(y1))); b.Add(seg)

def add(lib, name, ref, x, y):
    fp = load_fp(lib, name, ref); fp.SetPosition(pcbnew.VECTOR2I(mm(x), mm(y))); b.Add(fp); return fp

add("RF_Module", "ESP32-WROOM-32E", "U1", 60, 40)
add("Resistor_SMD", "R_0603_1608Metric", "R1", 120, 80)
add("Resistor_SMD", "R_0603_1608Metric", "R2", 120.3, 80)       # GENUINE overlap with R1
b.Save(params["dst"])

# find a spot for R3 that is inside U1's courtyard BBOX but outside its real polygon
b = pcbnew.LoadBoard(params["dst"])
f = {x.GetReference(): x for x in b.GetFootprints()}
u1 = f["U1"]; poly = u1.GetCourtyard(pcbnew.F_CrtYd); bb = poly.BBox()
r3 = load_fp("Resistor_SMD", "R_0603_1608Metric", "R3"); b.Add(r3)
placed = None
y = bb.GetY() + mm(2)
while y < bb.GetBottom() - mm(2) and placed is None:
    x = bb.GetX() + mm(2)
    while x < bb.GetRight() - mm(2):
        r3.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
        if hasattr(r3, "BuildCourtyardCaches"):   # KiCad 9: a fresh (not loaded) footprint has none
            r3.BuildCourtyardCaches()
        p3 = r3.GetCourtyard(pcbnew.F_CrtYd)
        b3 = p3.BBox()
        inside_bbox = (b3.GetX() >= bb.GetX() and b3.GetRight() <= bb.GetRight()
                       and b3.GetY() >= bb.GetY() and b3.GetBottom() <= bb.GetBottom())
        if inside_bbox and not courtyards_collide(u1, r3):
            placed = (pcbnew.ToMM(int(x)), pcbnew.ToMM(int(y))); break
        x += mm(1)
    y += mm(1)
b.Save(params["dst"])
print(json.dumps({"notch_found": placed is not None,
                  "u1_bbox_mm": [pcbnew.ToMM(bb.GetWidth()), pcbnew.ToMM(bb.GetHeight())],
                  "u1_area_ratio": poly.Area() / (bb.GetWidth() * bb.GetHeight())}))
"""

_POSITIONS_SCRIPT = _LIB_PREAMBLE + """
b = pcbnew.LoadBoard(params["pcb_path"])
print(json.dumps({"pos": {f.GetReference(): [round(pcbnew.ToMM(f.GetPosition().x), 3),
                                              round(pcbnew.ToMM(f.GetPosition().y), 3)]
                          for f in b.GetFootprints()}}))
"""


def test_drc_autofix_ignores_parts_in_a_nonrectangular_courtyards_notch(tmp_path):
    from kicad_mcp.server import create_server

    pcb = str(tmp_path / "esp.kicad_pcb")
    built = _run(_BOARD_SCRIPT, dst=pcb)
    pro = _write_pro(pcb)

    # fixture validity: the T-shaped courtyard really is much smaller than its bbox,
    # and R3 really sits in a notch (bbox overlap, no polygon collision)
    assert built["u1_area_ratio"] < 0.85, built
    assert built["notch_found"], built

    before_cats = _drc_categories(pcb)
    assert before_cats.get("courtyards_overlap") == 1, \
        f"only the R1/R2 overlap may be flagged (R3-in-the-notch must not be): {before_cats}"
    before = _run(_POSITIONS_SCRIPT, pcb_path=pcb)["pos"]

    drc = asyncio.run(create_server().get_tool("drc")).fn
    result = asyncio.run(drc(operation="autofix", ctx=None, pcb_path=pcb, project_path=pro,
                             fix_routing=False, fix_silkscreen=False, fix_placement=True))
    assert result["status"] == "ok", result

    after = _run(_POSITIONS_SCRIPT, pcb_path=pcb)["pos"]
    moved = sorted(r for r in before if before[r] != after[r])
    assert moved and set(moved) <= {"R1", "R2"}, \
        f"autofix moved parts it had no reason to touch: {moved}"
    assert "courtyards_overlap" not in _drc_categories(pcb), "the genuine overlap was not fixed"
