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
    def check_silkscreen_overlaps(pcb_path: str) -> Dict[str, Any]:
        """Find silkscreen text items that overlap copper pads.

        Checks all visible silkscreen text (reference designators, values,
        standalone text) against all pads on the board. Reports overlaps
        between different components that could cause manufacturing issues.
        Skips text overlapping its own component's pads (which is normal).

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

# Check for AABB overlaps (skip text overlapping own component)
overlaps = []
for si in silk_items:
    for pad in pads:
        if si["component"] == pad["reference"]:
            continue
        if (si["bbox_x_min"] < pad["x_max"] and si["bbox_x_max"] > pad["x_min"] and
            si["bbox_y_min"] < pad["y_max"] and si["bbox_y_max"] > pad["y_min"]):
            overlaps.append({{
                "silk_type": si["type"],
                "silk_component": si["component"],
                "silk_text": si["text"],
                "silk_layer": si["layer"],
                "pad_component": pad["reference"],
                "pad_number": pad["pad_number"],
            }})

print(json.dumps({{
    "status": "ok",
    "silk_items_checked": len(silk_items),
    "pads_checked": len(pads),
    "overlap_count": len(overlaps),
    "overlaps": overlaps,
}}))
"""
        return run_pcbnew_script(script)
