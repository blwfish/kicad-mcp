"""PCB routing tools: traces and vias."""

import logging
import os
from typing import Any, Dict

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def register_pcb_routing_tools(mcp: FastMCP) -> None:
    """Register PCB routing tools."""

    @mcp.tool()
    def add_trace(
        pcb_path: str,
        start_x_mm: float,
        start_y_mm: float,
        end_x_mm: float,
        end_y_mm: float,
        width_mm: float = 0.25,
        layer: str = "F.Cu",
        net_name: str = "",
    ) -> Dict[str, Any]:
        """Add a copper trace between two points on the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            start_x_mm: Start X position in mm.
            start_y_mm: Start Y position in mm.
            end_x_mm: End X position in mm.
            end_y_mm: End Y position in mm.
            width_mm: Trace width in mm (default 0.25).
            layer: Copper layer name (default "F.Cu").
            net_name: Net name to assign (empty for unassigned).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

track = pcbnew.PCB_TRACK(board)
track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM({start_x_mm}), pcbnew.FromMM({start_y_mm})))
track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM({end_x_mm}), pcbnew.FromMM({end_y_mm})))
track.SetWidth(pcbnew.FromMM({width_mm}))
track.SetLayer(board.GetLayerID({layer!r}))

net_name = {net_name!r}
if net_name:
    net = board.FindNet(net_name)
    if net:
        track.SetNet(net)

board.Add(track)
board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "trace": {{
        "start": [{start_x_mm}, {start_y_mm}],
        "end": [{end_x_mm}, {end_y_mm}],
        "width_mm": {width_mm},
        "layer": {layer!r},
        "net": net_name,
    }},
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def add_via(
        pcb_path: str,
        x_mm: float,
        y_mm: float,
        drill_mm: float = 0.3,
        size_mm: float = 0.6,
        net_name: str = "",
        via_type: str = "through",
    ) -> Dict[str, Any]:
        """Add a via to the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            x_mm: X position in millimeters.
            y_mm: Y position in millimeters.
            drill_mm: Drill diameter in mm (default 0.3).
            size_mm: Via pad diameter in mm (default 0.6).
            net_name: Net name to assign (empty for unassigned).
            via_type: Via type - "through", "blind_buried", or "micro" (default "through").
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

via = pcbnew.PCB_VIA(board)
via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))
via.SetDrill(pcbnew.FromMM({drill_mm}))
via.SetWidth(pcbnew.FromMM({size_mm}))

via_type = {via_type!r}
if via_type == "blind_buried":
    via.SetViaType(pcbnew.VIATYPE_BLIND_BURIED)
elif via_type == "micro":
    via.SetViaType(pcbnew.VIATYPE_MICROVIA)
else:
    via.SetViaType(pcbnew.VIATYPE_THROUGH)

net_name = {net_name!r}
if net_name:
    net = board.FindNet(net_name)
    if net:
        via.SetNet(net)

board.Add(via)
board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "via": {{
        "x_mm": {x_mm},
        "y_mm": {y_mm},
        "drill_mm": {drill_mm},
        "size_mm": {size_mm},
        "type": via_type,
        "net": net_name,
    }},
}}))
"""
        return run_pcbnew_script(script)
