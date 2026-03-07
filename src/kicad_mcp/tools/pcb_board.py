"""PCB board management tools: load, create, outline, and design rules."""

import logging
import os
from typing import Any, Dict

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def register_pcb_board_tools(mcp: FastMCP) -> None:
    """Register PCB board management tools."""

    @mcp.tool()
    def load_pcb(pcb_path: str) -> Dict[str, Any]:
        """Load a .kicad_pcb file and return its summary.

        Args:
            pcb_path: Absolute path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"File not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
footprints = board.GetFootprints()
tracks = board.GetTracks()

fp_list = []
for fp in footprints:
    pos = fp.GetPosition()
    fp_list.append({{
        "reference": fp.GetReference(),
        "value": fp.GetValue(),
        "footprint": fp.GetFPID().GetUniStringLibItemName(),
        "x_mm": pcbnew.ToMM(pos.x),
        "y_mm": pcbnew.ToMM(pos.y),
        "layer": board.GetLayerName(fp.GetLayer()),
    }})

print(json.dumps({{
    "status": "ok",
    "file": {pcb_path!r},
    "footprint_count": len(fp_list),
    "track_count": len(tracks),
    "footprints": fp_list,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def create_pcb(pcb_path: str) -> Dict[str, Any]:
        """Create a new empty .kicad_pcb file.

        Args:
            pcb_path: Absolute path for the new PCB file.
        """
        script = f"""
import pcbnew, json

board = pcbnew.CreateEmptyBoard()
board.SetFileName({pcb_path!r})
board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "file": {pcb_path!r},
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def add_board_outline(
        pcb_path: str,
        x_mm: float,
        y_mm: float,
        width_mm: float,
        height_mm: float,
    ) -> Dict[str, Any]:
        """Add a rectangular board outline (Edge.Cuts) to the PCB.

        Any existing Edge.Cuts segments are removed first, so this can be
        used to resize the board without creating a new PCB file.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            x_mm: Top-left X position in mm.
            y_mm: Top-left Y position in mm.
            width_mm: Board width in mm.
            height_mm: Board height in mm.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

# Remove existing Edge.Cuts segments so outline can be replaced
edge_cuts_id = pcbnew.Edge_Cuts
to_remove = []
for drawing in board.GetDrawings():
    if drawing.GetLayer() == edge_cuts_id:
        to_remove.append(drawing)
removed_count = len(to_remove)
for item in to_remove:
    board.Remove(item)

x = pcbnew.FromMM({x_mm})
y = pcbnew.FromMM({y_mm})
w = pcbnew.FromMM({width_mm})
h = pcbnew.FromMM({height_mm})

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

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "previous_edge_cuts_removed": removed_count,
    "outline": {{
        "x_mm": {x_mm},
        "y_mm": {y_mm},
        "width_mm": {width_mm},
        "height_mm": {height_mm},
    }},
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def set_design_rules(
        pcb_path: str,
        min_track_width_mm: float = 0.2,
        min_clearance_mm: float = 0.2,
        min_via_diameter_mm: float = 0.6,
        min_via_drill_mm: float = 0.3,
        min_hole_to_hole_mm: float = 0.25,
        min_through_hole_diameter_mm: float = 0.3,
        min_copper_edge_clearance_mm: float = 0.5,
    ) -> Dict[str, Any]:
        """Set PCB design rules (DRC constraints).

        Sets rules in both the PCB file (pcbnew design settings) and the
        project file (.kicad_pro DRC rules section).  The project file rules
        control DRC checks that pcbnew's DesignSettings doesn't cover, such
        as ``min_through_hole_diameter`` (needed for ESP32 thermal vias at
        0.2mm) and ``min_copper_edge_clearance`` (needed for edge-mounted
        connectors).

        Args:
            pcb_path: Path to the .kicad_pcb file.
            min_track_width_mm: Minimum track width in mm (default 0.2).
            min_clearance_mm: Minimum clearance between copper in mm (default 0.2).
            min_via_diameter_mm: Minimum via pad diameter in mm (default 0.6).
            min_via_drill_mm: Minimum via drill diameter in mm (default 0.3).
            min_hole_to_hole_mm: Minimum hole-to-hole distance in mm (default 0.25).
            min_through_hole_diameter_mm: Minimum through-hole drill diameter
                in mm (default 0.3).  Set to 0.15 for boards with ESP32 modules
                (their thermal vias use 0.2mm drills).
            min_copper_edge_clearance_mm: Minimum clearance from copper to board
                edge in mm (default 0.5).  Set to 0.0 for boards with edge-mounted
                connectors (USB-C, Phoenix terminals).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
ds = board.GetDesignSettings()

ds.SetCopperLayerCount(2)
ds.m_TrackMinWidth = pcbnew.FromMM({min_track_width_mm})
ds.m_MinClearance = pcbnew.FromMM({min_clearance_mm})
ds.m_ViasMinSize = pcbnew.FromMM({min_via_diameter_mm})
ds.m_ViasMinDrill = pcbnew.FromMM({min_via_drill_mm})
ds.m_HoleToHoleMin = pcbnew.FromMM({min_hole_to_hole_mm})

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "design_rules": {{
        "min_track_width_mm": {min_track_width_mm},
        "min_clearance_mm": {min_clearance_mm},
        "min_via_diameter_mm": {min_via_diameter_mm},
        "min_via_drill_mm": {min_via_drill_mm},
        "min_hole_to_hole_mm": {min_hole_to_hole_mm},
        "min_through_hole_diameter_mm": {min_through_hole_diameter_mm},
        "min_copper_edge_clearance_mm": {min_copper_edge_clearance_mm},
    }},
}}))
"""
        result = run_pcbnew_script(script)

        # Also update the .kicad_pro project file DRC rules
        stem = os.path.splitext(pcb_path)[0]
        pro_path = stem + ".kicad_pro"
        pro_updated = False
        if os.path.exists(pro_path):
            try:
                import json as _json
                with open(pro_path, "r") as f:
                    project = _json.load(f)
                # Navigate to board.design_settings.rules
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
            except Exception as e:
                logger.warning("Could not update .kicad_pro rules: %s", e)

        if isinstance(result, dict) and "error" not in result:
            result["project_rules_updated"] = pro_updated
            if pro_updated:
                result["project_file"] = pro_path

        return result
