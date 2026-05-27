"""Audit router — placement, clearance, and constraint verification for KiCad PCBs.

See docs/SPEC_Tool_Consolidation.md §3.
"""

import logging
import os
from typing import Any, Dict

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
from kicad_mcp.utils.keepout_helpers import (
    KEEPOUT_HELPER,
    COURTYARD_BBOX_HELPER,
    COURTYARD_BBOX_TUPLE_HELPER,
    LIB_SEARCH_HELPER,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level helper string constants (embedded in pcbnew subprocess scripts)
# ---------------------------------------------------------------------------
_KEEPOUT_HELPER = KEEPOUT_HELPER
_COURTYARD_BBOX = COURTYARD_BBOX_HELPER
_COURTYARD_BBOX_TUPLE = COURTYARD_BBOX_TUPLE_HELPER
_LIB_SEARCH = LIB_SEARCH_HELPER


# ---------------------------------------------------------------------------
# Op implementations
# ---------------------------------------------------------------------------

def _op_keepouts(pcb_path: str) -> Dict[str, Any]:
    """List all keepout/rule areas on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
keepouts = extract_keepouts(board)
print(json.dumps({"status": "ok", "keepout_count": len(keepouts), "keepouts": keepouts}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_constraints(pcb_path: str) -> Dict[str, Any]:
    """Get a complete summary of board outline, keepout zones, design rules, and placement area."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
keepouts = extract_keepouts(board)
outline = get_board_outline(board)

ds = board.GetDesignSettings()
design_rules = {
    "min_track_width_mm": round(pcbnew.ToMM(ds.m_TrackMinWidth), 3),
    "min_clearance_mm": round(pcbnew.ToMM(ds.m_MinClearance), 3),
    "min_via_diameter_mm": round(pcbnew.ToMM(ds.m_ViasMinSize), 3),
}

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

result = {
    "status": "ok",
    "board_outline": outline,
    "keepout_zones": keepouts,
    "design_rules": design_rules,
    "existing_footprints_count": len(list(board.GetFootprints())),
    "total_keepout_area_mm2": round(total_keepout_area, 1),
}
if board_area > 0:
    result["effective_placement_area_mm2"] = round(board_area - total_keepout_area, 1)
print(json.dumps(result))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_validate_one(
    pcb_path: str,
    library: str,
    footprint_name: str,
    x_mm: float,
    y_mm: float,
    rotation_deg: float = 0.0,
) -> Dict[str, Any]:
    """Check if placing a footprint at a given position would violate keepout zones or board boundaries."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, os, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])

""" + _LIB_SEARCH + """
lib_path = find_lib(params["library"])
if not lib_path:
    print(json.dumps({"error": f"Library {params['library']!r} not found"}))
    raise SystemExit(0)

fp = pcbnew.FootprintLoad(lib_path, params["footprint_name"])
if fp is None:
    print(json.dumps({"error": f"Footprint {params['footprint_name']!r} not found in {params['library']!r}"}))
    raise SystemExit(0)

fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(params["x_mm"]), pcbnew.FromMM(params["y_mm"])))
if params["rotation_deg"] != 0:
    fp.SetOrientationDegrees(params["rotation_deg"])

fp_bbox = fp.GetBoundingBox(False, False)  # exclude text for accurate body bbox
fp_rect = {
    "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
    "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
    "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
    "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
}

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
        violations.append({
            "type": "keepout_overlap",
            "keepout_source": kz["source"],
            "keepout_ref": kz["source_ref"],
            "overlap_mm2": area,
            "blocked": blocked_constraints(c),
            "message": "Footprint overlaps keepout zone"
                       + (f" from {kz['source_ref']}" if kz["source_ref"] else ""),
        })
    else:
        blocked = blocked_constraints(c)
        if blocked:
            warnings.append({
                "type": "routing_keepout_overlap",
                "keepout_source": kz["source"],
                "keepout_ref": kz["source_ref"],
                "overlap_mm2": area,
                "blocked": blocked,
                "message": f"Footprint overlaps zone that blocks {', '.join(blocked)} (routing may be difficult)",
            })

if outline and not rect_inside(fp_rect, outline):
    overhang = {}
    if fp_rect["x_min_mm"] < outline["x_min_mm"]:
        overhang["left_mm"] = round(outline["x_min_mm"] - fp_rect["x_min_mm"], 3)
    if fp_rect["x_max_mm"] > outline["x_max_mm"]:
        overhang["right_mm"] = round(fp_rect["x_max_mm"] - outline["x_max_mm"], 3)
    if fp_rect["y_min_mm"] < outline["y_min_mm"]:
        overhang["top_mm"] = round(outline["y_min_mm"] - fp_rect["y_min_mm"], 3)
    if fp_rect["y_max_mm"] > outline["y_max_mm"]:
        overhang["bottom_mm"] = round(fp_rect["y_max_mm"] - outline["y_max_mm"], 3)
    violations.append({
        "type": "outside_board",
        "overhang": overhang,
        "message": "Footprint extends beyond board outline",
    })

print(json.dumps({
    "status": "ok",
    "valid": len(violations) == 0,
    "violations": violations,
    "warnings": warnings,
    "footprint_bbox_mm": fp_rect,
    "board_outline_mm": outline,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "library": library,
        "footprint_name": footprint_name,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "rotation_deg": rotation_deg,
    })


def _op_footprint_overlaps(
    pcb_path: str,
    min_clearance_mm: float = 0.0,
    use_courtyard: bool = True,
) -> Dict[str, Any]:
    """Audit all footprint pairs for physical overlap or insufficient clearance."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
min_clearance = params["min_clearance_mm"]
use_courtyard = params["use_courtyard"]

""" + _COURTYARD_BBOX + """
# Wrap to add source annotation (courtyard vs pads vs none)
_base_get_bbox = get_courtyard_bbox
def get_courtyard_bbox(fp):
    result = _base_get_bbox(fp)
    if result is None:
        return None, "none"
    for item in fp.GraphicalItems():
        if "CrtYd" in board.GetLayerName(item.GetLayer()):
            return result, "courtyard"
    return result, "pads"

# Collect bounding boxes for all footprints
footprints = []
for fp in board.GetFootprints():
    pos = fp.GetPosition()
    fp_bbox = fp.GetBoundingBox(False, False)
    body_box = {
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }

    if use_courtyard:
        tight_box, source = get_courtyard_bbox(fp)
        check_box = tight_box if tight_box else body_box
        box_source = source if tight_box else "body"
    else:
        check_box = body_box
        box_source = "body"

    footprints.append({
        "reference": fp.GetReference(),
        "value": fp.GetValue(),
        "footprint": fp.GetFPID().GetUniStringLibItemName(),
        "position_mm": [round(pcbnew.ToMM(pos.x), 3), round(pcbnew.ToMM(pos.y), 3)],
        "bbox": check_box,
        "bbox_source": box_source,
    })

# Pairwise overlap check
overlaps = []
for i in range(len(footprints)):
    a = footprints[i]
    a_box = a["bbox"]
    for j in range(i + 1, len(footprints)):
        b = footprints[j]
        b_box = b["bbox"]

        # Check actual overlap (body collision)
        actual_overlap = rects_overlap(a_box, b_box)
        area = overlap_area(a_box, b_box) if actual_overlap else 0.0

        # Check clearance violation using canonical helper
        is_clearance_violation = clearance_violation(a_box, b_box, min_clearance)

        if actual_overlap or is_clearance_violation:
            # Compute gap (negative = overlap, positive = clearance)
            gap_x = max(a_box["x_min_mm"], b_box["x_min_mm"]) - min(a_box["x_max_mm"], b_box["x_max_mm"])
            gap_y = max(a_box["y_min_mm"], b_box["y_min_mm"]) - min(a_box["y_max_mm"], b_box["y_max_mm"])
            # Closest approach: positive = separation, negative = penetration
            gap_mm = max(gap_x, gap_y)

            entry = {
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
            }
            if actual_overlap:
                entry["severity"] = "error"
                entry["message"] = f"{a['reference']} and {b['reference']} physically overlap by {area} mm2"
            else:
                entry["severity"] = "warning"
                entry["message"] = f"{a['reference']} and {b['reference']} are only {round(gap_mm, 3)} mm apart (min clearance: {min_clearance} mm)"
            overlaps.append(entry)

total = len(footprints)
pairs_checked = total * (total - 1) // 2
error_count = sum(1 for o in overlaps if o["severity"] == "error")
warning_count = sum(1 for o in overlaps if o["severity"] == "warning")

if overlaps:
    summary = f"{len(overlaps)} overlap(s) found among {total} footprints ({error_count} collisions, {warning_count} clearance warnings)"
else:
    summary = f"All {total} footprints are clear of each other"
    if min_clearance > 0:
        summary += f" (min clearance {min_clearance} mm)"

print(json.dumps({
    "status": "ok",
    "total_footprints": total,
    "pairs_checked": pairs_checked,
    "overlap_count": len(overlaps),
    "error_count": error_count,
    "warning_count": warning_count,
    "overlaps": overlaps,
    "summary": summary,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "min_clearance_mm": min_clearance_mm,
        "use_courtyard": use_courtyard,
    })


def _op_placement(pcb_path: str) -> Dict[str, Any]:
    """Audit all footprint placements for keepout zone violations and board boundary issues."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
keepouts = extract_keepouts(board)
outline = get_board_outline(board)

violations_list = []
clean_count = 0

for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_bbox = fp.GetBoundingBox(False, False)  # exclude text for accurate body bbox
    fp_rect = {
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }
    issues = []

    for kz in keepouts:
        if kz["source"] == "footprint" and kz["source_ref"] == ref:
            continue
        kz_bb = kz["bounding_box"]
        if not rects_overlap(fp_rect, kz_bb):
            continue
        area = overlap_area(fp_rect, kz_bb)
        c = kz["constraints"]
        blocked = blocked_constraints(c)
        severity = "violation" if c["no_footprints"] else "warning"
        issues.append({
            "type": "keepout_overlap",
            "severity": severity,
            "keepout_source": kz["source"],
            "keepout_ref": kz["source_ref"],
            "overlap_mm2": area,
            "blocked": blocked,
        })

    if outline and not rect_inside(fp_rect, outline):
        overhang = {}
        if fp_rect["x_min_mm"] < outline["x_min_mm"]:
            overhang["left_mm"] = round(outline["x_min_mm"] - fp_rect["x_min_mm"], 3)
        if fp_rect["x_max_mm"] > outline["x_max_mm"]:
            overhang["right_mm"] = round(fp_rect["x_max_mm"] - outline["x_max_mm"], 3)
        if fp_rect["y_min_mm"] < outline["y_min_mm"]:
            overhang["top_mm"] = round(outline["y_min_mm"] - fp_rect["y_min_mm"], 3)
        if fp_rect["y_max_mm"] > outline["y_max_mm"]:
            overhang["bottom_mm"] = round(fp_rect["y_max_mm"] - outline["y_max_mm"], 3)
        issues.append({
            "type": "outside_board",
            "severity": "violation",
            "overhang": overhang,
        })

    if issues:
        pos = fp.GetPosition()
        violations_list.append({
            "reference": ref,
            "value": fp.GetValue(),
            "footprint": fp.GetFPID().GetUniStringLibItemName(),
            "position_mm": [round(pcbnew.ToMM(pos.x), 3), round(pcbnew.ToMM(pos.y), 3)],
            "bbox_mm": fp_rect,
            "issues": issues,
        })
    else:
        clean_count += 1

total = len(list(board.GetFootprints()))
vcount = len(violations_list)
summary = f"{vcount} of {total} footprints have placement issues" if vcount > 0 else f"All {total} footprints pass placement checks"

print(json.dumps({
    "status": "ok",
    "total_footprints": total,
    "violations_count": vcount,
    "clean_count": clean_count,
    "violations": violations_list,
    "summary": summary,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_pad_clearances(
    pcb_path: str,
    min_clearance_mm: float = 0.0,
) -> Dict[str, Any]:
    """Check pad-to-pad clearances between all footprints on the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, math, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
min_cl = params["min_clearance_mm"]

# Use board design rule if no explicit clearance given. Track source so the
# caller can tell whether the value came from board design rules or a fallback.
min_cl_source = "caller"
if min_cl <= 0:
    ds = board.GetDesignSettings()
    min_cl = pcbnew.ToMM(ds.m_MinClearance)
    min_cl_source = "board"
    if min_cl <= 0:
        min_cl = 0.2  # fallback
        min_cl_source = "default_fallback"

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
        all_pads.append({
            "ref": ref,
            "pad": pad.GetNumber(),
            "net": pad.GetNetname(),
            "x": x, "y": y,
            "w": w, "h": h,
            # Pad bounding box
            "x0": x - w / 2, "y0": y - h / 2,
            "x1": x + w / 2, "y1": y + h / 2,
        })

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
        # Signed gap between pad bounding boxes.
        # positive = clearance, zero = touching, negative = penetration depth.
        gap_x = max(a["x0"], b["x0"]) - min(a["x1"], b["x1"])
        gap_y = max(a["y0"], b["y0"]) - min(a["y1"], b["y1"])
        if gap_x >= 0 and gap_y >= 0:
            gap = min(gap_x, gap_y)            # separated; binding clearance
        elif gap_x >= 0 or gap_y >= 0:
            gap = max(gap_x, gap_y)            # separated on one axis only
        else:
            gap = max(gap_x, gap_y)            # overlap; less-negative = penetration on easier-to-fix axis
        if gap < min_cl:
            violations.append({
                "pad_a": f"{a['ref']}:{a['pad']}",
                "pad_b": f"{b['ref']}:{b['pad']}",
                "net_a": a["net"],
                "net_b": b["net"],
                "gap_mm": round(gap, 3),
                "min_clearance_mm": round(min_cl, 3),
                "overlap": gap <= 0,
                "pad_a_center": [round(a["x"], 3), round(a["y"], 3)],
                "pad_b_center": [round(b["x"], 3), round(b["y"], 3)],
            })

# Deduplicate by footprint pair and summarize
fp_pairs = {}
for v in violations:
    ref_a = v["pad_a"].split(":")[0]
    ref_b = v["pad_b"].split(":")[0]
    key = tuple(sorted([ref_a, ref_b]))
    if key not in fp_pairs:
        fp_pairs[key] = {
            "ref_a": key[0], "ref_b": key[1],
            "pad_violations": 0, "min_gap_mm": float("inf"),
        }
    fp_pairs[key]["pad_violations"] += 1
    fp_pairs[key]["min_gap_mm"] = min(fp_pairs[key]["min_gap_mm"], v["gap_mm"])

fp_summaries = []
for p in fp_pairs.values():
    p["min_gap_mm"] = round(p["min_gap_mm"], 3)
    fp_summaries.append(p)
fp_summaries.sort(key=lambda x: x["min_gap_mm"])

if violations:
    summary = f"{len(violations)} pad clearance violation(s) across {len(fp_summaries)} footprint pair(s) (min_clearance={min_cl}mm)"
else:
    summary = f"All inter-footprint pad clearances >= {min_cl}mm ({n} pads checked)"

print(json.dumps({
    "status": "ok",
    "total_pads": n,
    "min_clearance_mm": round(min_cl, 3),
    "min_clearance_source": min_cl_source,
    "violation_count": len(violations),
    "footprint_pairs_affected": len(fp_summaries),
    "footprint_pair_summary": fp_summaries,
    "violations": violations[:50],  # Cap at 50 to avoid huge output
    "violations_truncated": len(violations) > 50,
    "summary": summary,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "min_clearance_mm": min_clearance_mm,
    })


def _op_pre_route_check(
    pcb_path: str,
    min_clearance_mm: float = 0.0,
) -> Dict[str, Any]:
    """Single 'is this board ready to route?' check combining all placement audits."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
min_cl = params["min_clearance_mm"]

# Use board design rule if no explicit clearance given. Track source so the
# caller can tell whether the value came from board design rules or a fallback.
min_cl_source = "caller"
if min_cl <= 0:
    ds = board.GetDesignSettings()
    min_cl = pcbnew.ToMM(ds.m_MinClearance)
    min_cl_source = "board"
    if min_cl <= 0:
        min_cl = 0.2
        min_cl_source = "default_fallback"

errors = []
warnings = []

# --- 1. Courtyard overlap check ---
""" + _COURTYARD_BBOX + """

footprints = []
for fp in board.GetFootprints():
    tight_box = get_courtyard_bbox(fp)
    if not tight_box:
        fp_bbox = fp.GetBoundingBox(False, False)
        tight_box = {
            "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
            "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
            "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
            "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
        }
    footprints.append({"reference": fp.GetReference(), "bbox": tight_box})

courtyard_overlaps = []
for i in range(len(footprints)):
    a = footprints[i]; a_box = a["bbox"]
    for j in range(i + 1, len(footprints)):
        b = footprints[j]; b_box = b["bbox"]
        if rects_overlap(a_box, b_box):
            area = overlap_area(a_box, b_box)
            courtyard_overlaps.append({
                "ref_a": a["reference"], "ref_b": b["reference"],
                "overlap_mm2": area,
            })
            errors.append(f"Courtyard overlap: {a['reference']} and {b['reference']} ({area} mm2)")

# --- 2. Keepout zone check ---
keepouts = extract_keepouts(board)
outline = get_board_outline(board)
keepout_violations = []

for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_bbox = fp.GetBoundingBox(False, False)
    fp_rect = {
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }
    for kz in keepouts:
        if kz["source"] == "footprint" and kz["source_ref"] == ref:
            continue
        kz_bb = kz["bounding_box"]
        if not rects_overlap(fp_rect, kz_bb):
            continue
        c = kz["constraints"]
        if c["no_footprints"]:
            msg = f"Keepout violation: {ref} in keepout from {kz['source_ref'] or kz['source']}"
            keepout_violations.append({"reference": ref, "keepout": kz["source_ref"] or kz["source"]})
            errors.append(msg)
    if outline and not rect_inside(fp_rect, outline):
        msg = f"Board edge: {ref} extends outside board outline"
        keepout_violations.append({"reference": ref, "keepout": "board_outline"})
        warnings.append(msg)

# --- 3. Pad clearance check ---
all_pads = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for pad in fp.Pads():
        pos = pad.GetPosition(); size = pad.GetSize()
        x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
        all_pads.append({
            "ref": ref, "pad": pad.GetNumber(),
            "x0": x - w/2, "y0": y - h/2,
            "x1": x + w/2, "y1": y + h/2,
        })

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
        # Signed gap — see check_pad_clearances above for the convention.
        gap_x = max(a["x0"], b["x0"]) - min(a["x1"], b["x1"])
        gap_y = max(a["y0"], b["y0"]) - min(a["y1"], b["y1"])
        if gap_x >= 0 and gap_y >= 0:
            gap = min(gap_x, gap_y)
        elif gap_x >= 0 or gap_y >= 0:
            gap = max(gap_x, gap_y)
        else:
            gap = max(gap_x, gap_y)
        if gap < min_cl:
            pad_violations.append({
                "pad_a": f"{a['ref']}:{a['pad']}",
                "pad_b": f"{b['ref']}:{b['pad']}",
                "gap_mm": round(gap, 3),
            })
            if gap < 0:
                errors.append(f"Pad overlap: {a['ref']}:{a['pad']} and {b['ref']}:{b['pad']} (penetration {round(-gap, 3)}mm)")
            elif gap == 0:
                errors.append(f"Pad contact: {a['ref']}:{a['pad']} and {b['ref']}:{b['pad']} (touching, min {min_cl}mm required)")
            else:
                errors.append(f"Pad clearance: {a['ref']}:{a['pad']} and {b['ref']}:{b['pad']} only {round(gap, 3)}mm apart (min {min_cl}mm)")

# --- Summary ---
route_ready = len(errors) == 0
total_fp = len(footprints)

parts = []
if courtyard_overlaps:
    parts.append(f"{len(courtyard_overlaps)} courtyard overlap(s)")
if keepout_violations:
    parts.append(f"{len(keepout_violations)} keepout/boundary issue(s)")
if pad_violations:
    parts.append(f"{len(pad_violations)} pad clearance violation(s)")
if parts:
    summary = "NOT ready to route: " + ", ".join(parts)
else:
    summary = f"Ready to route: {total_fp} footprints, {n} pads all clear"

print(json.dumps({
    "status": "ok",
    "route_ready": route_ready,
    "total_footprints": total_fp,
    "total_pads": n,
    "min_clearance_mm": round(min_cl, 3),
    "min_clearance_source": min_cl_source,
    "error_count": len(errors),
    "warning_count": len(warnings),
    "courtyard_overlaps": courtyard_overlaps,
    "keepout_violations": keepout_violations,
    "pad_violations": pad_violations[:30],
    "pad_violations_truncated": len(pad_violations) > 30,
    "errors": errors[:20],
    "errors_truncated": len(errors) > 20,
    "warnings": warnings[:20],
    "warnings_truncated": len(warnings) > 20,
    "summary": summary,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "min_clearance_mm": min_clearance_mm,
    })


def _op_auto_fix_placement(
    pcb_path: str,
    spacing_mm: float = 0.5,
    max_passes: int = 3,
) -> Dict[str, Any]:
    """Resolve courtyard overlaps by nudging footprints apart."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
spacing = params["spacing_mm"]
max_passes = params["max_passes"]

""" + _COURTYARD_BBOX_TUPLE + """

# Board outline. Use hasattr guard so unknown failures propagate rather
# than silently leaving outline=None (which makes bbox_inside_board return
# True unconditionally — components could be moved off-board).
outline = None
if hasattr(board, 'GetBoardEdgesBoundingBox'):
    bb = board.GetBoardEdgesBoundingBox()
    if bb.GetWidth() > 0:
        outline = (pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY()),
                   pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))

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
        fp_data.append({
            "ref": fp.GetReference(),
            "fp": fp,
            "bbox": bbox,
            "nets": signal_net_count(fp),
        })

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
            all_moves.append({
                "reference": mover["ref"],
                "old_x_mm": round(old_x, 3), "old_y_mm": round(old_y, 3),
                "new_x_mm": round(new_x, 3), "new_y_mm": round(new_y, 3),
                "reason": f"overlap with {anchor['ref']}",
                "pass": pass_num,
            })
            # Update bbox for subsequent pair checks this pass
            mover["bbox"] = nb
            resolved = True
            moved_this_pass = True
            break

        if not resolved:
            unfixed.append({
                "ref_a": a["ref"], "ref_b": b["ref"],
                "reason": "could not resolve without leaving board",
            })

    if not moved_this_pass:
        break

board.Save(params["pcb_path"])

print(json.dumps({
    "status": "ok",
    "moves": all_moves,
    "move_count": len(all_moves),
    "unfixed": unfixed,
    "unfixed_count": len(unfixed),
    "passes_used": passes_used,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "spacing_mm": spacing_mm,
        "max_passes": max_passes,
    })


def _op_all_summary(pcb_path: str, min_clearance_mm: float = 0.0) -> Dict[str, Any]:
    """Run all placement audits in a single subprocess call (summary detail level)."""
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + _KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
min_clearance = params["min_clearance_mm"]

# --- 1. Footprint overlap check (courtyard-based) ---
""" + _COURTYARD_BBOX + """

footprints = []
for fp in board.GetFootprints():
    tight_box = get_courtyard_bbox(fp)
    if not tight_box:
        fp_bbox = fp.GetBoundingBox(False, False)
        tight_box = {
            "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
            "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
            "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
            "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
        }
    footprints.append({
        "reference": fp.GetReference(),
        "bbox": tight_box,
    })

fp_overlaps = []
for i in range(len(footprints)):
    a = footprints[i]; a_box = a["bbox"]
    for j in range(i + 1, len(footprints)):
        b = footprints[j]; b_box = b["bbox"]
        actual = rects_overlap(a_box, b_box)
        is_clearance_violation = clearance_violation(a_box, b_box, min_clearance)
        if actual or is_clearance_violation:
            area = overlap_area(a_box, b_box) if actual else 0.0
            fp_overlaps.append({
                "ref_a": a["reference"], "ref_b": b["reference"],
                "overlap": actual, "overlap_mm2": area,
            })

# --- 2. Keepout / board boundary check ---
keepouts = extract_keepouts(board)
outline = get_board_outline(board)
keepout_violations = []

for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_bbox = fp.GetBoundingBox(False, False)
    fp_rect = {
        "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
        "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
        "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
        "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
    }
    for kz in keepouts:
        if kz["source"] == "footprint" and kz["source_ref"] == ref:
            continue
        kz_bb = kz["bounding_box"]
        if not rects_overlap(fp_rect, kz_bb):
            continue
        c = kz["constraints"]
        blocked = blocked_constraints(c)
        if blocked:
            keepout_violations.append({
                "reference": ref,
                "keepout_source": kz["source_ref"] or kz["source"],
                "blocked": blocked,
                "is_footprint_keepout": c["no_footprints"],
            })
    if outline and not rect_inside(fp_rect, outline):
        keepout_violations.append({
            "reference": ref,
            "keepout_source": "board_outline",
            "blocked": ["outside_board"],
            "is_footprint_keepout": True,
        })

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
        silk_items.append({
            "component": ref, "type": ft,
            "bbox": sb,
            "layer": fo.GetLayer(),
            "x_min": sb.GetX(), "y_min": sb.GetY(),
            "x_max": sb.GetRight(), "y_max": sb.GetBottom(),
        })

# Also include standalone text as obstacles
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
        if vis:
            sb = drawing.GetBoundingBox()
            silk_items.append({
                "component": None, "type": "standalone",
                "bbox": sb,
                "layer": drawing.GetLayer(),
                "x_min": sb.GetX(), "y_min": sb.GetY(),
                "x_max": sb.GetRight(), "y_max": sb.GetBottom(),
            })

all_pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        pb = pad.GetBoundingBox()
        all_pads.append({
            "reference": fp.GetReference(),
            "x_min": pb.GetX(), "y_min": pb.GetY(),
            "x_max": pb.GetRight(), "y_max": pb.GetBottom(),
        })

def _aabb(ax0, ay0, ax1, ay1, bx0, by0, bx1, by1):
    return ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0

# Text over pads
for si in silk_items:
    for pad in all_pads:
        if si["component"] == pad["reference"]:
            continue
        if _aabb(si["x_min"], si["y_min"], si["x_max"], si["y_max"],
                 pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
            silk_overlaps.append({
                "silk_component": si["component"],
                "silk_type": si["type"],
                "pad_component": pad["reference"],
            })

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
            silk_text_overlaps.append({
                "text_a_component": a["component"], "text_a_type": a["type"],
                "text_b_component": b["component"], "text_b_type": b["type"],
            })

# --- Summary ---
total_fp = len(footprints)
all_silk_issues = len(silk_overlaps) + len(silk_text_overlaps)
issues = len(fp_overlaps) + len(keepout_violations) + all_silk_issues
parts = []
if fp_overlaps:
    parts.append(f"{len(fp_overlaps)} footprint overlap(s)")
if keepout_violations:
    parts.append(f"{len(keepout_violations)} keepout/boundary issue(s)")
if silk_overlaps:
    parts.append(f"{len(silk_overlaps)} silkscreen-over-pad overlap(s)")
if silk_text_overlaps:
    parts.append(f"{len(silk_text_overlaps)} silkscreen text-to-text overlap(s)")
summary = ", ".join(parts) if parts else f"All {total_fp} footprints pass all checks"

print(json.dumps({
    "status": "ok",
    "total_footprints": total_fp,
    "total_issues": issues,
    "footprint_overlaps": fp_overlaps,
    "keepout_violations": keepout_violations,
    "silkscreen_overlaps": silk_overlaps,
    "silkscreen_text_overlaps": silk_text_overlaps,
    "summary": summary,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "min_clearance_mm": min_clearance_mm,
    })


def _op_all_full(pcb_path: str, min_clearance_mm: float = 0.0) -> Dict[str, Any]:
    """Run all placement audits and return full per-op detail (full detail level)."""
    results: Dict[str, Any] = {"status": "ok"}

    placement = _op_placement(pcb_path)
    if "error" in placement:
        return placement
    results["placement"] = placement

    overlaps = _op_footprint_overlaps(pcb_path, min_clearance_mm=min_clearance_mm)
    if "error" in overlaps:
        return overlaps
    results["footprint_overlaps"] = overlaps

    pad_cl = _op_pad_clearances(pcb_path, min_clearance_mm=min_clearance_mm)
    if "error" in pad_cl:
        return pad_cl
    results["pad_clearances"] = pad_cl

    keepouts = _op_keepouts(pcb_path)
    if "error" in keepouts:
        return keepouts
    results["keepouts"] = keepouts

    # Aggregate summary from sub-results
    total_issues = (
        placement.get("violations_count", 0)
        + overlaps.get("overlap_count", 0)
        + pad_cl.get("violation_count", 0)
    )
    results["total_issues"] = total_issues
    results["summary"] = (
        f"placement={placement.get('violations_count', 0)} violations, "
        f"overlaps={overlaps.get('overlap_count', 0)}, "
        f"pad_clearances={pad_cl.get('violation_count', 0)}"
    )
    return results


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_pcb_keepout_tools(mcp: FastMCP) -> None:
    """Register the audit domain router."""

    @mcp.tool()
    def audit(
        operation: str,
        *,
        pcb_path: str | None = None,
        library: str | None = None,
        footprint_name: str | None = None,
        x_mm: float | None = None,
        y_mm: float | None = None,
        rotation_deg: float = 0.0,
        min_clearance_mm: float = 0.0,
        use_courtyard: bool = True,
        spacing_mm: float = 0.5,
        max_passes: int = 3,
        detail: str = "summary",
    ) -> Dict[str, Any]:
        """Audit domain router — placement, clearance, and constraint verification.

        Operations:
          all(pcb_path, min_clearance_mm=0, detail="summary"|"full")
              -> combined audit of footprint overlaps, keepout violations, and
                 silkscreen overlaps. detail="summary" returns abridged counts
                 (one subprocess call). detail="full" calls each sub-op
                 independently and returns their complete output under
                 "placement", "footprint_overlaps", "pad_clearances", "keepouts".

          placement(pcb_path)
              -> {total_footprints, violations_count, clean_count, violations}
                 Audit every placed footprint against keepout zones and board edge.
                 Returns per-footprint issue list with bbox, severity, overlap_mm2.

          footprint_overlaps(pcb_path, min_clearance_mm=0, use_courtyard=True)
              -> {total_footprints, pairs_checked, overlap_count, error_count,
                  warning_count, overlaps}
                 Check every pair of footprints for body collision or clearance
                 violation. use_courtyard=True uses courtyard/pad bounds (fewer
                 false positives for large modules like ESP32).

          pad_clearances(pcb_path, min_clearance_mm=0)
              -> {total_pads, min_clearance_mm, violation_count,
                  footprint_pairs_affected, footprint_pair_summary, violations}
                 Check individual pad geometries for clearance violations.
                 min_clearance_mm=0 uses the board's design-rule minimum.

          validate_one(pcb_path, library, footprint_name, x_mm, y_mm,
                       rotation_deg=0)
              -> {valid, violations, warnings, footprint_bbox_mm, board_outline_mm}
                 Check if placing a specific footprint at a proposed position would
                 violate keepout zones or board boundaries. Read-only — does NOT
                 modify the PCB file.

          auto_fix_placement(pcb_path, spacing_mm=0.5, max_passes=3)
              -> {moves, move_count, unfixed, unfixed_count, passes_used}
                 Resolve courtyard overlaps by nudging footprints apart. Moves
                 the less-connected component (fewer signal nets). Respects board
                 outline — will not push components outside the board.

          keepouts(pcb_path)
              -> {keepout_count, keepouts}
                 List all keepout/rule areas with boundaries and constraints.
                 Includes footprint-embedded keepouts (e.g. ESP32 antenna).

          pre_route_check(pcb_path, min_clearance_mm=0)
              -> {route_ready, total_footprints, total_pads, error_count,
                  warning_count, courtyard_overlaps, keepout_violations,
                  pad_violations, errors, warnings}
                 Combined readiness check before autorouting: courtyard overlaps +
                 keepout violations + pad clearances. route_ready=True only when
                 there are zero errors (warnings OK).

          constraints(pcb_path)
              -> {board_outline, keepout_zones, design_rules,
                  existing_footprints_count, total_keepout_area_mm2,
                  effective_placement_area_mm2?}
                 Board outline + keepouts + design rules in one call. Use before
                 placement decisions to understand available area.
        """
        if detail not in ("summary", "full"):
            return {"error": f"detail must be 'summary' or 'full', got {detail!r}"}

        if operation == "all":
            if pcb_path is None:
                return {"error": "operation='all' requires 'pcb_path'"}
            if not os.path.exists(pcb_path):
                return {"error": f"PCB file not found: {pcb_path}"}
            if detail == "full":
                return _op_all_full(pcb_path, min_clearance_mm=min_clearance_mm)
            return _op_all_summary(pcb_path, min_clearance_mm=min_clearance_mm)

        if operation == "placement":
            if pcb_path is None:
                return {"error": "operation='placement' requires 'pcb_path'"}
            return _op_placement(pcb_path)

        if operation == "footprint_overlaps":
            if pcb_path is None:
                return {"error": "operation='footprint_overlaps' requires 'pcb_path'"}
            return _op_footprint_overlaps(
                pcb_path,
                min_clearance_mm=min_clearance_mm,
                use_courtyard=use_courtyard,
            )

        if operation == "pad_clearances":
            if pcb_path is None:
                return {"error": "operation='pad_clearances' requires 'pcb_path'"}
            return _op_pad_clearances(pcb_path, min_clearance_mm=min_clearance_mm)

        if operation == "validate_one":
            if pcb_path is None:
                return {"error": "operation='validate_one' requires 'pcb_path'"}
            if library is None:
                return {"error": "operation='validate_one' requires 'library'"}
            if footprint_name is None:
                return {"error": "operation='validate_one' requires 'footprint_name'"}
            if x_mm is None:
                return {"error": "operation='validate_one' requires 'x_mm'"}
            if y_mm is None:
                return {"error": "operation='validate_one' requires 'y_mm'"}
            return _op_validate_one(
                pcb_path, library, footprint_name, x_mm, y_mm, rotation_deg
            )

        if operation == "auto_fix_placement":
            if pcb_path is None:
                return {"error": "operation='auto_fix_placement' requires 'pcb_path'"}
            return _op_auto_fix_placement(
                pcb_path, spacing_mm=spacing_mm, max_passes=max_passes
            )

        if operation == "keepouts":
            if pcb_path is None:
                return {"error": "operation='keepouts' requires 'pcb_path'"}
            return _op_keepouts(pcb_path)

        if operation == "pre_route_check":
            if pcb_path is None:
                return {"error": "operation='pre_route_check' requires 'pcb_path'"}
            return _op_pre_route_check(pcb_path, min_clearance_mm=min_clearance_mm)

        if operation == "constraints":
            if pcb_path is None:
                return {"error": "operation='constraints' requires 'pcb_path'"}
            return _op_constraints(pcb_path)

        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: all|placement|footprint_overlaps|pad_clearances|"
                f"validate_one|auto_fix_placement|keepouts|pre_route_check|constraints"
            )
        }
