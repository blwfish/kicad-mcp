"""PCB pipeline tool: build a routed PCB from a schematic in one step."""
# TODO: Migrate !r script interpolation to JSON params (see pcb_board.py for pattern)

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

from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER, LIB_SEARCH_HELPER
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

{LIB_SEARCH_HELPER}

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


def _step_load_footprints(
    pcb_path: str,
    components: Dict[str, Dict],
) -> Dict[str, Any]:
    """Step 3: Load all footprints onto the board at stacked positions.

    This is a simple loading step — footprints are placed at temporary
    positions so step 4 can find them by reference for net assignment.
    Step 5 (smart_placement) handles final positioning.
    """
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
import pcbnew, json, os

board = pcbnew.LoadBoard({pcb_path!r})
placements = {placements_repr}

{LIB_SEARCH_HELPER}

placed = []
errors = []

# Stack all footprints at (5, 5) — step 5 will move them
for p in placements:
    lib_path = find_lib(p["library"])
    if not lib_path:
        errors.append(f"Library '{{p['library']}}' not found for {{p['ref']}}")
        continue

    fp = pcbnew.FootprintLoad(lib_path, p["footprint_name"])
    if fp is None:
        errors.append(f"Footprint '{{p['footprint_name']}}' not found for {{p['ref']}}")
        continue

    fp.SetReference(p["ref"])
    fp.SetValue(p["value"])
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(5)))
    board.Add(fp)
    placed.append(p["ref"])

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "placed_count": len(placed),
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


