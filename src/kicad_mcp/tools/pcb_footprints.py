"""PCB footprint ops: place, move, list, pad positions, dimensions.

These are module-level _op_* helpers consumed by the pcb router (pcb.py).
"""

import logging
import os
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER, LIB_SEARCH_HELPER

logger = logging.getLogger(__name__)

_KEEPOUT_HELPER = KEEPOUT_HELPER


def _op_place_footprint(
    pcb_path: str,
    library: str,
    footprint_name: str,
    reference: str,
    value: str,
    x_mm: float,
    y_mm: float,
    rotation_deg: float = 0.0,
    layer: str = "F.Cu",
    check_keepouts: bool = True,
) -> Dict[str, Any]:
    """Place a footprint on the PCB from a KiCad library."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    keepout_code = ""
    if check_keepouts:
        keepout_code = """
""" + _KEEPOUT_HELPER + """

# Check placement against keepout zones and board boundary
fp_bbox = fp.GetBoundingBox(False, False)
fp_rect = {
    "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
    "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
    "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
    "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
}
keepouts = extract_keepouts(board)
outline = get_board_outline(board)
placement_warnings = []

for kz in keepouts:
    kz_bb = kz["bounding_box"]
    if not rects_overlap(fp_rect, kz_bb):
        continue
    c = kz["constraints"]
    blocked = [k.replace("no_", "") for k, v in c.items() if v]
    if blocked:
        src = kz["source_ref"] or kz["source"]
        placement_warnings.append(f"Overlaps keepout from {src} (blocks {', '.join(blocked)})")

if outline is None:
    placement_warnings.append(
        "No board outline (Edge.Cuts) found — cannot validate footprint boundary. "
        "Add a board outline before placing components."
    )
elif not rect_inside(fp_rect, outline):
    overhang_parts = []
    if fp_rect["x_min_mm"] < outline["x_min_mm"]:
        overhang_parts.append(f"left {round(outline['x_min_mm'] - fp_rect['x_min_mm'], 1)}mm")
    if fp_rect["x_max_mm"] > outline["x_max_mm"]:
        overhang_parts.append(f"right {round(fp_rect['x_max_mm'] - outline['x_max_mm'], 1)}mm")
    if fp_rect["y_min_mm"] < outline["y_min_mm"]:
        overhang_parts.append(f"top {round(outline['y_min_mm'] - fp_rect['y_min_mm'], 1)}mm")
    if fp_rect["y_max_mm"] > outline["y_max_mm"]:
        overhang_parts.append(f"bottom {round(fp_rect['y_max_mm'] - outline['y_max_mm'], 1)}mm")
    placement_warnings.append(
        f"EXTENDS BEYOND BOARD OUTLINE ({', '.join(overhang_parts)}) — "
        "move this footprint before routing or pads will be unreachable."
    )
"""

    script = """
import pcbnew, json, os, glob, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

""" + LIB_SEARCH_HELPER + """
lib_name = params["library"]
fp_name = params["footprint_name"]
lib_path = find_lib(lib_name)
if not lib_path:
    print(json.dumps({"error": f"Library '{lib_name}' not found"}))
    raise SystemExit(0)

fp = pcbnew.FootprintLoad(lib_path, fp_name)
if fp is None:
    print(json.dumps({"error": f"Footprint '{fp_name}' not found in '{lib_name}'"}))
    raise SystemExit(0)

fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pcbnew.FromMM(params["y_mm"])))
fp.SetReference(params["reference"])
fp.SetValue(params["value"])

if params["rotation_deg"] != 0:
    fp.SetOrientationDegrees(params["rotation_deg"])

# Add to board BEFORE flipping — Flip() calls GetBoard()->FlipLayer()
# internally, which segfaults if the footprint isn't on a board yet.
board.Add(fp)

if params["layer"] == "B.Cu":
    fp.Flip(fp.GetPosition(), False)

placement_warnings = []
""" + keepout_code + """

board.Save(pcb_path)

# Get bounding box dimensions for placement planning
bbox = fp.GetBoundingBox(False, False)
bbox_info = {
    "x_min_mm": round(pcbnew.ToMM(bbox.GetX()), 2),
    "y_min_mm": round(pcbnew.ToMM(bbox.GetY()), 2),
    "x_max_mm": round(pcbnew.ToMM(bbox.GetRight()), 2),
    "y_max_mm": round(pcbnew.ToMM(bbox.GetBottom()), 2),
    "width_mm": round(pcbnew.ToMM(bbox.GetWidth()), 2),
    "height_mm": round(pcbnew.ToMM(bbox.GetHeight()), 2),
}

