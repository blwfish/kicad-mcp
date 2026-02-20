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
    ) -> Dict[str, Any]:
        """Set PCB design rules (DRC constraints).

        Args:
            pcb_path: Path to the .kicad_pcb file.
            min_track_width_mm: Minimum track width in mm (default 0.2).
            min_clearance_mm: Minimum clearance between copper in mm (default 0.2).
            min_via_diameter_mm: Minimum via pad diameter in mm (default 0.6).
            min_via_drill_mm: Minimum via drill diameter in mm (default 0.3).
            min_hole_to_hole_mm: Minimum hole-to-hole distance in mm (default 0.25).
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
    }},
}}))
"""
        return run_pcbnew_script(script)
