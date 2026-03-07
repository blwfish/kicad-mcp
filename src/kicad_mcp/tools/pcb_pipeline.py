"""PCB pipeline tool: build a routed PCB from a schematic in one step."""

import glob
import logging
import math
import os
import re
import subprocess
import time
import zipfile
from typing import Any, Dict, List

from fastmcp import FastMCP

from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path
from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli
from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal pipeline step functions (module-level for testability)
# ---------------------------------------------------------------------------


def _step_extract_netlist(sch_path: str) -> Dict[str, Any]:
    """Step 1: Extract components and nets from schematic via kicad-cli."""
    netlist = extract_netlist_via_cli(sch_path)
    if netlist is None:
        return {"error": "kicad-cli not available for netlist export"}

    components = netlist.get("components", {})
    nets = netlist.get("nets", {})

    # Separate components with/without footprints
    with_fp = {}
    without_fp = []
    for ref, info in components.items():
        fp = info.get("footprint", "")
        if fp and ":" in fp:
            with_fp[ref] = info
        else:
            without_fp.append(ref)

    return {
        "status": "ok",
        "components": with_fp,
        "components_without_footprint": without_fp,
        "nets": nets,
        "component_count": len(with_fp),
        "net_count": len(nets),
        "skipped_count": len(without_fp),
    }


def _step_create_pcb_and_outline(
    pcb_path: str,
    width_mm: float,
    height_mm: float,
    components: Dict[str, Dict],
) -> Dict[str, Any]:
    """Step 2: Create PCB file and add board outline.

    If width/height are 0, auto-estimates from component footprints.
    """
    # Build footprint list for size estimation if needed
    auto_sized = False
    if width_mm <= 0 or height_mm <= 0:
        fp_specs = []
        for ref, info in components.items():
            fp_str = info["footprint"]
            lib, name = fp_str.split(":", 1)
            fp_specs.append({"library": lib, "footprint_name": name})

        if not fp_specs:
            return {"error": "No footprints to estimate board size from"}

        size_result = _estimate_board_size(fp_specs)
        if "error" in size_result:
            return size_result

        # Use the 4:3 suggestion (good balance of space)
        suggestions = size_result.get("suggested_sizes", [])
        chosen = next((s for s in suggestions if s["label"] == "4:3"), suggestions[0])
        width_mm = chosen["width_mm"]
        height_mm = chosen["height_mm"]
        auto_sized = True

    # Create the PCB file
    script = f"""
import pcbnew, json

board = pcbnew.CreateEmptyBoard()
board.Save({pcb_path!r})
print(json.dumps({{"status": "ok"}}))
"""
    result = run_pcbnew_script(script)
    if "error" in result:
        return result

    # Add board outline
    script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

# Remove any existing Edge.Cuts
to_remove = []
for dwg in board.GetDrawings():
    if dwg.GetLayer() == pcbnew.Edge_Cuts:
        to_remove.append(dwg)
for dwg in to_remove:
    board.Remove(dwg)

# Add rectangular outline
x = pcbnew.FromMM(0)
y = pcbnew.FromMM(0)
w = pcbnew.FromMM({width_mm})
h = pcbnew.FromMM({height_mm})

corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
for i in range(4):
    seg = pcbnew.PCB_SHAPE(board)
    seg.SetShape(pcbnew.SHAPE_T_SEGMENT)
    seg.SetLayer(pcbnew.Edge_Cuts)
    seg.SetWidth(pcbnew.FromMM(0.05))
    x0, y0 = corners[i]
    x1, y1 = corners[(i + 1) % 4]
    seg.SetStart(pcbnew.VECTOR2I(x0, y0))
    seg.SetEnd(pcbnew.VECTOR2I(x1, y1))
    board.Add(seg)

