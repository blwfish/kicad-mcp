"""PCB keepout validation tools: zones, constraints, placement validation, audit."""

import logging
import os
from typing import Any, Dict

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER

logger = logging.getLogger(__name__)


def register_pcb_keepout_tools(mcp: FastMCP) -> None:
    """Register PCB keepout validation tools."""

    # Keepout helper code is a string constant that gets embedded
    # in each pcbnew subprocess script.
    _KEEPOUT_HELPER = KEEPOUT_HELPER

    @mcp.tool()
    def get_keepout_zones(pcb_path: str) -> Dict[str, Any]:
        """List all keepout/rule areas on the PCB with their boundaries and constraints.

        Returns keepout zones from both board-level and footprint-embedded sources.
        Useful for understanding placement constraints before placing components.

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})
keepouts = extract_keepouts(board)
print(json.dumps({{"status": "ok", "keepout_count": len(keepouts), "keepouts": keepouts}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def get_board_constraints(pcb_path: str) -> Dict[str, Any]:
        """Get a complete summary of board outline, keepout zones, design rules, and placement area.

        Returns all information needed to make informed placement decisions:
        board dimensions, keepout zone locations and restrictions, design rules,
        and the effective available area for component placement.

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})
keepouts = extract_keepouts(board)
outline = get_board_outline(board)

ds = board.GetDesignSettings()
design_rules = {{
    "min_track_width_mm": round(pcbnew.ToMM(ds.m_TrackMinWidth), 3),
    "min_clearance_mm": round(pcbnew.ToMM(ds.m_MinClearance), 3),
    "min_via_diameter_mm": round(pcbnew.ToMM(ds.m_ViasMinSize), 3),
}}

board_area = 0
if outline:
    board_area = round(outline["width_mm"] * outline["height_mm"], 1)
    outline["area_mm2"] = board_area

total_keepout_area = 0
for kz in keepouts:
    bb = kz["bounding_box"]
    kz_area = round((bb["x_max_mm"] - bb["x_min_mm"]) * (bb["y_max_mm"] - bb["y_min_mm"]), 1)
    kz["area_mm2"] = kz_area
    if board_area > 0:
        kz["board_coverage_pct"] = round(100 * kz_area / board_area, 1)
    total_keepout_area += kz_area

result = {{
    "status": "ok",
    "board_outline": outline,
    "keepout_zones": keepouts,
    "design_rules": design_rules,
    "existing_footprints_count": len(list(board.GetFootprints())),
    "total_keepout_area_mm2": round(total_keepout_area, 1),
}}
if board_area > 0:
    result["effective_placement_area_mm2"] = round(board_area - total_keepout_area, 1)
print(json.dumps(result))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def validate_placement(
        pcb_path: str,
        library: str,
        footprint_name: str,
        x_mm: float,
        y_mm: float,
        rotation_deg: float = 0.0,
    ) -> Dict[str, Any]:
        """Check if placing a footprint at the given position would violate keepout zones or board boundaries.

        Loads the footprint from the library to get its bounding box, then checks
        for overlap with all keepout zones and whether it fits within the board outline.
        Does NOT modify the PCB file.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            library: Footprint library name (e.g., "Resistor_SMD").
            footprint_name: Footprint name within the library.
            x_mm: Proposed X position in millimeters.
            y_mm: Proposed Y position in millimeters.
            rotation_deg: Proposed rotation in degrees (default 0).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json, os
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})

lib_search_paths = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    os.path.expanduser("~/Documents/KiCad/footprints"),
    "/usr/share/kicad/footprints",
]
lib_path = None
for sp in lib_search_paths:
    candidate = os.path.join(sp, {library!r} + ".pretty")
    if os.path.isdir(candidate):
        lib_path = candidate
        break
if not lib_path:
    print(json.dumps({{"error": "Library '{library}' not found"}}))
    raise SystemExit(0)

fp = pcbnew.FootprintLoad(lib_path, {footprint_name!r})
if fp is None:
    print(json.dumps({{"error": "Footprint '{footprint_name}' not found in '{library}'"}}))
    raise SystemExit(0)

fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))
if {rotation_deg} != 0:
    fp.SetOrientationDegrees({rotation_deg})

fp_bbox = fp.GetBoundingBox(False, False)  # exclude text for accurate body bbox
fp_rect = {{
    "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
    "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
    "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
    "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
}}

keepouts = extract_keepouts(board)
outline = get_board_outline(board)
violations = []
warnings = []

for kz in keepouts:
    kz_bb = kz["bounding_box"]
    if not rects_overlap(fp_rect, kz_bb):
        continue
    area = overlap_area(fp_rect, kz_bb)
    c = kz["constraints"]
    if c["no_footprints"]:
        violations.append({{
            "type": "keepout_overlap",
            "keepout_source": kz["source"],
            "keepout_ref": kz["source_ref"],
            "overlap_mm2": area,
            "blocked": [k.replace("no_", "") for k, v in c.items() if v],
            "message": "Footprint overlaps keepout zone"
                       + (f" from {{kz['source_ref']}}" if kz["source_ref"] else ""),
        }})
    else:
        blocked = [k.replace("no_", "") for k, v in c.items() if v]
        if blocked:
            warnings.append({{
                "type": "routing_keepout_overlap",
                "keepout_source": kz["source"],
                "keepout_ref": kz["source_ref"],
                "overlap_mm2": area,
                "blocked": blocked,
                "message": f"Footprint overlaps zone that blocks {{', '.join(blocked)}} (routing may be difficult)",
            }})

if outline and not rect_inside(fp_rect, outline):
    overhang = {{}}
    if fp_rect["x_min_mm"] < outline["x_min_mm"]:
        overhang["left_mm"] = round(outline["x_min_mm"] - fp_rect["x_min_mm"], 3)
    if fp_rect["x_max_mm"] > outline["x_max_mm"]:
        overhang["right_mm"] = round(fp_rect["x_max_mm"] - outline["x_max_mm"], 3)
    if fp_rect["y_min_mm"] < outline["y_min_mm"]:
        overhang["top_mm"] = round(outline["y_min_mm"] - fp_rect["y_min_mm"], 3)
    if fp_rect["y_max_mm"] > outline["y_max_mm"]:
        overhang["bottom_mm"] = round(fp_rect["y_max_mm"] - outline["y_max_mm"], 3)
    violations.append({{
        "type": "outside_board",
        "overhang": overhang,
        "message": "Footprint extends beyond board outline",
    }})

print(json.dumps({{
    "status": "ok",
    "valid": len(violations) == 0,
    "violations": violations,
    "warnings": warnings,
    "footprint_bbox_mm": fp_rect,
    "board_outline_mm": outline,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def audit_footprint_overlaps(
        pcb_path: str,
        min_clearance_mm: float = 0.0,
        use_courtyard: bool = True,
    ) -> Dict[str, Any]:
        """Audit all footprint pairs for physical overlap or insufficient clearance.

        Checks every pair of placed footprints for bounding-box overlap.
        Unlike audit_pcb_placement (which checks keepout zones and board edges),
        this detects when two footprints physically collide with each other.
        Does NOT modify the PCB file.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            min_clearance_mm: Minimum required clearance between footprints in mm (default 0).
                When > 0, footprints closer than this distance are flagged even if not overlapping.
            use_courtyard: Use courtyard or pad bounds instead of full body bbox (default True).
                Reduces false positives for large modules like ESP32 where the body bbox
                is much larger than the actual copper area.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})
