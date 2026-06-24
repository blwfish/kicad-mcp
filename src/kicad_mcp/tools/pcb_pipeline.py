"""PCB pipeline tool: build a routed PCB from a schematic in one step."""

import logging
import math
import os
import re
import shutil
import subprocess
import time
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from fastmcp import FastMCP
from mcp_events import emit_event, event_context

from kicad_mcp.tools.pcb_silkscreen import _op_auto_fix_silkscreen
from kicad_mcp.utils.firmware.intent import load_intent
from kicad_mcp.utils.keepout_helpers import BODY_EXTENT_HELPER, KEEPOUT_HELPER, LIB_SEARCH_HELPER
from kicad_mcp.utils.kicad_cli import KiCadCLIError, get_kicad_cli_path
from kicad_mcp.utils.net_injection import existing_net_codes, inject_net_definitions
from kicad_mcp.utils.netlist_parser import POWER_NET_HELPER, extract_netlist_via_cli
from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
from kicad_mcp.utils.placement.edge_terminal import (
    EDGE_TERMINAL_HELPER,
    distribute_terminals,
    normalize_hint,
)
from kicad_mcp.utils.placement.wire_entry import WIRE_ENTRY, WIRE_ENTRY_HELPER

logger = logging.getLogger(__name__)


def _emit_placement_decision(d: Dict[str, Any]) -> None:
    """Translate one engine placement-decision record into an OOB event.

    The embedded placement script can't emit events directly, so it accumulates
    structured decision dicts and returns them; this maps each to ``emit_event``
    inside the caller's ``event_context``. Unknown event kinds fall through to a
    generic info record rather than being silently dropped."""
    ev = d.get("event")
    ref = d.get("ref")
    if ev == "rotation_chosen":
        emit_event("info", "rotation_chosen",
                   f"{ref} rotated {d.get('angle')}° on the {d.get('edge')} edge "
                   f"(via {d.get('source')})",
                   {"ref": ref, "edge": d.get("edge"), "angle": d.get("angle"),
                    "source": d.get("source")})
    elif ev == "rotation_ambiguous":
        emit_event("warn", "rotation_ambiguous",
                   f"{ref} has symmetric pads; rotation left at 0°", {"ref": ref})
    elif ev == "rotation_fallback":
        emit_event("warn", "rotation_fallback",
                   f"{ref} ({d.get('footprint')}) has no wire-entry data; orientation "
                   f"fell back to the pad-centroid proxy", {"ref": ref,
                   "footprint": d.get("footprint")})
    elif ev == "keepout_overhang":
        emit_event("info", "keepout_overhang",
                   f"{ref} placed with its keepout overhanging the {d.get('edge')} edge "
                   f"(rotated {d.get('angle')}°)",
                   {"ref": ref, "edge": d.get("edge"), "angle": d.get("angle")})
    elif ev == "keepout_fallback_interior":
        emit_event("warn", "keepout_fallback_interior",
                   f"{ref} could not be seated at any edge; placed interior with its "
                   f"keepout NOT overhanging", {"ref": ref})
    elif ev == "terminal_edge_crowded":
        emit_event("warn", "terminal_edge_crowded",
                   f"{ref} packed onto a crowded {d.get('edge')} edge (kept at the "
                   f"edge for wire access; board may be undersized)",
                   {"ref": ref, "edge": d.get("edge")})
    elif ev == "placement_hint_applied":
        emit_event("info", "placement_hint_applied",
                   f"Applied placement hint to {ref}: {d.get('directive')}",
                   {"ref": ref, "directive": d.get("directive")})
    elif ev == "placement_hint_offboard":
        emit_event("warn", "placement_hint_offboard",
                   f"Fixed-position hint for {ref} is off-board or overlaps a "
                   f"placed part; placed as given", {"ref": ref})
    else:
        emit_event("info", "placement_decision", str(d), {"ref": ref})


def _resolve_board_size(
    explicit_w: float, explicit_h: float, intent_source: Optional[Dict[str, Any]],
) -> tuple:
    """Resolve board ``(width, height, from_intent)``.

    Precedence per axis: explicit dimension (>0) > intent ``source.board_size_mm``
    > 0 (auto-estimate downstream). Each axis is resolved independently so a
    caller may fix one and inherit the other. The intent size is honoured only
    when it is a 2-element ``[w, h]`` of positive non-bool numbers; a malformed,
    partial, or non-positive value is ignored (never substitutes). ``[0]`` is
    width, ``[1]`` is height — transpose is a footgun, so the order is fixed."""
    w, h = explicit_w, explicit_h
    from_intent = False
    if (explicit_w > 0 and explicit_h > 0) or not intent_source:
        return w, h, from_intent
    bsz = intent_source.get("board_size_mm")
    if (isinstance(bsz, (list, tuple)) and len(bsz) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                    for v in bsz)):
        if explicit_w <= 0:
            w = float(bsz[0])
            from_intent = True
        if explicit_h <= 0:
            h = float(bsz[1])
            from_intent = True
    return w, h, from_intent


def _merge_placement_hints(
    intent_hints: Optional[Dict[str, Any]], param_hints: Optional[Dict[str, Any]],
) -> tuple:
    """Merge placement hints and validate each. Returns ``(clean, warnings)``.

    Sources merge intent-first, explicit ``param`` second (the caller wins on a
    ref collision). Every directive passes through ``normalize_hint`` — the single
    validation source — so invalid keys/values are dropped and surfaced in
    ``warnings`` (``"<ref>: <reason>"``), never silently substituted."""
    merged: Dict[str, Any] = {}
    if intent_hints:
        merged.update(intent_hints)
    if param_hints:
        merged.update(param_hints)
    clean: Dict[str, Dict[str, Any]] = {}
    warnings: List[str] = []
    for ref, raw in merged.items():
        c, ws = normalize_hint(raw)
        if c:
            clean[ref] = c
        for w in ws:
            warnings.append(f"{ref}: {w}")
    return clean, warnings


# ---------------------------------------------------------------------------
# Internal pipeline step functions (module-level for testability)
# ---------------------------------------------------------------------------


def _step_extract_netlist(sch_path: str) -> Dict[str, Any]:
    """Step 1: Extract components and nets from schematic via kicad-cli."""
    netlist = extract_netlist_via_cli(sch_path)
    if netlist is None:
        return {"error": "kicad-cli not available for netlist export"}

    if "components" not in netlist or "nets" not in netlist:
        return {"error": f"netlist extraction returned malformed result (keys: {sorted(netlist.keys())})"}

    components = netlist["components"]
    nets = netlist["nets"]

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


# The Edge.Cuts removal+redraw sub-script — SINGLE SOURCE. Used by pass 1
# (_step_create_pcb_and_outline, after CreateEmptyBoard) AND by re-fit pass 2
# (_step_redraw_outline, via LoadBoard with NO CreateEmptyBoard, so the placed
# footprints + nets survive). It only touches Edge.Cuts drawings — footprint
# positions and pad nets are untouched. Keeping it in one constant means the two
# call sites can never drift (Rule 3: one source of truth for the outline geometry).
_OUTLINE_REDRAW_SCRIPT = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])

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
w = pcbnew.FromMM(params["width_mm"])
h = pcbnew.FromMM(params["height_mm"])

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

board.Save(params["pcb_path"])
print(json.dumps({"status": "ok"}))
"""


def _step_create_pcb_and_outline(
    pcb_path: str,
    width_mm: float,
    height_mm: float,
    components: Dict[str, Dict],
    holes: Optional[Dict[str, Any]] = None,
    terminal_distribution: str = "single_edge",
) -> Dict[str, Any]:
    """Step 2: Create PCB file and add board outline.

    If width/height are 0, auto-estimates from component footprints. When
    ``holes`` requests corner mounting holes, the estimate reserves a
    per-side inset so the holes don't crowd the content (spec §3 / Phase 5).
    ``terminal_distribution`` ("single_edge"/"multi_edge") chooses whether the
    auto-size spreads field terminals across the side edges; the resulting
    per-terminal ``edge_of`` is returned for the caller to feed to placement.
    """
    # Build footprint list for size estimation if needed
    auto_sized = False
    edge_of: Dict[str, str] = {}
    antenna_side: Optional[str] = None
    measured_comps: List[Dict[str, Any]] = []   # re-fit pass 2 reuses these (§4)
    if width_mm <= 0 or height_mm <= 0:
        fp_specs = []
        for ref, info in components.items():
            fp_str = info["footprint"]
            lib, name = fp_str.split(":", 1)
            fp_specs.append({"library": lib, "footprint_name": name, "ref": ref})

        if not fp_specs:
            return {"error": "No footprints to estimate board size from"}

        # Axis-aware corner reservation (RFE #1): the FULL corner clearance along
        # the terminal edge (so terminals laid out past the corner holes still
        # fit), but only the hole-CENTER inset on the perpendicular depth axis
        # (the holes sit in the empty corner columns there) — full clearance on
        # both axes made the board taller than its content.
        size_result = _estimate_board_size(
            fp_specs, corner_inset_mm=_hole_corner_clear(holes),
            corner_center_inset_mm=_hole_center_inset(holes),
            terminal_distribution=terminal_distribution)
        if "error" in size_result:
            return size_result

        chosen = size_result["size"]   # content-aware (layout fixes the aspect)
        width_mm = chosen["width_mm"]
        height_mm = chosen["height_mm"]
        edge_of = size_result.get("edge_of", {})        # per-terminal edge → hints
        antenna_side = size_result.get("antenna_side")  # for the frame-mismatch guard
        measured_comps = size_result.get("comps", [])   # cached body w/h for pass 2
        auto_sized = True

    # Create the PCB file. Set the minimum through-hole drill to 0.2 mm: an empty
    # board inherits KiCad's 0.3 mm default, but standard ESP32/MCU module
    # footprints carry 0.2 mm thermal-pad vias, so every generated board would
    # otherwise trip `drill_out_of_range` on those vias. 0.2 mm is JLCPCB's
    # standard PTH minimum and the smallest hole these footprints use, so the
    # rule stays meaningful (a smaller user hole is still flagged) while the
    # board is DRC-clean out of the box.
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.CreateEmptyBoard()
ds = board.GetDesignSettings()
ds.m_MinThroughDrill = pcbnew.FromMM(0.2)
board.Save(params["pcb_path"])
print(json.dumps({"status": "ok"}))
"""
    result = run_pcbnew_script(script, params={"pcb_path": pcb_path})
    if "error" in result:
        return result

    # Add board outline (Edge.Cuts redraw — SINGLE SOURCE, shared with pass-2
    # re-fit via _OUTLINE_REDRAW_SCRIPT). Safe here: the board was just created
    # empty, so there is nothing to preserve.
    result = run_pcbnew_script(_OUTLINE_REDRAW_SCRIPT, params={
        "pcb_path": pcb_path,
        "width_mm": width_mm,
        "height_mm": height_mm,
    })
    if "error" in result:
        return result

    return {
        "status": "ok",
        "width_mm": width_mm,
        "height_mm": height_mm,
        "auto_sized": auto_sized,
        "edge_of": edge_of,          # {} unless auto-sized; multi_edge → spans 2-3 edges
        "antenna_side": antenna_side,
        "comps": measured_comps,     # [] unless auto-sized; re-fit pass 2 reuses these
    }


# Designator classes that get EDGE placement (so they reserve perimeter, not
# interior area). SINGLE SOURCE — passed into both embedded scripts (the size
# estimator and the smart-placement loop) as a param so the two can't drift.
# NOTE: "H" (mounting holes) is intentionally NOT here as of v1.1 — holes are
# corner FIXTURES placed by _step_add_mounting_holes and pinned with `fixed`
# hints, not edge-placed connectors. (A stray H from a schematic symbol falls to
# the interior tier; the firmware front end never emits one.)
_EDGE_DESIGNATOR_CLASSES = ("J", "SW", "USB")


# Corner mounting-hole defaults (board.yaml `mounting_holes:` overrides these;
# see sidecar._validate_mounting_holes). count=0 disables holes entirely.
_HOLE_DEFAULTS = {"count": 4, "drill_mm": 3.2, "inset_mm": 3.5, "keepout_mm": 1.5}

# Board re-fit routing slack (SPEC_Post_Placement_Board_Refit §4 / R1). The measured
# interior-cluster bbox already reflects the spacing the placer used, so pass 2
# inflates it by only this modest margin PER SIDE (not the 2.5× area factor) to leave
# routing channels at the cluster boundary. Empirical — tuned against the golden
# routing counts at the eyeball/integration gate; bump it (less shrink) if routing
# starves, never loosen the unrouted bound.
_CLUSTER_ROUTING_MARGIN_MM = 2.0


