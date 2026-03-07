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
"""

# ---------------------------------------------------------------------------
# Keepout zone helpers
# Provides: extract_keepouts(), get_board_outline(), rects_overlap(),
#           overlap_area(), rect_inside()
# Requires: pcbnew in scope (imported inside functions)
# ---------------------------------------------------------------------------
KEEPOUT_HELPER = """
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
        try:
            uuid_str = zone.m_Uuid.AsString()
        except Exception:
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
                "no_copper_pour": zone.GetDoNotAllowCopperPour(),
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
        try:
            for zone in fp.Zones():
                info = process_zone(zone, "footprint", fp.GetReference())
                if info:
                    keepouts.append(info)
        except AttributeError:
            pass
    return keepouts

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

def rects_overlap(a, b):
    return (a["x_min_mm"] < b["x_max_mm"] and a["x_max_mm"] > b["x_min_mm"] and
            a["y_min_mm"] < b["y_max_mm"] and a["y_max_mm"] > b["y_min_mm"])

def overlap_area(a, b):
    dx = max(0, min(a["x_max_mm"], b["x_max_mm"]) - max(a["x_min_mm"], b["x_min_mm"]))
    dy = max(0, min(a["y_max_mm"], b["y_max_mm"]) - max(a["y_min_mm"], b["y_min_mm"]))
    return round(dx * dy, 2)

def rect_inside(inner, outer):
    return (inner["x_min_mm"] >= outer["x_min_mm"] and inner["x_max_mm"] <= outer["x_max_mm"] and
            inner["y_min_mm"] >= outer["y_min_mm"] and inner["y_max_mm"] <= outer["y_max_mm"])
"""

# ---------------------------------------------------------------------------
# Courtyard bounding box helper (dict return)
# Provides: get_courtyard_bbox(fp) -> dict or None
# Requires: pcbnew, board in scope
# ---------------------------------------------------------------------------
COURTYARD_BBOX_HELPER = """
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
        return {"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}
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
        return {"x_min_mm": round(x_min, 3), "y_min_mm": round(y_min, 3),
                "x_max_mm": round(x_max, 3), "y_max_mm": round(y_max, 3)}
    return None
"""

# ---------------------------------------------------------------------------
# Courtyard bounding box helper (tuple return)
# Provides: get_courtyard_bbox(fp) -> (x_min, y_min, x_max, y_max) or None,
#           POWER_NETS, signal_net_count(fp)
# Requires: pcbnew, board in scope
# ---------------------------------------------------------------------------
COURTYARD_BBOX_TUPLE_HELPER = """
POWER_NETS = {"", "GND", "+5V", "+3V3", "+3.3V", "+12V", "VCC", "VDD", "VSS", "VBUS"}

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
"""

# ---------------------------------------------------------------------------
# Library search helper
# Provides: lib_search_paths list, find_lib(lib_name) -> path or None
# Requires: os in scope
# ---------------------------------------------------------------------------
LIB_SEARCH_HELPER = """
lib_search_paths = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
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