def _step_smart_placement(
    pcb_path: str,
    nets: Dict[str, List],
    spacing_mm: float = 1.0,
) -> Dict[str, Any]:
    """Step 5: Smart tiered placement based on connectivity and component type.

    Classifies components into 4 tiers and places them in priority order:
    - Tier 1: Components with keepout zones (ESP32, etc.) → board edges
    - Tier 2: Connectors (J*, SW*, H*) → board edges near partners
    - Tier 3: ICs and large active components → near connected partners
    - Tier 4: Small passives → fill gaps near connected ICs

    All placements are strictly boundary-checked (full bbox must fit inside
    the board outline with margin). Keepout zones from placed components are
    tracked and avoided by all subsequent placements.
    """
    # Build connectivity from nets dict (component-pair signal affinity)
    # nets: {"net_name": [{"component": "R1", "pin": "1"}, ...]}
    net_members: Dict[str, List[str]] = {}
    for net_name, pins in nets.items():
        members = list({p["component"] for p in pins})
        if len(members) > 1:
            net_members[net_name] = members

    nets_repr = repr(dict(net_members))

    script = f"""
import pcbnew, json, math
{KEEPOUT_HELPER}

board = pcbnew.LoadBoard({pcb_path!r})
spacing = {spacing_mm}
outline = get_board_outline(board)

if not outline:
    print(json.dumps({{"status": "ok", "message": "No board outline, skipping placement"}}))
    raise SystemExit(0)

POWER_PATS = ["GND", "VCC", "VDD", "3V3", "3.3V", "5V", "+5V", "+3", "+12", "VBUS"]
board_xmin = outline["x_min_mm"]
board_ymin = outline["y_min_mm"]
board_xmax = outline["x_max_mm"]
board_ymax = outline["y_max_mm"]
board_cx = (board_xmin + board_xmax) / 2
board_cy = (board_ymin + board_ymax) / 2
margin = max(0.5, spacing)

# --- Collect footprint info ---
# Track ASYMMETRIC extents from footprint origin (not symmetric half-sizes).
# Footprint origins are often at pin 1, not bbox center — e.g. a 13mm
# Phoenix connector with origin at pin 1 extends 3mm left and 10mm right.
fp_info = {{}}
for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_pos = fp.GetPosition()
    fp_x = pcbnew.ToMM(fp_pos.x)
    fp_y = pcbnew.ToMM(fp_pos.y)

    bbox = fp.GetBoundingBox(False, False)
    # Extent from origin in each direction (positive values)
    ext_left  = fp_x - pcbnew.ToMM(bbox.GetX())
    ext_right = pcbnew.ToMM(bbox.GetRight()) - fp_x
    ext_top   = fp_y - pcbnew.ToMM(bbox.GetY())
    ext_bot   = pcbnew.ToMM(bbox.GetBottom()) - fp_y

    # Courtyard bounds (preferred over body bbox)
    cy_found = False
    cy_xmin_abs = float("inf"); cy_ymin_abs = float("inf")
    cy_xmax_abs = float("-inf"); cy_ymax_abs = float("-inf")
    for item in fp.GraphicalItems():
        ln = board.GetLayerName(item.GetLayer())
        if "CrtYd" in ln:
            cy_found = True
            ib = item.GetBoundingBox()
            cy_xmin_abs = min(cy_xmin_abs, pcbnew.ToMM(ib.GetX()))
            cy_ymin_abs = min(cy_ymin_abs, pcbnew.ToMM(ib.GetY()))
            cy_xmax_abs = max(cy_xmax_abs, pcbnew.ToMM(ib.GetRight()))
            cy_ymax_abs = max(cy_ymax_abs, pcbnew.ToMM(ib.GetBottom()))
    if cy_found:
        ext_left  = fp_x - cy_xmin_abs
        ext_right = cy_xmax_abs - fp_x
        ext_top   = fp_y - cy_ymin_abs
        ext_bot   = cy_ymax_abs - fp_y

    # Keepout zones
    has_keepout = False
    keepout_side = None
    keepout_rel = None  # relative bbox (dx_min, dy_min, dx_max, dy_max)
    try:
        for zone in fp.Zones():
            if zone.GetIsRuleArea():
                has_keepout = True
                zbb = zone.GetBoundingBox()
                dx_min = pcbnew.ToMM(zbb.GetX()) - fp_x
                dy_min = pcbnew.ToMM(zbb.GetY()) - fp_y
                dx_max = pcbnew.ToMM(zbb.GetRight()) - fp_x
                dy_max = pcbnew.ToMM(zbb.GetBottom()) - fp_y
                keepout_rel = (dx_min, dy_min, dx_max, dy_max)
                extents_map = {{"left": abs(dx_min), "right": abs(dx_max),
                               "top": abs(dy_min), "bottom": abs(dy_max)}}
                keepout_side = max(extents_map, key=extents_map.get)
                # Merge keepout into envelope
                ext_left  = max(ext_left, -dx_min)
                ext_right = max(ext_right, dx_max)
                ext_top   = max(ext_top, -dy_min)
                ext_bot   = max(ext_bot, dy_max)
    except AttributeError:
        pass

    w = ext_left + ext_right
    h = ext_top + ext_bot
    fp_info[ref] = {{
        "ext_left": round(ext_left, 2), "ext_right": round(ext_right, 2),
        "ext_top": round(ext_top, 2), "ext_bot": round(ext_bot, 2),
        "width": round(w, 2), "height": round(h, 2),
        "has_keepout": has_keepout, "keepout_side": keepout_side,
        "keepout_rel": keepout_rel,
        "area": round(w * h, 2),
    }}

if not fp_info:
    print(json.dumps({{"status": "ok", "message": "No footprints to place"}}))
    raise SystemExit(0)

# --- Build connectivity from netlist ---
net_members = {nets_repr}
connectivity = {{}}
conn_score = {{ref: 0.0 for ref in fp_info}}

for net_name, members in net_members.items():
    is_power = any(p in net_name.upper() for p in POWER_PATS)
    weight = 0.1 if is_power else 1.0
    placed_members = [m for m in members if m in fp_info]
    for i in range(len(placed_members)):
        for j in range(i + 1, len(placed_members)):
            a, b = placed_members[i], placed_members[j]
            key = (min(a, b), max(a, b))
            connectivity[key] = connectivity.get(key, 0.0) + weight
            conn_score[a] = conn_score.get(a, 0.0) + weight
            conn_score[b] = conn_score.get(b, 0.0) + weight

# --- Classify into tiers ---
EDGE_PREFIXES = ("J", "SW", "H", "USB")
tier1, tier2, tier3, tier4 = [], [], [], []

for ref, info in fp_info.items():
    if info["has_keepout"]:
        tier1.append(ref)
    elif any(ref.startswith(p) for p in EDGE_PREFIXES):
        tier2.append(ref)
    elif (ref.startswith("U") or ref.startswith("Q")) and info["area"] > 50:
        tier3.append(ref)
    else:
        tier4.append(ref)

def sort_key(r):
    return (-conn_score.get(r, 0), -fp_info[r]["area"], r)

tier1.sort(key=sort_key)
tier2.sort(key=sort_key)
tier3.sort(key=sort_key)
tier4.sort(key=sort_key)

# --- Placement engine ---
placements = {{}}
placed_boxes = []
keepout_boxes = []  # absolute keepout zone bboxes

def get_extents(ref):
    info = fp_info[ref]
    return info["ext_left"], info["ext_right"], info["ext_top"], info["ext_bot"]

def fits_on_board(cx, cy, el, er, et, eb):
    return (cx - el >= board_xmin + margin and cx + er <= board_xmax - margin and
            cy - et >= board_ymin + margin and cy + eb <= board_ymax - margin)

def box_overlaps(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]

def collides(cx, cy, el, er, et, eb):
    box = (cx - el - spacing, cy - et - spacing, cx + er + spacing, cy + eb + spacing)
    for pb in placed_boxes:
        if box_overlaps(box, pb):
            return True
    return False

def hits_keepout(cx, cy, el, er, et, eb):
    box = (cx - el, cy - et, cx + er, cy + eb)
    for kz in keepout_boxes:
        if box_overlaps(box, kz):
            return True
    return False

def place_at(ref, cx, cy):
    info = fp_info[ref]
    el, er, et, eb = info["ext_left"], info["ext_right"], info["ext_top"], info["ext_bot"]
    placements[ref] = (round(cx, 2), round(cy, 2))
    placed_boxes.append((cx - el, cy - et, cx + er, cy + eb))
    # Track keepout zones in absolute coords
    if info["keepout_rel"]:
        dx0, dy0, dx1, dy1 = info["keepout_rel"]
        keepout_boxes.append((cx + dx0, cy + dy0, cx + dx1, cy + dy1))

def find_partner_pos(ref):
    best_pos = None
    best_score = 0
    for placed_ref, pos in placements.items():
        key = (min(ref, placed_ref), max(ref, placed_ref))
        score = connectivity.get(key, 0)
        if score > best_score:
            best_score = score
            best_pos = pos
    return best_pos if best_pos else (board_cx, board_cy)

def valid_pos(cx, cy, el, er, et, eb):
    return fits_on_board(cx, cy, el, er, et, eb) and not collides(cx, cy, el, er, et, eb) and not hits_keepout(cx, cy, el, er, et, eb)

def fallback_grid(el, er, et, eb, step=1.0):
    y = board_ymin + et + margin
    while y <= board_ymax - eb - margin:
        x = board_xmin + el + margin
        while x <= board_xmax - er - margin:
            if valid_pos(x, y, el, er, et, eb):
                return (x, y)
            x += step
        y += step
    return None

# --- Tier 1: Keepout components → edge placement ---
for ref in tier1:
    el, er, et, eb = get_extents(ref)
    ks = fp_info[ref]["keepout_side"] or "top"

    best = None
    best_dist = float("inf")
    target = find_partner_pos(ref)

    # Try preferred edge first, then others
    edges_order = [ks] + [e for e in ["top", "bottom", "left", "right"] if e != ks]
    for edge in edges_order:
        if edge in ("top", "bottom"):
            fixed_y = board_ymin + et + margin if edge == "top" else board_ymax - eb - margin
            x = board_xmin + el + margin
            while x <= board_xmax - er - margin:
                if valid_pos(x, fixed_y, el, er, et, eb):
                    d = math.hypot(x - target[0], fixed_y - target[1])
                    if d < best_dist:
                        best_dist = d
                        best = (x, fixed_y)
                x += 2.0
        else:
            fixed_x = board_xmin + el + margin if edge == "left" else board_xmax - er - margin
            y = board_ymin + et + margin
            while y <= board_ymax - eb - margin:
                if valid_pos(fixed_x, y, el, er, et, eb):
                    d = math.hypot(fixed_x - target[0], y - target[1])
                    if d < best_dist:
                        best_dist = d
                        best = (fixed_x, y)
                y += 2.0

    if best:
        place_at(ref, best[0], best[1])
    else:
        fb = fallback_grid(el, er, et, eb)
        if fb:
            place_at(ref, fb[0], fb[1])

# --- Tier 2: Connectors → edge placement near partners ---
for ref in tier2:
    el, er, et, eb = get_extents(ref)
    target = find_partner_pos(ref)

    best = None
    best_dist = float("inf")

    for edge in ["top", "bottom", "left", "right"]:
        if edge in ("top", "bottom"):
            fixed_y = board_ymin + et + margin if edge == "top" else board_ymax - eb - margin
            x = board_xmin + el + margin
            while x <= board_xmax - er - margin:
                if valid_pos(x, fixed_y, el, er, et, eb):
                    d = math.hypot(x - target[0], fixed_y - target[1])
                    if d < best_dist:
                        best_dist = d
                        best = (x, fixed_y)
                x += 2.0
        else:
            fixed_x = board_xmin + el + margin if edge == "left" else board_xmax - er - margin
            y = board_ymin + et + margin
            while y <= board_ymax - eb - margin:
                if valid_pos(fixed_x, y, el, er, et, eb):
                    d = math.hypot(fixed_x - target[0], y - target[1])
                    if d < best_dist:
                        best_dist = d
                        best = (fixed_x, y)
                y += 2.0

    if best:
        place_at(ref, best[0], best[1])
    else:
        # Connector couldn't fit on edge, try interior
        fb = fallback_grid(el, er, et, eb)
        if fb:
            place_at(ref, fb[0], fb[1])

# --- Tier 3: ICs → spiral from connected partner ---
for ref in tier3:
    el, er, et, eb = get_extents(ref)
    target = find_partner_pos(ref)

    step = max(2.0, min(el + er, et + eb) / 2)
    found = False
    for r_mult in range(1, 60):
        if found:
            break
        radius = r_mult * step
        n_angles = max(12, int(2 * math.pi * radius / step))
        for i in range(n_angles):
            angle = 2 * math.pi * i / n_angles
            cx = target[0] + radius * math.cos(angle)
            cy = target[1] + radius * math.sin(angle)
            if valid_pos(cx, cy, el, er, et, eb):
                place_at(ref, cx, cy)
                found = True
                break

    if not found:
        fb = fallback_grid(el, er, et, eb)
        if fb:
            place_at(ref, fb[0], fb[1])

# --- Tier 4: Passives → tight spiral from connected partner ---
for ref in tier4:
    el, er, et, eb = get_extents(ref)
    target = find_partner_pos(ref)

    found = False
    for r_mult in range(1, 80):
        if found:
            break
        radius = r_mult * 1.0  # 1mm steps for small passives
        n_angles = max(12, int(2 * math.pi * radius / 1.0))
        for i in range(n_angles):
            angle = 2 * math.pi * i / n_angles
            cx = target[0] + radius * math.cos(angle)
            cy = target[1] + radius * math.sin(angle)
            if valid_pos(cx, cy, el, er, et, eb):
                place_at(ref, cx, cy)
                found = True
                break

    if not found:
        fb = fallback_grid(el, er, et, eb, step=0.5)
        if fb:
            place_at(ref, fb[0], fb[1])

# --- Apply placements ---
moved = []
failed = []
all_refs = set(fp_info.keys())
placed_refs = set(placements.keys())

for ref, (px, py) in placements.items():
    fp = board.FindFootprintByReference(ref)
    if fp:
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(px), pcbnew.FromMM(py)))
        moved.append({{"ref": ref, "x_mm": px, "y_mm": py}})

for ref in all_refs - placed_refs:
    failed.append(ref)

board.Save({pcb_path!r})

# Hub = most-connected component overall
hub = max(conn_score, key=conn_score.get) if conn_score else None

print(json.dumps({{
    "status": "ok",
    "components_placed": len(moved),
    "hub_component": hub,
    "tiers": {{
        "keepout": tier1,
        "edge": tier2,
        "ic": tier3,
        "passive": tier4,
    }},
    "failed_placements": failed,
    "placements": moved[:10],
}}))
"""
    return run_pcbnew_script(script, timeout=60.0)


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

        Runs the full pipeline: extract netlist → create PCB → load
        footprints → assign nets → smart placement → autoroute →
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

        # Step 3: Load footprints onto board (temporary positions)
        step = _step_load_footprints(pcb_path, components)
        if not _record("load_footprints", step):
            return pipeline_result

        pipeline_result["footprints_placed"] = step.get("placed_count", 0)

        # Step 4: Inject nets + assign pads
        step = _step_inject_nets_and_assign_pads(pcb_path, nets)
        if not _record("assign_nets", step):
            return pipeline_result

        pipeline_result["pads_assigned"] = step.get("pads_assigned", 0)

        # Step 5: Smart placement (tiered, connectivity-aware)
        step = _step_smart_placement(pcb_path, nets)
        _record("smart_placement", step)
        # Non-fatal — continue even if placement is imperfect

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