class _RefitDecline(Exception):
    """Re-fit declined BEFORE any board mutation (empty cluster / not meaningfully
    smaller). No backup was taken, so there is nothing to restore — distinct from a
    post-commit fallback (which restores the .pass1 backup)."""


def _resolve_mounting_holes(mh_source: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a board.yaml ``mounting_holes`` block over the defaults → one
    canonical dict. Pure (no pcbnew). ``None`` (no sidecar / no key) yields the
    full default (4 × M3); ``{}`` likewise; ``count: 0`` disables holes."""
    holes = dict(_HOLE_DEFAULTS)
    if mh_source:
        holes.update({k: mh_source[k] for k in _HOLE_DEFAULTS if k in mh_source})
    return holes


def _hole_corner_clear(holes: Optional[Dict[str, Any]]) -> float:
    """Along-edge distance from a board corner that a mounting hole occupies: the
    hole CENTER is ``inset`` in, and its keepout extends ``drill/2 + keepout``
    further, so the corner is occupied out to their sum. 0 when holes are off.
    SINGLE SOURCE for the ALONG-the-terminal-edge sizing reserve (a corner hole
    eats the seating span) AND terminal layout (start the run past it) — so the
    two never disagree and a terminal can't land on a hole. The PERPENDICULAR
    (depth) sizing axis uses :func:`_hole_center_inset` instead (RFE #1)."""
    if not holes or holes.get("count", 0) <= 0:
        return 0.0
    return float(holes["inset_mm"] + holes["drill_mm"] / 2.0 + holes["keepout_mm"])


def _hole_center_inset(holes: Optional[Dict[str, Any]]) -> float:
    """Hole-CENTER inset from each edge — the depth-axis sizing reserve (RFE #1).
    Perpendicular to the terminal edge the holes sit in the empty corner columns,
    away from the centered cluster and the terminal band, so only the center inset
    (not the full keepout clearance :func:`_hole_corner_clear` gives) needs
    reserving; the full clearance there made the board taller than its content.
    0 when holes are off (mirrors ``_hole_corner_clear`` so both vanish together)."""
    if not holes or holes.get("count", 0) <= 0:
        return 0.0
    return float(holes["inset_mm"])


def _step_add_mounting_holes(pcb_path: str, holes: Dict[str, Any]) -> Dict[str, Any]:
    """Add corner M3 mounting holes + a no-copper keepout per hole.

    Runs after the outline, before placement, so the placer (fed the holes as
    ``fixed`` hints by the caller) keeps clear of the corners. Holes are
    ``MountingHole_3.2mm_M3`` footprints (no net); each gets a square rule-area
    keepout (no tracks/vias/copper-pour — but pads ALLOWED, so the hole's own
    NPTH pad doesn't self-flag) of half-extent ``drill/2 + keepout_mm`` on both
    copper layers. ``count`` 4 = all corners, 2 = TL+BR diagonal, 0 = skip.

    All pcbnew calls here were verified on KiCad 9.0.9 AND 10.0.3: the plain M3
    footprint name, ``LSET().AddLayer`` (the ``&`` operator and 2-arg ctor both
    fail), and the ``Outline().NewOutline()/Append`` zone build.
    """
    count = holes.get("count", 0)
    if count == 0:
        return {"status": "ok", "holes_added": 0, "skipped": "count=0"}

    script = """
import pcbnew, json, os, sys

params = json.loads(open(sys.argv[1]).read())
""" + LIB_SEARCH_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
lib = find_lib("MountingHole")
if not lib:
    print(json.dumps({"error": "MountingHole footprint library not found"}))
    sys.exit(0)

bb = board.GetBoardEdgesBoundingBox()
inset = pcbnew.FromMM(params["inset_mm"])
x0, y0, x1, y1 = bb.GetX(), bb.GetY(), bb.GetRight(), bb.GetBottom()
# TL, TR, BR, BL — clockwise from top-left.
all_corners = [(x0 + inset, y0 + inset), (x1 - inset, y0 + inset),
               (x1 - inset, y1 - inset), (x0 + inset, y1 - inset)]
corners = all_corners if params["count"] == 4 else [all_corners[0], all_corners[2]]

half = pcbnew.FromMM(params["drill_mm"] / 2.0 + params["keepout_mm"])
positions = []
for i, (cx, cy) in enumerate(corners):
    fp = pcbnew.FootprintLoad(lib, "MountingHole_3.2mm_M3")
    if fp is None:
        print(json.dumps({"error": "MountingHole_3.2mm_M3 not found"}))
        sys.exit(0)
    board.Add(fp)
    fp.SetReference("H%d" % (i + 1))
    fp.SetPosition(pcbnew.VECTOR2I(int(cx), int(cy)))
    # No-copper keepout (square) so routing leaves the screw-head annulus clear:
    # disallow tracks, vias, and the copper POUR (zone fills). Do NOT disallow
    # pads — the only pad in here is the mounting hole's OWN NPTH pad (a bare
    # 3.2mm hole, no copper, no net), which would otherwise flag itself as an
    # items_not_allowed DRC error. Disallowing zone fills (was missing) is what
    # actually keeps the GND pour off the annulus — the stated intent.
    z = pcbnew.ZONE(board)
    z.SetIsRuleArea(True)
    z.SetDoNotAllowTracks(True)
    z.SetDoNotAllowVias(True)
    z.SetDoNotAllowPads(False)
    # KiCad 10 renamed SetDoNotAllowCopperPour -> SetDoNotAllowZoneFills; mirror
    # the version-robust getter guard used in pcb_footprints/keepout_helpers so
    # this works on both 9 and 10 (was 10-only, broke the 9.0 integration job).
    if hasattr(z, "SetDoNotAllowZoneFills"):
        z.SetDoNotAllowZoneFills(True)
    else:
        z.SetDoNotAllowCopperPour(True)
    ls = pcbnew.LSET()
    ls.AddLayer(pcbnew.F_Cu)
    ls.AddLayer(pcbnew.B_Cu)
    z.SetLayerSet(ls)
    outline = z.Outline()
    outline.NewOutline()
    for dx, dy in ((-half, -half), (half, -half), (half, half), (-half, half)):
        outline.Append(int(cx + dx), int(cy + dy))
    board.Add(z)
    positions.append({"ref": "H%d" % (i + 1),
                      "x_mm": round(pcbnew.ToMM(int(cx)), 3),
                      "y_mm": round(pcbnew.ToMM(int(cy)), 3)})

board.Save(params["pcb_path"])
print(json.dumps({"status": "ok", "holes_added": len(positions),
                  "positions": positions}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "count": count,
        "drill_mm": holes["drill_mm"],
        "inset_mm": holes["inset_mm"],
        "keepout_mm": holes["keepout_mm"],
    }, timeout=60.0)


# ---------------------------------------------------------------------------
# Board re-fit pass-2 steps (SPEC_Post_Placement_Board_Refit §3, §6).
# These run ONLY when board_refit is on, after a measured-smaller second size.
# ---------------------------------------------------------------------------

def _step_redraw_outline(pcb_path: str, width_mm: float, height_mm: float) -> Dict[str, Any]:
    """Re-draw the Edge.Cuts rectangle to a NEW size on an EXISTING board (re-fit
    pass 2, §3 step 4 / §9-A1).

    Runs the shared :data:`_OUTLINE_REDRAW_SCRIPT` via ``LoadBoard`` — it NEVER
    calls ``CreateEmptyBoard`` (which would wipe the placed footprints + nets), so
    the cluster placed in pass 1 survives and only the board outline changes. The
    subsequent placement pass re-anchors edge terminals + holes to this new outline
    via ``GetBoardEdgesBoundingBox()``."""
    return run_pcbnew_script(_OUTLINE_REDRAW_SCRIPT, params={
        "pcb_path": pcb_path,
        "width_mm": width_mm,
        "height_mm": height_mm,
    })


def _step_measure_cluster(
    pcb_path: str, edge_placed_refs: List[str]
) -> Dict[str, Any]:
    """Measure the placed INTERIOR-cluster rectangle (re-fit §3 step 2).

    Unions the body bboxes (antenna keepout EXCLUDED, same ``body_bbox`` the sizer
    uses) of every footprint that is NEITHER an edge-placed terminal NOR a mounting
    hole — the rectangle the tighter board must hug. The cluster is defined by
    EXCLUSION, not by ``is_terminal`` (§2 blocker):

    - ``edge_placed_refs`` — refs the pass-1 placer anchored to an edge (from its
      ``rotation_chosen`` decisions). These march along the edges, so they are NOT
      part of the interior cluster.
    - ``_ref_class(ref) == "H"`` — corner mounting holes. H is NOT an edge-designator
      class, so an ``is_terminal`` filter would KEEP them and inflate the bbox to the
      full board → the no-op guard fires → the feature silently does nothing.

    Everything else IS cluster — crucially INCLUDING interior module headers (J6 the
    OLED, ``edge:"none"``, spiral-placed inside the cluster): they are ``is_terminal``
    for sizing but physically sit in the cluster, so the measure must count them or
    it under-sizes. Returns ``{cluster_w, cluster_h, counted:[refs]}`` in mm; an
    empty cluster yields ``0×0`` (the caller's no-op guard then declines)."""
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + BODY_EXTENT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])
edge_refs = set(params["edge_placed_refs"])

def _ref_class(ref):
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref

bx0 = by0 = float("inf")
bx1 = by1 = float("-inf")
counted = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    if ref in edge_refs:
        continue                  # edge-placed terminal — anchors to the edge band
    if _ref_class(ref) == "H":
        continue                  # corner hole — would inflate the bbox to full board
    has_keepout = any(z.GetIsRuleArea() for z in fp.Zones()) if hasattr(fp, 'Zones') else False
    fx0, fy0, fx1, fy1 = body_bbox(fp, has_keepout)
    bx0 = min(bx0, fx0); by0 = min(by0, fy0)
    bx1 = max(bx1, fx1); by1 = max(by1, fy1)
    counted.append(ref)

if bx0 == float("inf"):
    print(json.dumps({"status": "ok", "cluster_w": 0.0, "cluster_h": 0.0, "counted": []}))
else:
    print(json.dumps({"status": "ok",
                      "cluster_w": round(bx1 - bx0, 3),
                      "cluster_h": round(by1 - by0, 3),
                      "counted": counted}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "edge_placed_refs": list(edge_placed_refs),
    })


def _step_remove_mounting_holes(pcb_path: str) -> Dict[str, Any]:
    """Delete every ``H*`` mounting-hole footprint AND its keepout zone, so re-fit
    pass 2 can re-add them at the NEW corners via ``_step_add_mounting_holes``
    without duplicating (H5–H8) or leaving stale keepouts (re-fit §6).

    The hole keepouts are ANONYMOUS board-level rule-area ZONEs (no ref linkage to
    their H* footprint, §6 blocker), so they are identified by type: every
    board-level zone with ``GetIsRuleArea()``. This is safe because the only other
    rule-area keepout — the MCU antenna — lives as a FOOTPRINT child zone
    (``fp.Zones()``), not a board-level zone; and GND copper pours are NOT rule
    areas and are added only AFTER autoroute, so none exist at pass-2 time. The
    script asserts that invariant by reporting the removed zone count."""
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])

def _ref_class(ref):
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref

# Collect-then-remove (a SWIG container must NOT be mutated while iterating, and
# wrapping it in list() yields raw SwigPyObjects with no typed methods — so iterate
# cleanly first, gather, then Remove). Mirrors pcb_autoroute._export_dsn's zone loop.
fps_to_remove = []
for fp in board.GetFootprints():
    if _ref_class(fp.GetReference()) == "H":
        fps_to_remove.append(fp)
removed_fps = [fp.GetReference() for fp in fps_to_remove]
for fp in fps_to_remove:
    board.Remove(fp)

# Anonymous board-level rule-area keepouts (the hole keepouts). The antenna keepout
# is a FOOTPRINT child zone, not board-level, so it is untouched here.
zones_to_remove = []
for z in board.Zones():
    if z.GetIsRuleArea():
        zones_to_remove.append(z)
removed_zones = len(zones_to_remove)
for z in zones_to_remove:
    board.Remove(z)

board.Save(params["pcb_path"])
print(json.dumps({"status": "ok", "removed_footprints": removed_fps,
                  "removed_zones": removed_zones}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path}, timeout=60.0)


