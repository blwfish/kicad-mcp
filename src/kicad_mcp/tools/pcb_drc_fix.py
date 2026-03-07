"""DRC auto-fix: compound tool that reads DRC, fixes violations, re-verifies."""

import asyncio
import logging
import os
from typing import Any, Dict

from fastmcp import FastMCP

from kicad_mcp.utils.keepout_helpers import COURTYARD_BBOX_TUPLE_HELPER

logger = logging.getLogger(__name__)

# DRC violation categories and which fix strategy applies
ROUTING_VIOLATIONS = {
    "clearance",
    "tracks_crossing",
    "shorting_items",
    "Clearance violation",
    "Track too close",
    "Tracks crossing",
    "Items shorting",
}

SILKSCREEN_VIOLATIONS = {
    "silk_overlap",
    "silk_over_copper",
    "Silk over copper",
    "Silkscreen overlap",
    "Silk text over pad",
}

PLACEMENT_VIOLATIONS = {
    "courtyards_overlap",
    "Courtyards overlap",
}


def _categorize_violations(categories: Dict[str, int]) -> Dict[str, list]:
    """Sort DRC violation categories into fixable groups."""
    routing = []
    silkscreen = []
    placement = []
    other = []

    for msg, count in categories.items():
        msg_lower = msg.lower()
        if any(kw in msg_lower for kw in ("clearance", "crossing", "shorting", "track too close")):
            routing.append({"message": msg, "count": count})
        elif any(kw in msg_lower for kw in ("silk", "silkscreen")):
            silkscreen.append({"message": msg, "count": count})
        elif "courtyard" in msg_lower:
            placement.append({"message": msg, "count": count})
        else:
            other.append({"message": msg, "count": count})

    return {
        "routing": routing,
        "silkscreen": silkscreen,
        "placement": placement,
        "other": other,
    }


