"""PCB silkscreen ops: add text, list items, update items, check overlaps, auto-fix.

These are module-level _op_* helpers consumed by the pcb router (pcb.py).
_op_check_silkscreen_overlaps is also imported by the audit router (pcb_keepout.py)
to serve audit.check_silkscreen_overlaps — single source of truth, two surfaces.
"""

import logging
import os
from typing import Any, Dict, Optional

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def _op_add_text(
    pcb_path: str,
    text: str,
    x_mm: float,
    y_mm: float,
    layer: str = "F.SilkS",
    size_mm: float = 1.0,
    thickness_mm: float = 0.15,
    rotation_deg: float = 0.0,
) -> Dict[str, Any]:
    """Add text to the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

txt = pcbnew.PCB_TEXT(board)
txt.SetText(params["text"])
txt.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pcbnew.FromMM(params["y_mm"])))
txt.SetLayer(board.GetLayerID(params["layer"]))
txt.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(params["size_mm"]), pcbnew.FromMM(params["size_mm"])))
txt.SetTextThickness(pcbnew.FromMM(params["thickness_mm"]))
if params["rotation_deg"] != 0:
    txt.SetTextAngleDegrees(params["rotation_deg"])

board.Add(txt)
board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "text": params["text"],
    "x_mm": params["x_mm"],
    "y_mm": params["y_mm"],
    "layer": params["layer"],
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "text": text,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "layer": layer,
        "size_mm": size_mm,
        "thickness_mm": thickness_mm,
        "rotation_deg": rotation_deg,
    })


def _op_list_silkscreen(pcb_path: str) -> Dict[str, Any]:
    """List all silkscreen text items on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

items = []

for fp in board.GetFootprints():
    ref = fp.GetReference()
    for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
        pos = field_obj.GetPosition()
        size = field_obj.GetTextSize()
        rel_pos = field_obj.GetFPRelativePosition()
        items.append({
            "type": field_type,
            "component": ref,
            "text": field_obj.GetText(),
            "visible": field_obj.IsVisible(),
            "layer": board.GetLayerName(field_obj.GetLayer()),
            "x_mm": round(pcbnew.ToMM(pos.x), 3),
            "y_mm": round(pcbnew.ToMM(pos.y), 3),
            "rel_x_mm": round(pcbnew.ToMM(rel_pos.x), 3),
            "rel_y_mm": round(pcbnew.ToMM(rel_pos.y), 3),
            "size_mm": round(pcbnew.ToMM(size.x), 3),
            "thickness_mm": round(pcbnew.ToMM(field_obj.GetTextThickness()), 3),
            "angle_deg": field_obj.GetTextAngle().AsDegrees(),
        })

silk_layers = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layers:
        pos = drawing.GetPosition()
        size = drawing.GetTextSize()
        items.append({
            "type": "standalone",
            "component": None,
            "text": drawing.GetText(),
            "visible": drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True,
            "layer": board.GetLayerName(drawing.GetLayer()),
            "x_mm": round(pcbnew.ToMM(pos.x), 3),
            "y_mm": round(pcbnew.ToMM(pos.y), 3),
            "rel_x_mm": 0,
            "rel_y_mm": 0,
            "size_mm": round(pcbnew.ToMM(size.x), 3),
            "thickness_mm": round(pcbnew.ToMM(drawing.GetTextThickness()), 3),
            "angle_deg": drawing.GetTextAngle().AsDegrees(),
        })

