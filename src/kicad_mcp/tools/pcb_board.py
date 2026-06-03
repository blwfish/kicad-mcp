"""PCB board management ops: load, create, outline, and design rules.

These are module-level _op_* helpers consumed by the pcb router (pcb.py).
"""

import logging
import os
from typing import Any, Dict, Optional

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def _op_load(pcb_path: str) -> Dict[str, Any]:
    """Load a .kicad_pcb file and return its summary."""
    if not os.path.exists(pcb_path):
        return {"error": f"File not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)
footprints = board.GetFootprints()
tracks = board.GetTracks()

fp_list = []
for fp in footprints:
    pos = fp.GetPosition()
    fp_list.append({
        "reference": fp.GetReference(),
        "value": fp.GetValue(),
        "footprint": fp.GetFPID().GetUniStringLibItemName(),
        "x_mm": pcbnew.ToMM(pos.x),
        "y_mm": pcbnew.ToMM(pos.y),
        "layer": board.GetLayerName(fp.GetLayer()),
    })

print(json.dumps({
    "status": "ok",
    "file": pcb_path,
    "footprint_count": len(fp_list),
    "track_count": len(tracks),
    "footprints": fp_list,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_create(pcb_path: str) -> Dict[str, Any]:
    """Create a new empty .kicad_pcb file."""
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.CreateEmptyBoard()
board.SetFileName(pcb_path)
board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "file": pcb_path,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_set_outline(
    pcb_path: str,
    x_mm: float,
    y_mm: float,
    width_mm: float,
    height_mm: float,
) -> Dict[str, Any]:
    """Add a rectangular board outline (Edge.Cuts) to the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]
x_mm = params["x_mm"]
y_mm = params["y_mm"]
width_mm = params["width_mm"]
height_mm = params["height_mm"]

board = pcbnew.LoadBoard(pcb_path)

# Remove existing Edge.Cuts segments so outline can be replaced
edge_cuts_id = pcbnew.Edge_Cuts
to_remove = []
for drawing in board.GetDrawings():
    if drawing.GetLayer() == edge_cuts_id:
        to_remove.append(drawing)
removed_count = len(to_remove)
for item in to_remove:
    board.Remove(item)

x = pcbnew.FromMM(x_mm)
y = pcbnew.FromMM(y_mm)
w = pcbnew.FromMM(width_mm)
h = pcbnew.FromMM(height_mm)

corners = [
    (x, y), (x + w, y), (x + w, y + h), (x, y + h)
]

for i in range(4):
    seg = pcbnew.PCB_SHAPE(board)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetStart(pcbnew.VECTOR2I(corners[i][0], corners[i][1]))
    seg.SetEnd(pcbnew.VECTOR2I(corners[(i+1)%4][0], corners[(i+1)%4][1]))
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetWidth(pcbnew.FromMM(0.1))
    board.Add(seg)

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "previous_edge_cuts_removed": removed_count,
    "outline": {
        "x_mm": x_mm,
        "y_mm": y_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
    },
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "width_mm": width_mm,
        "height_mm": height_mm,
    })


def _op_set_design_rules(
    pcb_path: str,
    min_track_width_mm: float = 0.2,
    min_clearance_mm: float = 0.2,
    min_via_diameter_mm: float = 0.6,
    min_via_drill_mm: float = 0.3,
    min_hole_to_hole_mm: float = 0.25,
    min_through_hole_diameter_mm: float = 0.3,
    min_copper_edge_clearance_mm: float = 0.5,
) -> Dict[str, Any]:
    """Set PCB design rules (DRC constraints)."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)
ds = board.GetDesignSettings()

# NOTE: do NOT set copper layer count here. Layer count is a board-structure
# concern, not a design rule — an unconditional SetCopperLayerCount(2) silently
# demoted 4-layer boards to 2 (dropping inner-layer routing from DRC's view).
# Fresh boards already default to 2 copper layers (verified KiCad 9 + 10).
ds.m_TrackMinWidth = pcbnew.FromMM(params["min_track_width_mm"])
ds.m_MinClearance = pcbnew.FromMM(params["min_clearance_mm"])
ds.m_ViasMinSize = pcbnew.FromMM(params["min_via_diameter_mm"])
ds.m_ViasMinDrill = pcbnew.FromMM(params["min_via_drill_mm"])
ds.m_HoleToHoleMin = pcbnew.FromMM(params["min_hole_to_hole_mm"])
# These two were reported in the return JSON but never applied to the board —
# only to the .kicad_pro. A pcbnew-only DRC load (no project) used stale values.
# Attribute names verified present + settable on KiCad 9 and 10.
ds.m_MinThroughDrill = pcbnew.FromMM(params["min_through_hole_diameter_mm"])
ds.m_CopperEdgeClearance = pcbnew.FromMM(params["min_copper_edge_clearance_mm"])

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "design_rules": {
        "min_track_width_mm": params["min_track_width_mm"],
        "min_clearance_mm": params["min_clearance_mm"],
        "min_via_diameter_mm": params["min_via_diameter_mm"],
        "min_via_drill_mm": params["min_via_drill_mm"],
        "min_hole_to_hole_mm": params["min_hole_to_hole_mm"],
        "min_through_hole_diameter_mm": params["min_through_hole_diameter_mm"],
        "min_copper_edge_clearance_mm": params["min_copper_edge_clearance_mm"],
    },
}))
"""
    result = run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "min_track_width_mm": min_track_width_mm,
        "min_clearance_mm": min_clearance_mm,
        "min_via_diameter_mm": min_via_diameter_mm,
        "min_via_drill_mm": min_via_drill_mm,
        "min_hole_to_hole_mm": min_hole_to_hole_mm,
        "min_through_hole_diameter_mm": min_through_hole_diameter_mm,
        "min_copper_edge_clearance_mm": min_copper_edge_clearance_mm,
    })

    # Also update the .kicad_pro project file DRC rules.
    stem = os.path.splitext(pcb_path)[0]
    pro_path = stem + ".kicad_pro"
    pro_updated = False
    pro_error: Optional[str] = None

    if not os.path.exists(pro_path):
        pro_error = (
            f".kicad_pro project file not found at {pro_path}; "
            "min_through_hole_diameter_mm and min_copper_edge_clearance_mm "
            "were NOT applied (these live only in the project file)"
        )
    else:
        try:
            import json as _json
            with open(pro_path, "r") as f:
                project = _json.load(f)
            rules = (
                project
                .setdefault("board", {})
                .setdefault("design_settings", {})
                .setdefault("rules", {})
            )
            rules["min_through_hole_diameter"] = min_through_hole_diameter_mm
            rules["min_copper_edge_clearance"] = min_copper_edge_clearance_mm
            rules["min_track_width"] = min_track_width_mm
            rules["min_clearance"] = min_clearance_mm
            rules["min_hole_to_hole"] = min_hole_to_hole_mm
            rules["min_via_diameter"] = min_via_diameter_mm
            with open(pro_path, "w") as f:
                _json.dump(project, f, indent=2)
                f.write("\n")
            pro_updated = True
        except (OSError, ValueError) as e:
            pro_error = f"Failed to update .kicad_pro: {type(e).__name__}: {e}"
            logger.warning(pro_error)

    if isinstance(result, dict) and "error" not in result:
        result["project_rules_updated"] = pro_updated
        if pro_updated:
            result["project_file"] = pro_path
        if pro_error is not None:
            result["project_rules_warning"] = pro_error

    return result


def _op_finalize(
    pcb_path: str,
    fix_silkscreen: bool = True,
    fill_zones: bool = True,
) -> Dict[str, Any]:
    """Fix silkscreen overlaps and fill copper zones in one operation."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)
results = {}

# ── Silkscreen fix ────────────────────────────────────────────────────────────
if params["fix_silkscreen"]:
    silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

    all_pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            sz = pad.GetBoundingBox()
            all_pads.append({
                "reference": fp.GetReference(),
                "x_min": sz.GetX(), "y_min": sz.GetY(),
                "x_max": sz.GetRight(), "y_max": sz.GetBottom(),
            })

    # Collect all visible silk text objects for text-vs-text checking
    all_silk = []
    for fp in board.GetFootprints():
        _ref = fp.GetReference()
        for _ft, _fo in [("reference", fp.Reference()), ("value", fp.Value())]:
            if not _fo.IsVisible() or _fo.GetLayer() not in silk_layer_ids:
                continue
            all_silk.append({"component": _ref, "obj": _fo, "layer": _fo.GetLayer()})
    for drawing in board.GetDrawings():
        if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
            _vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
            if _vis:
                all_silk.append({"component": None, "obj": drawing, "layer": drawing.GetLayer()})

    try:
        board_bb = board.GetBoardEdgesBoundingBox()
        board_valid = board_bb.GetWidth() > 0
    except Exception:
        board_valid = False

    def _aabb_hit(a, bx_min, by_min, bx_max, by_max):
        # Non-strict: touching edges count as overlap (matches KiCad DRC).
        return (a.GetX() <= bx_max and a.GetRight() >= bx_min and
                a.GetY() <= by_max and a.GetBottom() >= by_min)

    def has_pad_overlap(text_bbox, own_ref):
        for pad in all_pads:
            if pad["reference"] == own_ref:
                continue
            if _aabb_hit(text_bbox, pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
                return True
        return False

    def has_text_overlap(text_bbox, own_ref, own_layer, own_obj):
        for si in all_silk:
            if si["obj"] is own_obj:
                continue
            if si["component"] is not None and si["component"] == own_ref:
                continue
            if si["layer"] != own_layer:
                continue
            if not si["obj"].IsVisible() if hasattr(si["obj"], 'IsVisible') else False:
                continue
            ob = si["obj"].GetBoundingBox()
            if _aabb_hit(text_bbox, ob.GetX(), ob.GetY(), ob.GetRight(), ob.GetBottom()):
                return True
        return False

    def has_any_overlap(text_bbox, own_ref, own_layer, own_obj):
        return has_pad_overlap(text_bbox, own_ref) or has_text_overlap(text_bbox, own_ref, own_layer, own_obj)

    def in_board(text_bbox):
        if not board_valid:
            return True
        return (board_bb.GetX() <= text_bbox.GetX() and
                text_bbox.GetRight() <= board_bb.GetRight() and
                board_bb.GetY() <= text_bbox.GetY() and
                text_bbox.GetBottom() <= board_bb.GetBottom())

    MARGIN = pcbnew.FromMM(0.3)
    silk_fixed = []
    silk_hidden = []
    silk_ok = 0

    for fp in board.GetFootprints():
        ref = fp.GetReference()
        for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
            if not field_obj.IsVisible():
                continue
            if field_obj.GetLayer() not in silk_layer_ids:
                continue
            text_bbox = field_obj.GetBoundingBox()
            own_layer = field_obj.GetLayer()
            _has_overlap = has_any_overlap(text_bbox, ref, own_layer, field_obj)
            _clips_edge = not in_board(text_bbox)
            if not _has_overlap and not _clips_edge:
                silk_ok += 1
                continue

            fp_bb = fp.GetBoundingBox()
            cx  = fp_bb.GetCenter().x
            cy  = fp_bb.GetCenter().y
            fw2 = fp_bb.GetWidth()  // 2
            fh2 = fp_bb.GetHeight() // 2
            tw2 = text_bbox.GetWidth()  // 2
            th2 = text_bbox.GetHeight() // 2

            candidates = [
                (cx,           cy - fh2 - th2 - MARGIN),
                (cx,           cy + fh2 + th2 + MARGIN),
                (cx - fw2 - tw2 - MARGIN, cy),
                (cx + fw2 + tw2 + MARGIN, cy),
                (cx - fw2 - tw2 - MARGIN, cy - fh2 - th2 - MARGIN),
                (cx + fw2 + tw2 + MARGIN, cy - fh2 - th2 - MARGIN),
                (cx - fw2 - tw2 - MARGIN, cy + fh2 + th2 + MARGIN),
                (cx + fw2 + tw2 + MARGIN, cy + fh2 + th2 + MARGIN),
            ]

            orig_pos = field_obj.GetPosition()
            resolved = False
            for px, py in candidates:
                field_obj.SetPosition(pcbnew.VECTOR2I(int(px), int(py)))
                new_bbox = field_obj.GetBoundingBox()
                if not has_any_overlap(new_bbox, ref, own_layer, field_obj) and in_board(new_bbox):
                    resolved = True
                    pos = field_obj.GetPosition()
                    silk_fixed.append({"component": ref, "field": field_type,
                                        "action": "moved",
                                        "x_mm": round(pcbnew.ToMM(pos.x), 2),
                                        "y_mm": round(pcbnew.ToMM(pos.y), 2)})
                    break
            if not resolved:
                field_obj.SetPosition(orig_pos)
                field_obj.SetVisible(False)
                silk_hidden.append({"component": ref, "field": field_type, "action": "hidden"})

    results["silkscreen"] = {
        "already_ok": silk_ok,
        "moved": len(silk_fixed),
        "hidden": len(silk_hidden),
        "fixes": silk_fixed + silk_hidden,
    }

# ── Zone fill ─────────────────────────────────────────────────────────────────
if params["fill_zones"]:
    copper_zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
    if copper_zones:
        for z in copper_zones:
            z.UnFill()
        filler = pcbnew.ZONE_FILLER(board)
        fill_ok = filler.Fill(board.Zones())
        zone_info = []
        for z in copper_zones:
            ls = z.GetLayerSet()
            lname = "F.Cu" if ls.Contains(pcbnew.F_Cu) else "B.Cu" if ls.Contains(pcbnew.B_Cu) else "other"
            zone_info.append({"net": z.GetNetname(), "layer": lname, "filled": z.IsFilled()})
        results["zones"] = {"fill_success": fill_ok, "zones_filled": len(copper_zones), "zones": zone_info}
    else:
        results["zones"] = {"fill_success": True, "zones_filled": 0, "message": "No copper zones"}

board.Save(pcb_path)
results["status"] = "ok"
print(json.dumps(results))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "fix_silkscreen": fix_silkscreen,
        "fill_zones": fill_zones,
    }, timeout=120.0)