def _content_aware_size(
    components: List[Dict[str, Any]],
    terminal_edge_horizontal: bool = True,
    routing_factor: float = 2.5,
    padding: float = 2.0,
    spacing: float = 1.0,
    corner_inset_mm: float = 0.0,
    corner_center_inset_mm: float = 0.0,
) -> Dict[str, Any]:
    """Content-aware board size (spec §4) — pure, unit-tested without KiCad.

    Models the human-rational layout: the interior parts (MCU + actives +
    passives + interior headers) pack into a routed square cluster
    (``area × routing_factor``); ALL field-wiring terminals march along ONE edge
    — the one opposite the antenna (§1) — so the board's dimension ALONG that edge
    must be long enough to seat them end-to-end, and the perpendicular dimension
    gains a single terminal-DEPTH band. Antenna keepouts are NOT counted (they
    overhang, §2). ``terminal_edge_horizontal`` = the terminal edge is the
    top/bottom (so terminals fit along WIDTH); else left/right (along HEIGHT).
    Returns one ``{width_mm, height_mm}`` (no aspect choices — the layout fixes
    the aspect)."""
    interior = [c for c in components if not c.get("is_terminal")]
    terminals = [c for c in components if c.get("is_terminal")]
    interior_area = sum(c["w"] * c["h"] for c in interior)
    max_interior_dim = max((max(c["w"], c["h"]) for c in interior), default=0.0)
    cluster = math.sqrt(interior_area * routing_factor) if interior_area > 0 else 0.0
    cluster = max(cluster, max_interior_dim)
    # Terminals: total span ALONG their shared edge, and their inward DEPTH.
    term_along = sum(max(c["w"], c["h"]) + spacing for c in terminals)
    term_depth = max((min(c["w"], c["h"]) for c in terminals), default=0.0)
    # Corner mounting holes reserve real estate ASYMMETRICALLY (RFE #1). ALONG the
    # terminal edge a corner hole's full keepout footprint eats into the seating
    # span, so the edge grows by the FULL corner clearance at each end
    # (``corner_inset_mm`` = inset + drill/2 + keepout). On the PERPENDICULAR depth
    # axis the holes sit in the empty corner columns — clear of the centered cluster
    # and the terminal band — so only the hole-CENTER inset is reserved
    # (``corner_center_inset_mm`` = inset). Reserving the full clearance on BOTH
    # axes (the old behaviour) made the board taller than its content needed.
    along = max(cluster, term_along) + 2 * padding + 2 * corner_inset_mm
    depth = cluster + term_depth + 2 * padding + 2 * corner_center_inset_mm
    if terminal_edge_horizontal:
        w, h = along, depth
    else:
        w, h = depth, along
    return {"width_mm": math.ceil(w), "height_mm": math.ceil(h)}


def _estimate_board_size(
    fp_specs: List[Dict[str, str]], corner_inset_mm: float = 0.0,
    corner_center_inset_mm: float = 0.0, terminal_distribution: str = "single_edge",
    side_silk_gap_mm: float = 2.5,
    cluster_wh: Optional[Tuple[float, float]] = None,
    comps: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Estimate board dimensions from footprint specs. The bridge script only
    MEASURES (body w/h + whether the ref is an edge terminal); the content-aware
    math lives in the pure :func:`_content_aware_size` so it is testable and
    shared. ``fp_specs`` entries carry ``ref`` for the terminal classification.

    Board re-fit (SPEC_Post_Placement_Board_Refit §4): when ``comps`` (the measured
    footprint list returned by a prior call) is supplied, the bridge measurement
    script is SKIPPED and those cached comps reused — pass 2 must not re-measure.
    ``cluster_wh`` overrides the square-cluster interior estimate with a measured
    ``(w, h)`` rectangle. The result ALWAYS includes ``comps`` so the caller can
    hand them to a second pass."""
    script = """
import pcbnew, json, os, sys

params = json.loads(open(sys.argv[1]).read())
fp_specs = params["fp_specs"]

""" + LIB_SEARCH_HELPER + BODY_EXTENT_HELPER + """

# Edge designator classes come from params (single source) and the prefix is
# extracted EXACTLY as _ref_class does in smart placement (up to the first
# digit), so the two stay consistent — a ref like "SW_A1" classifies identically.
_EDGE_CLASSES = set(params["edge_classes"])
def _ref_class(ref):
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref
def _is_terminal(ref):
    return _ref_class(ref) in _EDGE_CLASSES

components = []
errors = []
for spec in fp_specs:
    lib_name = spec["library"]
    fp_name = spec["footprint_name"]
    lib_path = find_lib(lib_name)
    if not lib_path:
        errors.append(f"Library '{lib_name}' not found")
        continue
    fp = pcbnew.FootprintLoad(lib_path, fp_name)
    if fp is None:
        errors.append(f"Footprint '{fp_name}' not found in '{lib_name}'")
        continue
    # Body size EXCLUDING the antenna keepout (it overhangs off-board, spec §2,
    # so it must not inflate the estimate — the old GetBoundingBox term over-sized
    # antenna boards). Same shared measurement as the placement collection loop.
    has_keepout = any(z.GetIsRuleArea() for z in fp.Zones()) if hasattr(fp, 'Zones') else False
    bx0, by0, bx1, by1 = body_bbox(fp, has_keepout)
    w = round(bx1 - bx0, 2)
    h = round(by1 - by0, 2)
    # Antenna side in the footprint's 0° frame (the tier-1 placer overhangs it on
    # that board edge): which side the rule-area keepout extends toward. Used to
    # decide the terminal edge (terminals go OPPOSITE — same axis as the antenna).
    keepout_side = None
    if has_keepout:
        z = next(z for z in fp.Zones() if z.GetIsRuleArea())
        zb = z.GetBoundingBox()
        # RELATIVE to the footprint origin (same formula as the tier-1 placer —
        # FootprintLoad's origin is (0,0) so this matches today, but subtracting it
        # keeps the two sources of "antenna side" from drifting if it ever isn't).
        fp_pos = fp.GetPosition()
        ox, oy = pcbnew.ToMM(fp_pos.x), pcbnew.ToMM(fp_pos.y)
        dxn = pcbnew.ToMM(zb.GetX()) - ox; dyn = pcbnew.ToMM(zb.GetY()) - oy
        dxp = pcbnew.ToMM(zb.GetRight()) - ox; dyp = pcbnew.ToMM(zb.GetBottom()) - oy
        em = {"left": abs(dxn), "right": abs(dxp), "top": abs(dyn), "bottom": abs(dyp)}
        keepout_side = max(em, key=em.get)
    components.append({"footprint": fp_name, "w": w, "h": h,
                       "ref": spec.get("ref", ""),
                       "is_terminal": _is_terminal(spec.get("ref", "")),
                       "keepout_side": keepout_side})

print(json.dumps({"components": components, "errors": errors}))
"""
    # Pass 1 measures via the bridge script; pass 2 (re-fit) supplies cached
    # ``comps`` and SKIPS the measurement entirely — the measured body w/h +
    # keepout_side are reused verbatim (only ``cluster_wh`` changes the math).
    if comps is None:
        result = run_pcbnew_script(script, params={
            "fp_specs": fp_specs, "edge_classes": list(_EDGE_DESIGNATOR_CLASSES)})
        if "error" in result:
            return result
        comps = result.get("components", [])
        measure_errors = result.get("errors", [])
    else:
        measure_errors = []
    if not comps:
        return {"error": "No valid footprints found", "details": measure_errors}
    # Distribute terminals across edge(s) — the SINGLE source for BOTH the board
    # size and the per-terminal edge assignment (single_edge collapses to today's
    # _content_aware_size; the regression-lock test pins this). The returned
    # ``edge_of`` is threaded to placement as {"edge": E} hints (parent-side, so it
    # never re-encodes the rule in the embedded script — Rule 3).
    antenna_side = next((c["keepout_side"] for c in comps if c.get("keepout_side")), None)
    dist = distribute_terminals(
        comps, antenna_side, mode=terminal_distribution,
        corner_inset_mm=corner_inset_mm,
        corner_center_inset_mm=corner_center_inset_mm,
        side_silk_gap_mm=side_silk_gap_mm,
        cluster_wh=cluster_wh)
    return {
        "status": "ok",
        "size": dist["size"],
        "edge_of": dist["edge_of"],
        "primary_edge": dist["primary_edge"],
        "antenna_side": antenna_side,
        "comps": comps,
        "errors": measure_errors,
        "error_count": len(measure_errors),
    }


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

    script = """
import pcbnew, json, os, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
placements = params["placements"]

""" + LIB_SEARCH_HELPER + """

placed = []
errors = []

# Stack all footprints at (5, 5) — step 5 will move them
for p in placements:
    lib_path = find_lib(p["library"])
    if not lib_path:
        errors.append(f"Library '{p['library']}' not found for {p['ref']}")
        continue

    fp = pcbnew.FootprintLoad(lib_path, p["footprint_name"])
    if fp is None:
        errors.append(f"Footprint '{p['footprint_name']}' not found for {p['ref']}")
        continue

    fp.SetReference(p["ref"])
    fp.SetValue(p["value"])
    fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(5)))
    board.Add(fp)
    placed.append(p["ref"])

board.Save(params["pcb_path"])

print(json.dumps({
    "status": "ok",
    "placed_count": len(placed),
    "errors": errors,
    "error_count": len(errors),
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "placements": placements,
    }, timeout=30.0)


def _finalize_pad_assignment(result: Dict[str, Any], pads_requested: int) -> Dict[str, Any]:
    """Guard the pad-assignment step against an electrically-DEAD board.

    Pure (no pcbnew) so the decision is unit-testable. If pads were requested but
    NONE were assigned, the board has no pad↔net associations — promote to a hard
    error (an ``error`` key, which the pipeline's _record halts on) instead of
    autorouting a dead board with status:ok (h-pad-deadboard; the net-injection
    half already guards its analogous failure). Partial loss is surfaced as a
    capped warning, not hidden.
    """
    result["pads_requested"] = pads_requested
    if result.get("status") != "ok" or pads_requested == 0:
        return result
    assigned = result.get("pads_assigned", 0)
    errs = result.get("assignment_errors", [])
    if assigned == 0:
        result["status"] = "error"
        result["error"] = (
            f"0 of {pads_requested} pad assignments succeeded — the board would be "
            f"electrically dead. First errors: {errs[:5]}"
        )
    elif assigned < pads_requested:
        result.setdefault("warnings", []).append(
            f"{pads_requested - assigned} of {pads_requested} pad assignments failed: "
            f"{errs[:5]}" + (" …" if len(errs) > 5 else "")
        )
    return result


def _routed_incomplete_count(step: Dict[str, Any]) -> int | None:
    """Reported unconnected-net count after autoroute (pure; unit-testable).

    The SES-import-MEASURED ``unconnected_after_routing`` is the source of
    truth — read back from the actual routed board by KiCad's connectivity
    engine, immune to FreeRouter stdout wording. ``None`` if the autoroute step
    didn't report it (e.g. it errored before import).
    """
    return step.get("unconnected_after_routing")  # type: ignore[no-any-return]


def _step_inject_nets_and_assign_pads(
    pcb_path: str,
    nets: Dict[str, List],
) -> Dict[str, Any]:
    """Step 4: Inject nets into PCB file and assign pads.

    Implements the net-injection + pad-assignment pattern used internally
    by build_pcb_from_schematic.
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

    # Inject nets via direct file editing (pcbnew prunes unused nets).
    # Insertion point + line formatting live in utils/net_injection (single
    # source of truth, shared with pcb_nets.add_net). This is what carries the
    # KiCad-10 (net 0 "")-absence fallback — without it, fresh K10 boards got
    # 0 nets injected and built electrically-dead while returning status:ok.
    with open(pcb_path, "r") as f:
        pcb_content = f.read()

    existing_nets = {name: code for code, name in existing_net_codes(pcb_content)}

    max_code = max(existing_nets.values()) if existing_nets else 0
    nets_created = []

    for net_name in net_definitions:
        if net_name not in existing_nets:
            max_code += 1
            existing_nets[net_name] = max_code
            nets_created.append(net_name)

    if nets_created:
        new_content = inject_net_definitions(
            pcb_content,
            [(existing_nets[name], name) for name in nets_created],
        )
        if new_content is None:
            return {
                "status": "error",
                "error": "Could not find insertion point in PCB file",
                "nets_created": 0,
                "total_nets": len(net_definitions),
            }
        pcb_content = new_content
        with open(pcb_path, "w") as f:
            f.write(pcb_content)

    # Assign pads via pcbnew
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
assignments = params["assignments"]
assigned = []
assign_errors = []

for a in assignments:
    ref = a["reference"]
    pad_num = a["pad"]
    net_name = a["net"]

    fp = board.FindFootprintByReference(ref)
    if fp is None:
        assign_errors.append(f"Footprint {ref} not found in PCB")
        continue

    net = board.FindNet(net_name)
    if net is None or net.GetNetCode() == 0:
        assign_errors.append(f"Net {net_name} not found")
        continue

    pad_count = 0
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            pad.SetNet(net)
            pad_count += 1
    if pad_count > 0:
        assigned.append({"reference": ref, "pad": pad_num, "net": net_name})
    else:
        assign_errors.append(f"Pad {pad_num} not found on {ref}")

board.Save(params["pcb_path"])

print(json.dumps({
    "status": "ok",
    "pads_assigned": len(assigned),
    "assignment_errors": assign_errors,
}))
"""
    result = run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "assignments": pad_assignments,
    }, timeout=60.0)

    # Merge counts, then guard against an electrically-dead board (all pad
    # assignments failed) — see _finalize_pad_assignment.
    result["nets_created"] = len(nets_created)
    result["total_nets"] = len(net_definitions)
    return _finalize_pad_assignment(result, len(pad_assignments))