min_clearance = {min_clearance_mm}
use_courtyard = {use_courtyard}

def get_courtyard_bbox(fp):
    \"\"\"Get courtyard bounding box, falling back to pad bbox, then body bbox.\"\"\"
    # Try courtyard layer first
    x_min = float("inf")
    y_min = float("inf")
    x_max = float("-inf")
    y_max = float("-inf")
    found = False
    for item in fp.GraphicalItems():
        layer_name = board.GetLayerName(item.GetLayer())
        if "CrtYd" in layer_name:
            found = True
            bbox = item.GetBoundingBox()
            x_min = min(x_min, pcbnew.ToMM(bbox.GetX()))
            y_min = min(y_min, pcbnew.ToMM(bbox.GetY()))
            x_max = max(x_max, pcbnew.ToMM(bbox.GetRight()))
            y_max = max(y_max, pcbnew.ToMM(bbox.GetBottom()))
    if found:
        return {{"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                 "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}}, "courtyard"

    # Fall back to pad bounding box
    x_min = float("inf")
    y_min = float("inf")
    x_max = float("-inf")
    y_max = float("-inf")
    found = False
    for pad in fp.Pads():
        found = True
        pos = pad.GetPosition()
        size = pad.GetSize()
        x = pcbnew.ToMM(pos.x)
        y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x)
        h = pcbnew.ToMM(size.y)
        x_min = min(x_min, x - w / 2)
        y_min = min(y_min, y - h / 2)
        x_max = max(x_max, x + w / 2)
        y_max = max(y_max, y + h / 2)
    if found:
        return {{"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                 "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}}, "pads"

    # Last resort: body bbox
    return None, "none"

# Collect bounding boxes for all footprints
footprints = []
for fp in board.GetFootprints():
    pos = fp.GetPosition()
    fp_bbox = fp.GetBoundingBox(False, False)
    body_box = {{
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }}

    if use_courtyard:
        tight_box, source = get_courtyard_bbox(fp)
        check_box = tight_box if tight_box else body_box
        box_source = source if tight_box else "body"
    else:
        check_box = body_box
        box_source = "body"

    footprints.append({{
        "reference": fp.GetReference(),
        "value": fp.GetValue(),
        "footprint": fp.GetFPID().GetUniStringLibItemName(),
        "position_mm": [round(pcbnew.ToMM(pos.x), 3), round(pcbnew.ToMM(pos.y), 3)],
        "bbox": check_box,
        "bbox_source": box_source,
    }})

