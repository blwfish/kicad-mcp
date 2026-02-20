"""PCB copper zone tools: add zones and fill zones."""

import logging
import os
from typing import Any, Dict, List

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def register_pcb_zone_tools(mcp: FastMCP) -> None:
    """Register PCB copper zone tools."""

    @mcp.tool()
    def add_copper_zone(
        pcb_path: str,
        net_name: str,
        layer: str = "F.Cu",
        corners: List[List[float]] = [],
        clearance_mm: float = 0.3,
        min_width_mm: float = 0.2,
        connect_pads: str = "thermal",
        priority: int = 0,
    ) -> Dict[str, Any]:
        """Add a copper zone (pour/fill) to the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            net_name: Net name for the zone (e.g., "GND").
            layer: Copper layer (default "F.Cu").
            corners: List of [x_mm, y_mm] corner points defining the zone outline.
            clearance_mm: Clearance to other nets in mm (default 0.3).
            min_width_mm: Minimum fill width in mm (default 0.2).
            connect_pads: Pad connection type - "thermal", "solid", or "none" (default "thermal").
            priority: Zone priority (higher fills first, default 0).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        if len(corners) < 3:
            return {"error": "Zone needs at least 3 corner points"}

        corners_repr = repr(corners)

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

net = board.FindNet({net_name!r})
if net is None or net.GetNetCode() == 0:
    print(json.dumps({{"error": f"Net {net_name!r} not found"}}))
    raise SystemExit(0)

zone = pcbnew.ZONE(board)
zone.SetNet(net)
zone.SetLayer(board.GetLayerID({layer!r}))
zone.SetAssignedPriority({priority})

# Set clearance and minimum width
zone.SetLocalClearance(pcbnew.FromMM({clearance_mm}))
zone.SetMinThickness(pcbnew.FromMM({min_width_mm}))

# Set pad connection type
connect = {connect_pads!r}
if connect == "solid":
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_FULL)
elif connect == "none":
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_NONE)
else:
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)

# Build outline
outline = zone.Outline()
outline.NewOutline()
corners = {corners_repr}
for i, (cx, cy) in enumerate(corners):
    outline.Append(pcbnew.FromMM(cx), pcbnew.FromMM(cy))

board.Add(zone)

# Fill the zone
filler = pcbnew.ZONE_FILLER(board)
zones = board.Zones()
zone_list = pcbnew.ZONES()
for z in zones:
    zone_list.append(z)
filler.Fill(zone_list)

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "zone": {{
        "net": {net_name!r},
        "layer": {layer!r},
        "corners": corners,
        "clearance_mm": {clearance_mm},
        "min_width_mm": {min_width_mm},
        "connect_pads": connect,
        "priority": {priority},
    }},
}}))
"""
        return run_pcbnew_script(script, timeout=60.0)

    @mcp.tool()
    def fill_zones(pcb_path: str) -> Dict[str, Any]:
        """Fill all copper zones on the PCB.

        Runs the zone filler headlessly to compute copper fills for all
        non-rule-area zones. This replaces the manual "press B in KiCad" step.
        Zones must already exist on the board (use add_copper_zone first).

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

# Count copper zones (skip rule areas)
copper_zones = []
for z in board.Zones():
    if not z.GetIsRuleArea():
        copper_zones.append(z)

if not copper_zones:
    print(json.dumps({{"status": "ok", "message": "No copper zones to fill", "zones_filled": 0}}))
    raise SystemExit(0)

# Unfill first to force recomputation
for z in copper_zones:
    z.UnFill()

# Fill all zones (never pass aCheck=True headlessly — it hangs)
filler = pcbnew.ZONE_FILLER(board)
zones = board.Zones()
success = filler.Fill(zones)

# Collect results
zone_info = []
for z in copper_zones:
    layer_set = z.GetLayerSet()
    layer_name = "F.Cu" if layer_set.Contains(pcbnew.F_Cu) else "B.Cu" if layer_set.Contains(pcbnew.B_Cu) else "unknown"
    zone_info.append({{
        "net": z.GetNetname(),
        "layer": layer_name,
        "filled": z.IsFilled(),
        "filled_area_mm2": round(pcbnew.ToMM(z.GetFilledArea()), 1),
    }})

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "fill_success": success,
    "zones_filled": len(copper_zones),
    "zones": zone_info,
}}))
"""
        return run_pcbnew_script(script, timeout=60.0)