cy_x_min = float("inf"); cy_y_min = float("inf")
cy_x_max = float("-inf"); cy_y_max = float("-inf")
cy_found = False
for item in fp.GraphicalItems():
    ly = item.GetLayer()
    if ly in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        cy_found = True
        ibb = item.GetBoundingBox()
        cy_x_min = min(cy_x_min, pcbnew.ToMM(ibb.GetX()))
        cy_y_min = min(cy_y_min, pcbnew.ToMM(ibb.GetY()))
        cy_x_max = max(cy_x_max, pcbnew.ToMM(ibb.GetRight()))
        cy_y_max = max(cy_y_max, pcbnew.ToMM(ibb.GetBottom()))
if cy_found:
    bbox_info["courtyard"] = {
        "x_min_mm": round(cy_x_min, 2),
        "y_min_mm": round(cy_y_min, 2),
        "x_max_mm": round(cy_x_max, 2),
        "y_max_mm": round(cy_y_max, 2),
        "width_mm": round(cy_x_max - cy_x_min, 2),
        "height_mm": round(cy_y_max - cy_y_min, 2),
    }

result = {
    "status": "ok",
    "placed": {
        "reference": params["reference"],
        "footprint": f"{lib_name}:{fp_name}",
        "x_mm": params["x_mm"],
        "y_mm": params["y_mm"],
        "rotation": params["rotation_deg"],
        "layer": params["layer"],
    },
    "bounding_box": bbox_info,
}
if placement_warnings:
    result["placement_warnings"] = placement_warnings
print(json.dumps(result))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "library": library,
        "footprint_name": footprint_name,
        "reference": reference,
        "value": value,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "rotation_deg": rotation_deg,
        "layer": layer,
    })


def _op_move_footprint(
    pcb_path: str,
    reference: str,
    x_mm: float,
    y_mm: float,
    rotation_deg: Optional[float] = None,
) -> Dict[str, Any]:
    """Move a footprint to a new position on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]
reference = params["reference"]

""" + _KEEPOUT_HELPER + """

board = pcbnew.LoadBoard(pcb_path)

fp = board.FindFootprintByReference(reference)
if fp is None:
    print(json.dumps({"error": f"Footprint {reference!r} not found"}))
    raise SystemExit(0)

fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pcbnew.FromMM(params["y_mm"])))
if params["rotation_deg"] is not None:
    fp.SetOrientationDegrees(params["rotation_deg"])

board.Save(pcb_path)

# Check new position against board boundary
pos = fp.GetPosition()
placement_warnings = []
fp_bbox = fp.GetBoundingBox(False, False)
fp_rect = {
    "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
    "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
    "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
    "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
}
outline = get_board_outline(board)
if outline is None:
    placement_warnings.append(
        "No board outline (Edge.Cuts) found — cannot validate footprint boundary."
    )
elif not rect_inside(fp_rect, outline):
    overhang_parts = []
    if fp_rect["x_min_mm"] < outline["x_min_mm"]:
        overhang_parts.append(f"left {round(outline['x_min_mm'] - fp_rect['x_min_mm'], 1)}mm")
    if fp_rect["x_max_mm"] > outline["x_max_mm"]:
        overhang_parts.append(f"right {round(fp_rect['x_max_mm'] - outline['x_max_mm'], 1)}mm")
    if fp_rect["y_min_mm"] < outline["y_min_mm"]:
        overhang_parts.append(f"top {round(outline['y_min_mm'] - fp_rect['y_min_mm'], 1)}mm")
    if fp_rect["y_max_mm"] > outline["y_max_mm"]:
        overhang_parts.append(f"bottom {round(fp_rect['y_max_mm'] - outline['y_max_mm'], 1)}mm")
    placement_warnings.append(
        f"EXTENDS BEYOND BOARD OUTLINE ({', '.join(overhang_parts)}) — "
        "move this footprint before routing or pads will be unreachable."
    )

result = {
    "status": "ok",
    "reference": reference,
    "x_mm": round(pcbnew.ToMM(pos.x), 3),
    "y_mm": round(pcbnew.ToMM(pos.y), 3),
    "rotation": fp.GetOrientationDegrees(),
}
if placement_warnings:
    result["placement_warnings"] = placement_warnings
print(json.dumps(result))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "reference": reference,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "rotation_deg": rotation_deg,
    })


def _op_list_footprints(pcb_path: str) -> Dict[str, Any]:
    """List all footprints currently placed on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

