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
