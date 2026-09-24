"""DRC auto-fix: compound tool that reads DRC, fixes violations, re-verifies."""

import logging
import os
from typing import Any, Dict


from kicad_mcp.utils.geometry import GEOMETRY_HELPER
from kicad_mcp.utils.keepout_helpers import (
    COURTYARD_BBOX_TUPLE_HELPER,
    NUDGE_PLACEMENT_HELPER,
)
from kicad_mcp.utils.path_validation import validate_project_path
from kicad_mcp.utils.pcb_lock import busy_error, pcb_write_lock

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
    # cli_drc.py's parse_drc_report emits this exact key for kicad-cli's
    # unconnected_items array (unrouted nets). It needs the same fix
    # strategy as routing violations (clear + re-autoroute) -- without it
    # here, a board whose ONLY violations are unrouted nets has an empty
    # groups["routing"], so autofix's routing step never runs at all.
    "unconnected",
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


_ROUTING_KEYWORDS = ("clearance", "crossing", "shorting", "track too close")
_SILKSCREEN_KEYWORDS = ("silk", "silkscreen")
_PLACEMENT_KEYWORDS = ("courtyard",)


def _categorize_violations(categories: Dict[str, int]) -> Dict[str, list]:
    """Sort DRC violation categories into fixable groups.

    Two-tier matching:
      1. Exact membership in ROUTING/SILKSCREEN/PLACEMENT_VIOLATIONS sets
         (authoritative for known violation IDs/messages).
      2. Substring keyword fallback (catches new variants until added
         to the sets explicitly).

    When KiCad introduces a new violation type the keyword fallback
    keeps things working; when we observe its exact string, add it to
    the appropriate set for the explicit-match path.
    """
    routing = []
    silkscreen = []
    placement = []
    other = []

    for msg, count in categories.items():
        msg_lower = msg.lower()
        entry = {"message": msg, "count": count}

        if msg in ROUTING_VIOLATIONS or msg_lower in {v.lower() for v in ROUTING_VIOLATIONS}:
            routing.append(entry)
        elif msg in SILKSCREEN_VIOLATIONS or msg_lower in {v.lower() for v in SILKSCREEN_VIOLATIONS}:
            silkscreen.append(entry)
        elif msg in PLACEMENT_VIOLATIONS or msg_lower in {v.lower() for v in PLACEMENT_VIOLATIONS}:
            placement.append(entry)
        # Keyword fallback, most-specific first. "silk" must beat the routing
        # keywords: a type like `silk_clearance` / `silk_mask_clearance` contains
        # both "silk" AND "clearance", and a routing-first order would
        # misclassify it as routing — triggering a full clear-and-reroute instead
        # of the silkscreen fix. "courtyard" (placement) doesn't overlap routing.
        elif any(kw in msg_lower for kw in _SILKSCREEN_KEYWORDS):
            silkscreen.append(entry)
        elif any(kw in msg_lower for kw in _PLACEMENT_KEYWORDS):
            placement.append(entry)
        elif any(kw in msg_lower for kw in _ROUTING_KEYWORDS):
            routing.append(entry)
        else:
            other.append(entry)

    return {
        "routing": routing,
        "silkscreen": silkscreen,
        "placement": placement,
        "other": other,
    }


async def _op_autofix(
    pcb_path: str,
    project_path: str = "",
    fix_routing: bool = True,
    fix_silkscreen: bool = True,
    fix_placement: bool = True,
    autoroute_passes: int = 2,
) -> Dict[str, Any]:
    """Automatically fix common DRC violations.

    Used by the drc router's autofix operation.
    """
    _pv_err = validate_project_path(pcb_path)
    if _pv_err:
        return {"error": _pv_err}

    with pcb_write_lock(pcb_path) as _acquired:
        if not _acquired:
            return busy_error(pcb_path)
        try:
            return await _op_autofix_locked(
                pcb_path, project_path, fix_routing, fix_silkscreen,
                fix_placement, autoroute_passes,
            )
        except RuntimeError as exc:
            # run_pcbnew_script normalizes every subprocess failure to
            # RuntimeError (its documented contract) -- this locked body
            # calls it 3 separate times with no wrapper of its own, so any
            # one of those failures escaped the tool call raw instead of the
            # {"status": "ok"|"error"} envelope every other failure path
            # here returns (same class of bug as pcb_autoroute.py's fix).
            logger.error("drc autofix failed for %s: %s", pcb_path, exc)
            return {"error": str(exc)}


