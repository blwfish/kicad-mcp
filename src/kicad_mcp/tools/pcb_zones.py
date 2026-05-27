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
                If empty, automatically uses the board outline (Edge.Cuts)
                as the zone boundary — the most common use case for ground pours.
            clearance_mm: Clearance to other nets in mm (default 0.3).
            min_width_mm: Minimum fill width in mm (default 0.2).
            connect_pads: Pad connection type - "thermal", "solid", or "none" (default "thermal").
                Note: "thermal" relief can cause starved-thermal DRC violations when
                autorouted traces block spoke formation. Use "solid" to avoid this.
            priority: Zone priority (higher fills first, default 0).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

net = board.FindNet(params["net_name"])
if net is None or net.GetNetCode() == 0:
    print(json.dumps({"error": f"Net {params['net_name']!r} not found"}))
    raise SystemExit(0)

# Determine zone corners. Empty list = auto-derive from board outline.
# A list with 1 or 2 entries is almost certainly a caller error (a polygon
# needs >= 3 vertices); we reject it explicitly rather than silently
# falling back to the auto-outline path.
corners = params["corners"]
auto_outline = False
if len(corners) == 0:
    # Auto-derive from board outline (Edge.Cuts bounding box)
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
zone.SetLayer(board.GetLayerID(params["layer"]))
zone.SetAssignedPriority(params["priority"])

# Set clearance and minimum width
zone.SetLocalClearance(pcbnew.FromMM(params["clearance_mm"]))
zone.SetMinThickness(pcbnew.FromMM(params["min_width_mm"]))

# Set pad connection type. Reject unknown values explicitly — silently
# defaulting to thermal would hide caller typos that change the
# electrical behavior of the zone.
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

# Build outline
outline = zone.Outline()
outline.NewOutline()
for i, (cx, cy) in enumerate(corners):
    outline.Append(pcbnew.FromMM(cx), pcbnew.FromMM(cy))

board.Add(zone)

# Fill the zone (pass board.Zones() directly — matches fill_zones pattern;
# the previous manual ZONES() copy was redundant.)
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

        script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

# Count copper zones (skip rule areas)
copper_zones = []
for z in board.Zones():
    if not z.GetIsRuleArea():
        copper_zones.append(z)

if not copper_zones:
    print(json.dumps({"status": "ok", "message": "No copper zones to fill", "zones_filled": 0}))
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
    # Use board.GetLayerName for the actual layer — the previous
    # two-branch F.Cu/B.Cu/"unknown" classifier silently reported every
    # inner-copper zone as "unknown".
    layer_id = z.GetLayer()
    layer_name = board.GetLayerName(layer_id)
    # GetFilledArea returns IU² (nm²). ToMM(x) converts nm → mm (one
    # power of 10^-6). For area we need 10^-12 — apply ToMM twice, or
    # equivalently divide by 1e12. Previously this called ToMM once and
    # reported filled_area_mm2 inflated by a factor of 10^6.
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