board.Save({pcb_path!r})
print(json.dumps({{"status": "ok"}}))
"""
    result = run_pcbnew_script(script)
    if "error" in result:
        return result

    return {
        "status": "ok",
        "width_mm": width_mm,
        "height_mm": height_mm,
        "auto_sized": auto_sized,
    }


def _estimate_board_size(fp_specs: List[Dict[str, str]]) -> Dict[str, Any]:
    """Estimate board dimensions from footprint specs (same logic as estimate_board_size tool)."""
    fp_list_repr = repr(fp_specs)

    script = f"""
import pcbnew, json, os, math

fp_specs = {fp_list_repr}
padding = 2.0
routing_factor = 2.5

lib_search_paths = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    os.path.expanduser("~/Documents/KiCad/footprints"),
    "/usr/share/kicad/footprints",
]

def find_lib(lib_name):
    for sp in lib_search_paths:
        candidate = os.path.join(sp, lib_name + ".pretty")
        if os.path.isdir(candidate):
            return candidate
    return None

components = []
errors = []
total_area = 0.0
max_dim = 0.0

for spec in fp_specs:
    lib_name = spec["library"]
    fp_name = spec["footprint_name"]
    lib_path = find_lib(lib_name)
    if not lib_path:
        errors.append(f"Library '{{lib_name}}' not found")
        continue
    fp = pcbnew.FootprintLoad(lib_path, fp_name)
    if fp is None:
        errors.append(f"Footprint '{{fp_name}}' not found in '{{lib_name}}'")
        continue
    bbox = fp.GetBoundingBox(False, False)
    w = round(pcbnew.ToMM(bbox.GetWidth()), 2)
    h = round(pcbnew.ToMM(bbox.GetHeight()), 2)
    total_area += w * h
    max_dim = max(max_dim, w, h)
    components.append({{"library": lib_name, "footprint": fp_name, "width_mm": w, "height_mm": h}})

if not components:
    print(json.dumps({{"error": "No valid footprints found", "details": errors}}))
    raise SystemExit(0)

needed_area = total_area * routing_factor
min_dim = max_dim + padding * 2

suggestions = []
for label, ratio in [("square", 1.0), ("4:3", 4/3), ("3:2", 3/2)]:
    h = math.sqrt(needed_area / ratio)
    w = h * ratio
    w = max(w, min_dim) + padding * 2
    h = max(h, min_dim) + padding * 2
    w = math.ceil(w)
    h = math.ceil(h)
    suggestions.append({{"label": label, "width_mm": w, "height_mm": h}})

print(json.dumps({{"status": "ok", "suggested_sizes": suggestions, "errors": errors}}))
"""
    return run_pcbnew_script(script)


def _step_place_footprints(
    pcb_path: str,
    components: Dict[str, Dict],
    board_width_mm: float,
    board_height_mm: float,
) -> Dict[str, Any]:
    """Step 3: Place all footprints in a grid layout (temporary positions)."""
    # Build placement list: [{ref, library, footprint_name, value}, ...]
    placements = []
    for ref, info in components.items():
        fp_str = info["footprint"]
        lib, name = fp_str.split(":", 1)
        placements.append({
            "ref": ref,
            "library": lib,
            "footprint_name": name,
            "value": info.get("value", ref),
        })

    placements_repr = repr(placements)

    script = f"""
import pcbnew, json, os, math

board = pcbnew.LoadBoard({pcb_path!r})
placements = {placements_repr}
board_w = {board_width_mm}
board_h = {board_height_mm}

lib_search_paths = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    os.path.expanduser("~/Documents/KiCad/footprints"),
    "/usr/share/kicad/footprints",
]

def find_lib(lib_name):
    for sp in lib_search_paths:
        candidate = os.path.join(sp, lib_name + ".pretty")
        if os.path.isdir(candidate):
            return candidate
    return None

placed = []
errors = []

# Grid layout: place components in rows, starting from top-left with padding
margin = 5.0  # mm from board edge
grid_x = margin
grid_y = margin
row_height = 0.0
max_x = board_w - margin