def _step_smart_placement(
    pcb_path: str,
    nets: Dict[str, List],
    spacing_mm: float = 1.0,
    placement_hints: Optional[Dict[str, Dict[str, Any]]] = None,
    corner_clear_mm: float = 0.0,
    terminal_anchor: str = "start",
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

    script = """
import pcbnew, json, math, sys

params = json.loads(open(sys.argv[1]).read())
""" + KEEPOUT_HELPER + POWER_NET_HELPER + EDGE_TERMINAL_HELPER + WIRE_ENTRY_HELPER + BODY_EXTENT_HELPER + """

board = pcbnew.LoadBoard(params["pcb_path"])
spacing = params["spacing_mm"]
corner_clear = params.get("corner_clear_mm", 0.0)  # corner space the mounting holes occupy
terminal_anchor = params.get("anchor", "start")    # "start" | "center" along each edge
outline = get_board_outline(board)

if not outline:
    print(json.dumps({"status": "ok", "message": "No board outline, skipping placement"}))
    raise SystemExit(0)

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
fp_info = {}
for fp in board.GetFootprints():
    ref = fp.GetReference()
    fp_pos = fp.GetPosition()
    fp_x = pcbnew.ToMM(fp_pos.x)
    fp_y = pcbnew.ToMM(fp_pos.y)

    # --- Keepout (rule-area) zones — detected FIRST because it conditions the
    # body extent below (an antenna keepout must be free to overhang the edge). ---
    has_keepout = False
    keepout_side = None
    keepout_rel = None  # relative bbox (dx_min, dy_min, dx_max, dy_max)
    # Guard fp.Zones() — absent on older pcbnew API. Other AttributeErrors
    # (e.g. zone.GetIsRuleArea missing) propagate rather than silently dropping
    # keepout classification, which would mistier components into tier 4.
    if hasattr(fp, 'Zones'):
        for zone in fp.Zones():
            if zone.GetIsRuleArea():
                has_keepout = True
                zbb = zone.GetBoundingBox()
                dx_min = pcbnew.ToMM(zbb.GetX()) - fp_x
                dy_min = pcbnew.ToMM(zbb.GetY()) - fp_y
                dx_max = pcbnew.ToMM(zbb.GetRight()) - fp_x
                dy_max = pcbnew.ToMM(zbb.GetBottom()) - fp_y
                keepout_rel = (dx_min, dy_min, dx_max, dy_max)
                extents_map = {"left": abs(dx_min), "right": abs(dx_max),
                               "top": abs(dy_min), "bottom": abs(dy_max)}
                keepout_side = max(extents_map, key=extents_map.get)

    # Body extent for board containment — pads + body outline, NEVER Zones(), so
    # an antenna keepout is free to overhang the edge (tier-1 seats the body
    # flush; keepout_rel is tracked separately via place_at -> keepout_boxes).
    # Shared single-source measurement (body_bbox / BODY_EXTENT_HELPER).
    bx0, by0, bx1, by1 = body_bbox(fp, has_keepout)
    ext_left  = fp_x - bx0
    ext_right = bx1 - fp_x
    ext_top   = fp_y - by0
    ext_bot   = by1 - fp_y

    # Pad bounding boxes (absolute mm) → pad-centroid offset + pad extent from
    # the footprint origin. The edge-terminal heuristic rotates a connector so
    # its pad side faces INWARD (wire/screw side overhangs the edge); the pad
    # extent lets terminals overhang with their copper still on-board.
    pad_boxes = []
    for pad in fp.Pads():
        pbb = pad.GetBoundingBox()
        pad_boxes.append((
            pcbnew.ToMM(pbb.GetX()), pcbnew.ToMM(pbb.GetY()),
            pcbnew.ToMM(pbb.GetRight()), pcbnew.ToMM(pbb.GetBottom()),
        ))
    pad_cx, pad_cy = pad_centroid_offset(pad_boxes, (fp_x, fp_y))
    pad_el, pad_er, pad_et, pad_eb = pad_extent(pad_boxes, (fp_x, fp_y))

    # Footprint item name (the only stable id surviving schematic->PCB) → keys
    # the wire-entry table for terminal orientation (replaces the pad-centroid
    # proxy for known families; see WIRE_ENTRY_HELPER / the rotation branch).
    try:
        fpname = fp.GetFPID().GetLibItemName()
    except (AttributeError, RuntimeError):
        # Narrow: only a missing/!older pcbnew API or a malformed FPID. Anything
        # else propagates rather than silently mis-keying the wire-entry lookup.
        # An empty name -> table miss -> a flagged rotation_fallback at use-time.
        fpname = ""

    w = ext_left + ext_right
    h = ext_top + ext_bot
    fp_info[ref] = {
        "ext_left": round(ext_left, 2), "ext_right": round(ext_right, 2),
        "ext_top": round(ext_top, 2), "ext_bot": round(ext_bot, 2),
        "width": round(w, 2), "height": round(h, 2),
        "has_keepout": has_keepout, "keepout_side": keepout_side,
        "keepout_rel": keepout_rel,
        "area": round(w * h, 2),
        "pad_cx": round(pad_cx, 4), "pad_cy": round(pad_cy, 4),
        "pad_ext": [round(pad_el, 3), round(pad_er, 3),
                    round(pad_et, 3), round(pad_eb, 3)],
        "footprint": str(fpname),
    }

if not fp_info:
    print(json.dumps({"status": "ok", "message": "No footprints to place"}))
    raise SystemExit(0)

# --- Build connectivity from netlist ---
net_members = params["net_members"]
# Per-ref placement hints (validated parent-side by normalize_hint): a subset
# of {edge, rotation, fixed}. Absent ref → heuristic. Single source of truth
# for placement overrides; the engine never silently substitutes a default.
placement_hints = params.get("placement_hints", {})
# Wire-entry table (data, single-source via JSON; keyed by normalize_family from
# WIRE_ENTRY_HELPER). Resolves terminal orientation for known footprint families;
# unknown families fall back to the pad-centroid proxy with a logged event.
wire_entry_table = {k: tuple(v) for k, v in (params.get("wire_entry") or {}).items()}
connectivity = {}
conn_score = {ref: 0.0 for ref in fp_info}

for net_name, members in net_members.items():
    weight = 0.1 if is_power_net(net_name) else 1.0
    placed_members = [m for m in members if m in fp_info]
    for i in range(len(placed_members)):
        for j in range(i + 1, len(placed_members)):
            a, b = placed_members[i], placed_members[j]
            key = (min(a, b), max(a, b))
            connectivity[key] = connectivity.get(key, 0.0) + weight
            conn_score[a] = conn_score.get(a, 0.0) + weight
            conn_score[b] = conn_score.get(b, 0.0) + weight

# --- Classify into tiers ---
# Tier classification by KiCad designator class — the letter prefix before
# the first digit. `startswith("H")` would match "HV1" (HV regulator) etc.;
# the designator class for "HV1" is "HV", not "H".
EDGE_CLASSES = set(params["edge_classes"])  # single source (see _EDGE_DESIGNATOR_CLASSES)

def _ref_class(ref):
    for i, c in enumerate(ref):
        if c.isdigit():
            return ref[:i]
    return ref

tier1, tier2, tier3, tier4 = [], [], [], []

for ref, info in fp_info.items():
    cls = _ref_class(ref)
    if "fixed" in placement_hints.get(ref, {}):
        # An explicit fixed-coordinate hint is an ABSOLUTE override — honor it
        # regardless of class. tier2 owns the fixed-placement path; without this a
        # non-edge-class ref with fixed coords (e.g. an "H" mounting hole, no
        # longer an edge class) would fall to the interior tier and be
        # spiral-placed, silently ignoring its hint.
        tier2.append(ref)
    elif info["has_keepout"]:
        tier1.append(ref)
    elif cls in EDGE_CLASSES:
        tier2.append(ref)
    elif cls in ("U", "Q") and info["area"] > 50:
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
placements = {}            # ref -> (x_mm, y_mm, angle_deg)
placed_boxes = []
keepout_boxes = []  # absolute keepout zone bboxes
# Seed board-level rule-area keepouts (e.g. mounting-hole keep-clears added by
# _step_add_mounting_holes before this script runs).  fp.Zones() only returns
# zones OWNED by a footprint; board.Zones() is needed for board-level zones.
for _bz in board.Zones():
    if _bz.GetIsRuleArea():
        _bzbb = _bz.GetBoundingBox()
        keepout_boxes.append((
            pcbnew.ToMM(_bzbb.GetX()), pcbnew.ToMM(_bzbb.GetY()),
            pcbnew.ToMM(_bzbb.GetRight()), pcbnew.ToMM(_bzbb.GetBottom()),
        ))
placement_decisions = []   # structured rotation/hint events → response envelope

def rotate_keepout(krel, theta):
    # Rotate a relative keepout bbox (dx0,dy0,dx1,dy1) about the origin by theta
    # and re-derive an axis-aligned bbox. Uses the SAME _rotate_vec as
    # rotate_extents, so keepout and courtyard rotate by one convention.
    if not krel:
        return krel
    dx0, dy0, dx1, dy1 = krel
    xs = []
    ys = []
    for cxv in (dx0, dx1):
        for cyv in (dy0, dy1):
            rx, ry = _rotate_vec(cxv, cyv, theta)
            xs.append(rx)
            ys.append(ry)
    return (min(xs), min(ys), max(xs), max(ys))

def get_extents(ref):
    info = fp_info[ref]
    return info["ext_left"], info["ext_right"], info["ext_top"], info["ext_bot"]

def fits_on_board(cx, cy, el, er, et, eb):
    # Strict containment via canonical aabb_inside — touching the margin
    # boundary counts as not fitting (consistent with rect_inside semantics
    # in geometry.py). The footprint box must be strictly inside the
    # margin-shrunk board outline.
    fp_box = (cx - el, cy - et, cx + er, cy + eb)
    safe = (board_xmin + margin, board_ymin + margin,
            board_xmax - margin, board_ymax - margin)
    return aabb_inside(fp_box, safe)

def collides(cx, cy, el, er, et, eb):
    box = (cx - el - spacing, cy - et - spacing, cx + er + spacing, cy + eb + spacing)
    for pb in placed_boxes:
        if aabb_overlap(box, pb):
            return True
    return False

def hits_keepout(cx, cy, el, er, et, eb):
    box = (cx - el, cy - et, cx + er, cy + eb)
    for kz in keepout_boxes:
        if aabb_overlap(box, kz):
            return True
    return False

def place_at(ref, cx, cy, angle=0, rot_ext=None, rot_keepout=None):
    # rot_ext / rot_keepout carry the rotated courtyard / keepout for rotated
    # connectors so collision tracking uses the as-placed footprint, not the 0°
    # one. Absent → use the footprint's stored (0°) extents.
    info = fp_info[ref]
    if rot_ext is None:
        el, er, et, eb = info["ext_left"], info["ext_right"], info["ext_top"], info["ext_bot"]
    else:
        el, er, et, eb = rot_ext
    placements[ref] = (round(cx, 2), round(cy, 2), int(angle))
    placed_boxes.append((cx - el, cy - et, cx + er, cy + eb))
    # Track keepout zones in absolute coords
    krel = rot_keepout if rot_keepout is not None else info["keepout_rel"]
    if krel:
        dx0, dy0, dx1, dy1 = krel
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
    # placements values are (x, y, angle); partner targeting needs only (x, y).
    return (best_pos[0], best_pos[1]) if best_pos else (board_cx, board_cy)

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

# --- Tier 1: Keepout components → edge placement, ANTENNA OVERHANGING ---
# The MCU is rotated so its keepout (antenna) faces OFF the chosen edge and the
# body sits flush inside; the keepout polygon then overhangs Edge.Cuts (the
# antenna radiates into free space — DRC-clean, verified on KiCad 9 & 10). We
# prefer the keepout's natural side (keepout_side); the first edge that seats the
# body wins, so the antenna doesn't get dragged inboard by partner proximity.
for ref in tier1:
    el0, er0, et0, eb0 = get_extents(ref)
    info = fp_info[ref]
    ks = info["keepout_side"] or "top"
    krel = info["keepout_rel"]
    # Antenna direction in the footprint's 0° frame: origin → keepout centre.
    if krel:
        kdir = ((krel[0] + krel[2]) / 2.0, (krel[1] + krel[3]) / 2.0)
    else:
        kdir = (0.0, 0.0)
    target = find_partner_pos(ref)
    placed_t1 = False
    for edge in [ks] + [e for e in ("top", "bottom", "left", "right") if e != ks]:
        # Rotate so the keepout faces this edge's OUTWARD normal (overhang).
        ang = rotation_to_face(kdir, outward_normal(edge))
        el, er, et, eb = rotate_extents((el0, er0, et0, eb0), ang)
        rkeep = rotate_keepout(krel, ang)
        best = None
        best_dist = float("inf")
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
            place_at(ref, best[0], best[1], ang, (el, er, et, eb), rkeep)
            placement_decisions.append({"event": "keepout_overhang", "ref": ref,
                                        "edge": edge, "angle": ang})
            placed_t1 = True
            break
    if not placed_t1:
        # No edge could seat the body — try interior at 0° (keepout still tracked
        # via place_at). The antenna does NOT overhang here. Only FLAG
        # keepout_fallback_interior once the interior grid actually seats it; if
        # the board is too full (fb is None) the component stays unplaced and is
        # surfaced via failed_placements, not as a misleading "fallback" event for
        # a part that never landed on the board.
        fb = fallback_grid(el0, er0, et0, eb0)
        if fb:
            place_at(ref, fb[0], fb[1])
            placement_decisions.append({"event": "keepout_fallback_interior", "ref": ref})

# --- Tier 2: Connectors → hint-aware, ordered, pad-inward edge placement ---
# Three dispositions per connector (hint = validated {edge,rotation,fixed}):
#   fixed:[x,y]  → absolute coords (skip edge logic)
#   edge:"none"  → interior, like a passive (module headers belong near their IC)
#   otherwise    → an edge (hint, else nearest to partner), ordered + rotated
# J terminals rotate so their pad side faces INWARD (wire side overhangs the
# board edge); SW/H/USB edge-place but never rotate. Layout is ordered by
# natural ref key so J1..J10 march along the edge, not scatter by proximity.
board_box = (board_xmin, board_ymin, board_xmax, board_ymax)

def overlaps_prior(boxes, cx, cy, el, er, et, eb):
    # True if this footprint (spacing-buffered) overlaps any box in `boxes`.
    # Edge layout checks only boxes placed BEFORE the current edge (tier-1 +
    # prior edges); the current edge's members are already spaced by
    # layout_along_edge, so buffering them against each other would false-positive.
    box = (cx - el - spacing, cy - et - spacing, cx + er + spacing, cy + eb + spacing)
    for pb in boxes:
        if aabb_overlap(box, pb):
            return True
    return False

def is_field_terminal(ref):
    # A FIELD-WIRING terminal = a J connector whose footprint is a known
    # wire-entry family (e.g. an MKDS screw terminal). ONLY these get the
    # human-rational treatment (antenna-opposite edge, seat-on-board, force-edge);
    # plug-in module headers / USB / switches keep their original placement, so
    # boards without field wiring are byte-for-byte unchanged by this feature.
    return (is_screw_terminal_class(_ref_class(ref))
            and normalize_family(fp_info[ref].get("footprint", "")) in wire_entry_table)

edge_groups = {"top": [], "bottom": [], "left": [], "right": []}
t2_interior = []
t2_fixed = []
t2_terminals = []   # field-wiring terminals: edge-assigned by rule (below)
for ref in tier2:
    hint = placement_hints.get(ref, {})
    if "fixed" in hint:
        t2_fixed.append(ref)
    elif hint.get("edge") == "none":
        t2_interior.append(ref)
    elif hint.get("edge") in ("top", "bottom", "left", "right"):
        edge_groups[hint["edge"]].append(ref)
    elif is_field_terminal(ref):
        # FIELD-WIRING terminal (a known wire-entry family, e.g. MKDS screw
        # terminal) → antenna-opposite rule below. The RFI rule is about external
        # field wires; plug-in MODULE headers (vertical pin headers, no wire-entry
        # family) are NOT field-wiring, so they keep partner-proximity placement.
        t2_terminals.append(ref)
    else:
        edge_groups[nearest_edge(find_partner_pos(ref), board_box)].append(ref)

# --- Rule-based terminal edge assignment (spec §1) ---
# Field-wiring terminals go on the edge OPPOSITE the MCU antenna (RFI — keep field
# wires away from the radio), spilling to the SIDE edges by CAPACITY, NEVER the
# antenna edge and NEVER the interior. This replaces nearest_edge(partner), which
# piled every terminal on whichever edge the (top-heavy) components clustered near
# and overflowed the rest into the interior (J5/J7 buried mid-board).
antenna_edge = next((d["edge"] for d in placement_decisions
                     if d.get("event") == "keepout_overhang"), None)
_OPP = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
if antenna_edge:
    _perp = ["left", "right"] if antenna_edge in ("top", "bottom") else ["top", "bottom"]
    pref_edges = [_OPP[antenna_edge]] + _perp     # antenna edge excluded entirely
else:
    pref_edges = ["bottom", "left", "right", "top"]
_elen = {"top": board_xmax - board_xmin, "bottom": board_xmax - board_xmin,
         "left": board_ymax - board_ymin, "right": board_ymax - board_ymin}
_eused = {e: 2 * margin for e in _elen}           # both ends start margin-inset
for ref in sorted(t2_terminals, key=natural_ref_key):
    _info = fp_info[ref]
    _along = max(_info["width"], _info["height"]) + spacing   # span along the edge
    _chosen = next((e for e in pref_edges if _eused[e] + _along <= _elen[e]), None)
    if _chosen is None:                           # no edge has room → least-full one
        _chosen = min(pref_edges, key=lambda e: _eused[e])
        placement_decisions.append({"event": "terminal_edge_crowded",
                                    "ref": ref, "edge": _chosen})
    edge_groups[_chosen].append(ref)
    _eused[_chosen] += _along

# Fixed-coordinate connectors (explicit override): place as given. A rotation
# hint is honored (extents/keepout rotate with it); off-board / collision /
# keepout violations are flagged but still placed — the user's explicit coords
# win (they may know something the heuristic doesn't).
for ref in t2_fixed:
    hint = placement_hints[ref]
    fx, fy = hint["fixed"]
    ang = hint.get("rotation", 0)
    rext = rotate_extents(get_extents(ref), ang)
    el, er, et, eb = rext
    rkeep = rotate_keepout(fp_info[ref]["keepout_rel"], ang)
    placement_decisions.append({"event": "placement_hint_applied", "ref": ref,
                                "directive": dict(hint)})
    if (not fits_on_board(fx, fy, el, er, et, eb)
            or collides(fx, fy, el, er, et, eb)
            or hits_keepout(fx, fy, el, er, et, eb)):
        placement_decisions.append({"event": "placement_hint_offboard", "ref": ref})
    place_at(ref, fx, fy, ang, rext, rkeep)

# Edge groups: order by ref, choose rotation, lay out along the edge.
for edge in ("top", "bottom", "left", "right"):
    group = sorted(edge_groups[edge], key=natural_ref_key)
    if not group:
        continue
    prior_boxes = list(placed_boxes)  # tier-1 + already-laid edges (not this one)
    items = []
    rot_by_ref = {}
    rot_source = {}
    for ref in group:
        info = fp_info[ref]
        cls = _ref_class(ref)
        hint = placement_hints.get(ref, {})
        is_term = is_screw_terminal_class(cls)
        ext0 = (info["ext_left"], info["ext_right"], info["ext_top"], info["ext_bot"])
        pad0 = (info["pad_ext"][0], info["pad_ext"][1], info["pad_ext"][2], info["pad_ext"][3])
        if "rotation" in hint:
            ang = hint["rotation"]
            rot_source[ref] = "hint"
        elif is_term:
            wv = wire_entry_table.get(normalize_family(info.get("footprint", "")))
            if wv is not None:
                # Known family: aim the WIRE-ENTRY face at the edge's OUTWARD
                # normal — the cable side hangs off-board, pads stay inboard.
                ang = rotation_to_face(wv, outward_normal(edge))
                rot_source[ref] = "wire_entry"
            else:
                # Unknown family: fall back to the pad-centroid proxy (aim the
                # pad side inward) and FLAG it — never silently mis-orient.
                ang = rotation_to_face((info["pad_cx"], info["pad_cy"]), inward_normal(edge))
                rot_source[ref] = "pad_centroid"
                placement_decisions.append({"event": "rotation_fallback", "ref": ref,
                                            "footprint": info.get("footprint", "")})
                if math.hypot(info["pad_cx"], info["pad_cy"]) < 0.3:
                    placement_decisions.append({"event": "rotation_ambiguous", "ref": ref})
        else:
            ang = 0
        rext = rotate_extents(ext0, ang)
        rpad = rotate_extents(pad0, ang)
        rot_by_ref[ref] = (ang, rext, rpad)
        # Field-wiring terminals SEAT on-board (overhang=False) so the block isn't
        # cantilevered off the edge with its silk "supported by air"; other edge
        # connectors keep their original overhang behavior (boards without field
        # wiring are unchanged). Only the antenna keepout (tier 1) ever overhangs.
        items.append((ref, rext, rpad, is_term and not is_field_terminal(ref)))
        if hint:
            placement_decisions.append({"event": "placement_hint_applied",
                                        "ref": ref, "directive": hint})
    # Shrink the usable span ALONG this edge by the corner-hole clearance so the
    # terminal run starts/ends past the two corner mounting holes (the cross-axis
    # / seating depth is untouched). Without this a field terminal lands on a hole.
    if edge in ("top", "bottom"):
        lay_box = (board_xmin + corner_clear, board_ymin,
                   board_xmax - corner_clear, board_ymax)
    else:
        lay_box = (board_xmin, board_ymin + corner_clear,
                   board_xmax, board_ymax - corner_clear)
    laid = layout_along_edge(items, edge, lay_box, margin, spacing, anchor=terminal_anchor)
    for (ref, x, y, fits) in laid:
        ang, rext, rpad = rot_by_ref[ref]
        el, er, et, eb = rext
        info = fp_info[ref]
        rkeep = rotate_keepout(info["keepout_rel"], ang)
        is_term = is_screw_terminal_class(_ref_class(ref))
        clear = (fits and not overlaps_prior(prior_boxes, x, y, el, er, et, eb)
                 and not hits_keepout(x, y, el, er, et, eb))
        if clear or is_field_terminal(ref):
            # FIELD-WIRING terminals ALWAYS stay at their edge (a crowded edge
            # beats burying one in the interior, inaccessible). Other connectors
            # (module headers / SW / H / USB) fall back interior if the slot is
            # taken — their original behavior.
            if not clear:
                placement_decisions.append({"event": "terminal_edge_crowded",
                                            "ref": ref, "edge": edge})
            place_at(ref, x, y, ang, rext, rkeep)
            if is_term:
                placement_decisions.append({"event": "rotation_chosen", "ref": ref,
                                            "edge": edge, "angle": ang,
                                            "source": rot_source.get(ref, "pad_centroid")})
        else:
            fb = fallback_grid(*get_extents(ref))
            if fb:
                place_at(ref, fb[0], fb[1])

# Interior connectors (edge:"none") → spiral near partner, like a passive.
for ref in t2_interior:
    el, er, et, eb = get_extents(ref)
    placement_decisions.append({"event": "placement_hint_applied", "ref": ref,
                                "directive": {"edge": "none"}})
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

for ref, (px, py, pa) in placements.items():
    fp = board.FindFootprintByReference(ref)
    if fp:
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(px), pcbnew.FromMM(py)))
        if pa:
            # KiCad 9/10: SetOrientationDegrees; older API: EDA_ANGLE.
            if hasattr(fp, "SetOrientationDegrees"):
                fp.SetOrientationDegrees(pa)
            else:
                fp.SetOrientation(pcbnew.EDA_ANGLE(pa, pcbnew.DEGREES_T))
        moved.append({"ref": ref, "x_mm": px, "y_mm": py, "angle": pa})

for ref in all_refs - placed_refs:
    failed.append(ref)

board.Save(params["pcb_path"])

# Hub = most-connected component overall
hub = max(conn_score, key=conn_score.get) if conn_score else None

print(json.dumps({
    "status": "ok",
    "components_placed": len(moved),
    "hub_component": hub,
    "tiers": {
        "keepout": tier1,
        "edge": tier2,
        "ic": tier3,
        "passive": tier4,
    },
    "failed_placements": failed,
    "placements": moved[:10],
    "placements_truncated": len(moved) > 10,
    "placement_decisions": placement_decisions,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "spacing_mm": spacing_mm,
        "corner_clear_mm": corner_clear_mm,
        "anchor": terminal_anchor,
        "net_members": dict(net_members),
        "placement_hints": dict(placement_hints or {}),
        "edge_classes": list(_EDGE_DESIGNATOR_CLASSES),
        # Wire-entry table as plain data (lists, JSON-safe); the embedded script
        # keys it via normalize_family (WIRE_ENTRY_HELPER). Single source = WIRE_ENTRY.
        "wire_entry": {k: list(v) for k, v in WIRE_ENTRY.items()},
    }, timeout=60.0)


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

    if preflight.get("status") == "ok":
        if "route_ready" not in preflight:
            logger.warning("preflight result missing 'route_ready' key; assuming not ready")
            route_ready = False
        else:
            route_ready = preflight["route_ready"]
        if not route_ready:
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
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
ground_net = params["ground_net"]

net = board.FindNet(ground_net)
if net is None or net.GetNetCode() == 0:
    # GND net might not exist — skip zones, not an error
    print(json.dumps({"status": "ok", "message": f"Ground net {ground_net!r} not found, skipping zones", "zones_added": 0}))
    raise SystemExit(0)

# Get board outline for zone boundary
bb = board.GetBoardEdgesBoundingBox()
if bb.GetWidth() == 0 or bb.GetHeight() == 0:
    print(json.dumps({"status": "ok", "message": "No board outline, skipping zones", "zones_added": 0}))
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

board.Save(params["pcb_path"])

print(json.dumps({
    "status": "ok",
    "zones_added": zones_added,
    "ground_net": ground_net,
    "layers": ["F.Cu", "B.Cu"],
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "ground_net": ground_net,
    }, timeout=60.0)


def _step_silkscreen_legends(
    pcb_path: str, legends: List[Dict[str, Any]], margin_mm: float = 0.5,
    terminal_edges: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Add a per-position silk legend to each synthesized connector/terminal.

    For a field-wired terminal the silk legend IS the wiring documentation, so a
    connector is not complete without it (placement-locus §7.1). For each
    ``ConnectorLegend`` (``{ref, positions, device}``): label every pad ``i``
    (1-indexed) with ``positions[i-1]`` placed on exposed board just past the whole
    BLOCK (the footprint body envelope) on the INBOARD side — readable beside the
    terminal, never UNDER its plastic (the body extends ~3mm past the pads on the
    inboard foot side, so a pad-relative offset would hide the label; spec §6) —
    and set the footprint value to the device identity. After
    labelling, the existing silk overlap auto-fixer tidies footprint fields the
    new text may crowd (shared single source — same machinery the audit router
    uses)."""
    if not legends:
        return {"status": "ok", "labels_added": 0, "skipped": "no connector legends"}

    script = r"""
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])
margin = pcbnew.FromMM(params["margin_mm"])
size = pcbnew.FromMM(0.8)
thick = pcbnew.FromMM(0.15)
silk = board.GetLayerID("F.SilkS")

by_ref = {fp.GetReference(): fp for fp in board.GetFootprints()}
# For an EDGE terminal the legend goes on the INBOARD side of each pad (away from
# the terminal's edge), clear of the body that extends OUTBOARD toward the
# wire-entry face (spec §6: readable beside the block, never under it). An interior
# module header (no edge assignment) has no inboard normal, so its labels go to the
# SIDE of the pad column — past the body, toward the nearer board edge — never
# "above the pad", which lands ON the pad above it for a VERTICAL header.
terminal_edges = params.get("terminal_edges") or {}
# edge -> inboard (inward-normal) unit step. (0,0) marks "no edge" (interior).
_INBOARD = {"top": (0, 1), "bottom": (0, -1), "left": (1, 0), "right": (-1, 0)}
# Every pad bbox on the board (ref, x0, y0, x1, y1). An interior module header has
# no inboard normal and is packed into the cluster, so we pick the SIDE whose label
# band is CLEAR of all OTHER pads — and skip the header's silk if no side is clear,
# rather than draw a label on a neighbour's pad.
_all_pads = []
for f in board.GetFootprints():
    for pd in f.Pads():
        pb = pd.GetBoundingBox()
        _all_pads.append((f.GetReference(), pb.GetLeft(), pb.GetTop(),
                          pb.GetRight(), pb.GetBottom()))

def _band_clear(own, x0, y0, x1, y1):
    for ref, px0, py0, px1, py1 in _all_pads:
        if ref == own:
            continue
        if not (px1 < x0 or px0 > x1 or py1 < y0 or py0 > y1):
            return False
    return True

labels_added = 0
missing = []
legends_unplaced = []   # interior headers boxed into the cluster (no clear side)
for leg in params["legends"]:
    fp = by_ref.get(leg["ref"])
    if fp is None:
        missing.append(leg["ref"])
        continue
    fp.SetValue(leg["device"])
    positions = leg["positions"]
    ix, iy = _INBOARD.get(terminal_edges.get(leg["ref"]), (0, 0))
    # Clear the whole BLOCK (the footprint body envelope), not just the pad — the
    # plastic body extends ~3mm past the pads on the inboard (foot) side, so a
    # label merely past the pad edge sits UNDER the block. Offset past the body
    # edge so the label lands on exposed board, beyond the block, in the open gap.
    fbb = fp.GetBoundingBox(False, False)

    # Interior module header (no edge normal): choose the side whose label band is
    # clear of every other pad. A VERTICAL pad column -> labels go left/right (each
    # beside its own pad); a HORIZONTAL row -> up/down. "Above the pad" (the old
    # default) lands ON the pad above for a vertical header. If neither valid side
    # is clear (the header is boxed into the cluster) the per-pad silk is SKIPPED,
    # not drawn over a neighbour — the pinout still lives in the connector legend
    # data; rendering it would need a placement-level silk clearance.
    side = None
    if (ix, iy) == (0, 0):
        maxw = max((len(positions[i]) for i in range(len(positions)) if positions[i]),
                   default=1) * size
        d = margin + maxw + size // 2      # band depth (+ a glyph of safety)
        if (fbb.GetBottom() - fbb.GetTop()) >= (fbb.GetRight() - fbb.GetLeft()):
            cands = [("R", fbb.GetRight() + margin, fbb.GetTop(), fbb.GetRight() + d, fbb.GetBottom()),
                     ("L", fbb.GetLeft() - d, fbb.GetTop(), fbb.GetLeft() - margin, fbb.GetBottom())]
        else:
            cands = [("U", fbb.GetLeft(), fbb.GetTop() - d, fbb.GetRight(), fbb.GetTop() - margin),
                     ("D", fbb.GetLeft(), fbb.GetBottom() + margin, fbb.GetRight(), fbb.GetBottom() + d)]
        side = next((s for s, a, b, c, e in cands if _band_clear(leg["ref"], a, b, c, e)), None)
        if side is None:
            legends_unplaced.append(leg["ref"])
            continue

    for pad in fp.Pads():
        num = pad.GetNumber()
        if not num.isdigit():
            continue
        idx = int(num) - 1
        if idx < 0 or idx >= len(positions) or not positions[idx]:
            continue
        p = pad.GetPosition()
        lw = len(positions[idx]) * size    # this label's width (centre-justified)
        # Cross-axis = the pad's own position (label stays over its pad); the offset
        # axis clears the BODY envelope (fbb). Edge terminals push the centre out by
        # half a glyph (their body already extends past the pads); interior headers
        # push by half the LABEL width so the whole centre-justified glyph clears the
        # thin header body.
        if ix > 0:        # edge terminal, inboard right (terminal on the left edge)
            tx, ty = fbb.GetRight() + margin + size // 2, p.y
        elif ix < 0:      # inboard left (right edge)
            tx, ty = fbb.GetLeft() - margin - size // 2, p.y
        elif iy > 0:      # inboard down (top edge)
            tx, ty = p.x, fbb.GetBottom() + margin + size // 2
        elif iy < 0:      # inboard up (bottom edge)
            tx, ty = p.x, fbb.GetTop() - margin - size // 2
        elif side == "R":
            tx, ty = fbb.GetRight() + margin + lw // 2, p.y
        elif side == "L":
            tx, ty = fbb.GetLeft() - margin - lw // 2, p.y
        elif side == "U":
            tx, ty = p.x, fbb.GetTop() - margin - size // 2
        else:             # "D"
            tx, ty = p.x, fbb.GetBottom() + margin + size // 2
        txt = pcbnew.PCB_TEXT(board)
        txt.SetText(positions[idx])
        txt.SetPosition(pcbnew.VECTOR2I(int(tx), int(ty)))
        txt.SetLayer(silk)
        txt.SetTextSize(pcbnew.VECTOR2I(size, size))
        txt.SetTextThickness(thick)
        board.Add(txt)
        labels_added += 1

    # Reference designator ("J5") as a clean callout: centred on the block, one row
    # BEYOND the legend labels on the inboard side. This pins what the generic silk
    # auto-fixer does inconsistently for WIDE terminals — it shoves their refdes to
    # the SIDE at pad level instead of above-centre like the narrow ones.
    if (ix, iy) != (0, 0):
        cxb = (fbb.GetLeft() + fbb.GetRight()) // 2
        cyb = (fbb.GetTop() + fbb.GetBottom()) // 2
        roff = margin + 2 * size      # clear the label row + a gap
        if iy < 0:
            rx, ry = cxb, fbb.GetTop() - roff
        elif iy > 0:
            rx, ry = cxb, fbb.GetBottom() + roff
        elif ix > 0:
            rx, ry = fbb.GetRight() + roff, cyb
        else:
            rx, ry = fbb.GetLeft() - roff, cyb
        ref = fp.Reference()
        ref.SetPosition(pcbnew.VECTOR2I(int(rx), int(ry)))
        ref.SetTextSize(pcbnew.VECTOR2I(size, size))

board.Save(params["pcb_path"])
print(json.dumps({"status": "ok", "labels_added": labels_added,
                  "missing_refs": missing,
                  "legends_unplaced": legends_unplaced}))
"""
    res = run_pcbnew_script(script, params={
        "pcb_path": pcb_path, "legends": legends, "margin_mm": margin_mm,
        "terminal_edges": dict(terminal_edges or {}),
    }, timeout=60.0)
    # Tidy footprint refdes/value silk the new labels may crowd (best-effort).
    # NOTE: the auto-fixer relocates footprint FIELDS, not the free-text legend
    # labels themselves — those are placed clear of the pad by construction.
    # The legend terminals' refdes callouts ARE deliberately placed above, so we
    # protect them (skip_refs) — otherwise the generic auto-fixer could relocate
    # or hide a callout that overlaps a legend label, undoing this step's work.
    if res.get("status") == "ok" and res.get("labels_added", 0) > 0:
        legend_refs = [leg["ref"] for leg in legends if leg.get("ref")]
        fix = _op_auto_fix_silkscreen(pcb_path, skip_refs=legend_refs)
        res["overlap_autofix"] = {
            "moved": fix.get("moved"), "hidden": fix.get("hidden"),
            "error": fix.get("error"),
        }
    return res


def _render_board_svg(pcb_path: str) -> Optional[str]:
    """Best-effort flat SVG of the board (copper + silk + edge) for the approval
    proposal. Returns the path, or None if kicad-cli is unavailable or the export
    fails — NEVER raises: the proposal's value is its data; the picture is a bonus."""
    try:
        cli = get_kicad_cli_path(required=True)
    except KiCadCLIError:
        return None
    assert cli is not None
    out = os.path.splitext(pcb_path)[0] + "_proposal.svg"
    try:
        r = subprocess.run(
            # --exclude-drawing-sheet: no title-block/rev frame, so the Edge.Cuts
            # rectangle IS the only boundary in the image (a frame around a larger
            # page reads as a false board edge and hides where parts really sit).
            [cli, "pcb", "export", "svg", pcb_path, "-o", out,
             "--layers", "F.Cu,B.Cu,F.SilkS,Edge.Cuts",
             "--page-size-mode", "2", "--exclude-drawing-sheet"],
            capture_output=True, text=True, timeout=60,
        )
        return out if (r.returncode == 0 and os.path.exists(out)) else None
    except (subprocess.SubprocessError, OSError):
        return None


def _build_placement_proposal(
    pcb_path: str,
    width_mm: float,
    height_mm: float,
    antenna_edge: Optional[str],
    terminal_edges: Dict[str, str],
    hole_positions: List[Dict[str, Any]],
    legends: List[Dict[str, Any]],
    holes: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the approval-gate proposal for the PLACED (unrouted) board: board
    size, antenna edge, the per-terminal edge+device+positions table, the corner
    hole positions, a machine-readable ``proposed_board_yaml`` the user can tweak
    in natural language (Claude translates), and a best-effort SVG render. All
    derived from the REAL placement decisions — no re-derivation, no seam."""
    legend_by_ref = {L["ref"]: L for L in legends}
    terminal_table = [
        {"ref": ref, "edge": edge,
         "device": (legend_by_ref.get(ref) or {}).get("device"),
         "positions": (legend_by_ref.get(ref) or {}).get("positions", [])}
        for ref, edge in sorted(terminal_edges.items())
    ]
    proposed_board_yaml: Dict[str, Any] = {
        "board_size_mm": [width_mm, height_mm],
        "mounting_holes": {k: holes[k]
                           for k in ("count", "drill_mm", "inset_mm", "keepout_mm")},
        "placement_hints": {ref: {"edge": edge}
                            for ref, edge in sorted(terminal_edges.items())},
    }
    return {
        "board_size_mm": [width_mm, height_mm],
        "antenna_edge": antenna_edge,
        "terminal_table": terminal_table,
        "mounting_holes": hole_positions,
        "render_path": _render_board_svg(pcb_path),
        "proposed_board_yaml": proposed_board_yaml,
    }


def _step_export_gerbers(pcb_path: str) -> Dict[str, Any]:
    """Step 8 (optional): Export Gerber + drill files and create ZIP."""
    try:
        kicad_cli = get_kicad_cli_path(required=True)
    except KiCadCLIError as e:
        return {"error": str(e)}
    assert kicad_cli is not None  # required=True raises above if CLI not found

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

    # Collect everything kicad-cli wrote — *.gbr (legacy), *.gtl/.gbl/.gto/etc.
    # (RS-274X layer extensions), *.drl/.xln (drill), *.gbrjob (fab manifest).
    # Matches the export.py _op_gerbers pattern; the narrower *.gbr-only glob
    # was silently shipping fab packages missing layers.
    all_files = sorted(
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if os.path.isfile(os.path.join(output_dir, f))
    )
    drill_files = [f for f in all_files if f.endswith(".drl") or f.endswith(".xln")]
    gerber_files = [f for f in all_files if f not in drill_files]

    if not all_files:
        return {"error": "No output files generated"}

    pcb_name = os.path.splitext(os.path.basename(pcb_path))[0]
    zip_path = os.path.join(pcb_dir, f"{pcb_name}-gerbers.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in all_files:
            zf.write(f, os.path.basename(f))

    return {
        "status": "ok",
        "output_dir": output_dir,
        "gerber_count": len(gerber_files),
        "drill_count": len(drill_files),
        "total_files": len(all_files),
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
        intent_path: str = "",
        placement_hints: Optional[Dict[str, Dict[str, Any]]] = None,
        add_mounting_holes: bool = True,
        approved: bool = True,
    ) -> Dict[str, Any]:
        """Build a complete routed PCB from a KiCad schematic in one step.

        Runs the full pipeline: extract netlist → create PCB → load
        footprints → assign nets → smart placement → autoroute →
        add ground planes → fill zones → silk legends → (optionally)
        export Gerbers.

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
            intent_path: Optional path to the design-intent YAML. When given,
                its connector legends drive a silk-legend pass that labels each
                synthesized terminal's positions (the field-wiring documentation)
                — the firmware front end passes the same intent it generated from.
                Its ``source.board_size_mm`` (if any) sizes the board when no
                explicit dimensions are given, and its ``placement_hints`` feed
                the per-ref placement overrides below.
            approved: When False, the APPROVAL GATE (spec §7): run the pipeline
                through placement (which produces the real terminal-edge layout)
                but STOP before the expensive autoroute, returning
                ``status="pending_approval"`` and a ``proposal`` (board size,
                antenna edge, per-terminal edge/positions table, hole positions,
                a tweakable ``proposed_board_yaml``, and a best-effort SVG render).
                Re-run with ``approved=True`` once the layout looks right.
            add_mounting_holes: Add corner M3 mounting holes (default True). The
                count/drill/inset/keepout come from the design intent's
                ``source.mounting_holes`` (board.yaml) or default to 4 × M3; pass
                False to suppress holes without a board.yaml (or set
                ``mounting_holes.count: 0`` there).
            placement_hints: Per-ref PCB placement overrides, keyed by KiCad
                reference. Each value is a subset of ``{edge, rotation, fixed}``
                — ``edge`` ∈ {top,bottom,left,right,none}, ``rotation`` ∈
                {0,90,180,270}, ``fixed`` = [x,y] mm. Invalid keys/values are
                reported (events) and ignored, never crash. Merged over any
                hints carried in the design intent (these win — explicit caller).
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
            _missing = step["components_without_footprint"]
            _extra = len(_missing) - 10
            _suffix = f" (and {_extra} more)" if _extra > 0 else ""
            pipeline_result["warnings"] = [
                f"{step['skipped_count']} component(s) skipped (no footprint): "
                f"{', '.join(_missing[:10])}{_suffix}"
            ]

        if not components:
            pipeline_result["error"] = "No components with footprints found in schematic"
            return pipeline_result

        # Load the design intent ONCE (reused for board size, placement hints,
        # and the silk-legend pass). Non-fatal: a bad/absent intent just means
        # no intent-derived board size or hints.
        design_intent = None
        if intent_path and os.path.exists(intent_path):
            try:
                design_intent = load_intent(intent_path)
            except Exception as e:  # noqa: BLE001 — intent is advisory here
                pipeline_result.setdefault("warnings", []).append(
                    f"Could not load intent {intent_path} (non-fatal): {e}"
                )

        # Corner mounting holes (firmware-blind; board.yaml `mounting_holes:` →
        # intent.source). Resolved before sizing so the auto-estimate can reserve
        # the corner inset, and before placement so the holes are fixtures.
        holes = _resolve_mounting_holes(
            design_intent.source.get("mounting_holes")
            if design_intent is not None else None
        )
        if not add_mounting_holes:        # caller escape hatch (no board.yaml needed)
            holes = {**holes, "count": 0}

        # Board size precedence: explicit args (>0) > intent source.board_size_mm
        # > auto-estimate (resolved per axis; see _resolve_board_size).
        eff_width_mm, eff_height_mm, _bsz_from_intent = _resolve_board_size(
            board_width_mm, board_height_mm,
            design_intent.source if design_intent is not None else None,
        )
        if _bsz_from_intent:
            pipeline_result.setdefault("warnings", []).append(
                f"Board sized to {eff_width_mm}x{eff_height_mm}mm from design intent"
            )

        # Step 2: Create PCB + board outline (sizing reserves the hole inset).
        # terminal_distribution (board.yaml, default single_edge) decides whether the
        # auto-size spreads field terminals across the side edges.
        _term_dist = "single_edge"
        _term_anchor = "start"
        _refit = False
        if design_intent is not None:
            _term_dist = design_intent.source.get("terminal_distribution", "single_edge")
            # terminal_centering (board.yaml, default off) centres each edge's
            # terminal group instead of packing it at one end (layout balance only).
            _term_anchor = ("center" if design_intent.source.get("terminal_centering")
                            else "start")
            # board_refit (board.yaml, default off) re-fits an AUTO-SIZED board to its
            # measured cluster after placement (SPEC_Post_Placement_Board_Refit). The
            # auto_sized gate (D2) is applied below — never re-fit user-dimensioned boards.
            _refit = bool(design_intent.source.get("board_refit"))
        step = _step_create_pcb_and_outline(
            pcb_path, eff_width_mm, eff_height_mm, components, holes=holes,
            terminal_distribution=_term_dist,
        )
        if not _record("create_pcb", step):
            return pipeline_result

        actual_width = step["width_mm"]
        actual_height = step["height_mm"]
        # Per-terminal edge assignment from the distributor (empty unless auto-sized).
        # Passed to placement as the LOWEST-priority {"edge": E} hints below.
        terminal_edge_of = step.get("edge_of") or {}
        antenna_side_est = step.get("antenna_side")
        # Captured for re-fit (D2 gate + pass-2 reuse): whether the board was
        # auto-sized, and the measured footprint bodies (so pass 2 skips re-measuring).
        auto_sized = bool(step.get("auto_sized"))
        measured_comps = step.get("comps") or []
        pipeline_result["board_width_mm"] = actual_width
        pipeline_result["board_height_mm"] = actual_height
        if step.get("auto_sized"):
            pipeline_result.setdefault("warnings", []).append(
                f"Board auto-sized to {actual_width}x{actual_height}mm"
            )

        # Step 2b: Corner mounting holes (fixtures). Added to the board now; the
        # placer is told to pin them (fixed hints below) so they stay at the
        # corners and their keepout boxes are reserved against other parts.
        hole_hints: Dict[str, Dict[str, Any]] = {}
        hole_positions: List[Dict[str, Any]] = []
        if holes.get("count", 0) > 0:
            step_holes = _step_add_mounting_holes(pcb_path, holes)
            _record("mounting_holes", step_holes)   # non-fatal
            hole_positions = step_holes.get("positions") or []
            hole_hints = {
                r["ref"]: {"fixed": [r["x_mm"], r["y_mm"]], "rotation": 0}
                for r in hole_positions
            }

        # Step 3: Load footprints onto board (temporary positions)
        step = _step_load_footprints(pcb_path, components)
        if not _record("load_footprints", step):
            return pipeline_result

        # A footprint that fails to load is fatal: the board would silently be
        # missing components, net assignment for them would no-op, and the final
        # incomplete_nets count can read 0 (no ratsnest for absent pads) — fully
        # masking the gap. Surface it as an error the caller must resolve (almost
        # always a wrong/missing footprint mapping) rather than fabricating a
        # board that doesn't match the schematic.
        if step.get("error_count", 0) > 0:
            _fp_errs = step.get("errors", [])
            _more = len(_fp_errs) - 5
            _tail = f" (and {_more} more)" if _more > 0 else ""
            pipeline_result["error"] = (
                f"{step['error_count']} footprint(s) failed to load — the board "
                f"would be missing components: "
                f"{'; '.join(_fp_errs[:5])}{_tail}"
            )
            return pipeline_result

        pipeline_result["footprints_placed"] = step.get("placed_count", 0)

        # Step 4: Inject nets + assign pads
        step = _step_inject_nets_and_assign_pads(pcb_path, nets)
        if not _record("assign_nets", step):
            return pipeline_result

        pipeline_result["pads_assigned"] = step.get("pads_assigned", 0)

        # Merge placement hints, lowest → highest priority:
        #   auto terminal distribution  <  intent-carried  <  explicit param  <
        #   mounting-hole fixtures (pinned at the corners). SINGLE SOURCE so pass 1
        # AND a re-fit pass 2 compose hints identically. The distributor's {ref:edge}
        # is the BASE so an explicit user/intent hint on a terminal still wins;
        # terminals with no hint fall to the script's antenna-opposite rule.
        # normalize_hint validates every directive.
        def _compose_hints(edge_of, hole_hints_):
            base = {ref: {"edge": e} for ref, e in edge_of.items()}
            if design_intent is not None and design_intent.placement_hints:
                base.update(design_intent.placement_hints)
            clean, warns = _merge_placement_hints(base, placement_hints)
            # Mounting holes are physical fixtures we already placed — pin them at the
            # corners at HIGHEST priority (else the placer, which no longer edge-places
            # "H", would shove them into the interior). Normalized via the same path.
            if hole_hints_:
                norm_holes, hole_ws = _merge_placement_hints(None, hole_hints_)
                clean.update(norm_holes)
                warns.extend(hole_ws)
            return clean, warns

        clean_hints, hint_warnings = _compose_hints(terminal_edge_of, hole_hints)

        # Step 5: Smart placement (tiered, connectivity-aware). corner_clear keeps
        # edge terminals clear of the corner mounting holes (single source with
        # the board-size reservation above, so the cleared span actually fits).
        step = _step_smart_placement(
            pcb_path, nets, placement_hints=clean_hints,
            corner_clear_mm=_hole_corner_clear(holes),
            terminal_anchor=_term_anchor)
        _record("smart_placement", step)
        # Non-fatal — continue even if placement is imperfect

        # ── Board re-fit pass 2 (SPEC_Post_Placement_Board_Refit §3) ───────────
        # Measure the placed interior cluster; if the board can hug it more tightly,
        # redraw a smaller outline and re-place onto it. Opt-in (board_refit) and
        # ONLY for auto-sized boards (D1/D2) — the size lever that recovers the
        # sizing over-estimate. ANY decline/failure/soft-regression keeps pass 1; a
        # committed attempt that fails restores the .pass1 backup so the on-disk
        # board always matches the in-memory `step`.
        if _refit and auto_sized and measured_comps:
            _backup = pcb_path + ".pass1"
            _old_w, _old_h = actual_width, actual_height
            _refit_tel: Dict[str, Any] = {}   # telemetry, merged into every outcome
            try:
                # Cluster = everything NOT edge-placed and NOT a mounting hole (§2:
                # includes interior J6, excludes H* + the edge-anchored terminals).
                _edge_placed = [
                    d["ref"] for d in (step.get("placement_decisions") or [])
                    if d.get("event") == "rotation_chosen" and d.get("edge")
                ]
                _meas = _step_measure_cluster(pcb_path, _edge_placed)
                _cw, _ch = _meas.get("cluster_w", 0.0), _meas.get("cluster_h", 0.0)
                if "error" in _meas or _cw <= 0 or _ch <= 0:
                    raise _RefitDecline("cluster measure empty or failed")
                # Inflate by the routing-slack margin per side (tunable, §4/R1).
                _m = _CLUSTER_ROUTING_MARGIN_MM
                _resize = _estimate_board_size(
                    [], corner_inset_mm=_hole_corner_clear(holes),
                    corner_center_inset_mm=_hole_center_inset(holes),
                    terminal_distribution=_term_dist,
                    cluster_wh=(_cw + 2 * _m, _ch + 2 * _m), comps=measured_comps)
                if "error" in _resize:
                    raise _RefitDecline("re-estimate failed: %s" % _resize.get("error"))
                _new_w = _resize["size"]["width_mm"]
                _new_h = _resize["size"]["height_mm"]
                # Telemetry (data-capture rule: never drop the measurement) — merged
                # into EVERY outcome record so a decline is diagnosable without a rebuild.
                _refit_tel = {
                    "cluster_w": _cw, "cluster_h": _ch, "margin_mm": _m,
                    "from_width_mm": _old_w, "from_height_mm": _old_h,
                    "recomputed_width_mm": _new_w, "recomputed_height_mm": _new_h,
                    "recomputed_edge_of": _resize.get("edge_of"),
                }
                # No-op guard: meaningfully smaller on ≥1 axis, growing on neither.
                _TOL = 1.0
                _dw, _dh = _old_w - _new_w, _old_h - _new_h
                if not ((_dw > _TOL or _dh > _TOL) and _dw >= -_TOL and _dh >= -_TOL):
                    raise _RefitDecline(
                        "not meaningfully smaller (%sx%s → %sx%s)"
                        % (_old_w, _old_h, _new_w, _new_h))

                # Commit: backup, redraw outline (footprints/nets survive), re-fixture
                # the corner holes on the new outline, re-place. Any sub-step error
                # below is a generic exception → the fallback handler restores .pass1.
                shutil.copy2(pcb_path, _backup)
                if "error" in _step_redraw_outline(pcb_path, _new_w, _new_h):
                    raise RuntimeError("redraw_outline failed")
                if "error" in _step_remove_mounting_holes(pcb_path):
                    raise RuntimeError("remove_mounting_holes failed")
                _hole_positions2: List[Dict[str, Any]] = []
                _hole_hints2: Dict[str, Dict[str, Any]] = {}
                if holes.get("count", 0) > 0:
                    _ah = _step_add_mounting_holes(pcb_path, holes)
                    if "error" in _ah:
                        raise RuntimeError("add_mounting_holes failed")
                    _hole_positions2 = _ah.get("positions") or []
                    _hole_hints2 = {
                        r["ref"]: {"fixed": [r["x_mm"], r["y_mm"]], "rotation": 0}
                        for r in _hole_positions2
                    }
                _clean2, _warns2 = _compose_hints(_resize.get("edge_of") or {}, _hole_hints2)
                _step2 = _step_smart_placement(
                    pcb_path, nets, placement_hints=_clean2,
                    corner_clear_mm=_hole_corner_clear(holes),
                    terminal_anchor=_term_anchor)
                if "error" in _step2:
                    raise RuntimeError("pass-2 placement failed")
                # Fit-fallback (§3 step 5): a hard miss OR a soft regression
                # (terminal crowded / MCU dropped to interior so the antenna no longer
                # overhangs) means the tighter board doesn't seat — restore pass 1.
                _soft = {d.get("event") for d in (_step2.get("placement_decisions") or [])} & {
                    "terminal_edge_crowded", "keepout_fallback_interior"}
                if _step2.get("failed_placements") or _soft:
                    raise RuntimeError(
                        "fit regression (failed=%s soft=%s)"
                        % (_step2.get("failed_placements"), sorted(_soft)))

                # Pass 2 won — adopt its board, dims, holes, placement, hints.
                step = _step2
                _record("smart_placement", _step2)   # returned telemetry = shipped board
                actual_width, actual_height = _new_w, _new_h
                hole_positions = _hole_positions2
                clean_hints, hint_warnings = _clean2, _warns2
                pipeline_result["board_width_mm"] = _new_w
                pipeline_result["board_height_mm"] = _new_h
                pipeline_result.setdefault("warnings", []).append(
                    "Board re-fit %sx%s → %sx%smm (measured cluster %.1fx%.1f)"
                    % (_old_w, _old_h, _new_w, _new_h, _cw, _ch))
                _record("board_refit", {
                    "status": "ok", "width_mm": _new_w, "height_mm": _new_h,
                    "from_width_mm": _old_w, "from_height_mm": _old_h,
                    "cluster_w": _cw, "cluster_h": _ch})
                try:
                    os.remove(_backup)
                except OSError:
                    pass
            except _RefitDecline as _decl:
                _record("board_refit", {"status": "declined",
                                        "reason": str(_decl), **_refit_tel})
            except Exception as _f:  # noqa: BLE001 — re-fit is an optimization, never fatal
                # Restore the pass-1 board if we had committed, so disk matches `step`.
                try:
                    if os.path.exists(_backup):
                        shutil.copy2(_backup, pcb_path)
                        os.remove(_backup)
                except OSError:
                    pass
                _record("board_refit", {"status": "fallback", "reason": str(_f)})
                pipeline_result.setdefault("warnings", []).append(
                    "Board re-fit fell back to pass 1: %s" % _f)

        # Which edge each terminal was placed on (from the rotation decisions) —
        # used by the silk-legend step to offset labels inboard, away from that
        # edge. Interior module headers (edge:"none") are absent here, so they
        # keep the default label offset.
        terminal_edges = {
            d["ref"]: d["edge"]
            for d in (step.get("placement_decisions") or [])
            if d.get("event") == "rotation_chosen" and d.get("edge")
        }

        # antenna_frame_mismatch guard (SPEC §3.1): the distributor assigned side
        # edges relative to the antenna it derived in the FOOTPRINT 0° frame
        # (antenna_side_est); the placer overhangs the antenna on some board edge in
        # the POST-ROTATION frame (the keepout_overhang decision). They agree on
        # every golden — if they ever diverge the distribution is relative to the
        # wrong axis, so surface it loudly rather than silently mis-placing.
        if antenna_side_est:
            _placed_antenna = next(
                (d["edge"] for d in (step.get("placement_decisions") or [])
                 if d.get("event") == "keepout_overhang" and d.get("edge")), None)
            if _placed_antenna and _placed_antenna != antenna_side_est:
                pipeline_result.setdefault("warnings", []).append(
                    f"antenna_frame_mismatch: distributor sized for antenna on "
                    f"{antenna_side_est!r} but the placer overhung it on "
                    f"{_placed_antenna!r} — terminal distribution may be off-axis")

        def _run_silk_legends() -> None:
            """Silk-legend pass (single source — used by the approval gate AND the
            post-route step). Strictly NON-FATAL: a bad intent or a pcbnew failure
            (run_pcbnew_script RAISES) is recorded, never propagated."""
            if design_intent is None:
                return
            try:
                _legends = [
                    {"ref": L.ref, "positions": L.positions, "device": L.device}
                    for L in design_intent.connector_legends
                ]
                _record("silkscreen_legends",
                        _step_silkscreen_legends(pcb_path, _legends,
                                                 terminal_edges=terminal_edges))
            except Exception as e:  # noqa: BLE001 — documentation step, never fatal
                _record("silkscreen_legends",
                        {"error": f"silk legend step failed (non-fatal): {e}"})

        # Surface placement decisions + hint diagnostics as an events envelope
        # (mcp-events). Info-level rotation/hint records included so the response
        # documents what the autoplacer chose; warnings for bad/unmatched hints.
        # NOTE: this relies on NO outer event_context being active — event_context
        # nests by sharing the accumulator, so an outer one would make
        # to_envelope("info") return foreign events too. build_pcb_from_schematic
        # has no @with_events wrapper today; if one is ever added, accumulate the
        # decisions into a list and emit them outside this nested context instead.
        with event_context() as _ev:
            for _d in step.get("placement_decisions", []) or []:
                _emit_placement_decision(_d)
            for _ref in clean_hints:
                if _ref not in components:
                    emit_event("warn", "placement_hint_unmatched",
                               f"Placement hint for {_ref} has no matching "
                               f"component on the board", {"ref": _ref})
            for _w in hint_warnings:
                emit_event("warn", "placement_hint_invalid", _w, {})
            _env = _ev.to_envelope("info")
        if _env:
            pipeline_result["events"] = _env

        # Approval gate (§7): stop BEFORE the expensive autoroute and return a
        # proposal of the placed (unrouted) board. Placement is done, so the
        # terminal edges/holes are REAL; only routing (the 3–4 min FreeRouter
        # pass) is skipped. Run silk first so the render documents the terminals.
        if not approved:
            _run_silk_legends()
            antenna_edge = next(
                (d.get("edge") for d in (step.get("placement_decisions") or [])
                 if d.get("event") == "keepout_overhang"), None)
            legends = [
                {"ref": L.ref, "positions": L.positions, "device": L.device}
                for L in (design_intent.connector_legends if design_intent else [])
            ]
            pipeline_result["status"] = "pending_approval"
            pipeline_result["proposal"] = _build_placement_proposal(
                pcb_path, actual_width, actual_height, antenna_edge,
                terminal_edges, hole_positions, legends, holes,
            )
            pipeline_result["pcb_path"] = pcb_path
            pipeline_result["elapsed_seconds"] = round(time.time() - t0, 1)
            return pipeline_result

        # Step 6: Autoroute
        step = _step_autoroute(pcb_path, passes=autoroute_passes)
        if not _record("autoroute", step):
            return pipeline_result

        # Surface None when keys are absent rather than 0 — defaulting to 0
        # would silently report "fully routed, no incompletes" if the
        # autoroute step ever changes its return shape.
        pipeline_result["tracks"] = step.get("tracks_after")
        pipeline_result["vias"] = step.get("vias_after")
        # Report the SES-import-MEASURED unconnected count (see helper).
        pipeline_result["incomplete_nets"] = _routed_incomplete_count(step)
        if any(v is None for v in (pipeline_result["tracks"],
                                    pipeline_result["vias"],
                                    pipeline_result["incomplete_nets"])):
            pipeline_result.setdefault("warnings", []).append(
                f"autoroute step result missing expected counts (keys: {sorted(step.keys())})"
            )

        # Step 7: Copper zones + fill. Strictly NON-FATAL: the board is already
        # routed and that result is valuable — a zone-fill timeout or pcbnew
        # failure (run_pcbnew_script RAISES) must be recorded, not allowed to
        # propagate and discard the routed board.
        try:
            _record("zones", _step_add_zones_and_fill(pcb_path, ground_net))
        except Exception as e:  # noqa: BLE001 — finishing step, never fatal
            _record("zones", {"error": f"zone fill failed (non-fatal): {e}"})

        # Step 7.5: Silk legends for synthesized terminals (firmware front end).
        # Field-wired terminals carry their wiring documentation on the silk.
        # (Single source: same _run_silk_legends used by the approval gate above.)
        _run_silk_legends()

        # Step 8: Export gerbers (optional). Non-fatal for the same reason as
        # zones — never lose the routed board to an export failure.
        if export_gerbers:
            try:
                step = _step_export_gerbers(pcb_path)
                _record("export_gerbers", step)
                if step.get("zip_path"):
                    pipeline_result["gerber_zip"] = step["zip_path"]
            except Exception as e:  # noqa: BLE001 — optional step, never fatal
                _record("export_gerbers", {"error": f"gerber export failed (non-fatal): {e}"})

        pipeline_result["status"] = "ok"
        pipeline_result["pcb_path"] = pcb_path
        pipeline_result["elapsed_seconds"] = round(time.time() - t0, 1)

        return pipeline_result