print(json.dumps({
    "status": "ok",
    "item_count": len(items),
    "items": items,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_update_silkscreen(
    pcb_path: str,
    reference: str,
    field: str = "reference",
    visible: Optional[bool] = None,
    x_mm: Optional[float] = None,
    y_mm: Optional[float] = None,
    rel_x_mm: Optional[float] = None,
    rel_y_mm: Optional[float] = None,
    size_mm: Optional[float] = None,
    thickness_mm: Optional[float] = None,
    angle_deg: Optional[float] = None,
    layer: Optional[str] = None,
) -> Dict[str, Any]:
    """Update a silkscreen text item's properties."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    if field not in ("reference", "value"):
        return {"error": f"field must be 'reference' or 'value', got {field!r}"}

    if all(v is None for v in (visible, x_mm, y_mm, rel_x_mm, rel_y_mm,
                                size_mm, thickness_mm, angle_deg, layer)):
        return {"error": "No modifications specified"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]
reference = params["reference"]

board = pcbnew.LoadBoard(pcb_path)

fp = board.FindFootprintByReference(reference)
if fp is None:
    print(json.dumps({"error": f"Footprint {reference!r} not found"}))
    raise SystemExit(0)

text = fp.Reference() if params["field"] == "reference" else fp.Value()

if params["visible"] is not None:
    text.SetVisible(params["visible"])
if params["x_mm"] is not None and params["y_mm"] is not None:
    text.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pcbnew.FromMM(params["y_mm"])))
elif params["x_mm"] is not None:
    pos = text.GetPosition()
    text.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pos.y))
elif params["y_mm"] is not None:
    pos = text.GetPosition()
    text.SetPosition(pcbnew.VECTOR2I(pos.x, pcbnew.FromMM(params["y_mm"])))
if params["rel_x_mm"] is not None and params["rel_y_mm"] is not None:
    text.SetFPRelativePosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["rel_x_mm"]), pcbnew.FromMM(params["rel_y_mm"])))
if params["size_mm"] is not None:
    text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(params["size_mm"]), pcbnew.FromMM(params["size_mm"])))
if params["thickness_mm"] is not None:
    text.SetTextThickness(pcbnew.FromMM(params["thickness_mm"]))
if params["angle_deg"] is not None:
    text.SetTextAngle(pcbnew.EDA_ANGLE(params["angle_deg"], pcbnew.DEGREES_T))
if params["layer"] is not None:
    text.SetLayer(board.GetLayerID(params["layer"]))

board.Save(pcb_path)

pos = text.GetPosition()
size = text.GetTextSize()
rel_pos = text.GetFPRelativePosition()
print(json.dumps({
    "status": "ok",
    "reference": reference,
    "field": params["field"],
    "text": text.GetText(),
    "visible": text.IsVisible(),
    "layer": board.GetLayerName(text.GetLayer()),
    "x_mm": round(pcbnew.ToMM(pos.x), 3),
    "y_mm": round(pcbnew.ToMM(pos.y), 3),
    "rel_x_mm": round(pcbnew.ToMM(rel_pos.x), 3),
    "rel_y_mm": round(pcbnew.ToMM(rel_pos.y), 3),
    "size_mm": round(pcbnew.ToMM(size.x), 3),
    "thickness_mm": round(pcbnew.ToMM(text.GetTextThickness()), 3),
    "angle_deg": text.GetTextAngle().AsDegrees(),
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "reference": reference,
        "field": field,
        "visible": visible,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "rel_x_mm": rel_x_mm,
        "rel_y_mm": rel_y_mm,
        "size_mm": size_mm,
        "thickness_mm": thickness_mm,
        "angle_deg": angle_deg,
        "layer": layer,
    })


def _op_check_silkscreen_overlaps(pcb_path: str) -> Dict[str, Any]:
    """Find silkscreen text items that overlap copper pads or other silkscreen text.

    This function is the single source of truth for silkscreen overlap checking.
    It is used by both:
      - pcb router (operation='check_silkscreen_overlaps')
      - audit router (operation='check_silkscreen_overlaps') via import in pcb_keepout.py
    """
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

silk_items = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not field_obj.IsVisible():
            continue
        if field_obj.GetLayer() not in silk_layer_ids:
            continue
        bbox = field_obj.GetBoundingBox()
        silk_items.append({
            "type": field_type,
            "component": ref,
            "text": field_obj.GetText(),
            "layer": board.GetLayerName(field_obj.GetLayer()),
            "bbox_x_min": bbox.GetX(),
            "bbox_y_min": bbox.GetY(),
            "bbox_x_max": bbox.GetRight(),
            "bbox_y_max": bbox.GetBottom(),
        })

for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        if hasattr(drawing, 'IsVisible') and not drawing.IsVisible():
            continue
        bbox = drawing.GetBoundingBox()
        silk_items.append({
            "type": "standalone",
            "component": None,
            "text": drawing.GetText(),
            "layer": board.GetLayerName(drawing.GetLayer()),
            "bbox_x_min": bbox.GetX(),
            "bbox_y_min": bbox.GetY(),
            "bbox_x_max": bbox.GetRight(),
            "bbox_y_max": bbox.GetBottom(),
        })

pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        sz = pad.GetBoundingBox()
        pads.append({
            "reference": fp.GetReference(),
            "pad_number": pad.GetNumber(),
            "x_min": sz.GetX(),
            "y_min": sz.GetY(),
            "x_max": sz.GetRight(),
            "y_max": sz.GetBottom(),
        })

def aabb_overlap(ax_min, ay_min, ax_max, ay_max, bx_min, by_min, bx_max, by_max):
    return ax_min < bx_max and ax_max > bx_min and ay_min < by_max and ay_max > by_min

pad_overlaps = []
for si in silk_items:
    for pad in pads:
        if si["component"] == pad["reference"]:
            continue
        if aabb_overlap(si["bbox_x_min"], si["bbox_y_min"], si["bbox_x_max"], si["bbox_y_max"],
                        pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
            pad_overlaps.append({
                "silk_type": si["type"],
                "silk_component": si["component"],
                "silk_text": si["text"],
                "silk_layer": si["layer"],
                "pad_component": pad["reference"],
                "pad_number": pad["pad_number"],
            })

text_overlaps = []
for i in range(len(silk_items)):
    a = silk_items[i]
    for j in range(i + 1, len(silk_items)):
        b = silk_items[j]
        if a["component"] is not None and a["component"] == b["component"]:
            continue
        if a["layer"] != b["layer"]:
            continue
        if aabb_overlap(a["bbox_x_min"], a["bbox_y_min"], a["bbox_x_max"], a["bbox_y_max"],
                        b["bbox_x_min"], b["bbox_y_min"], b["bbox_x_max"], b["bbox_y_max"]):
            text_overlaps.append({
                "text_a_type": a["type"], "text_a_component": a["component"], "text_a": a["text"],
                "text_b_type": b["type"], "text_b_component": b["component"], "text_b": b["text"],
                "layer": a["layer"],
            })

print(json.dumps({
    "status": "ok",
    "silk_items_checked": len(silk_items),
    "pads_checked": len(pads),
    "overlap_count": len(pad_overlaps),
    "overlaps": pad_overlaps,
    "text_overlap_count": len(text_overlaps),
    "text_overlaps": text_overlaps,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_auto_fix_silkscreen(pcb_path: str) -> Dict[str, Any]:
    """Automatically fix silkscreen text that overlaps copper pads or other text."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

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

all_silk = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for ft, fo in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not fo.IsVisible():
            continue
        if fo.GetLayer() not in silk_layer_ids:
            continue
        all_silk.append({"component": ref, "field_type": ft, "obj": fo,
                          "layer": fo.GetLayer()})
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
        if vis:
            all_silk.append({"component": None, "field_type": "standalone",
                              "obj": drawing, "layer": drawing.GetLayer()})

if hasattr(board, 'GetBoardEdgesBoundingBox'):
    board_bb = board.GetBoardEdgesBoundingBox()
    board_valid = board_bb.GetWidth() > 0
else:
    board_bb = None
    board_valid = False

def aabb_hit(a, bx_min, by_min, bx_max, by_max):
    # Non-strict: touching edges count as overlap, matching KiCad DRC's
    # silk_over_copper / silk_overlap semantics.
    return (a.GetX() <= bx_max and a.GetRight() >= bx_min and
            a.GetY() <= by_max and a.GetBottom() >= by_min)

def has_pad_overlap(text_bbox, own_ref):
    for pad in all_pads:
        if pad["reference"] == own_ref:
            continue
        if aabb_hit(text_bbox, pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
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
        visible = si["obj"].IsVisible() if hasattr(si["obj"], 'IsVisible') else True
        if not visible:
            continue
        ob = si["obj"].GetBoundingBox()
        if aabb_hit(text_bbox, ob.GetX(), ob.GetY(), ob.GetRight(), ob.GetBottom()):
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

fixed = []
hidden = []
already_ok = 0

for fp in board.GetFootprints():
    ref = fp.GetReference()

    for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not field_obj.IsVisible():
            continue
        if field_obj.GetLayer() not in silk_layer_ids:
            continue

        text_bbox = field_obj.GetBoundingBox()
        own_layer = field_obj.GetLayer()
        has_overlap = has_any_overlap(text_bbox, ref, own_layer, field_obj)
        clips_edge = not in_board(text_bbox)
        if not has_overlap and not clips_edge:
            already_ok += 1
            continue

        if text_bbox.GetWidth() <= 0 or text_bbox.GetHeight() <= 0:
            already_ok += 1
            continue

        fp_bb = fp.GetBoundingBox()
        cx  = fp_bb.GetCenter().x
        cy  = fp_bb.GetCenter().y
        fw2 = fp_bb.GetWidth()  // 2
        fh2 = fp_bb.GetHeight() // 2
        tw2 = text_bbox.GetWidth()  // 2
        th2 = text_bbox.GetHeight() // 2

        candidates = [
            (cx,           cy - fh2 - th2 - MARGIN),              # N
            (cx,           cy + fh2 + th2 + MARGIN),              # S
            (cx - fw2 - tw2 - MARGIN, cy),                        # W
            (cx + fw2 + tw2 + MARGIN, cy),                        # E
            (cx - fw2 - tw2 - MARGIN, cy - fh2 - th2 - MARGIN),  # NW
            (cx + fw2 + tw2 + MARGIN, cy - fh2 - th2 - MARGIN),  # NE
            (cx - fw2 - tw2 - MARGIN, cy + fh2 + th2 + MARGIN),  # SW
            (cx + fw2 + tw2 + MARGIN, cy + fh2 + th2 + MARGIN),  # SE
        ]

        orig_pos = field_obj.GetPosition()
        resolved = False

        for px, py in candidates:
            field_obj.SetPosition(pcbnew.VECTOR2I(int(px), int(py)))
            new_bbox = field_obj.GetBoundingBox()
            if not has_any_overlap(new_bbox, ref, own_layer, field_obj) and in_board(new_bbox):
                resolved = True
                pos = field_obj.GetPosition()
                fixed.append({
                    "component": ref,
                    "field": field_type,
                    "action": "moved",
                    "x_mm": round(pcbnew.ToMM(pos.x), 2),
                    "y_mm": round(pcbnew.ToMM(pos.y), 2),
                })
                break

        if not resolved:
            field_obj.SetPosition(orig_pos)
            field_obj.SetVisible(False)
            hidden.append({
                "component": ref,
                "field": field_type,
                "action": "hidden",
            })

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "already_ok": already_ok,
    "moved": len(fixed),
    "hidden": len(hidden),
    "fixes": fixed + hidden,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})