# Pairwise overlap check
overlaps = []
for i in range(len(footprints)):
    a = footprints[i]
    a_box = a["bbox"]
    # Expand bbox by min_clearance for proximity check
    a_expanded = {{
        "x_min_mm": a_box["x_min_mm"] - min_clearance,
        "y_min_mm": a_box["y_min_mm"] - min_clearance,
        "x_max_mm": a_box["x_max_mm"] + min_clearance,
        "y_max_mm": a_box["y_max_mm"] + min_clearance,
    }}
    for j in range(i + 1, len(footprints)):
        b = footprints[j]
        b_box = b["bbox"]

        # Check actual overlap (body collision)
        actual_overlap = rects_overlap(a_box, b_box)
        area = overlap_area(a_box, b_box) if actual_overlap else 0.0

        # Check clearance violation (expanded bbox)
        clearance_violation = min_clearance > 0 and rects_overlap(a_expanded, b_box)

        if actual_overlap or clearance_violation:
            # Compute gap (negative = overlap, positive = clearance)
            gap_x = max(a_box["x_min_mm"], b_box["x_min_mm"]) - min(a_box["x_max_mm"], b_box["x_max_mm"])
            gap_y = max(a_box["y_min_mm"], b_box["y_min_mm"]) - min(a_box["y_max_mm"], b_box["y_max_mm"])
            # Closest approach: positive = separation, negative = penetration
            gap_mm = max(gap_x, gap_y)

            entry = {{
                "ref_a": a["reference"],
                "ref_b": b["reference"],
                "value_a": a["value"],
                "value_b": b["value"],
                "overlap": actual_overlap,
                "overlap_mm2": area,
                "gap_mm": round(gap_mm, 3),
                "bbox_a": a_box,
                "bbox_b": b_box,
                "bbox_source_a": a["bbox_source"],
                "bbox_source_b": b["bbox_source"],
            }}
            if actual_overlap:
                entry["severity"] = "error"
                entry["message"] = f"{{a['reference']}} and {{b['reference']}} physically overlap by {{area}} mm2"
            else:
                entry["severity"] = "warning"
                entry["message"] = f"{{a['reference']}} and {{b['reference']}} are only {{round(gap_mm, 3)}} mm apart (min clearance: {{min_clearance}} mm)"
            overlaps.append(entry)

total = len(footprints)
pairs_checked = total * (total - 1) // 2
error_count = sum(1 for o in overlaps if o["severity"] == "error")
warning_count = sum(1 for o in overlaps if o["severity"] == "warning")

if overlaps:
    summary = f"{{len(overlaps)}} overlap(s) found among {{total}} footprints ({{error_count}} collisions, {{warning_count}} clearance warnings)"
else:
    summary = f"All {{total}} footprints are clear of each other"
    if min_clearance > 0:
        summary += f" (min clearance {{min_clearance}} mm)"

