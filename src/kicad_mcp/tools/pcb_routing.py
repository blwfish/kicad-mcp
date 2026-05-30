"""PCB routing ops: traces, vias, and routing management.

These are module-level _op_* helpers consumed by the pcb router (pcb.py).
"""

import logging
import os
from typing import Any, Dict

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def _op_add_trace(
    pcb_path: str,
    start_x_mm: float,
    start_y_mm: float,
    end_x_mm: float,
    end_y_mm: float,
    width_mm: float = 0.25,
    layer: str = "F.Cu",
    net_name: str = "",
) -> Dict[str, Any]:
    """Add a copper trace between two points on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}
    if width_mm <= 0:
        return {"error": f"width_mm must be positive (got {width_mm})"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

track = pcbnew.PCB_TRACK(board)
track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(params["start_x_mm"]), pcbnew.FromMM(params["start_y_mm"])))
track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(params["end_x_mm"]), pcbnew.FromMM(params["end_y_mm"])))
track.SetWidth(pcbnew.FromMM(params["width_mm"]))
_layer_id = board.GetLayerID(params["layer"])
if _layer_id < 0:   # GetLayerID returns -1 for an unknown name; SetLayer(-1) won't raise
    print(json.dumps({"error": f"unknown layer {params['layer']!r}"}))
    raise SystemExit(0)
track.SetLayer(_layer_id)

net_name = params["net_name"]
if net_name:
    net = board.FindNet(net_name)
    if net is None:
        print(json.dumps({"error": f"Net {net_name!r} not found on PCB"}))
        raise SystemExit(0)
    track.SetNet(net)

board.Add(track)
board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "trace": {
        "start": [params["start_x_mm"], params["start_y_mm"]],
        "end": [params["end_x_mm"], params["end_y_mm"]],
        "width_mm": params["width_mm"],
        "layer": params["layer"],
        "net": net_name,
    },
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "start_x_mm": start_x_mm,
        "start_y_mm": start_y_mm,
        "end_x_mm": end_x_mm,
        "end_y_mm": end_y_mm,
        "width_mm": width_mm,
        "layer": layer,
        "net_name": net_name,
    })


def _op_add_via(
    pcb_path: str,
    x_mm: float,
    y_mm: float,
    drill_mm: float = 0.3,
    size_mm: float = 0.6,
    net_name: str = "",
    via_type: str = "through",
) -> Dict[str, Any]:
    """Add a via to the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}
    if drill_mm <= 0:
        return {"error": f"drill_mm must be positive (got {drill_mm})"}
    if size_mm <= 0:
        return {"error": f"size_mm must be positive (got {size_mm})"}
    if drill_mm >= size_mm:
        return {"error": f"drill_mm ({drill_mm}) must be less than size_mm ({size_mm}) — a drill >= pad means no annular ring"}
    valid_via_types = ("through", "blind_buried", "micro")
    if via_type not in valid_via_types:
        return {"error": f"via_type must be one of {valid_via_types}; got {via_type!r}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

via = pcbnew.PCB_VIA(board)
via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pcbnew.FromMM(params["y_mm"])))
via.SetDrill(pcbnew.FromMM(params["drill_mm"]))
via.SetWidth(pcbnew.FromMM(params["size_mm"]))

via_type = params["via_type"]
_VIATYPE_MAP = {
    "through":      pcbnew.VIATYPE_THROUGH,
    "blind_buried": pcbnew.VIATYPE_BLIND_BURIED,
    "micro":        pcbnew.VIATYPE_MICROVIA,
}
via.SetViaType(_VIATYPE_MAP[via_type])

net_name = params["net_name"]
if net_name:
    net = board.FindNet(net_name)
    if net is None:
        print(json.dumps({"error": f"Net {net_name!r} not found on PCB"}))
        raise SystemExit(0)
    via.SetNet(net)

board.Add(via)
board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "via": {
        "x_mm": params["x_mm"],
        "y_mm": params["y_mm"],
        "drill_mm": params["drill_mm"],
        "size_mm": params["size_mm"],
        "type": via_type,
        "net": net_name,
    },
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "drill_mm": drill_mm,
        "size_mm": size_mm,
        "via_type": via_type,
        "net_name": net_name,
    })


def _op_edit_trace_width(
    pcb_path: str,
    new_width_mm: float,
    net_name: str = "",
    layer: str = "",
) -> Dict[str, Any]:
    """Change the width of existing traces."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}
    if new_width_mm <= 0:
        return {"error": f"new_width_mm must be positive (got {new_width_mm})"}
    if net_name and net_name != net_name.strip():
        return {"error": f"net_name has surrounding whitespace; use empty string for 'all nets', not {net_name!r}"}
    if layer and layer != layer.strip():
        return {"error": f"layer has surrounding whitespace; use empty string for 'all layers', not {layer!r}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

net_filter = params["net_name"]
layer_filter = params["layer"]
new_width = pcbnew.FromMM(params["new_width_mm"])
updated = 0
skipped = 0

for track in board.GetTracks():
    if track.GetClass() != "PCB_TRACK":
        skipped += 1
        continue
    if net_filter and track.GetNetname() != net_filter:
        skipped += 1
        continue
    if layer_filter and board.GetLayerName(track.GetLayer()) != layer_filter:
        skipped += 1
        continue
    track.SetWidth(new_width)
    updated += 1

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "updated": updated,
    "skipped": skipped,
    "new_width_mm": params["new_width_mm"],
    "net_filter": net_filter or "(all)",
    "layer_filter": layer_filter or "(all)",
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "new_width_mm": new_width_mm,
        "net_name": net_name,
        "layer": layer,
    })


def _op_clear_routing(
    pcb_path: str,
    clear_tracks: bool = True,
    clear_vias: bool = True,
    clear_zones: bool = False,
) -> Dict[str, Any]:
    """Remove tracks, vias, and/or copper zones from the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

tracks_removed = 0
vias_removed = 0
zones_removed = 0

if params["clear_tracks"] or params["clear_vias"]:
    to_remove = []
    for track in board.GetTracks():
        if params["clear_tracks"] and track.GetClass() == "PCB_TRACK":
            to_remove.append(track)
        elif params["clear_vias"] and track.GetClass() == "PCB_VIA":
            to_remove.append(track)
    for item in to_remove:
        if item.GetClass() == "PCB_VIA":
            vias_removed += 1
        else:
            tracks_removed += 1
        board.Remove(item)

if params["clear_zones"]:
    zone_list = list(board.Zones())
    for zone in zone_list:
        if zone.GetIsRuleArea():
            continue
        board.Remove(zone)
        zones_removed += 1

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "tracks_removed": tracks_removed,
    "vias_removed": vias_removed,
    "zones_removed": zones_removed,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "clear_tracks": clear_tracks,
        "clear_vias": clear_vias,
        "clear_zones": clear_zones,
    })