def register_pcb_drc_fix_tools(mcp: FastMCP) -> None:
    """Register DRC auto-fix tools."""

    @mcp.tool()
    async def drc_autofix(
        pcb_path: str,
        project_path: str = "",
        fix_routing: bool = True,
        fix_silkscreen: bool = True,
        fix_placement: bool = True,
        autoroute_passes: int = 2,
    ) -> Dict[str, Any]:
        """Automatically fix common DRC violations.

        Runs DRC, categorizes violations, and applies fixes in order:
        1. Placement fixes (courtyard overlaps) — nudges footprints apart
        2. Routing fixes (clearance, crossing, shorts) — clears and re-autoroutes
        3. Silkscreen fixes (silk over copper/pads) — repositions text
        4. Zone fill — refills copper pours after changes

        Runs DRC again afterward to verify improvement and returns a
        before/after comparison.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            project_path: Path to .kicad_pro file (auto-derived from pcb_path if empty).
            fix_routing: Clear routing and re-autoroute for track violations (default True).
            fix_silkscreen: Auto-fix silkscreen overlaps (default True).
            fix_placement: Auto-fix courtyard overlaps (default True).
            autoroute_passes: Number of autoroute passes when fixing routing (default 2).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        # Derive project_path if not provided
        if not project_path:
            base = os.path.splitext(pcb_path)[0]
            project_path = base + ".kicad_pro"

        if not os.path.exists(project_path):
            return {"error": f"Project file not found: {project_path}. Provide project_path explicitly."}

        # Lazy imports to avoid circular dependencies
        from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
        from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

        actions_taken = []

        # --- Run initial DRC ---
        before_drc = await run_drc_via_cli(pcb_path, ctx=None)
        if not before_drc.get("success"):
            return {"error": f"Initial DRC failed: {before_drc.get('error', 'unknown')}"}

        before_total = before_drc.get("total_violations", 0)
        before_cats = before_drc.get("violation_categories", {})
        groups = _categorize_violations(before_cats)

        if before_total == 0:
            return {
                "status": "ok",
                "message": "No DRC violations found — nothing to fix",
                "before": {"total": 0, "categories": {}},
                "after": {"total": 0, "categories": {}},
                "actions_taken": [],
            }

        # --- 1. Fix placement (courtyard overlaps) ---
        if fix_placement and groups["placement"]:
            # Call auto_fix_placement via its pcbnew script
            # (we inline the tool call to avoid MCP dispatch overhead)
            result = run_pcbnew_script(f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
spacing = 0.5
max_passes = 3

{COURTYARD_BBOX_TUPLE_HELPER}

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

move_count = 0
for pass_num in range(1, max_passes + 1):
    fp_data = []
    for fp in board.GetFootprints():
        bbox = get_courtyard_bbox(fp)
        if bbox is None:
            continue
        fp_data.append({{"ref": fp.GetReference(), "fp": fp, "bbox": bbox, "nets": signal_net_count(fp)}})
    pairs = []
    for i in range(len(fp_data)):
        a = fp_data[i]; ab = a["bbox"]
        for j in range(i + 1, len(fp_data)):
            b = fp_data[j]; bb_ = b["bbox"]
            if ab[0] < bb_[2] and ab[2] > bb_[0] and ab[1] < bb_[3] and ab[3] > bb_[1]:
                pairs.append((a, b))
    if not pairs:
        break
    moved = False
    for a, b in pairs:
        if a["nets"] <= b["nets"]:
            mover, anchor = a, b
        else:
            mover, anchor = b, a
        mb = mover["bbox"]; ab_ = anchor["bbox"]
        ox = min(mb[2], ab_[2]) - max(mb[0], ab_[0])
        oy = min(mb[3], ab_[3]) - max(mb[1], ab_[1])
        if ox <= 0 or oy <= 0:
            continue
        old_pos = mover["fp"].GetPosition()
        old_x = pcbnew.ToMM(old_pos.x); old_y = pcbnew.ToMM(old_pos.y)
        axes = []
        if ox <= oy:
            dx = ox + spacing; mc = (mb[0]+mb[2])/2; ac = (ab_[0]+ab_[2])/2; s = 1 if mc>=ac else -1
            axes += [(s*dx,0),(-s*dx,0)]
            dy = oy + spacing; mc = (mb[1]+mb[3])/2; ac = (ab_[1]+ab_[3])/2; s = 1 if mc>=ac else -1
            axes += [(0,s*dy),(0,-s*dy)]
        else:
            dy = oy + spacing; mc = (mb[1]+mb[3])/2; ac = (ab_[1]+ab_[3])/2; s = 1 if mc>=ac else -1
            axes += [(0,s*dy),(0,-s*dy)]
            dx = ox + spacing; mc = (mb[0]+mb[2])/2; ac = (ab_[0]+ab_[2])/2; s = 1 if mc>=ac else -1
            axes += [(s*dx,0),(-s*dx,0)]
        for ddx, ddy in axes:
            nx = old_x + ddx; ny = old_y + ddy
            w = mb[2]-mb[0]; h = mb[3]-mb[1]
            off_x = old_x-(mb[0]+w/2); off_y = old_y-(mb[1]+h/2)
            nb = (nx-off_x-w/2, ny-off_y-h/2, nx-off_x+w/2, ny-off_y+h/2)
            if bbox_inside_board(*nb):
                mover["fp"].SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(nx), pcbnew.FromMM(ny)))
                mover["bbox"] = nb; move_count += 1; moved = True; break
    if not moved:
        break

board.Save({pcb_path!r})
print(json.dumps({{"status": "ok", "move_count": move_count}}))
""")
            if result.get("status") == "ok" and result.get("move_count", 0) > 0:
                actions_taken.append(f"placement: nudged {result['move_count']} footprint(s)")

        # --- 2. Fix routing violations ---
        if fix_routing and groups["routing"]:
            # Clear existing routing
            clear_result = run_pcbnew_script(f"""
import pcbnew, json
board = pcbnew.LoadBoard({pcb_path!r})
removed = 0
to_remove = []
for track in board.GetTracks():
    to_remove.append(track)
for item in to_remove:
    board.Remove(item)
    removed += 1
board.Save({pcb_path!r})
print(json.dumps({{"status": "ok", "removed": removed}}))
""")
            tracks_cleared = clear_result.get("removed", 0)

            # Re-autoroute
            from kicad_mcp.tools.pcb_autoroute import (
                _find_freerouter_jar,
                _find_java,
                _run_full_autoroute,
            )

            jar_path = _find_freerouter_jar(None)
            java_path = _find_java()
            if jar_path and java_path:
                route_result = _run_full_autoroute(
                    pcb_path=pcb_path,
                    jar_path=jar_path,
                    java_path=java_path,
                    passes=autoroute_passes,
                    remove_zones=True,
                )
                incomplete = route_result.get("best_incomplete", "?")
                actions_taken.append(
                    f"routing: cleared {tracks_cleared} tracks/vias, "
                    f"re-autorouted ({autoroute_passes} passes, {incomplete} incomplete)"
                )
            else:
                actions_taken.append(
                    f"routing: cleared {tracks_cleared} tracks/vias but "
                    "FreeRouter/Java not available for re-autoroute"
                )

        # --- 3. Fix silkscreen ---
        if fix_silkscreen and groups["silkscreen"]:
            silk_result = run_pcbnew_script(f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})
silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

all_pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        sz = pad.GetBoundingBox()
        all_pads.append({{
            "reference": fp.GetReference(),
            "x_min": sz.GetX(), "y_min": sz.GetY(),
            "x_max": sz.GetRight(), "y_max": sz.GetBottom(),
        }})

all_silk = []
for fp in board.GetFootprints():
    _ref = fp.GetReference()
    for _ft, _fo in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not _fo.IsVisible() or _fo.GetLayer() not in silk_layer_ids:
            continue
        all_silk.append({{"component": _ref, "obj": _fo, "layer": _fo.GetLayer()}})
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        _vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
        if _vis:
            all_silk.append({{"component": None, "obj": drawing, "layer": drawing.GetLayer()}})

