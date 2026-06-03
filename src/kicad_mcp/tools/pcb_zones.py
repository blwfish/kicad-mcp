"""PCB copper zone ops: add zones and fill zones.

These are module-level _op_* helpers consumed by the pcb router (pcb.py).
"""

import logging
import os
from typing import Any, Dict, List, Optional

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def _op_add_zone(
    pcb_path: str,
    net_name: str,
    layer: str = "F.Cu",
    corners: Optional[List[List[float]]] = None,
    clearance_mm: float = 0.3,
    min_width_mm: float = 0.2,
    connect_pads: str = "thermal",
    priority: int = 0,
) -> Dict[str, Any]:
    """Add a copper zone (pour/fill) to the PCB. An empty/None ``corners`` list
    auto-derives the zone outline from the board edge."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}
    if corners is None:
        corners = []

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

net = board.FindNet(params["net_name"])
if net is None or net.GetNetCode() == 0:
    print(json.dumps({"error": f"Net {params['net_name']!r} not found"}))
    raise SystemExit(0)

corners = params["corners"]
auto_outline = False
if len(corners) == 0:
    bb = board.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0 and bb.GetHeight() > 0:
        x0 = round(pcbnew.ToMM(bb.GetX()), 2)
        y0 = round(pcbnew.ToMM(bb.GetY()), 2)
        x1 = round(pcbnew.ToMM(bb.GetRight()), 2)
        y1 = round(pcbnew.ToMM(bb.GetBottom()), 2)
        corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        auto_outline = True
    else:
        print(json.dumps({"error": "No corners provided and no board outline (Edge.Cuts) found"}))
        raise SystemExit(0)
elif len(corners) < 3:
    print(json.dumps({"error": f"corners must be empty (auto-derive) or have >= 3 entries; got {len(corners)}"}))
    raise SystemExit(0)

zone = pcbnew.ZONE(board)
zone.SetNet(net)
_layer_id = board.GetLayerID(params["layer"])
if _layer_id < 0:   # GetLayerID returns -1 for an unknown name; SetLayer(-1) won't raise
    print(json.dumps({"error": f"unknown layer {params['layer']!r}"}))
    raise SystemExit(0)
zone.SetLayer(_layer_id)
zone.SetAssignedPriority(params["priority"])

zone.SetLocalClearance(pcbnew.FromMM(params["clearance_mm"]))
zone.SetMinThickness(pcbnew.FromMM(params["min_width_mm"]))

_PAD_CONNECT_MAP = {
    "solid": pcbnew.ZONE_CONNECTION_FULL,
    "none": pcbnew.ZONE_CONNECTION_NONE,
    "thermal": pcbnew.ZONE_CONNECTION_THERMAL,
}
connect = params["connect_pads"]
if connect not in _PAD_CONNECT_MAP:
    print(json.dumps({"error": f"connect_pads must be one of {sorted(_PAD_CONNECT_MAP)}; got {connect!r}"}))
    raise SystemExit(0)
zone.SetPadConnection(_PAD_CONNECT_MAP[connect])

outline = zone.Outline()
outline.NewOutline()
for i, (cx, cy) in enumerate(corners):
    outline.Append(pcbnew.FromMM(cx), pcbnew.FromMM(cy))

board.Add(zone)

filler = pcbnew.ZONE_FILLER(board)
filler.Fill(board.Zones())

board.Save(pcb_path)

result = {
    "status": "ok",
    "zone": {
        "net": params["net_name"],
        "layer": params["layer"],
        "corners": corners,
        "clearance_mm": params["clearance_mm"],
        "min_width_mm": params["min_width_mm"],
        "connect_pads": connect,
        "priority": params["priority"],
    },
}
if auto_outline:
    result["auto_outline"] = True
    result["note"] = "Zone corners auto-derived from board outline (Edge.Cuts)"
print(json.dumps(result))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "net_name": net_name,
        "layer": layer,
        "corners": corners,
        "clearance_mm": clearance_mm,
        "min_width_mm": min_width_mm,
        "connect_pads": connect_pads,
        "priority": priority,
    }, timeout=60.0)


def _op_fill_zones(pcb_path: str) -> Dict[str, Any]:
    """Fill all copper zones on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

copper_zones = []
for z in board.Zones():
    if not z.GetIsRuleArea():
        copper_zones.append(z)

if not copper_zones:
    print(json.dumps({"status": "ok", "message": "No copper zones to fill", "zones_filled": 0}))
    raise SystemExit(0)

for z in copper_zones:
    z.UnFill()

filler = pcbnew.ZONE_FILLER(board)
zones = board.Zones()
success = filler.Fill(zones)

zone_info = []
for z in copper_zones:
    layer_id = z.GetLayer()
    layer_name = board.GetLayerName(layer_id)
    area_iu_sq = z.GetFilledArea()
    area_mm_sq = pcbnew.ToMM(pcbnew.ToMM(area_iu_sq))
    zone_info.append({
        "net": z.GetNetname(),
        "layer": layer_name,
        "filled": z.IsFilled(),
        "filled_area_mm2": round(area_mm_sq, 3),
    })

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "fill_success": success,
    "zones_filled": len(copper_zones),
    "zones": zone_info,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path}, timeout=60.0)