async def _op_autofix_locked(
    pcb_path: str,
    project_path: str,
    fix_routing: bool,
    fix_silkscreen: bool,
    fix_placement: bool,
    autoroute_passes: int,
) -> Dict[str, Any]:
    """The lock-held body of _op_autofix -- split out the same way
    pcb_autoroute._op_run is, so the lock's scope is exactly "everything
    that touches pcb_path," not accidentally narrower."""
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
    routing_regressed = False

    # --- Run initial DRC ---
    before_drc = await run_drc_via_cli(pcb_path, ctx=None)
    if before_drc.get("status") != "ok":
        return {"error": f"Initial DRC failed: {before_drc.get('error', 'unknown')}"}

    # DRC result schema: status="ok" implies total_violations present.
    # Default 0 here would silently misreport "fully clean" if the key
    # ever goes missing (schema change, partial result, etc.).
    if "total_violations" not in before_drc:
        return {"error": f"DRC result missing 'total_violations' key (have: {sorted(before_drc.keys())})"}
    before_total = before_drc["total_violations"]
    before_cats = before_drc.get("violation_categories", {})  # empty dict is a legitimate value
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
        result = run_pcbnew_script("""
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
if board is None:
    print(json.dumps({"error": "Failed to load board: " + str(params["pcb_path"])}))
    sys.exit(0)

""" + COURTYARD_BBOX_TUPLE_HELPER + NUDGE_PLACEMENT_HELPER + """

move_count, _ = nudge_overlapping_footprints(board, 0.5, 3)
board.Save(params["pcb_path"])
print(json.dumps({"status": "ok", "move_count": move_count}))
""", params={"pcb_path": pcb_path})
        if result.get("status") == "ok" and result.get("move_count", 0) > 0:
            actions_taken.append(f"placement: nudged {result['move_count']} footprint(s)")

    # --- 2. Fix routing violations ---
    if fix_routing and groups["routing"]:
        # FreeRouter/Java MUST be available BEFORE we touch anything. Clearing
        # routing first and only then discovering we can't re-route would leave
        # the board fully unrouted — strictly worse than the input — while still
        # reporting status="ok". Check availability first; if it's missing, leave
        # the existing routing untouched and record that the fix was skipped.
        from kicad_mcp.tools.pcb_autoroute import (
            _find_freerouter_jar,
            _find_java,
            _run_full_autoroute,
        )

        jar_path = _find_freerouter_jar(None)
        java_path = _find_java()
        if not (jar_path and java_path):
            actions_taken.append(
                "routing: skipped — FreeRouter/Java not available; left existing "
                "routing intact (clearing it would leave the board unrouted)"
            )
        else:
            # Clear existing routing, then re-autoroute.
            clear_result = run_pcbnew_script("""
import pcbnew, json, sys
params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])
if board is None:
    print(json.dumps({"error": "Failed to load board: " + str(params["pcb_path"])}))
    sys.exit(0)
removed = 0
to_remove = []
for track in board.GetTracks():
    to_remove.append(track)
for item in to_remove:
    board.Remove(item)
    removed += 1
board.Save(params["pcb_path"])
print(json.dumps({"status": "ok", "removed": removed}))
""", params={"pcb_path": pcb_path})
            # Subprocess returned {"status": "ok", "removed": N} or {"error": ...}.
            # Default-to-0 would silently re-autoroute an uncleared board, masking
            # the failure with an optimistic action log entry.
            if "error" in clear_result:
                return {"error": f"Failed to clear existing routing: {clear_result['error']}"}
            tracks_cleared = clear_result["removed"]

            route_result = _run_full_autoroute(
                pcb_path=pcb_path,
                jar_path=jar_path,
                java_path=java_path,
                passes=autoroute_passes,
                remove_zones=True,
            )
            if "error" in route_result:
                # Routing was already cleared and saved above (required so
                # _run_full_autoroute has a clean board to route against) --
                # if it then fails, the board now has NO routing at all,
                # strictly worse than the input. Report this plainly rather
                # than logging "re-autorouted" with a "?" unconnected count,
                # which reads as a qualified success instead of a real
                # regression the caller needs to act on.
                routing_regressed = True
                actions_taken.append(
                    f"routing: cleared {tracks_cleared} tracks/vias, but re-autoroute "
                    f"FAILED ({route_result['error']}) -- the board now has NO routing. "
                    "Run autoroute(operation='run') to recover."
                )
            else:
                routing_regressed = False
                incomplete = route_result.get("unconnected_after_routing", "?")
                actions_taken.append(
                    f"routing: cleared {tracks_cleared} tracks/vias, "
                    f"re-autorouted ({autoroute_passes} passes, {incomplete} unconnected)"
                )

    # --- 3. Fix silkscreen ---
    if fix_silkscreen and groups["silkscreen"]:
        silk_result = run_pcbnew_script("""
import pcbnew, json, sys
""" + GEOMETRY_HELPER + """

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
if board is None:
    print(json.dumps({"error": "Failed to load board: " + str(params["pcb_path"])}))
    sys.exit(0)
silk_layer_ids = [board.GetLayerID("F.SilkS"), board.GetLayerID("B.SilkS")]

all_pads = []
for fp in board.GetFootprints():
    for pad in fp.Pads():
        sz = pad.GetBoundingBox()
        all_pads.append({
            "reference": fp.GetReference(),
            "x_min": sz.GetX(), "y_min": sz.GetY(),
            "x_max": sz.GetRight(), "y_max": sz.GetBottom(),
        })

all_silk = []
for fp in board.GetFootprints():
    _ref = fp.GetReference()
    for _ft, _fo in [("reference", fp.Reference()), ("value", fp.Value())]:
        if not _fo.IsVisible() or _fo.GetLayer() not in silk_layer_ids:
            continue
        all_silk.append({"component": _ref, "obj": _fo, "layer": _fo.GetLayer()})
for drawing in board.GetDrawings():
    if hasattr(drawing, 'GetText') and drawing.GetLayer() in silk_layer_ids:
        _vis = drawing.IsVisible() if hasattr(drawing, 'IsVisible') else True
        if _vis:
            all_silk.append({"component": None, "obj": drawing, "layer": drawing.GetLayer()})

if hasattr(board, 'GetBoardEdgesBoundingBox'):
    board_bb = board.GetBoardEdgesBoundingBox()
    board_valid = board_bb.GetWidth() > 0
else:
    board_bb = None
    board_valid = False

def _bbox_to_tuple(b):
    return (b.GetX(), b.GetY(), b.GetRight(), b.GetBottom())

def has_pad_overlap(text_bbox, own_ref):
    text_t = _bbox_to_tuple(text_bbox)
    for pad in all_pads:
        if pad["reference"] == own_ref:
            continue
        if aabb_overlap(text_t, (pad["x_min"], pad["y_min"], pad["x_max"], pad["y_max"])):
            return True
    return False

def has_text_overlap(text_bbox, own_ref, own_layer, own_obj):
    text_t = _bbox_to_tuple(text_bbox)
    for si in all_silk:
        if si["obj"] is own_obj:
            continue
        if si["component"] is not None and si["component"] == own_ref:
            continue
        if si["layer"] != own_layer:
            continue
        visible = si["obj"].IsVisible() if hasattr(si["obj"], 'IsVisible') else True
        if not visible:
            continue
        if aabb_overlap(text_t, _bbox_to_tuple(si["obj"].GetBoundingBox())):
            return True
    return False

def has_any_overlap(text_bbox, own_ref, own_layer, own_obj):
    return has_pad_overlap(text_bbox, own_ref) or has_text_overlap(text_bbox, own_ref, own_layer, own_obj)

def in_board(text_bbox):
    if not board_valid:
        return True
    return aabb_inside(_bbox_to_tuple(text_bbox), _bbox_to_tuple(board_bb))

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

board.Save(params["pcb_path"])
print(json.dumps({"status": "ok", "moved": moved, "hidden": hidden_count,
                    "zones_filled": len(copper_zones)}))
""", params={"pcb_path": pcb_path}, timeout=120.0)
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
    after_ok = after_drc.get("status") == "ok"
    after_total = after_drc.get("total_violations", 0) if after_ok else "error"
    after_cats = after_drc.get("violation_categories", {}) if after_ok else {}

    return {
        # "warning" when the routing-fix step left the board strictly
        # worse than it found it (cleared, then failed to re-route) --
        # "ok" would read as "this completed successfully" to a caller
        # that only checks status, which is exactly wrong here.
        "status": "warning" if routing_regressed else "ok",
        "before": {"total": before_total, "categories": before_cats},
        "after": {"total": after_total, "categories": after_cats},
        "actions_taken": actions_taken,
        "routing_regressed": routing_regressed,
        "improvement": before_total - after_total if isinstance(after_total, int) else None,
    }


