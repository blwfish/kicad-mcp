"""PCB silkscreen tools: add text, list items, update items, check overlaps."""

import logging
import os
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def register_pcb_silkscreen_tools(mcp: FastMCP) -> None:
    """Register PCB silkscreen tools."""

    @mcp.tool()
    def add_text_to_pcb(
        pcb_path: str,
        text: str,
        x_mm: float,
        y_mm: float,
        layer: str = "F.SilkS",
        size_mm: float = 1.0,
        thickness_mm: float = 0.15,
        rotation_deg: float = 0.0,
    ) -> Dict[str, Any]:
        """Add text to the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            text: Text content.
            x_mm: X position in millimeters.
            y_mm: Y position in millimeters.
            layer: PCB layer (default "F.SilkS").
            size_mm: Text height in mm (default 1.0).
            thickness_mm: Text line thickness in mm (default 0.15).
            rotation_deg: Text rotation in degrees (default 0).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

txt = pcbnew.PCB_TEXT(board)
txt.SetText({text!r})
txt.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))
txt.SetLayer(board.GetLayerID({layer!r}))
txt.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM({size_mm}), pcbnew.FromMM({size_mm})))
txt.SetTextThickness(pcbnew.FromMM({thickness_mm}))
if {rotation_deg} != 0:
    txt.SetTextAngleDegrees({rotation_deg})

board.Add(txt)
board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "text": {text!r},
    "x_mm": {x_mm},
    "y_mm": {y_mm},
    "layer": {layer!r},
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def list_silkscreen_items(pcb_path: str) -> Dict[str, Any]:
        """List all silkscreen text items on the PCB.

        Returns reference designators, values, and standalone text with their
        positions, sizes, visibility, and layers.

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

items = []

# Footprint text (references and values)
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
        pos = field_obj.GetPosition()
        size = field_obj.GetTextSize()
        rel_pos = field_obj.GetFPRelativePosition()
        items.append({{
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
        }})

# Standalone text items on silkscreen layers
silk_layers = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layers:
        pos = drawing.GetPosition()
        size = drawing.GetTextSize()
        items.append({{
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
        }})

