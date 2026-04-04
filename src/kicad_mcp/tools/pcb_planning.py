"""PCB planning tools: board size estimation and suggested component placement."""
# TODO: Migrate !r script interpolation to JSON params (see pcb_board.py for pattern)

import logging
import os
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER, LIB_SEARCH_HELPER

logger = logging.getLogger(__name__)


def register_pcb_planning_tools(mcp: FastMCP) -> None:
    """Register PCB planning tools."""

    _KEEPOUT_HELPER = KEEPOUT_HELPER

    @mcp.tool()
    def estimate_board_size(
        footprints: List[Dict[str, str]],
        padding_mm: float = 2.0,
        routing_factor: float = 2.5,
    ) -> Dict[str, Any]:
        """Estimate minimum board dimensions from a list of footprints.

        Loads each footprint from KiCad libraries to measure its bounding box,
        sums the total component area, applies a routing factor (components
        typically need 2-3x their area for routing), and suggests board
        dimensions. Use this BEFORE creating the PCB to choose the right
        board size and avoid resize iterations.

        Args:
            footprints: List of {"library": "...", "footprint_name": "..."} dicts.
                Example: [{"library": "Package_QFP", "footprint_name": "LQFP-48_7x7mm_P0.5mm"}]
            padding_mm: Edge clearance around board perimeter in mm (default 2.0).
            routing_factor: Multiplier for total component area to account for
                routing space (default 2.5). Use 2.0 for simple boards, 3.0 for
                dense designs with many nets.
        """
        if not footprints:
            return {"error": "No footprints provided"}

        # Build the list as a Python literal for the subprocess
        fp_list_repr = repr(footprints)

        script = f"""
import pcbnew, json, os, math

fp_specs = {fp_list_repr}
padding = {padding_mm}
routing_factor = {routing_factor}

{LIB_SEARCH_HELPER}

components = []
errors = []
total_area = 0.0
max_width = 0.0
max_height = 0.0

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

    # Get bounding box (body only, no text)
    bbox = fp.GetBoundingBox(False, False)
    w = round(pcbnew.ToMM(bbox.GetWidth()), 2)
    h = round(pcbnew.ToMM(bbox.GetHeight()), 2)
    area = round(w * h, 2)
    total_area += area
    max_width = max(max_width, w)
    max_height = max(max_height, h)

    # Check for embedded keepout zones (like ESP32 antenna)
    keepout_area = 0.0
    try:
        for zone in fp.Zones():
            if zone.GetIsRuleArea():
                zbb = zone.GetBoundingBox()
                kw = pcbnew.ToMM(zbb.GetWidth())
                kh = pcbnew.ToMM(zbb.GetHeight())
                keepout_area += kw * kh
    except AttributeError:
        pass

    components.append({{
        "library": lib_name,
        "footprint": fp_name,
        "width_mm": w,
        "height_mm": h,
        "area_mm2": area,
        "keepout_area_mm2": round(keepout_area, 2),
    }})

if not components:
    print(json.dumps({{"error": "No valid footprints found", "details": errors}}))
    raise SystemExit(0)

# Calculate suggested board size
# Total area needed = component area * routing factor + keepout areas
total_keepout = sum(c["keepout_area_mm2"] for c in components)
needed_area = total_area * routing_factor + total_keepout

# Ensure board is at least as wide/tall as largest component + padding
min_dim = max(max_width, max_height) + padding * 2

# Suggest three aspect ratios: square, 4:3, 3:2
suggestions = []
for label, ratio in [("square", 1.0), ("4:3", 4/3), ("3:2", 3/2)]:
    # w * h = needed_area, w = h * ratio
    h = math.sqrt(needed_area / ratio)
    w = h * ratio
    # Apply minimum dimension constraint and padding
    w = max(w, min_dim) + padding * 2
    h = max(h, min_dim) + padding * 2
    # Round up to nearest mm
    w = math.ceil(w)
    h = math.ceil(h)
    suggestions.append({{
        "label": label,
        "width_mm": w,
        "height_mm": h,
        "area_mm2": w * h,
    }})

# Sort components by area (largest first) for reference
components.sort(key=lambda c: c["area_mm2"], reverse=True)

print(json.dumps({{
    "status": "ok",
    "component_count": len(components),
    "components": components,
    "total_component_area_mm2": round(total_area, 1),
    "total_keepout_area_mm2": round(total_keepout, 1),
    "routing_factor": routing_factor,
    "estimated_area_needed_mm2": round(needed_area, 1),
    "largest_component": {{
        "footprint": components[0]["footprint"],
        "width_mm": components[0]["width_mm"],
        "height_mm": components[0]["height_mm"],
    }},
    "suggested_sizes": suggestions,
    "errors": errors,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def suggest_placement(
        pcb_path: str,
        spacing_mm: float = 1.0,
    ) -> Dict[str, Any]:
        """Suggest component positions based on net connectivity.

        Analyzes the netlist connections between footprints already placed on
        the PCB and suggests optimized positions that minimize total trace
        length. The most-connected component (usually the MCU) is placed
        centrally, and other components are arranged around it based on
        connectivity strength.

        Does NOT modify the PCB. Returns suggested positions that can be
        applied with move_footprint.

        Respects board outline and keepout zones. Components with embedded
        keepout zones (like ESP32 antenna keepouts) are placed to keep
        their keepout areas within the board boundary.

        Args:
            pcb_path: Path to the .kicad_pcb file with footprints placed
                and nets assigned (run update_pcb_from_schematic first).
            spacing_mm: Minimum gap between component courtyards in mm (default 1.0).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json, math
{_KEEPOUT_HELPER}