print(json.dumps({{
    "status": "ok",
    "total_footprints": total,
    "pairs_checked": pairs_checked,
    "overlap_count": len(overlaps),
    "error_count": error_count,
    "warning_count": warning_count,
    "overlaps": overlaps,
    "summary": summary,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def audit_pcb_placement(pcb_path: str) -> Dict[str, Any]:
        """Audit all footprint placements for keepout zone violations and board boundary issues.

        Checks every placed footprint against all keepout/rule areas and the board outline.
        Reports violations with overlap details. Does NOT modify the PCB file.

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})
keepouts = extract_keepouts(board)
outline = get_board_outline(board)

violations_list = []
clean_count = 0

for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_bbox = fp.GetBoundingBox(False, False)  # exclude text for accurate body bbox
    fp_rect = {{
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }}
    issues = []

    for kz in keepouts:
        if kz["source"] == "footprint" and kz["source_ref"] == ref:
            continue
        kz_bb = kz["bounding_box"]
        if not rects_overlap(fp_rect, kz_bb):
            continue
        area = overlap_area(fp_rect, kz_bb)
        c = kz["constraints"]
        blocked = [k.replace("no_", "") for k, v in c.items() if v]
        severity = "violation" if c["no_footprints"] else "warning"
        issues.append({{
            "type": "keepout_overlap",
            "severity": severity,
            "keepout_source": kz["source"],
            "keepout_ref": kz["source_ref"],
            "overlap_mm2": area,
            "blocked": blocked,
        }})

    if outline and not rect_inside(fp_rect, outline):
        overhang = {{}}
        if fp_rect["x_min_mm"] < outline["x_min_mm"]:
            overhang["left_mm"] = round(outline["x_min_mm"] - fp_rect["x_min_mm"], 3)
        if fp_rect["x_max_mm"] > outline["x_max_mm"]:
            overhang["right_mm"] = round(fp_rect["x_max_mm"] - outline["x_max_mm"], 3)
        if fp_rect["y_min_mm"] < outline["y_min_mm"]:
            overhang["top_mm"] = round(outline["y_min_mm"] - fp_rect["y_min_mm"], 3)
        if fp_rect["y_max_mm"] > outline["y_max_mm"]:
            overhang["bottom_mm"] = round(fp_rect["y_max_mm"] - outline["y_max_mm"], 3)
        issues.append({{
            "type": "outside_board",
            "severity": "violation",
            "overhang": overhang,
        }})

    if issues:
        pos = fp.GetPosition()
        violations_list.append({{
            "reference": ref,
            "value": fp.GetValue(),
            "footprint": fp.GetFPID().GetUniStringLibItemName(),
            "position_mm": [round(pcbnew.ToMM(pos.x), 3), round(pcbnew.ToMM(pos.y), 3)],
            "bbox_mm": fp_rect,
            "issues": issues,
        }})
    else:
        clean_count += 1

total = len(list(board.GetFootprints()))
vcount = len(violations_list)
summary = f"{{vcount}} of {{total}} footprints have placement issues" if vcount > 0 else f"All {{total}} footprints pass placement checks"

print(json.dumps({{
    "status": "ok",
    "total_footprints": total,
    "violations_count": vcount,
    "clean_count": clean_count,
    "violations": violations_list,
    "summary": summary,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def audit_all(
        pcb_path: str,
        min_clearance_mm: float = 0.0,
    ) -> Dict[str, Any]:
        """Run all placement audits in a single call: footprint overlaps, keepout violations, and silkscreen overlaps.

        Combines audit_footprint_overlaps, audit_pcb_placement, and
        check_silkscreen_overlaps into one operation to save tool calls.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            min_clearance_mm: Minimum required clearance between footprints in mm (default 0).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})
min_clearance = {min_clearance_mm}

# --- 1. Footprint overlap check (courtyard-based) ---
def get_courtyard_bbox(fp):
    x_min = float("inf"); y_min = float("inf")
    x_max = float("-inf"); y_max = float("-inf")
    found = False
    for item in fp.GraphicalItems():
        layer_name = board.GetLayerName(item.GetLayer())
        if "CrtYd" in layer_name:
            found = True
            bbox = item.GetBoundingBox()
            x_min = min(x_min, pcbnew.ToMM(bbox.GetX()))
            y_min = min(y_min, pcbnew.ToMM(bbox.GetY()))
            x_max = max(x_max, pcbnew.ToMM(bbox.GetRight()))
            y_max = max(y_max, pcbnew.ToMM(bbox.GetBottom()))
    if found:
        return {{"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                 "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}}
    x_min = float("inf"); y_min = float("inf")
    x_max = float("-inf"); y_max = float("-inf")
    found = False
    for pad in fp.Pads():
        found = True
        pos = pad.GetPosition(); size = pad.GetSize()
        x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
        x_min = min(x_min, x - w/2); y_min = min(y_min, y - h/2)
        x_max = max(x_max, x + w/2); y_max = max(y_max, y + h/2)
    if found:
        return {{"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                 "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}}
    return None

footprints = []
for fp in board.GetFootprints():
    tight_box = get_courtyard_bbox(fp)
    if not tight_box:
        fp_bbox = fp.GetBoundingBox(False, False)
        tight_box = {{
            "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
            "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
            "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
            "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
        }}
    footprints.append({{
        "reference": fp.GetReference(),
        "bbox": tight_box,
    }})

fp_overlaps = []
for i in range(len(footprints)):
    a = footprints[i]; a_box = a["bbox"]
    a_exp = {{
        "x_min_mm": a_box["x_min_mm"] - min_clearance,
        "y_min_mm": a_box["y_min_mm"] - min_clearance,
        "x_max_mm": a_box["x_max_mm"] + min_clearance,
        "y_max_mm": a_box["y_max_mm"] + min_clearance,
    }}
    for j in range(i + 1, len(footprints)):
        b = footprints[j]; b_box = b["bbox"]
        actual = rects_overlap(a_box, b_box)
        clearance_fail = min_clearance > 0 and rects_overlap(a_exp, b_box)
        if actual or clearance_fail:
            area = overlap_area(a_box, b_box) if actual else 0.0
            fp_overlaps.append({{
                "ref_a": a["reference"], "ref_b": b["reference"],
                "overlap": actual, "overlap_mm2": area,
            }})

# --- 2. Keepout / board boundary check ---
keepouts = extract_keepouts(board)
outline = get_board_outline(board)
keepout_violations = []

for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_bbox = fp.GetBoundingBox(False, False)
    fp_rect = {{
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }}
    for kz in keepouts:
        if kz["source"] == "footprint" and kz["source_ref"] == ref:
            continue
        kz_bb = kz["bounding_box"]
        if not rects_overlap(fp_rect, kz_bb):
            continue
        c = kz["constraints"]
        blocked = [k.replace("no_", "") for k, v in c.items() if v]
        if blocked:
            keepout_violations.append({{
                "reference": ref,
                "keepout_source": kz["source_ref"] or kz["source"],
                "blocked": blocked,
                "is_footprint_keepout": c["no_footprints"],
            }})
    if outline and not rect_inside(fp_rect, outline):
        keepout_violations.append({{
            "reference": ref,
            "keepout_source": "board_outline",
            "blocked": ["outside_board"],
            "is_footprint_keepout": True,
        }})

# --- 3. Silkscreen overlap check (pads + text-to-text) ---
silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]
silk_overlaps = []
silk_text_overlaps = []
silk_items = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for ft, fo in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not fo.IsVisible() or fo.GetLayer() not in silk_layer_ids:
            continue
        sb = fo.GetBoundingBox()
        silk_items.append({{
            "component": ref, "type": ft,
            "bbox": sb,
            "layer": fo.GetLayer(),
            "x_min": sb.GetX(), "y_min": sb.GetY(),
            "x_max": sb.GetRight(), "y_max": sb.GetBottom(),
        }})

# Also include standalone text as obstacles
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
        if vis:
            sb = drawing.GetBoundingBox()
            silk_items.append({{
                "component": None, "type": "standalone",
                "bbox": sb,
                "layer": drawing.GetLayer(),
                "x_min": sb.GetX(), "y_min": sb.GetY(),
                "x_max": sb.GetRight(), "y_max": sb.GetBottom(),
            }})

all_pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        pb = pad.GetBoundingBox()
        all_pads.append({{
            "reference": fp.GetReference(),
            "x_min": pb.GetX(), "y_min": pb.GetY(),
            "x_max": pb.GetRight(), "y_max": pb.GetBottom(),
        }})

def _aabb(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0

# Text over pads
for si in silk_items:
    for pad in all_pads:
        if si["component"] == pad["reference"]:
            continue
        if _aabb(si["x_min"], si["y_min"], si["x_max"], si["y_max"],
                 pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
            silk_overlaps.append({{
                "silk_component": si["component"],
                "silk_type": si["type"],
                "pad_component": pad["reference"],
            }})

# Text over text (different components, same layer)
for i in range(len(silk_items)):
    a = silk_items[i]
    for j in range(i + 1, len(silk_items)):
        b = silk_items[j]
        if a["component"] is not None and a["component"] == b["component"]:
            continue
        if a["layer"] != b["layer"]:
            continue
        if _aabb(a["x_min"], a["y_min"], a["x_max"], a["y_max"],
                 b["x_min"], b["y_min"], b["x_max"], b["y_max"]):
            silk_text_overlaps.append({{
                "text_a_component": a["component"], "text_a_type": a["type"],
                "text_b_component": b["component"], "text_b_type": b["type"],
            }})

# --- Summary ---
total_fp = len(footprints)
all_silk_issues = len(silk_overlaps) + len(silk_text_overlaps)
issues = len(fp_overlaps) + len(keepout_violations) + all_silk_issues
parts = []
if fp_overlaps:
    parts.append(f"{{len(fp_overlaps)}} footprint overlap(s)")
if keepout_violations:
    parts.append(f"{{len(keepout_violations)}} keepout/boundary issue(s)")
if silk_overlaps:
    parts.append(f"{{len(silk_overlaps)}} silkscreen-over-pad overlap(s)")
if silk_text_overlaps:
    parts.append(f"{{len(silk_text_overlaps)}} silkscreen text-to-text overlap(s)")
summary = ", ".join(parts) if parts else f"All {{total_fp}} footprints pass all checks"

print(json.dumps({{
    "status": "ok",
    "total_footprints": total_fp,
    "total_issues": issues,
    "footprint_overlaps": fp_overlaps,
    "keepout_violations": keepout_violations,
    "silkscreen_overlaps": silk_overlaps,
    "silkscreen_text_overlaps": silk_text_overlaps,
    "summary": summary,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def check_pad_clearances(
        pcb_path: str,
        min_clearance_mm: float = 0.0,
    ) -> Dict[str, Any]:
        """Check pad-to-pad clearances between all footprints on the PCB.

        Unlike audit_footprint_overlaps (which checks courtyard bounding boxes),
        this tool checks individual pad geometries.  Two courtyards can be 1mm
        apart while individual pads are only 0.05mm apart — this catches those
        cases.

        Run this BEFORE autorouting to catch placement issues that would
        otherwise only appear in DRC after a lengthy routing pass.

        When min_clearance_mm is 0, uses the board's design rule minimum
        clearance.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            min_clearance_mm: Minimum required clearance between pads of
                different footprints in mm.  0 = use board design rule minimum.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json, math

board = pcbnew.LoadBoard({pcb_path!r})
min_cl = {min_clearance_mm}

# Use board design rule if no explicit clearance given
if min_cl <= 0:
    ds = board.GetDesignSettings()
    min_cl = pcbnew.ToMM(ds.m_MinClearance)
    if min_cl <= 0:
        min_cl = 0.2  # fallback

# Collect all pads with their absolute position and size
all_pads = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for pad in fp.Pads():
        pos = pad.GetPosition()
        size = pad.GetSize()
        x = pcbnew.ToMM(pos.x)
        y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x)
        h = pcbnew.ToMM(size.y)
        all_pads.append({{
            "ref": ref,
            "pad": pad.GetNumber(),
            "net": pad.GetNetname(),
            "x": x, "y": y,
            "w": w, "h": h,
            # Pad bounding box
            "x0": x - w / 2, "y0": y - h / 2,
            "x1": x + w / 2, "y1": y + h / 2,
        }})

# Pairwise check across different footprints
violations = []
n = len(all_pads)

for i in range(n):
    a = all_pads[i]
    # Expand pad A by min_clearance for fast AABB rejection
    ax0 = a["x0"] - min_cl
    ay0 = a["y0"] - min_cl
    ax1 = a["x1"] + min_cl
    ay1 = a["y1"] + min_cl
    for j in range(i + 1, n):
        b = all_pads[j]
        # Skip same-footprint pairs
        if a["ref"] == b["ref"]:
            continue
        # Fast AABB rejection with clearance expansion
        if ax0 >= b["x1"] or ax1 <= b["x0"] or ay0 >= b["y1"] or ay1 <= b["y0"]:
            continue
        # Compute actual gap between pad bounding boxes
        gap_x = max(a["x0"], b["x0"]) - min(a["x1"], b["x1"])
        gap_y = max(a["y0"], b["y0"]) - min(a["y1"], b["y1"])
        # If both gaps are negative, pads overlap — gap is 0 (or negative)
        if gap_x < 0 and gap_y < 0:
            gap = 0.0  # actual overlap
        else:
            # Gap is the Chebyshev distance (max of axis-aligned gaps)
            # For non-overlapping: distance = max(gap_x, gap_y) if one is positive
            # For partially overlapping on one axis: distance = max(0, gap_x, gap_y)
            gap = max(0.0, gap_x, gap_y)
        if gap < min_cl:
            violations.append({{
                "pad_a": f"{{a['ref']}}:{{a['pad']}}",
                "pad_b": f"{{b['ref']}}:{{b['pad']}}",
                "net_a": a["net"],
                "net_b": b["net"],
                "gap_mm": round(gap, 3),
                "min_clearance_mm": round(min_cl, 3),
                "overlap": gap == 0.0,
                "pad_a_center": [round(a["x"], 3), round(a["y"], 3)],
                "pad_b_center": [round(b["x"], 3), round(b["y"], 3)],
            }})

# Deduplicate by footprint pair and summarize
fp_pairs = {{}}
for v in violations:
    ref_a = v["pad_a"].split(":")[0]
    ref_b = v["pad_b"].split(":")[0]
    key = tuple(sorted([ref_a, ref_b]))
    if key not in fp_pairs:
        fp_pairs[key] = {{
            "ref_a": key[0], "ref_b": key[1],
            "pad_violations": 0, "min_gap_mm": float("inf"),
        }}
    fp_pairs[key]["pad_violations"] += 1
    fp_pairs[key]["min_gap_mm"] = min(fp_pairs[key]["min_gap_mm"], v["gap_mm"])

fp_summaries = []
for p in fp_pairs.values():
    p["min_gap_mm"] = round(p["min_gap_mm"], 3)
    fp_summaries.append(p)
fp_summaries.sort(key=lambda x: x["min_gap_mm"])

if violations:
    summary = f"{{len(violations)}} pad clearance violation(s) across {{len(fp_summaries)}} footprint pair(s) (min_clearance={{min_cl}}mm)"
else:
    summary = f"All inter-footprint pad clearances >= {{min_cl}}mm ({{n}} pads checked)"

print(json.dumps({{
    "status": "ok",
    "total_pads": n,
    "min_clearance_mm": round(min_cl, 3),
    "violation_count": len(violations),
    "footprint_pairs_affected": len(fp_summaries),
    "footprint_pair_summary": fp_summaries,
    "violations": violations[:50],  # Cap at 50 to avoid huge output
    "violations_truncated": len(violations) > 50,
    "summary": summary,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def pre_route_check(
        pcb_path: str,
        min_clearance_mm: float = 0.0,
    ) -> Dict[str, Any]:
        """Single "is this board ready to route?" check combining all placement audits.

        Runs in one subprocess call:
        1. Footprint courtyard overlap check (``audit_footprint_overlaps``)
        2. Keepout zone violation check (``audit_pcb_placement``)
        3. Pad-to-pad clearance check (``check_pad_clearances``)
        4. Board edge clearance check (pads/copper outside outline)

        Returns a ``route_ready`` boolean — True only when there are zero
        errors (warnings are OK).  Use this BEFORE calling ``autoroute_pcb``
        to catch placement issues that would otherwise require expensive
        re-routing iterations.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            min_clearance_mm: Minimum required clearance between pads of
                different footprints in mm.  0 = use board design rule minimum.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json
{_KEEPOUT_HELPER}
board = pcbnew.LoadBoard({pcb_path!r})
min_cl = {min_clearance_mm}

# Use board design rule if no explicit clearance given
if min_cl <= 0:
    ds = board.GetDesignSettings()
    min_cl = pcbnew.ToMM(ds.m_MinClearance)
    if min_cl <= 0:
        min_cl = 0.2

errors = []
warnings = []

# --- 1. Courtyard overlap check ---
def get_courtyard_bbox(fp):
    x_min = float("inf"); y_min = float("inf")
    x_max = float("-inf"); y_max = float("-inf")
    found = False
    for item in fp.GraphicalItems():
        layer_name = board.GetLayerName(item.GetLayer())
        if "CrtYd" in layer_name:
            found = True
            bbox = item.GetBoundingBox()
            x_min = min(x_min, pcbnew.ToMM(bbox.GetX()))
            y_min = min(y_min, pcbnew.ToMM(bbox.GetY()))
            x_max = max(x_max, pcbnew.ToMM(bbox.GetRight()))
            y_max = max(y_max, pcbnew.ToMM(bbox.GetBottom()))
    if found:
        return {{"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                 "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}}
    x_min = float("inf"); y_min = float("inf")
    x_max = float("-inf"); y_max = float("-inf")
    found = False
    for pad in fp.Pads():
        found = True
        pos = pad.GetPosition(); size = pad.GetSize()
        x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
        x_min = min(x_min, x - w/2); y_min = min(y_min, y - h/2)
        x_max = max(x_max, x + w/2); y_max = max(y_max, y + h/2)
    if found:
        return {{"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                 "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}}
    return None

footprints = []
for fp in board.GetFootprints():
    tight_box = get_courtyard_bbox(fp)
    if not tight_box:
        fp_bbox = fp.GetBoundingBox(False, False)
        tight_box = {{
            "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
            "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
            "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
            "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
        }}
    footprints.append({{"reference": fp.GetReference(), "bbox": tight_box}})

courtyard_overlaps = []
for i in range(len(footprints)):
    a = footprints[i]; a_box = a["bbox"]
    for j in range(i + 1, len(footprints)):
        b = footprints[j]; b_box = b["bbox"]
        if rects_overlap(a_box, b_box):
            area = overlap_area(a_box, b_box)
            courtyard_overlaps.append({{
                "ref_a": a["reference"], "ref_b": b["reference"],
                "overlap_mm2": area,
            }})
            errors.append(f"Courtyard overlap: {{a['reference']}} and {{b['reference']}} ({{area}} mm2)")

# --- 2. Keepout zone check ---
keepouts = extract_keepouts(board)
outline = get_board_outline(board)
keepout_violations = []

for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_bbox = fp.GetBoundingBox(False, False)
    fp_rect = {{
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }}
    for kz in keepouts:
        if kz["source"] == "footprint" and kz["source_ref"] == ref:
            continue
        kz_bb = kz["bounding_box"]
        if not rects_overlap(fp_rect, kz_bb):
            continue
        c = kz["constraints"]
        if c["no_footprints"]:
            msg = f"Keepout violation: {{ref}} in keepout from {{kz['source_ref'] or kz['source']}}"
            keepout_violations.append({{"reference": ref, "keepout": kz["source_ref"] or kz["source"]}})
            errors.append(msg)
    if outline and not rect_inside(fp_rect, outline):
        msg = f"Board edge: {{ref}} extends outside board outline"
        keepout_violations.append({{"reference": ref, "keepout": "board_outline"}})
        warnings.append(msg)

# --- 3. Pad clearance check ---
all_pads = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for pad in fp.Pads():
        pos = pad.GetPosition(); size = pad.GetSize()
        x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
        all_pads.append({{
            "ref": ref, "pad": pad.GetNumber(),
            "x0": x - w/2, "y0": y - h/2,
            "x1": x + w/2, "y1": y + h/2,
        }})

pad_violations = []
n = len(all_pads)
for i in range(n):
    a = all_pads[i]
    ax0 = a["x0"] - min_cl; ay0 = a["y0"] - min_cl
    ax1 = a["x1"] + min_cl; ay1 = a["y1"] + min_cl
    for j in range(i + 1, n):
        b = all_pads[j]
        if a["ref"] == b["ref"]:
            continue
        if ax0 >= b["x1"] or ax1 <= b["x0"] or ay0 >= b["y1"] or ay1 <= b["y0"]:
            continue
        gap_x = max(a["x0"], b["x0"]) - min(a["x1"], b["x1"])
        gap_y = max(a["y0"], b["y0"]) - min(a["y1"], b["y1"])
        if gap_x < 0 and gap_y < 0:
            gap = 0.0
        else:
            gap = max(0.0, gap_x, gap_y)
        if gap < min_cl:
            pad_violations.append({{
                "pad_a": f"{{a['ref']}}:{{a['pad']}}",
                "pad_b": f"{{b['ref']}}:{{b['pad']}}",
                "gap_mm": round(gap, 3),
            }})
            if gap == 0.0:
                errors.append(f"Pad overlap: {{a['ref']}}:{{a['pad']}} and {{b['ref']}}:{{b['pad']}}")
            else:
                errors.append(f"Pad clearance: {{a['ref']}}:{{a['pad']}} and {{b['ref']}}:{{b['pad']}} only {{round(gap, 3)}}mm apart (min {{min_cl}}mm)")

# --- Summary ---
route_ready = len(errors) == 0
total_fp = len(footprints)

parts = []
if courtyard_overlaps:
    parts.append(f"{{len(courtyard_overlaps)}} courtyard overlap(s)")
if keepout_violations:
    parts.append(f"{{len(keepout_violations)}} keepout/boundary issue(s)")
if pad_violations:
    parts.append(f"{{len(pad_violations)}} pad clearance violation(s)")
if parts:
    summary = "NOT ready to route: " + ", ".join(parts)
else:
    summary = f"Ready to route: {{total_fp}} footprints, {{n}} pads all clear"

print(json.dumps({{
    "status": "ok",
    "route_ready": route_ready,
    "total_footprints": total_fp,
    "total_pads": n,
    "min_clearance_mm": round(min_cl, 3),
    "error_count": len(errors),
    "warning_count": len(warnings),
    "courtyard_overlaps": courtyard_overlaps,
    "keepout_violations": keepout_violations,
    "pad_violations": pad_violations[:30],
    "errors": errors[:20],
    "warnings": warnings[:20],
    "summary": summary,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def auto_fix_placement(
        pcb_path: str,
        spacing_mm: float = 0.5,
        max_passes: int = 3,
    ) -> Dict[str, Any]:
        """Resolve courtyard overlaps by nudging footprints apart.

        For each pair of overlapping footprints, moves the less-connected
        component (fewer signal nets) along the axis of minimum penetration
        by enough to create the requested gap.  Runs iteratively since
        nudging one pair can create new overlaps.

        Respects board outline — will not push components outside the board.
        If a nudge in both directions would leave the board, the pair is
        reported as unfixable.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            spacing_mm: Target gap between courtyards after fix (default 0.5).
            max_passes: Maximum fix iterations (default 3).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
spacing = {spacing_mm}
max_passes = {max_passes}

POWER_NETS = {{"", "GND", "+5V", "+3V3", "+3.3V", "+12V", "VCC", "VDD", "VSS", "VBUS"}}

def get_courtyard_bbox(fp):
    x_min = float("inf"); y_min = float("inf")
    x_max = float("-inf"); y_max = float("-inf")
    found = False
    for item in fp.GraphicalItems():
        layer_name = board.GetLayerName(item.GetLayer())
        if "CrtYd" in layer_name:
            found = True
            bbox = item.GetBoundingBox()
            x_min = min(x_min, pcbnew.ToMM(bbox.GetX()))
            y_min = min(y_min, pcbnew.ToMM(bbox.GetY()))
            x_max = max(x_max, pcbnew.ToMM(bbox.GetRight()))
            y_max = max(y_max, pcbnew.ToMM(bbox.GetBottom()))
    if found:
        return (round(x_min, 3), round(y_min, 3), round(x_max, 3), round(y_max, 3))
    # Fallback to pad bbox
    for pad in fp.Pads():
        found = True
        pos = pad.GetPosition(); size = pad.GetSize()
        x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
        x_min = min(x_min, x - w/2); y_min = min(y_min, y - h/2)
        x_max = max(x_max, x + w/2); y_max = max(y_max, y + h/2)
    if found:
        return (round(x_min, 3), round(y_min, 3), round(x_max, 3), round(y_max, 3))
    return None

def signal_net_count(fp):
    nets = set()
    for pad in fp.Pads():
        n = pad.GetNetname()
        if n and n not in POWER_NETS:
            nets.add(n)
    return len(nets)

# Board outline
outline = None
try:
    bb = board.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0:
        outline = (pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY()),
                   pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
except Exception:
    pass

def bbox_inside_board(bx0, by0, bx1, by1):
    if outline is None:
        return True
    return bx0 >= outline[0] and by0 >= outline[1] and bx1 <= outline[2] and by1 <= outline[3]

all_moves = []
unfixed = []
passes_used = 0

for pass_num in range(1, max_passes + 1):
    passes_used = pass_num
    # Rebuild footprint data each pass (positions change)
    fp_data = []
    for fp in board.GetFootprints():
        bbox = get_courtyard_bbox(fp)
        if bbox is None:
            continue
        fp_data.append({{
            "ref": fp.GetReference(),
            "fp": fp,
            "bbox": bbox,
            "nets": signal_net_count(fp),
        }})

    # Find overlapping pairs
    pairs = []
    for i in range(len(fp_data)):
        a = fp_data[i]; ab = a["bbox"]
        for j in range(i + 1, len(fp_data)):
            b = fp_data[j]; bb_ = b["bbox"]
            if ab[0] < bb_[2] and ab[2] > bb_[0] and ab[1] < bb_[3] and ab[3] > bb_[1]:
                pairs.append((a, b))

    if not pairs:
        break

    moved_this_pass = False
    for a, b in pairs:
        # Decide which to move: fewer signal nets = less connected = move it
        if a["nets"] <= b["nets"]:
            mover, anchor = a, b
        else:
            mover, anchor = b, a

        mb = mover["bbox"]; ab_ = anchor["bbox"]
        # Overlap on each axis
        ox = min(mb[2], ab_[2]) - max(mb[0], ab_[0])  # x overlap
        oy = min(mb[3], ab_[3]) - max(mb[1], ab_[1])  # y overlap

        if ox <= 0 or oy <= 0:
            continue  # No longer overlapping (fixed by earlier nudge)

        mover_fp = mover["fp"]
        old_pos = mover_fp.GetPosition()
        old_x = pcbnew.ToMM(old_pos.x)
        old_y = pcbnew.ToMM(old_pos.y)

        resolved = False
        # Try nudging along axis of minimum overlap, then the other axis
        axes = []
        if ox <= oy:
            dx = ox + spacing
            mc = (mb[0] + mb[2]) / 2; ac = (ab_[0] + ab_[2]) / 2
            sign_x = 1 if mc >= ac else -1
            axes.append((sign_x * dx, 0))
            axes.append((-sign_x * dx, 0))
            dy = oy + spacing
            mc = (mb[1] + mb[3]) / 2; ac = (ab_[1] + ab_[3]) / 2
            sign_y = 1 if mc >= ac else -1
            axes.append((0, sign_y * dy))
            axes.append((0, -sign_y * dy))
        else:
            dy = oy + spacing
            mc = (mb[1] + mb[3]) / 2; ac = (ab_[1] + ab_[3]) / 2
            sign_y = 1 if mc >= ac else -1
            axes.append((0, sign_y * dy))
            axes.append((0, -sign_y * dy))
            dx = ox + spacing
            mc = (mb[0] + mb[2]) / 2; ac = (ab_[0] + ab_[2]) / 2
            sign_x = 1 if mc >= ac else -1
            axes.append((sign_x * dx, 0))
            axes.append((-sign_x * dx, 0))

        for ddx, ddy in axes:
            new_x = old_x + ddx
            new_y = old_y + ddy
            # Compute new bbox
            w = mb[2] - mb[0]; h = mb[3] - mb[1]
            off_x = old_x - (mb[0] + w/2); off_y = old_y - (mb[1] + h/2)
            nb = (new_x - off_x - w/2, new_y - off_y - h/2,
                  new_x - off_x + w/2, new_y - off_y + h/2)
            if not bbox_inside_board(nb[0], nb[1], nb[2], nb[3]):
                continue
            # Apply move
            mover_fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(new_x), pcbnew.FromMM(new_y)))
            all_moves.append({{
                "reference": mover["ref"],
                "old_x_mm": round(old_x, 3), "old_y_mm": round(old_y, 3),
                "new_x_mm": round(new_x, 3), "new_y_mm": round(new_y, 3),
                "reason": f"overlap with {{anchor['ref']}}",
                "pass": pass_num,
            }})
            # Update bbox for subsequent pair checks this pass
            mover["bbox"] = nb
            resolved = True
            moved_this_pass = True
            break

        if not resolved:
            unfixed.append({{
                "ref_a": a["ref"], "ref_b": b["ref"],
                "reason": "could not resolve without leaving board",
            }})

    if not moved_this_pass:
        break

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "moves": all_moves,
    "move_count": len(all_moves),
    "unfixed": unfixed,
    "unfixed_count": len(unfixed),
    "passes_used": passes_used,
}}))
"""
        return run_pcbnew_script(script)