PAD_SHAPE = {
    pcbnew.PAD_SHAPE_CIRCLE:    "circle",
    pcbnew.PAD_SHAPE_RECT:      "rect",
    pcbnew.PAD_SHAPE_OVAL:      "oval",
    pcbnew.PAD_SHAPE_TRAPEZOID: "trapezoid",
    pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
    pcbnew.PAD_SHAPE_CHAMFERED_RECT: "chamfered_rect",
}
unknown_pad_shapes = set()

fp_list = []
for fp in board.GetFootprints():
    pos = fp.GetPosition()
    pads = []
    for pad in fp.Pads():
        pad_pos = pad.GetPosition()
        size = pad.GetSize()
        drill = pad.GetDrillSize()
        lset = pad.GetLayerSet()
        copper_layers = [
            board.GetLayerName(lid)
            for lid in lset.Seq()
            if pcbnew.IsCopperLayer(lid)
        ]

        shape_id = pad.GetShape()
        if shape_id not in PAD_SHAPE:
            unknown_pad_shapes.add(shape_id)
        pad_info = {
            "number": pad.GetNumber(),
            "x_mm": round(pcbnew.ToMM(pad_pos.x), 3),
            "y_mm": round(pcbnew.ToMM(pad_pos.y), 3),
            "net": pad.GetNetname(),
            "shape": PAD_SHAPE.get(shape_id, f"unknown_shape_{shape_id}"),
            "size_x_mm": round(pcbnew.ToMM(size.x), 3),
            "size_y_mm": round(pcbnew.ToMM(size.y), 3),
            "copper_layers": copper_layers,
        }
        if drill.x > 0:
            pad_info["drill_x_mm"] = round(pcbnew.ToMM(drill.x), 3)
            pad_info["drill_y_mm"] = round(pcbnew.ToMM(drill.y), 3)
        pads.append(pad_info)

    fp_list.append({
        "reference": fp.GetReference(),
        "value": fp.GetValue(),
        "footprint": fp.GetFPID().GetUniStringLibItemName(),
        "x_mm": round(pcbnew.ToMM(pos.x), 3),
        "y_mm": round(pcbnew.ToMM(pos.y), 3),
        "rotation": fp.GetOrientationDegrees(),
        "layer": board.GetLayerName(fp.GetLayer()),
        "pads": pads,
    })

_out = {
    "status": "ok",
    "footprint_count": len(fp_list),
    "footprints": fp_list,
}
if unknown_pad_shapes:
    _out["unknown_pad_shape_ids"] = sorted(unknown_pad_shapes)