for p in placements:
    lib_path = find_lib(p["library"])
    if not lib_path:
        errors.append(f"Library '{{p['library']}}' not found for {{p['ref']}}")
        continue

    fp = pcbnew.FootprintLoad(lib_path, p["footprint_name"])
    if fp is None:
        errors.append(f"Footprint '{{p['footprint_name']}}' not found for {{p['ref']}}")
        continue

    # Measure footprint size
    bbox = fp.GetBoundingBox(False, False)
    w = pcbnew.ToMM(bbox.GetWidth())
    h = pcbnew.ToMM(bbox.GetHeight())

    # Check if we need to wrap to next row
    if grid_x + w > max_x and grid_x > margin:
        grid_x = margin
        grid_y += row_height + 2.0  # 2mm gap between rows
        row_height = 0.0

    # Position at grid cell center
    cx = grid_x + w / 2
    cy = grid_y + h / 2

    fp.SetReference(p["ref"])
    fp.SetValue(p["value"])
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(cx), pcbnew.FromMM(cy)))
    board.Add(fp)

    placed.append({{"ref": p["ref"], "x_mm": round(cx, 2), "y_mm": round(cy, 2)}})
    grid_x += w + 2.0  # 2mm gap between components
    row_height = max(row_height, h)

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "placed_count": len(placed),
    "placed": placed,
    "errors": errors,
}}))
"""
    return run_pcbnew_script(script, timeout=30.0)


def _step_inject_nets_and_assign_pads(
    pcb_path: str,
    nets: Dict[str, List],
) -> Dict[str, Any]:
    """Step 4: Inject nets into PCB file and assign pads.

    Reuses the same pattern as update_pcb_from_schematic.
    """
    # Build net definitions and pad assignments from netlist data
    net_definitions = list(nets.keys())
    pad_assignments = []
    for net_name, pins in nets.items():
        for pin_info in pins:
            pad_assignments.append({
                "reference": pin_info["component"],
                "pad": pin_info["pin"],
                "net": net_name,
                "pinfunction": pin_info.get("pinfunction", ""),
            })

    if not net_definitions:
        return {"status": "ok", "nets_created": 0, "pads_assigned": 0,
                "warning": "No nets found in schematic"}

    # Inject nets via direct file editing (pcbnew prunes unused nets)
    with open(pcb_path, "r") as f:
        pcb_content = f.read()

    existing_nets = {}
    for m in re.finditer(r'\(net\s+(\d+)\s+"([^"]*)"\)', pcb_content):
        existing_nets[m.group(2)] = int(m.group(1))

    max_code = max(existing_nets.values()) if existing_nets else 0
    nets_created = []

    for net_name in net_definitions:
        if net_name not in existing_nets:
            max_code += 1
            existing_nets[net_name] = max_code
            nets_created.append(net_name)

    if nets_created:
        last_net_match = None
        for m in re.finditer(r'\(net\s+\d+\s+"[^"]*"\)', pcb_content):
            last_net_match = m

        if last_net_match:
            insert_pos = last_net_match.end()
            new_lines = ""
            for net_name in nets_created:
                code = existing_nets[net_name]
                new_lines += f'\n\t(net {code} "{net_name}")'
            pcb_content = (
                pcb_content[:insert_pos] + new_lines + pcb_content[insert_pos:]
            )
            with open(pcb_path, "w") as f:
                f.write(pcb_content)

    # Assign pads via pcbnew
    assignments_repr = repr(pad_assignments)

    script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
assignments = {assignments_repr}
assigned = []
assign_errors = []

for a in assignments:
    ref = a["reference"]
    pad_num = a["pad"]
    net_name = a["net"]

    fp = board.FindFootprintByReference(ref)
    if fp is None:
        assign_errors.append(f"Footprint {{ref}} not found in PCB")
        continue

    net = board.FindNet(net_name)
    if net is None or net.GetNetCode() == 0:
        assign_errors.append(f"Net {{net_name}} not found")
        continue

    pad_count = 0
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            pad.SetNet(net)
            pad_count += 1
    if pad_count > 0:
        assigned.append({{"reference": ref, "pad": pad_num, "net": net_name}})
    else:
        assign_errors.append(f"Pad {{pad_num}} not found on {{ref}}")

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "pads_assigned": len(assigned),
    "assignment_errors": assign_errors,
}}))
"""
    result = run_pcbnew_script(script, timeout=60.0)

    # Merge
    result["nets_created"] = len(nets_created)
    result["total_nets"] = len(net_definitions)
    return result