board = pcbnew.LoadBoard({pcb_path!r})
spacing = {spacing_mm}
outline = get_board_outline(board)

if not outline:
    print(json.dumps({{"error": "No board outline found. Add a board outline first."}}))
    raise SystemExit(0)

board_w = outline["width_mm"]
board_h = outline["height_mm"]
board_cx = outline["x_min_mm"] + board_w / 2
board_cy = outline["y_min_mm"] + board_h / 2

# --- Collect footprint info ---
fp_info = {{}}
for fp in board.GetFootprints():
    ref = fp.GetReference()
    bbox = fp.GetBoundingBox(False, False)
    w = pcbnew.ToMM(bbox.GetWidth())
    h = pcbnew.ToMM(bbox.GetHeight())

    # Try courtyard for tighter bounds
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

    # Check for embedded keepout zones (e.g., ESP32 antenna)
    has_keepout = False
    keepout_side = None  # which side the keepout extends to
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
                # Determine which edge the keepout extends furthest toward
                extents = {{"left": abs(zx), "right": abs(zr), "top": abs(zy), "bottom": abs(zb)}}
                keepout_side = max(extents, key=extents.get)
    except AttributeError:
        pass

    # Collect pad nets
    pad_nets = set()
    for pad in fp.Pads():
        net = pad.GetNetname()
        if net and not net.startswith("unconnected-"):
            pad_nets.add(net)

    fp_info[ref] = {{
        "width": round(w, 2),
        "height": round(h, 2),
        "area": round(w * h, 2),
        "nets": pad_nets,
        "net_count": len(pad_nets),
        "has_keepout": has_keepout,
        "keepout_side": keepout_side,
        "value": fp.GetValue(),
    }}

if not fp_info:
    print(json.dumps({{"error": "No footprints found on board"}}))
    raise SystemExit(0)

# --- Build connectivity graph ---
# For each pair of components, count shared nets (higher = more connected)
connectivity = {{}}
refs = list(fp_info.keys())
for i in range(len(refs)):
    for j in range(i + 1, len(refs)):
        a, b = refs[i], refs[j]
        shared = fp_info[a]["nets"] & fp_info[b]["nets"]
        # Filter out power nets (GND, VCC, +3V3 etc) - they don't constrain placement
        signal_shared = {{n for n in shared if not any(
            p in n.upper() for p in ["GND", "VCC", "VDD", "3V3", "3.3V", "5V", "+5", "+3"])}}
        if signal_shared:
            key = (a, b)
            connectivity[key] = len(signal_shared)

# --- Rank components by connectivity ---
# Total signal connections per component
conn_score = {{ref: 0 for ref in refs}}
for (a, b), count in connectivity.items():
    conn_score[a] += count
    conn_score[b] += count

# Sort: most connected first (MCU will be first)
sorted_refs = sorted(refs, key=lambda r: (conn_score[r], fp_info[r]["area"]), reverse=True)

# --- Place components ---
placements = {{}}
placed_boxes = []  # list of (x_min, y_min, x_max, y_max) for collision detection

def box_collides(bx, placed, gap):
    for pb in placed:
        if (bx[0] - gap < pb[2] and bx[2] + gap > pb[0] and
            bx[1] - gap < pb[3] and bx[3] + gap > pb[1]):
            return True
    return False

def clamp_to_board(x, y, w, h, info):
    \"\"\"Clamp position to keep component within board outline.\"\"\"
    half_w, half_h = w / 2, h / 2
    x = max(outline["x_min_mm"] + half_w + spacing, min(x, outline["x_max_mm"] - half_w - spacing))
    y = max(outline["y_min_mm"] + half_h + spacing, min(y, outline["y_max_mm"] - half_h - spacing))
    return x, y

