"""
Shared pcbnew helper code embedded in subprocess scripts.

This module provides a single source of truth for the Python code that runs
inside KiCad's Python 3.9 subprocess (via pcbnew_bridge). The code is stored
as string constants because it gets embedded into dynamically generated
pcbnew scripts — it cannot be imported as a normal module.

Usage in f-string scripts:
    script = f\"\"\"
    import pcbnew, json, os
    {KEEPOUT_HELPER}
    {COURTYARD_BBOX_HELPER}
    {LIB_SEARCH_HELPER}
    ...
    \"\"\"

If you modify these helpers, all consumers pick up the change automatically.

The pure-geometry primitives (rects_overlap, rect_inside, overlap_area,
signed_gap_mm, aabb_overlap, aabb_inside) live in ``utils.geometry`` and
are made available to embedded scripts by prepending
:data:`~kicad_mcp.utils.geometry.GEOMETRY_HELPER` to KEEPOUT_HELPER.  Both
the Python-side ``import`` and the embedded-script string reference the
same source.
"""

from kicad_mcp.utils.geometry import GEOMETRY_HELPER
from kicad_mcp.utils.netlist_parser import POWER_NET_HELPER

# ---------------------------------------------------------------------------
# Keepout zone helpers
# Provides: extract_keepouts(), get_board_outline(), and (via GEOMETRY_HELPER)
#           rects_overlap(), overlap_area(), rect_inside(), signed_gap_mm()
# Requires: pcbnew in scope (imported inside functions)
# ---------------------------------------------------------------------------
KEEPOUT_HELPER = GEOMETRY_HELPER + """
def extract_keepouts(board):
    import pcbnew
    keepouts = []
    def process_zone(zone, source, source_ref=""):
        if not zone.GetIsRuleArea():
            return None
        bbox = zone.GetBoundingBox()
        layers = []
        for lid in zone.GetLayerSet().Seq():
            layers.append(board.GetLayerName(lid))
        poly_set = zone.Outline()
        pts = []
        if poly_set.OutlineCount() > 0:
            ol = poly_set.Outline(0)
            for i in range(ol.PointCount()):
                pt = ol.CPoint(i)
                pts.append([round(pcbnew.ToMM(pt.x), 3), round(pcbnew.ToMM(pt.y), 3)])
        # Narrow to AttributeError — older pcbnew versions may not expose
        # m_Uuid. Anything else (RuntimeError, etc.) propagates rather than
        # silently producing an empty UUID that breaks downstream dedup.
        if hasattr(zone, 'm_Uuid'):
            uuid_str = zone.m_Uuid.AsString()
        else:
            uuid_str = ""
        return {
            "source": source,
            "source_ref": source_ref,
            "uuid": uuid_str,
            "layers": layers,
            "constraints": {
                "no_tracks": zone.GetDoNotAllowTracks(),
                "no_vias": zone.GetDoNotAllowVias(),
                "no_pads": zone.GetDoNotAllowPads(),
                "no_footprints": zone.GetDoNotAllowFootprints(),
                # KiCad 10 renamed GetDoNotAllowCopperPour → GetDoNotAllowZoneFills
                "no_copper_pour": (zone.GetDoNotAllowZoneFills()
                                   if hasattr(zone, "GetDoNotAllowZoneFills")
                                   else zone.GetDoNotAllowCopperPour()),
            },
            "bounding_box": {
                "x_min_mm": round(pcbnew.ToMM(bbox.GetX()), 3),
                "y_min_mm": round(pcbnew.ToMM(bbox.GetY()), 3),
                "x_max_mm": round(pcbnew.ToMM(bbox.GetRight()), 3),
                "y_max_mm": round(pcbnew.ToMM(bbox.GetBottom()), 3),
            },
            "polygon_pts_mm": pts,
        }
    for zone in board.Zones():
        info = process_zone(zone, "board")
        if info:
            keepouts.append(info)
    for fp in board.GetFootprints():
        # Guard fp.Zones() — absent on older pcbnew API. Other
        # AttributeErrors propagate rather than silently dropping all
        # footprint-level keepouts.
        if hasattr(fp, 'Zones'):
            for zone in fp.Zones():
                info = process_zone(zone, "footprint", fp.GetReference())
                if info:
                    keepouts.append(info)
    return keepouts

_BLOCKED_CONSTRAINT_LABELS = {
    "no_tracks": "tracks",
    "no_vias": "vias",
    "no_pads": "pads",
    "no_footprints": "footprints",
    "no_copper_pour": "copper_pour",
}

def blocked_constraints(c):
    \"\"\"Return labels for True-valued constraint keys.

    Explicit mapping rather than `k.replace("no_", "")` so a new constraint
    key without the "no_" prefix (or one with "no_" in a different position)
    fails loudly instead of silently producing a wrong label.
    \"\"\"
    return [_BLOCKED_CONSTRAINT_LABELS[k] for k, v in c.items()
            if v and k in _BLOCKED_CONSTRAINT_LABELS]

def get_board_outline(board):
    import pcbnew
    edge_cuts_id = board.GetLayerID("Edge.Cuts")
    xs, ys = [], []
    for drawing in board.GetDrawings():
        if drawing.GetLayer() == edge_cuts_id:
            start = drawing.GetStart()
            end = drawing.GetEnd()
            xs.extend([pcbnew.ToMM(start.x), pcbnew.ToMM(end.x)])
            ys.extend([pcbnew.ToMM(start.y), pcbnew.ToMM(end.y)])
    if not xs:
        return None
    return {
        "x_min_mm": round(min(xs), 3),
        "y_min_mm": round(min(ys), 3),
        "x_max_mm": round(max(xs), 3),
        "y_max_mm": round(max(ys), 3),
        "width_mm": round(max(xs) - min(xs), 3),
        "height_mm": round(max(ys) - min(ys), 3),
    }

"""