print(json.dumps({{
    "status": "ok",
    "item_count": len(items),
    "items": items,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def update_silkscreen_item(
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
        """Update a silkscreen text item's properties.

        Modify visibility, position, size, rotation, or layer of a footprint's
        reference designator or value text.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            reference: Component reference (e.g., "R1").
            field: Which field to update - "reference" or "value" (default "reference").
            visible: Set visibility (True/False). None to keep current.
            x_mm: New absolute X position in mm. None to keep current.
            y_mm: New absolute Y position in mm. None to keep current.
            rel_x_mm: New X position relative to footprint center in mm. None to keep current.
            rel_y_mm: New Y position relative to footprint center in mm. None to keep current.
            size_mm: New text height in mm. None to keep current.
            thickness_mm: New text stroke thickness in mm. None to keep current.
            angle_deg: New text rotation in degrees. None to keep current.
            layer: New layer name (e.g., "F.SilkS", "B.SilkS"). None to keep current.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        if field not in ("reference", "value"):
            return {"error": f"field must be 'reference' or 'value', got {field!r}"}

        # Build the modification statements
        mods = []
        if visible is not None:
            mods.append(f"text.SetVisible({visible!r})")
        if x_mm is not None and y_mm is not None:
            mods.append(f"text.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))")
        elif x_mm is not None or y_mm is not None:
            if x_mm is not None:
                mods.append(f"pos = text.GetPosition(); text.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pos.y))")
            else:
                mods.append(f"pos = text.GetPosition(); text.SetPosition(pcbnew.VECTOR2I(pos.x, pcbnew.FromMM({y_mm})))")
        if rel_x_mm is not None and rel_y_mm is not None:
            mods.append(f"text.SetFPRelativePosition(pcbnew.VECTOR2I(pcbnew.FromMM({rel_x_mm}), pcbnew.FromMM({rel_y_mm})))")
        if size_mm is not None:
            mods.append(f"text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM({size_mm}), pcbnew.FromMM({size_mm})))")
        if thickness_mm is not None:
            mods.append(f"text.SetTextThickness(pcbnew.FromMM({thickness_mm}))")
        if angle_deg is not None:
            mods.append(f"text.SetTextAngle(pcbnew.EDA_ANGLE({angle_deg}, pcbnew.DEGREES_T))")
        if layer is not None:
            mods.append(f"text.SetLayer(board.GetLayerID({layer!r}))")

        if not mods:
            return {"error": "No modifications specified"}

        mods_code = "\n".join(mods)

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

fp = board.FindFootprintByReference({reference!r})
if fp is None:
    print(json.dumps({{"error": f"Footprint {reference!r} not found"}}))
    raise SystemExit(0)

text = fp.Reference() if {field!r} == "reference" else fp.Value()

{mods_code}

board.Save({pcb_path!r})

pos = text.GetPosition()
size = text.GetTextSize()
rel_pos = text.GetFPRelativePosition()
print(json.dumps({{
    "status": "ok",
    "reference": {reference!r},
    "field": {field!r},
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
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def edit_text(
        pcb_path: str,
        text: str,
        new_text: Optional[str] = None,
        x_mm: Optional[float] = None,
        y_mm: Optional[float] = None,
        layer: Optional[str] = None,
        size_mm: Optional[float] = None,
        thickness_mm: Optional[float] = None,
        rotation_deg: Optional[float] = None,
        near_x_mm: Optional[float] = None,
        near_y_mm: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Edit a standalone PCB text item in place.

        Finds the text by its current content and applies updates without
        requiring delete-and-recreate.  If multiple items share the same
        text, supply near_x_mm / near_y_mm to pick the closest one.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            text: Current text content to match.
            new_text: Replacement text content. None to keep current.
            x_mm: New X position in mm. None to keep current.
            y_mm: New Y position in mm. None to keep current.
            layer: New layer name (e.g. "F.SilkS", "B.SilkS"). None to keep current.
            size_mm: New text height in mm. None to keep current.
            thickness_mm: New stroke thickness in mm. None to keep current.
            rotation_deg: New rotation in degrees. None to keep current.
            near_x_mm: Disambiguate by proximity — X of expected location.
            near_y_mm: Disambiguate by proximity — Y of expected location.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        if all(v is None for v in (new_text, x_mm, y_mm, layer, size_mm, thickness_mm, rotation_deg)):
            return {"error": "No modifications specified"}

        near_x = near_x_mm if near_x_mm is not None else "None"
        near_y = near_y_mm if near_y_mm is not None else "None"

        mods = []
        if new_text is not None:
            mods.append(f"item.SetText({new_text!r})")
        if x_mm is not None and y_mm is not None:
            mods.append(f"item.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))")
        elif x_mm is not None:
            mods.append(f"pos = item.GetPosition(); item.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pos.y))")
        elif y_mm is not None:
            mods.append(f"pos = item.GetPosition(); item.SetPosition(pcbnew.VECTOR2I(pos.x, pcbnew.FromMM({y_mm})))")
        if layer is not None:
            mods.append(f"item.SetLayer(board.GetLayerID({layer!r}))")
        if size_mm is not None:
            mods.append(f"item.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM({size_mm}), pcbnew.FromMM({size_mm})))")
        if thickness_mm is not None:
            mods.append(f"item.SetTextThickness(pcbnew.FromMM({thickness_mm}))")
        if rotation_deg is not None:
            mods.append(f"item.SetTextAngle(pcbnew.EDA_ANGLE({rotation_deg}, pcbnew.DEGREES_T))")
        mods_code = "\n".join(mods)

        script = f"""
import pcbnew, json, math

board = pcbnew.LoadBoard({pcb_path!r})

target_text = {text!r}
near_x = {near_x}
near_y = {near_y}

candidates = []
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetText() == target_text:
        candidates.append(drawing)

if not candidates:
    print(json.dumps({{"error": f"No standalone text matching {{target_text!r}} found"}}))
    raise SystemExit(0)

if len(candidates) > 1:
    if near_x is not None and near_y is not None:
        ref_x = pcbnew.FromMM(near_x)
        ref_y = pcbnew.FromMM(near_y)
        candidates.sort(key=lambda d: math.hypot(d.GetPosition().x - ref_x, d.GetPosition().y - ref_y))
    else:
        positions = [f"({{round(pcbnew.ToMM(d.GetPosition().x),2)}}, {{round(pcbnew.ToMM(d.GetPosition().y),2)}})" for d in candidates]
        print(json.dumps({{"error": f"Multiple items match {{target_text!r}}: {{positions}}. Supply near_x_mm/near_y_mm to disambiguate."}}))
        raise SystemExit(0)

item = candidates[0]
{mods_code}

board.Save({pcb_path!r})

pos = item.GetPosition()
size = item.GetTextSize()
print(json.dumps({{
    "status": "ok",
    "text": item.GetText(),
    "x_mm": round(pcbnew.ToMM(pos.x), 3),
    "y_mm": round(pcbnew.ToMM(pos.y), 3),
    "layer": board.GetLayerName(item.GetLayer()),
    "size_mm": round(pcbnew.ToMM(size.x), 3),
    "thickness_mm": round(pcbnew.ToMM(item.GetTextThickness()), 3),
    "rotation_deg": item.GetTextAngle().AsDegrees(),
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def check_silkscreen_overlaps(pcb_path: str) -> Dict[str, Any]:
        """Find silkscreen text items that overlap copper pads or other silkscreen text.

        Checks all visible silkscreen text (reference designators, values,
        standalone text) against all pads on the board and against each other.
        Reports overlaps between different components that could cause
        manufacturing issues.  Skips text overlapping its own component's
        pads (which is normal).

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

# Collect all visible silkscreen text items with bounding boxes
silk_items = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not field_obj.IsVisible():
            continue
        if field_obj.GetLayer() not in silk_layer_ids:
            continue
        bbox = field_obj.GetBoundingBox()
        silk_items.append({{
            "type": field_type,
            "component": ref,
            "text": field_obj.GetText(),
            "layer": board.GetLayerName(field_obj.GetLayer()),
            "bbox_x_min": bbox.GetX(),
            "bbox_y_min": bbox.GetY(),
            "bbox_x_max": bbox.GetRight(),
            "bbox_y_max": bbox.GetBottom(),
        }})

# Also check standalone text
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        if hasattr(drawing, 'IsVisible') and not drawing.IsVisible():
            continue
        bbox = drawing.GetBoundingBox()
        silk_items.append({{
            "type": "standalone",
            "component": None,
            "text": drawing.GetText(),
            "layer": board.GetLayerName(drawing.GetLayer()),
            "bbox_x_min": bbox.GetX(),
            "bbox_y_min": bbox.GetY(),
            "bbox_x_max": bbox.GetRight(),
            "bbox_y_max": bbox.GetBottom(),
        }})

# Collect all pads
pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        sz = pad.GetBoundingBox()
        pads.append({{
            "reference": fp.GetReference(),
            "pad_number": pad.GetNumber(),
            "x_min": sz.GetX(),
            "y_min": sz.GetY(),
            "x_max": sz.GetRight(),
            "y_max": sz.GetBottom(),
        }})

def aabb_overlap(ax_min, ay_min, ax_max, ay_max, bx_min, by_min, bx_max, by_max):
    return ax_min < bx_max and ax_max > bx_min and ay_min < by_max and ay_max > by_min

# Check text-over-pad overlaps (skip text overlapping own component)
pad_overlaps = []
for si in silk_items:
    for pad in pads:
        if si["component"] == pad["reference"]:
            continue
        if aabb_overlap(si["bbox_x_min"], si["bbox_y_min"], si["bbox_x_max"], si["bbox_y_max"],
                        pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
            pad_overlaps.append({{
                "silk_type": si["type"],
                "silk_component": si["component"],
                "silk_text": si["text"],
                "silk_layer": si["layer"],
                "pad_component": pad["reference"],
                "pad_number": pad["pad_number"],
            }})

# Check text-over-text overlaps (different components, same layer)
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
            text_overlaps.append({{
                "text_a_type": a["type"], "text_a_component": a["component"], "text_a": a["text"],
                "text_b_type": b["type"], "text_b_component": b["component"], "text_b": b["text"],
                "layer": a["layer"],
            }})

print(json.dumps({{
    "status": "ok",
    "silk_items_checked": len(silk_items),
    "pads_checked": len(pads),
    "overlap_count": len(pad_overlaps),
    "overlaps": pad_overlaps,
    "text_overlap_count": len(text_overlaps),
    "text_overlaps": text_overlaps,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def auto_fix_silkscreen(pcb_path: str) -> Dict[str, Any]:
        """Automatically fix silkscreen text that overlaps copper pads or other text.

        For each visible silkscreen text item (reference designator or value)
        that overlaps pads from a different component or text from a different
        component, tries up to 8 candidate positions arranged around the
        footprint's bounding box (N, S, W, E, NW, NE, SW, SE).  The first
        position that is free of pad and text overlaps and lies within the
        board outline is used.  Text is hidden only when all 8 positions fail.

        Does not modify standalone text items (only footprint reference/value).

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

# Collect all pads with bounding boxes
all_pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        sz = pad.GetBoundingBox()
        all_pads.append({{
            "reference": fp.GetReference(),
            "x_min": sz.GetX(), "y_min": sz.GetY(),
            "x_max": sz.GetRight(), "y_max": sz.GetBottom(),
        }})

# Collect all visible silk text objects for text-vs-text checking.
# We store the actual pcbnew field objects so we can re-read bboxes
# after earlier items have been moved.
all_silk = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for ft, fo in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not fo.IsVisible():
            continue
        if fo.GetLayer() not in silk_layer_ids:
            continue
        all_silk.append({{"component": ref, "field_type": ft, "obj": fo,
                          "layer": fo.GetLayer()}})
# Standalone text (not fixable, but used as obstacles)
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
        if vis:
            all_silk.append({{"component": None, "field_type": "standalone",
                              "obj": drawing, "layer": drawing.GetLayer()}})

# Board outline bbox for boundary clamping
try:
    board_bb = board.GetBoardEdgesBoundingBox()
    board_valid = board_bb.GetWidth() > 0
except Exception:
    board_valid = False

def aabb_hit(a, bx_min, by_min, bx_max, by_max):
    return (a.GetX() < bx_max and a.GetRight() > bx_min and
            a.GetY() < by_max and a.GetBottom() > by_min)

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
        if not si["obj"].IsVisible() if hasattr(si["obj"], 'IsVisible') else False:
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
        if not has_any_overlap(text_bbox, ref, own_layer, field_obj):
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
                fixed.append({{
                    "component": ref,
                    "field": field_type,
                    "action": "moved",
                    "x_mm": round(pcbnew.ToMM(pos.x), 2),
                    "y_mm": round(pcbnew.ToMM(pos.y), 2),
                }})
                break

        if not resolved:
            field_obj.SetPosition(orig_pos)
            field_obj.SetVisible(False)
            hidden.append({{
                "component": ref,
                "field": field_type,
                "action": "hidden",
            }})

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "already_ok": already_ok,
    "moved": len(fixed),
    "hidden": len(hidden),
    "fixes": fixed + hidden,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def finalize_pcb(
        pcb_path: str,
        fix_silkscreen: bool = True,
        fill_zones: bool = True,
    ) -> Dict[str, Any]:
        """Fix silkscreen overlaps and fill copper zones in one operation.

        A compound finalisation step to run before generating fabrication
        outputs.  Equivalent to auto_fix_silkscreen + fill_zones in sequence
        but with a single board load and save, so it is faster and avoids
        intermediate file states.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            fix_silkscreen: Run silkscreen overlap auto-fix (default True).
            fill_zones: Run copper zone fill (default True).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
results = {{}}

# ── Silkscreen fix ────────────────────────────────────────────────────────────
if {fix_silkscreen!r}:
    silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

    all_pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            sz = pad.GetBoundingBox()
            all_pads.append({{
                "reference": fp.GetReference(),
                "x_min": sz.GetX(), "y_min": sz.GetY(),
                "x_max": sz.GetRight(), "y_max": sz.GetBottom(),
            }})

    # Collect all visible silk text objects for text-vs-text checking
    all_silk = []
    for fp in board.GetFootprints():
        _ref = fp.GetReference()
        for _ft, _fo in [("reference", fp.Reference()), ("value", fp.Value())]:
            if not _fo.IsVisible() or _fo.GetLayer() not in silk_layer_ids:
                continue
            all_silk.append({{"component": _ref, "obj": _fo, "layer": _fo.GetLayer()}})
    for drawing in board.GetDrawings():
        if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
            _vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
            if _vis:
                all_silk.append({{"component": None, "obj": drawing, "layer": drawing.GetLayer()}})

    try:
        board_bb = board.GetBoardEdgesBoundingBox()
        board_valid = board_bb.GetWidth() > 0
    except Exception:
        board_valid = False

    def _aabb_hit(a, bx_min, by_min, bx_max, by_max):
        return (a.GetX() < bx_max and a.GetRight() > bx_min and
                a.GetY() < by_max and a.GetBottom() > by_min)

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
            if not has_any_overlap(text_bbox, ref, own_layer, field_obj):
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
                    silk_fixed.append({{"component": ref, "field": field_type,
                                        "action": "moved",
                                        "x_mm": round(pcbnew.ToMM(pos.x), 2),
                                        "y_mm": round(pcbnew.ToMM(pos.y), 2)}})
                    break
            if not resolved:
                field_obj.SetPosition(orig_pos)
                field_obj.SetVisible(False)
                silk_hidden.append({{"component": ref, "field": field_type, "action": "hidden"}})

    results["silkscreen"] = {{
        "already_ok": silk_ok,
        "moved": len(silk_fixed),
        "hidden": len(silk_hidden),
        "fixes": silk_fixed + silk_hidden,
    }}

# ── Zone fill ─────────────────────────────────────────────────────────────────
if {fill_zones!r}:
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
            zone_info.append({{"net": z.GetNetname(), "layer": lname, "filled": z.IsFilled()}})
        results["zones"] = {{"fill_success": fill_ok, "zones_filled": len(copper_zones), "zones": zone_info}}
    else:
        results["zones"] = {{"fill_success": True, "zones_filled": 0, "message": "No copper zones"}}

board.Save({pcb_path!r})
results["status"] = "ok"
print(json.dumps(results))
"""
        return run_pcbnew_script(script, timeout=120.0)