print(json.dumps(_out))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_get_pad_positions(pcb_path: str, reference: str) -> Dict[str, Any]:
    """Get all pad positions for a footprint."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

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

pads = []
for pad in fp.Pads():
    pos = pad.GetPosition()
    pads.append({
        "number": pad.GetNumber(),
        "x_mm": round(pcbnew.ToMM(pos.x), 3),
        "y_mm": round(pcbnew.ToMM(pos.y), 3),
        "net": pad.GetNetname(),
        "shape": str(pad.GetShape()),
    })

print(json.dumps({
    "status": "ok",
    "reference": reference,
    "pad_count": len(pads),
    "pads": pads,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "reference": reference,
    })


def _op_get_footprint_dimensions(
    library: str,
    footprint_name: str,
    rotation_deg: float = 0.0,
) -> Dict[str, Any]:
    """Query a footprint's bounding box, pad span, and embedded keepout zones."""
    script = """
import pcbnew, json, os, sys

params = json.loads(open(sys.argv[1]).read())

""" + LIB_SEARCH_HELPER + """
lib_name = params["library"]
fp_name = params["footprint_name"]
lib_path = find_lib(lib_name)
if not lib_path:
    print(json.dumps({"error": f"Library '{lib_name}' not found"}))
    raise SystemExit(0)

fp = pcbnew.FootprintLoad(lib_path, fp_name)
if fp is None:
    print(json.dumps({"error": f"Footprint '{fp_name}' not found in '{lib_name}'"}))
    raise SystemExit(0)

# Place at origin, apply rotation
fp.SetPosition(pcbnew.VECTOR2I(0, 0))
if params["rotation_deg"] != 0:
    fp.SetOrientationDegrees(params["rotation_deg"])

# Body bounding box (excludes text)
bb = fp.GetBoundingBox(False, False)
body_bbox = {
    "x_min_mm": round(pcbnew.ToMM(bb.GetX()), 3),
    "y_min_mm": round(pcbnew.ToMM(bb.GetY()), 3),
    "x_max_mm": round(pcbnew.ToMM(bb.GetRight()), 3),
    "y_max_mm": round(pcbnew.ToMM(bb.GetBottom()), 3),
    "width_mm": round(pcbnew.ToMM(bb.GetWidth()), 3),
    "height_mm": round(pcbnew.ToMM(bb.GetHeight()), 3),
}

# Courtyard
courtyard = None
cx_min = float("inf"); cy_min = float("inf")
cx_max = float("-inf"); cy_max = float("-inf")
found_cy = False
for item in fp.GraphicalItems():
    ly = item.GetLayer()
    if ly in (pcbnew.F_CrtYd, pcbnew.B_CrtYd):
        found_cy = True
        cbb = item.GetBoundingBox()
        cx_min = min(cx_min, pcbnew.ToMM(cbb.GetX()))
        cy_min = min(cy_min, pcbnew.ToMM(cbb.GetY()))
        cx_max = max(cx_max, pcbnew.ToMM(cbb.GetRight()))
        cy_max = max(cy_max, pcbnew.ToMM(cbb.GetBottom()))
if found_cy:
    courtyard = {
        "x_min_mm": round(cx_min, 3), "y_min_mm": round(cy_min, 3),
        "x_max_mm": round(cx_max, 3), "y_max_mm": round(cy_max, 3),
        "width_mm": round(cx_max - cx_min, 3),
        "height_mm": round(cy_max - cy_min, 3),
    }

# Pad span (extent of actual copper pads)
px_min = float("inf"); py_min = float("inf")
px_max = float("-inf"); py_max = float("-inf")
pad_count = 0
for pad in fp.Pads():
    pad_count += 1
    pos = pad.GetPosition()
    size = pad.GetSize()
    x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
    w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
    px_min = min(px_min, x - w/2); py_min = min(py_min, y - h/2)
    px_max = max(px_max, x + w/2); py_max = max(py_max, y + h/2)
pad_span = None
if pad_count > 0:
    pad_span = {
        "x_min_mm": round(px_min, 3), "y_min_mm": round(py_min, 3),
        "x_max_mm": round(px_max, 3), "y_max_mm": round(py_max, 3),
        "width_mm": round(px_max - px_min, 3),
        "height_mm": round(py_max - py_min, 3),
    }

# Embedded keepout zones
keepouts = []
for zone in fp.Zones():
    if not zone.GetIsRuleArea():
        continue
    zbb = zone.GetBoundingBox()
    keepouts.append({
        "bounding_box": {
            "x_min_mm": round(pcbnew.ToMM(zbb.GetX()), 3),
            "y_min_mm": round(pcbnew.ToMM(zbb.GetY()), 3),
            "x_max_mm": round(pcbnew.ToMM(zbb.GetRight()), 3),
            "y_max_mm": round(pcbnew.ToMM(zbb.GetBottom()), 3),
            "width_mm": round(pcbnew.ToMM(zbb.GetWidth()), 3),
            "height_mm": round(pcbnew.ToMM(zbb.GetHeight()), 3),
        },
        "constraints": {
            "no_tracks": zone.GetDoNotAllowTracks(),
            "no_vias": zone.GetDoNotAllowVias(),
            "no_pads": zone.GetDoNotAllowPads(),
            # KiCad 10 renamed GetDoNotAllowCopperPour → GetDoNotAllowZoneFills
            "no_copper_pour": (zone.GetDoNotAllowZoneFills()
                               if hasattr(zone, "GetDoNotAllowZoneFills")
                               else zone.GetDoNotAllowCopperPour()),
            "no_footprints": zone.GetDoNotAllowFootprints(),
        },
    })

result = {
    "status": "ok",
    "library": lib_name,
    "footprint": fp_name,
    "rotation_deg": params["rotation_deg"],
    "pad_count": pad_count,
    "body_bbox": body_bbox,
    "pad_span": pad_span,
}
if courtyard:
    result["courtyard"] = courtyard
if keepouts:
    result["keepout_zones"] = keepouts
    result["keepout_count"] = len(keepouts)
print(json.dumps(result))
"""
    return run_pcbnew_script(script, params={
            "library": library,
            "footprint_name": footprint_name,
            "rotation_deg": rotation_deg,
        })