# ---------------------------------------------------------------------------
# Courtyard bounding box helpers
#
# Two flavors are exposed because different embedded scripts want different
# return types: COURTYARD_BBOX_HELPER returns a dict with named fields,
# COURTYARD_BBOX_TUPLE_HELPER returns a tuple plus POWER_NETS/signal_net_count.
# Both share the same underlying geometry through _GET_BBOX_TUPLE_BODY — a
# single source of truth for "compute the courtyard bbox of a footprint."
# ---------------------------------------------------------------------------
_GET_BBOX_TUPLE_BODY = """
def _get_courtyard_bbox_tuple(fp):
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
"""

COURTYARD_BBOX_HELPER = _GET_BBOX_TUPLE_BODY + """
def get_courtyard_bbox(fp):
    bbox = _get_courtyard_bbox_tuple(fp)
    if bbox is None:
        return None
    return {"x_min_mm": bbox[0], "y_min_mm": bbox[1],
            "x_max_mm": bbox[2], "y_max_mm": bbox[3]}
"""

COURTYARD_BBOX_TUPLE_HELPER = POWER_NET_HELPER + _GET_BBOX_TUPLE_BODY + """
get_courtyard_bbox = _get_courtyard_bbox_tuple

def signal_net_count(fp):
    # Count distinct non-power, non-empty nets attached to this footprint.
    # Power classification uses the canonical is_power_net (prefix match),
    # not a hand-typed set — the prior inline POWER_NETS missed nets like
    # "+12V_FILTERED" or "VSS_A" that match by prefix.
    nets = set()
    for pad in fp.Pads():
        n = pad.GetNetname()
        if n and not is_power_net(n):
            nets.add(n)
    return len(nets)
"""