def _step_optimize_placement(pcb_path: str, spacing_mm: float = 1.0) -> Dict[str, Any]:
    """Step 5: Optimize footprint placement based on connectivity, then apply moves."""
    script = f"""
import pcbnew, json, math
{KEEPOUT_HELPER}

board = pcbnew.LoadBoard({pcb_path!r})
spacing = {spacing_mm}
outline = get_board_outline(board)

if not outline:
    print(json.dumps({{"status": "ok", "message": "No board outline, skipping placement optimization"}}))
    raise SystemExit(0)

board_w = outline["width_mm"]
board_h = outline["height_mm"]
board_cx = outline["x_min_mm"] + board_w / 2
board_cy = outline["y_min_mm"] + board_h / 2

# Collect footprint info
fp_info = {{}}
for fp in board.GetFootprints():
    ref = fp.GetReference()
    bbox = fp.GetBoundingBox(False, False)
    w = pcbnew.ToMM(bbox.GetWidth())
    h = pcbnew.ToMM(bbox.GetHeight())

    cy_xmin = float("inf"); cy_ymin = float("inf")
    cy_xmax = float("-inf"); cy_ymax = float("-inf")
    cy_found = False
    for item in fp.GraphicalItems():
        layer_name = board.GetLayerName(item.GetLayer())
        if "CrtYd" in layer_name:
            cy_found = True
            ib = item.GetBoundingBox()
            cy_xmin = min(cy_xmin, pcbnew.ToMM(ib.GetX()))
            cy_ymin = min(cy_ymin, pcbnew.ToMM(ib.GetY()))
            cy_xmax = max(cy_xmax, pcbnew.ToMM(ib.GetRight()))
            cy_ymax = max(cy_ymax, pcbnew.ToMM(ib.GetBottom()))
    if cy_found:
        w = cy_xmax - cy_xmin
        h = cy_ymax - cy_ymin

    has_keepout = False
    keepout_side = None
    try:
        for zone in fp.Zones():
            if zone.GetIsRuleArea():
                has_keepout = True
                zbb = zone.GetBoundingBox()
                fp_pos = fp.GetPosition()
                zx = pcbnew.ToMM(zbb.GetX()) - pcbnew.ToMM(fp_pos.x)
                zy = pcbnew.ToMM(zbb.GetY()) - pcbnew.ToMM(fp_pos.y)
                zr = pcbnew.ToMM(zbb.GetRight()) - pcbnew.ToMM(fp_pos.x)
                zb = pcbnew.ToMM(zbb.GetBottom()) - pcbnew.ToMM(fp_pos.y)
                extents = {{"left": abs(zx), "right": abs(zr), "top": abs(zy), "bottom": abs(zb)}}
                keepout_side = max(extents, key=extents.get)
    except AttributeError:
        pass

    pad_nets = set()
    for pad in fp.Pads():
        net = pad.GetNetname()
        if net and not net.startswith("unconnected-"):
            pad_nets.add(net)

    fp_info[ref] = {{
        "width": round(w, 2), "height": round(h, 2),
        "nets": pad_nets, "has_keepout": has_keepout,
        "keepout_side": keepout_side,
    }}

if not fp_info:
    print(json.dumps({{"status": "ok", "message": "No footprints to optimize"}}))
    raise SystemExit(0)

# Build connectivity graph (signal nets only)
connectivity = {{}}
refs = list(fp_info.keys())
for i in range(len(refs)):
    for j in range(i + 1, len(refs)):
        a, b = refs[i], refs[j]
        shared = fp_info[a]["nets"] & fp_info[b]["nets"]
        signal_shared = {{n for n in shared if not any(
            p in n.upper() for p in ["GND", "VCC", "VDD", "3V3", "3.3V", "5V", "+5", "+3"])}}
        if signal_shared:
            connectivity[(a, b)] = len(signal_shared)

# Rank by connectivity
conn_score = {{ref: 0 for ref in refs}}
for (a, b), count in connectivity.items():
    conn_score[a] += count
    conn_score[b] += count

sorted_refs = sorted(refs, key=lambda r: (conn_score[r], fp_info[r]["width"] * fp_info[r]["height"]), reverse=True)

# Place components
placements = {{}}
placed_boxes = []

def box_collides(bx, placed, gap):
    for pb in placed:
        if (bx[0] - gap < pb[2] and bx[2] + gap > pb[0] and
            bx[1] - gap < pb[3] and bx[3] + gap > pb[1]):
            return True
    return False

def clamp_to_board(x, y, w, h):
    half_w, half_h = w / 2, h / 2
    x = max(outline["x_min_mm"] + half_w + spacing, min(x, outline["x_max_mm"] - half_w - spacing))
    y = max(outline["y_min_mm"] + half_h + spacing, min(y, outline["y_max_mm"] - half_h - spacing))
    return x, y

if sorted_refs:
    hub = sorted_refs[0]
    info = fp_info[hub]
    cx, cy = board_cx, board_cy

    if info["has_keepout"] and info["keepout_side"]:
        side = info["keepout_side"]
        if side == "top":
            cy = outline["y_min_mm"] + info["height"] / 2 + spacing
        elif side == "bottom":
            cy = outline["y_max_mm"] - info["height"] / 2 - spacing
        elif side == "left":
            cx = outline["x_min_mm"] + info["width"] / 2 + spacing
        elif side == "right":
            cx = outline["x_max_mm"] - info["width"] / 2 - spacing

    cx, cy = clamp_to_board(cx, cy, info["width"], info["height"])
    hw, hh = info["width"] / 2, info["height"] / 2
    placements[hub] = (round(cx, 2), round(cy, 2))
    placed_boxes.append((cx - hw, cy - hh, cx + hw, cy + hh))

    for ref in sorted_refs[1:]:
        info = fp_info[ref]
        hw, hh = info["width"] / 2, info["height"] / 2

        best_target = None
        best_score = 0
        for placed_ref in placements:
            for (a, b), score in connectivity.items():
                partner = b if a == ref else (a if b == ref else None)
                if partner == placed_ref and score > best_score:
                    best_score = score
                    best_target = placed_ref

        if best_target:
            tx, ty = placements[best_target]
        else:
            tx, ty = board_cx, board_cy

        placed = False
        for radius in [r * 2.0 for r in range(1, 40)]:
            if placed:
                break
            for angle_deg in range(0, 360, 30):
                angle = math.radians(angle_deg)
                px = tx + radius * math.cos(angle)
                py = ty + radius * math.sin(angle)
                px, py = clamp_to_board(px, py, info["width"], info["height"])
                box = (px - hw, py - hh, px + hw, py + hh)
                if (box[0] < outline["x_min_mm"] + 0.1 or
                    box[2] > outline["x_max_mm"] - 0.1 or
                    box[1] < outline["y_min_mm"] + 0.1 or
                    box[3] > outline["y_max_mm"] - 0.1):
                    continue
                if not box_collides(box, placed_boxes, spacing):
                    placements[ref] = (round(px, 2), round(py, 2))
                    placed_boxes.append(box)
                    placed = True
                    break
        if not placed:
            for gx in range(int(outline["x_min_mm"] + hw + 1), int(outline["x_max_mm"] - hw), 2):
                if placed:
                    break
                for gy in range(int(outline["y_min_mm"] + hh + 1), int(outline["y_max_mm"] - hh), 2):
                    box = (gx - hw, gy - hh, gx + hw, gy + hh)
                    if not box_collides(box, placed_boxes, spacing):
                        placements[ref] = (round(float(gx), 2), round(float(gy), 2))
                        placed_boxes.append(box)
                        placed = True
                        break

# Apply moves
moved = []
for ref, (px, py) in placements.items():
    fp = board.FindFootprintByReference(ref)
    if fp:
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(px), pcbnew.FromMM(py)))
        moved.append({{"ref": ref, "x_mm": px, "y_mm": py}})

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "components_moved": len(moved),
    "hub_component": sorted_refs[0] if sorted_refs else None,
    "moved": moved[:10],  # truncate for readability
}}))
"""
    return run_pcbnew_script(script, timeout=30.0)