try:
    board_bb = board.GetBoardEdgesBoundingBox()
    board_valid = board_bb.GetWidth() > 0
except Exception:
    board_valid = False

def _aabb_hit(a, bx_min, by_min, bx_max, by_max):
    return (a.GetX() < bx_max and a.GetRight() > bx_min and
            a.GetY() < by_max and a.GetBottom() > by_min)

def has_pad_overlap(text_bbox, own_ref):
    for pad in all_pads:
        if pad["reference"] == own_ref:
            continue
        if _aabb_hit(text_bbox, pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"]):
            return True
    return False

def has_text_overlap(text_bbox, own_ref, own_layer, own_obj):
    for si in all_silk:
        if si["obj"] is own_obj:
            continue
        if si["component"] is not None and si["component"] == own_ref:
            continue
        if si["layer"] != own_layer:
            continue
        if not si["obj"].IsVisible() if hasattr(si["obj"], 'IsVisible') else False:
            continue
        ob = si["obj"].GetBoundingBox()
        if _aabb_hit(text_bbox, ob.GetX(), ob.GetY(), ob.GetRight(), ob.GetBottom()):
            return True
    return False

def has_any_overlap(text_bbox, own_ref, own_layer, own_obj):
    return has_pad_overlap(text_bbox, own_ref) or has_text_overlap(text_bbox, own_ref, own_layer, own_obj)

def in_board(text_bbox):
    if not board_valid:
        return True
    return (board_bb.GetX() <= text_bbox.GetX() and
            text_bbox.GetRight() <= board_bb.GetRight() and
            board_bb.GetY() <= text_bbox.GetY() and
            text_bbox.GetBottom() <= board_bb.GetBottom())

MARGIN = pcbnew.FromMM(0.3)
moved = 0; hidden_count = 0

for fp in board.GetFootprints():
    ref = fp.GetReference()
    for field_type, field_obj in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not field_obj.IsVisible() or field_obj.GetLayer() not in silk_layer_ids:
            continue
        text_bbox = field_obj.GetBoundingBox()
        own_layer = field_obj.GetLayer()
        if not has_any_overlap(text_bbox, ref, own_layer, field_obj):
            continue
        fp_bb = fp.GetBoundingBox()
        cx = fp_bb.GetCenter().x; cy = fp_bb.GetCenter().y
        fw2 = fp_bb.GetWidth()//2; fh2 = fp_bb.GetHeight()//2
        tw2 = text_bbox.GetWidth()//2; th2 = text_bbox.GetHeight()//2
        candidates = [
            (cx, cy-fh2-th2-MARGIN), (cx, cy+fh2+th2+MARGIN),
            (cx-fw2-tw2-MARGIN, cy), (cx+fw2+tw2+MARGIN, cy),
            (cx-fw2-tw2-MARGIN, cy-fh2-th2-MARGIN), (cx+fw2+tw2+MARGIN, cy-fh2-th2-MARGIN),
            (cx-fw2-tw2-MARGIN, cy+fh2+th2+MARGIN), (cx+fw2+tw2+MARGIN, cy+fh2+th2+MARGIN),
        ]
        orig_pos = field_obj.GetPosition()
        resolved = False
        for px, py in candidates:
            field_obj.SetPosition(pcbnew.VECTOR2I(int(px), int(py)))
            new_bbox = field_obj.GetBoundingBox()
            if not has_any_overlap(new_bbox, ref, own_layer, field_obj) and in_board(new_bbox):
                resolved = True; moved += 1; break
        if not resolved:
            field_obj.SetPosition(orig_pos)
            field_obj.SetVisible(False)
            hidden_count += 1

# Fill zones
copper_zones = [z for z in board.Zones() if not z.GetIsRuleArea()]
if copper_zones:
    for z in copper_zones:
        z.UnFill()
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill(board.Zones())

board.Save({pcb_path!r})
print(json.dumps({{"status": "ok", "moved": moved, "hidden": hidden_count,
                    "zones_filled": len(copper_zones)}}))
""", timeout=120.0)
            if silk_result.get("status") == "ok":
                m = silk_result.get("moved", 0)
                h = silk_result.get("hidden", 0)
                z = silk_result.get("zones_filled", 0)
                parts = []
                if m: parts.append(f"moved {m}")
                if h: parts.append(f"hidden {h}")
                if z: parts.append(f"filled {z} zone(s)")
                actions_taken.append(f"silkscreen: {', '.join(parts)}" if parts else "silkscreen: no changes needed")

        # --- 4. Re-run DRC to verify ---
        after_drc = await run_drc_via_cli(pcb_path, ctx=None)
        after_total = after_drc.get("total_violations", 0) if after_drc.get("success") else "error"
        after_cats = after_drc.get("violation_categories", {}) if after_drc.get("success") else {}

        return {
            "status": "ok",
            "before": {"total": before_total, "categories": before_cats},
            "after": {"total": after_total, "categories": after_cats},
            "actions_taken": actions_taken,
            "improvement": before_total - after_total if isinstance(after_total, int) else None,
        }