# Place the most-connected component (MCU) in center
if sorted_refs:
    hub = sorted_refs[0]
    info = fp_info[hub]
    cx, cy = board_cx, board_cy

    # If it has an antenna keepout, shift away from center toward a board edge
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

    cx, cy = clamp_to_board(cx, cy, info["width"], info["height"], info)
    hw, hh = info["width"] / 2, info["height"] / 2
    placements[hub] = {{"x_mm": round(cx, 2), "y_mm": round(cy, 2), "reason": "hub (most connected)"}}
    placed_boxes.append((cx - hw, cy - hh, cx + hw, cy + hh))

    # Place remaining components around the hub
    for ref in sorted_refs[1:]:
        info = fp_info[ref]
        hw, hh = info["width"] / 2, info["height"] / 2

        # Find the component(s) this one is most connected to
        best_target = None
        best_score = 0
        for placed_ref in placements:
            for (a, b), score in connectivity.items():
                partner = b if a == ref else (a if b == ref else None)
                if partner == placed_ref and score > best_score:
                    best_score = score
                    best_target = placed_ref

        # Target position: near the most-connected placed component, or center
        if best_target:
            tx = placements[best_target]["x_mm"]
            ty = placements[best_target]["y_mm"]
            reason = f"near {{best_target}} ({{best_score}} shared signal nets)"
        else:
            tx, ty = board_cx, board_cy
            reason = "no signal connections, placed in available space"

        # Try positions in a spiral pattern around the target
        placed = False
        for radius in [r * 2.0 for r in range(1, 40)]:
            if placed:
                break
            # Try 12 angles at each radius (every 30 degrees)
            for angle_deg in range(0, 360, 30):
                angle = math.radians(angle_deg)
                px = tx + radius * math.cos(angle)
                py = ty + radius * math.sin(angle)

                px, py = clamp_to_board(px, py, info["width"], info["height"], info)
                box = (px - hw, py - hh, px + hw, py + hh)

                # Check within board
                if (box[0] < outline["x_min_mm"] + 0.1 or
                    box[2] > outline["x_max_mm"] - 0.1 or
                    box[1] < outline["y_min_mm"] + 0.1 or
                    box[3] > outline["y_max_mm"] - 0.1):
                    continue

                if not box_collides(box, placed_boxes, spacing):
                    placements[ref] = {{"x_mm": round(px, 2), "y_mm": round(py, 2), "reason": reason}}
                    placed_boxes.append(box)
                    placed = True
                    break

        if not placed:
            # Fallback: just find any open space
            for gx in range(int(outline["x_min_mm"] + hw + 1), int(outline["x_max_mm"] - hw), 2):
                if placed:
                    break
                for gy in range(int(outline["y_min_mm"] + hh + 1), int(outline["y_max_mm"] - hh), 2):
                    box = (gx - hw, gy - hh, gx + hw, gy + hh)
                    if not box_collides(box, placed_boxes, spacing):
                        placements[ref] = {{
                            "x_mm": round(float(gx), 2),
                            "y_mm": round(float(gy), 2),
                            "reason": "fallback grid placement",
                        }}
                        placed_boxes.append(box)
                        placed = True
                        break
            if not placed:
                placements[ref] = {{
                    "x_mm": round(board_cx, 2),
                    "y_mm": round(board_cy, 2),
                    "reason": "WARNING: could not find non-overlapping position",
                }}

# --- Build output ---
placement_list = []
for ref in sorted_refs:
    p = placements.get(ref, {{}})
    info = fp_info[ref]
    placement_list.append({{
        "reference": ref,
        "value": info["value"],
        "x_mm": p.get("x_mm", board_cx),
        "y_mm": p.get("y_mm", board_cy),
        "width_mm": info["width"],
        "height_mm": info["height"],
        "signal_connections": conn_score[ref],
        "reason": p.get("reason", ""),
    }})

# Connectivity summary
conn_summary = []
for (a, b), count in sorted(connectivity.items(), key=lambda x: -x[1]):
    conn_summary.append(f"{{a}} <-> {{b}}: {{count}} signal nets")

print(json.dumps({{
    "status": "ok",
    "board_outline": outline,
    "component_count": len(placement_list),
    "placements": placement_list,
    "connectivity": conn_summary[:20],
    "hub_component": sorted_refs[0] if sorted_refs else None,
    "note": "These are SUGGESTIONS. Apply with move_footprint, then run audit_all to verify.",
}}))
"""
        return run_pcbnew_script(script)