def _step_autoroute(pcb_path: str, passes: int = 1) -> Dict[str, Any]:
    """Step 6: Autoroute the PCB using FreeRouter (includes pre-flight check)."""
    from kicad_mcp.tools.pcb_autoroute import (
        _find_freerouter_jar,
        _find_java,
        _run_full_autoroute,
        _run_auto_fix_placement,
        _run_pre_route_check,
    )

    jar_path = _find_freerouter_jar()
    if not jar_path:
        return {"error": "FreeRouter JAR not found"}

    java_path = _find_java()
    if not java_path:
        return {"error": "Java not found"}

    # Pre-flight check (same logic as autoroute_pcb tool)
    preflight = _run_pre_route_check(pcb_path)
    preflight_info = {}

    if preflight.get("status") == "ok" and not preflight.get("route_ready", True):
        overlaps = preflight.get("courtyard_overlaps", 0)
        if overlaps > 0:
            logger.info("Pipeline pre-route: %d overlap(s), auto-fixing", overlaps)
            _run_auto_fix_placement(pcb_path)
            preflight_info["auto_fix_applied"] = True

    result = _run_full_autoroute(
        pcb_path=pcb_path,
        jar_path=jar_path,
        java_path=java_path,
        passes=passes,
        remove_zones=True,
    )

    if preflight_info:
        result["preflight"] = preflight_info

    return result