# ---------------------------------------------------------------------------
# Footprint-nudge helper — SINGLE SOURCE OF TRUTH for the "push overlapping
# footprints apart" operation, shared by the autoroute and DRC-fix placement
# steps (previously two drifted copies; the autoroute copy ignored the board
# outline and nudged against stale bboxes, so it could shove parts past
# Edge.Cuts — h-autoroute-nudge).
# Requires: pcbnew, and get_courtyard_bbox / signal_net_count from
# COURTYARD_BBOX_TUPLE_HELPER. Overlap + containment are inlined so it carries
# no other helper dependency. nudge_overlapping_footprints -> (count, refs).
# ---------------------------------------------------------------------------
NUDGE_PLACEMENT_HELPER = """
def _board_outline_mm(board):
    if hasattr(board, "GetBoardEdgesBoundingBox"):
        bb = board.GetBoardEdgesBoundingBox()
        if bb.GetWidth() > 0:
            return (pcbnew.ToMM(bb.GetX()), pcbnew.ToMM(bb.GetY()),
                    pcbnew.ToMM(bb.GetRight()), pcbnew.ToMM(bb.GetBottom()))
    return None


def nudge_overlapping_footprints(board, spacing=0.5, max_passes=3):
    # Move overlapping footprints apart, never outside the board outline, and
    # refresh each mover's bbox so later pairs in the same pass see the new
    # position. Returns (move_count, moved_refs).
    outline = _board_outline_mm(board)

    def _overlap(a, b):  # non-strict AABB overlap (touching counts)
        return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]

    def _inside(nb):
        return outline is None or (nb[0] >= outline[0] and nb[1] >= outline[1]
                                   and nb[2] <= outline[2] and nb[3] <= outline[3])

    moved_refs = []
    move_count = 0
    for _pass in range(max_passes):
        fp_data = []
        for fp in board.GetFootprints():
            bbox = get_courtyard_bbox(fp)
            if bbox is None:
                continue
            fp_data.append({"ref": fp.GetReference(), "fp": fp,
                            "bbox": bbox, "nets": signal_net_count(fp)})
        pairs = [(fp_data[i], fp_data[j])
                 for i in range(len(fp_data)) for j in range(i + 1, len(fp_data))
                 if _overlap(fp_data[i]["bbox"], fp_data[j]["bbox"])]
        if not pairs:
            break
        moved = False
        for a, b in pairs:
            # move the less-connected footprint; deterministic tie-break by ref
            mover, anchor = (a, b) if (a["nets"], a["ref"]) <= (b["nets"], b["ref"]) else (b, a)
            mb = mover["bbox"]; ab = anchor["bbox"]
            ox = min(mb[2], ab[2]) - max(mb[0], ab[0])
            oy = min(mb[3], ab[3]) - max(mb[1], ab[1])
            if ox < 0 or oy < 0:  # a prior move this pass already separated them
                continue
            old = mover["fp"].GetPosition()
            old_x = pcbnew.ToMM(old.x); old_y = pcbnew.ToMM(old.y)
            cand = []
            axes = ((ox, 0), (oy, 1)) if ox <= oy else ((oy, 1), (ox, 0))
            for overlap, axis in axes:
                d = overlap + spacing
                if axis == 0:
                    s = 1 if (mb[0] + mb[2]) >= (ab[0] + ab[2]) else -1
                    cand.extend([(s * d, 0), (-s * d, 0)])
                else:
                    s = 1 if (mb[1] + mb[3]) >= (ab[1] + ab[3]) else -1
                    cand.extend([(0, s * d), (0, -s * d)])
            w = mb[2] - mb[0]; h = mb[3] - mb[1]
            off_x = old_x - (mb[0] + w / 2); off_y = old_y - (mb[1] + h / 2)
            for ddx, ddy in cand:
                nx = old_x + ddx; ny = old_y + ddy
                nb = (nx - off_x - w / 2, ny - off_y - h / 2,
                      nx - off_x + w / 2, ny - off_y + h / 2)
                if _inside(nb):
                    mover["fp"].SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(nx), pcbnew.FromMM(ny)))
                    mover["bbox"] = nb
                    move_count += 1
                    moved = True
                    moved_refs.append(mover["ref"])
                    break
        if not moved:
            break
    return move_count, moved_refs
"""

# ---------------------------------------------------------------------------
# Library search helper
# Provides: lib_search_paths list, find_lib(lib_name) -> path or None
# Requires: os in scope
# ---------------------------------------------------------------------------
LIB_SEARCH_HELPER = """
_kicad_app = os.environ.get("KICAD_APP_PATH", "/Applications/KiCad/KiCad.app")
lib_search_paths = [
    _kicad_app + "/Contents/SharedSupport/footprints",
    os.path.expanduser("~/Documents/KiCad/footprints"),
    "/usr/share/kicad/footprints",
]

def find_lib(lib_name):
    for sp in lib_search_paths:
        candidate = os.path.join(sp, lib_name + ".pretty")
        if os.path.isdir(candidate):
            return candidate
    return None
"""
