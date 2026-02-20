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