def _step_add_zones_and_fill(
    pcb_path: str,
    ground_net: str = "GND",
) -> Dict[str, Any]:
    """Step 7: Add GND copper zones on F.Cu and B.Cu, then fill all zones."""
    script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

net = board.FindNet({ground_net!r})
if net is None or net.GetNetCode() == 0:
    # GND net might not exist — skip zones, not an error
    print(json.dumps({{"status": "ok", "message": "Ground net '{ground_net}' not found, skipping zones", "zones_added": 0}}))
    raise SystemExit(0)

# Get board outline for zone boundary
bb = board.GetBoardEdgesBoundingBox()
if bb.GetWidth() == 0 or bb.GetHeight() == 0:
    print(json.dumps({{"status": "ok", "message": "No board outline, skipping zones", "zones_added": 0}}))
    raise SystemExit(0)

x0 = round(pcbnew.ToMM(bb.GetX()), 2)
y0 = round(pcbnew.ToMM(bb.GetY()), 2)
x1 = round(pcbnew.ToMM(bb.GetRight()), 2)
y1 = round(pcbnew.ToMM(bb.GetBottom()), 2)
corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

zones_added = 0
for layer in [pcbnew.F_Cu, pcbnew.B_Cu]:
    zone = pcbnew.ZONE(board)
    zone.SetNet(net)
    zone.SetLayer(layer)
    zone.SetLocalClearance(pcbnew.FromMM(0.3))
    zone.SetMinThickness(pcbnew.FromMM(0.2))
    zone.SetPadConnection(pcbnew.ZONE_CONNECTION_THERMAL)

    outline = zone.Outline()
    outline.NewOutline()
    for cx, cy in corners:
        outline.Append(pcbnew.FromMM(cx), pcbnew.FromMM(cy))

    board.Add(zone)
    zones_added += 1

# Fill all zones
filler = pcbnew.ZONE_FILLER(board)
zone_list = pcbnew.ZONES()
for z in board.Zones():
    zone_list.append(z)
filler.Fill(zone_list)

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "zones_added": zones_added,
    "ground_net": {ground_net!r},
    "layers": ["F.Cu", "B.Cu"],
}}))
"""
    return run_pcbnew_script(script, timeout=60.0)


def _step_export_gerbers(pcb_path: str) -> Dict[str, Any]:
    """Step 8 (optional): Export Gerber + drill files and create ZIP."""
    try:
        kicad_cli = get_kicad_cli_path(required=True)
    except Exception as e:
        return {"error": str(e)}

    pcb_dir = os.path.dirname(os.path.abspath(pcb_path))
    output_dir = os.path.join(pcb_dir, "gerbers")
    os.makedirs(output_dir, exist_ok=True)

    errors = []

    # Gerber files
    try:
        subprocess.run(
            [kicad_cli, "pcb", "export", "gerbers",
             "--output", output_dir + "/", pcb_path],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.CalledProcessError as e:
        errors.append(f"Gerber export failed: {e.stderr or e.stdout}")
    except subprocess.TimeoutExpired:
        errors.append("Gerber export timed out")

    # Drill files
    try:
        subprocess.run(
            [kicad_cli, "pcb", "export", "drill",
             "--output", output_dir + "/",
             "--format", "excellon", "--excellon-units", "mm",
             pcb_path],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except subprocess.CalledProcessError as e:
        errors.append(f"Drill export failed: {e.stderr or e.stdout}")
    except subprocess.TimeoutExpired:
        errors.append("Drill export timed out")

    if errors:
        return {"error": "; ".join(errors)}

    gerber_files = sorted(glob.glob(os.path.join(output_dir, "*.gbr")))
    drill_files = sorted(
        glob.glob(os.path.join(output_dir, "*.drl"))
        + glob.glob(os.path.join(output_dir, "*.xln"))
    )

    if not gerber_files and not drill_files:
        return {"error": "No output files generated"}

    # ZIP
    pcb_name = os.path.splitext(os.path.basename(pcb_path))[0]
    zip_path = os.path.join(pcb_dir, f"{pcb_name}-gerbers.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in gerber_files + drill_files:
            zf.write(f, os.path.basename(f))

    return {
        "status": "ok",
        "output_dir": output_dir,
        "total_files": len(gerber_files) + len(drill_files),
        "zip_path": zip_path,
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def register_pipeline_tools(mcp: FastMCP) -> None:
    """Register PCB pipeline tools."""

    @mcp.tool()
    def build_pcb_from_schematic(
        project_path: str,
        board_width_mm: float = 0,
        board_height_mm: float = 0,
        ground_net: str = "GND",
        autoroute_passes: int = 1,
        export_gerbers: bool = False,
    ) -> Dict[str, Any]:
        """Build a complete routed PCB from a KiCad schematic in one step.

        Runs the full pipeline: extract netlist → create PCB → place
        footprints → assign nets → optimize placement → autoroute →
        add ground planes → fill zones → (optionally) export Gerbers.

        Requires a KiCad project with a schematic (.kicad_sch) that has
        footprints assigned to all components.  Creates the PCB file
        (.kicad_pcb) from scratch.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro).
            board_width_mm: Board width in mm. 0 = auto-estimate from
                component footprints. Use explicit dimensions for boards
                that must fit specific enclosures.
            board_height_mm: Board height in mm. 0 = auto-estimate.
            ground_net: Net name for copper pour zones (default "GND").
            autoroute_passes: Number of FreeRouter passes (default 1).
                Higher = better routing but slower. FreeRouter is
                non-deterministic; best result is kept.
            export_gerbers: Generate Gerber/drill files and ZIP for
                fabrication upload (default False).
        """
        if not os.path.exists(project_path):
            return {"error": f"Project file not found: {project_path}"}

        project_dir = os.path.dirname(os.path.abspath(project_path))
        project_name = os.path.splitext(os.path.basename(project_path))[0]

        sch_path = os.path.join(project_dir, project_name + ".kicad_sch")
        pcb_path = os.path.join(project_dir, project_name + ".kicad_pcb")

        if not os.path.exists(sch_path):
            return {"error": f"Schematic not found: {sch_path}"}

        pipeline_result: Dict[str, Any] = {"steps": {}}
        t0 = time.time()

        def _record(step_name: str, result: Dict[str, Any]) -> bool:
            """Record step result. Returns False if step had an error."""
            pipeline_result["steps"][step_name] = result
            return "error" not in result

        # Step 1: Extract netlist
        step = _step_extract_netlist(sch_path)
        if not _record("extract_netlist", step):
            return pipeline_result

        components = step["components"]
        nets = step["nets"]
        pipeline_result["component_count"] = step["component_count"]
        pipeline_result["net_count"] = step["net_count"]

        if step["skipped_count"] > 0:
            pipeline_result["warnings"] = [
                f"{step['skipped_count']} component(s) skipped (no footprint): "
                f"{', '.join(step['components_without_footprint'][:10])}"
            ]

        if not components:
            pipeline_result["error"] = "No components with footprints found in schematic"
            return pipeline_result

        # Step 2: Create PCB + board outline
        step = _step_create_pcb_and_outline(
            pcb_path, board_width_mm, board_height_mm, components,
        )
        if not _record("create_pcb", step):
            return pipeline_result

        actual_width = step["width_mm"]
        actual_height = step["height_mm"]
        pipeline_result["board_width_mm"] = actual_width
        pipeline_result["board_height_mm"] = actual_height
        if step.get("auto_sized"):
            pipeline_result.setdefault("warnings", []).append(
                f"Board auto-sized to {actual_width}x{actual_height}mm"
            )

        # Step 3: Place footprints
        step = _step_place_footprints(pcb_path, components, actual_width, actual_height)
        if not _record("place_footprints", step):
            return pipeline_result

        pipeline_result["footprints_placed"] = step.get("placed_count", 0)

        # Step 4: Inject nets + assign pads
        step = _step_inject_nets_and_assign_pads(pcb_path, nets)
        if not _record("assign_nets", step):
            return pipeline_result

        pipeline_result["pads_assigned"] = step.get("pads_assigned", 0)

        # Step 5: Optimize placement
        step = _step_optimize_placement(pcb_path)
        _record("optimize_placement", step)
        # Non-fatal — continue even if optimization fails

        # Step 6: Autoroute
        step = _step_autoroute(pcb_path, passes=autoroute_passes)
        if not _record("autoroute", step):
            return pipeline_result

        pipeline_result["tracks"] = step.get("tracks_after", 0)
        pipeline_result["vias"] = step.get("vias_after", 0)
        pipeline_result["incomplete_nets"] = step.get("best_incomplete", 0)

        # Step 7: Copper zones + fill
        step = _step_add_zones_and_fill(pcb_path, ground_net)
        _record("zones", step)
        # Non-fatal

        # Step 8: Export gerbers (optional)
        if export_gerbers:
            step = _step_export_gerbers(pcb_path)
            _record("export_gerbers", step)
            if step.get("zip_path"):
                pipeline_result["gerber_zip"] = step["zip_path"]

        pipeline_result["status"] = "ok"
        pipeline_result["pcb_path"] = pcb_path
        pipeline_result["elapsed_seconds"] = round(time.time() - t0, 1)

        return pipeline_result
