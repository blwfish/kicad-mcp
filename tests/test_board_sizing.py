"""Content-aware board sizing (spec §4, terminal-edge-aware) — pure-math tests.

The human-rational layout: interior parts pack into a routed cluster; ALL
field-wiring terminals march along ONE edge (opposite the antenna), so the board
dimension ALONG that edge must seat them end-to-end, and the perpendicular
dimension gains a single terminal-depth band. Boundary-focused per CLAUDE.md:
terminal-vs-no-terminal, the seat-all-terminals constraint, the horizontal/
vertical axis swap, and the keepout-exclusion contract.
"""
import math

from kicad_mcp.tools.pcb_pipeline import (
    _content_aware_size,
    _hole_center_inset,
    _hole_corner_clear,
)


def _wh(size):
    return size["width_mm"], size["height_mm"]


def test_no_terminals_is_cluster_plus_padding():
    w, h = _wh(_content_aware_size([{"w": 10.0, "h": 10.0, "is_terminal": False}],
                                   padding=2.0))
    # cluster = sqrt(100*2.5)=15.81; no terminal band -> +2*padding both axes.
    assert (w, h) == (20, 20)


def test_terminals_fit_along_the_horizontal_edge():
    # 5 wide terminals must ALL seat along the width (one edge), not spill.
    comps = [{"w": 4.0, "h": 4.0, "is_terminal": False}]
    comps += [{"w": 20.0, "h": 5.0, "is_terminal": True} for _ in range(5)]
    w, h = _wh(_content_aware_size(comps, terminal_edge_horizontal=True,
                                   padding=2.0, spacing=1.0))
    # term_along = 5*(20+1)=105 -> width must seat them.
    assert w >= 105
    # height is just the (tiny) cluster + one terminal-depth band, NOT the length.
    assert h < 30


def test_vertical_edge_swaps_axes():
    comps = [{"w": 4.0, "h": 4.0, "is_terminal": False}]
    comps += [{"w": 20.0, "h": 5.0, "is_terminal": True} for _ in range(5)]
    horiz = _content_aware_size(comps, terminal_edge_horizontal=True)
    vert = _content_aware_size(comps, terminal_edge_horizontal=False)
    # Same content, axes swapped: the long terminal edge moves from W to H.
    assert (vert["width_mm"], vert["height_mm"]) == (horiz["height_mm"], horiz["width_mm"])
    assert vert["height_mm"] >= 105


def test_terminal_depth_band_only_on_the_terminal_edge():
    # One terminal: its DEPTH (short axis) adds to the perpendicular dim only,
    # NOT both — no antenna-edge band (that edge carries the overhanging MCU).
    interior = {"w": 20.0, "h": 20.0, "is_terminal": False}
    base = _content_aware_size([interior], padding=2.0)
    with_t = _content_aware_size([interior, {"w": 8.0, "h": 12.0, "is_terminal": True}],
                                 terminal_edge_horizontal=True, padding=2.0)
    # depth = min(8,12)=8 added to HEIGHT only; width unchanged (terminal short
    # along-edge span fits within the cluster width).
    assert with_t["height_mm"] - base["height_mm"] == 8
    assert with_t["width_mm"] == base["width_mm"]


def test_keepout_is_never_counted():
    a = _content_aware_size([{"w": 18.0, "h": 25.0, "is_terminal": False}])
    b = _content_aware_size([{"w": 18.0, "h": 25.0, "is_terminal": False,
                              "keepout_side": "top"}])
    assert a == b


def test_all_terminals_no_interior():
    comps = [{"w": 20.0, "h": 5.0, "is_terminal": True} for _ in range(3)]
    w, h = _wh(_content_aware_size(comps, terminal_edge_horizontal=True,
                                   padding=2.0, spacing=1.0))
    assert w >= 3 * 21          # all three seat along the width
    assert h == math.ceil(5 + 2 * 2.0)   # depth band + padding only


def test_largest_interior_dim_is_a_floor():
    w, h = _wh(_content_aware_size([{"w": 40.0, "h": 2.0, "is_terminal": False}]))
    assert w >= 40 and h >= 40   # a long part floors the cluster on both axes


def test_returns_single_size_not_a_list():
    s = _content_aware_size([{"w": 10.0, "h": 10.0, "is_terminal": False}])
    assert set(s) == {"width_mm", "height_mm"}


# --- corner mounting-hole inset (Phase 5) ------------------------------------

def test_corner_inset_zero_is_noop():
    # Both corner params default to 0.0 — that baseline must equal the no-inset
    # case, which is what keeps every other sizing test valid.
    comp = [{"w": 10.0, "h": 10.0, "is_terminal": False}]
    assert _content_aware_size(comp, padding=2.0) == \
        _content_aware_size(comp, padding=2.0, corner_inset_mm=0.0,
                            corner_center_inset_mm=0.0)


def test_corner_full_clearance_grows_only_the_terminal_edge_axis():
    # RFE #1: corner_inset_mm (full keepout clearance) is reserved ALONG the
    # terminal edge only. terminal_edge_horizontal=True -> along = WIDTH, so width
    # grows by 2*3.5 and height is untouched. (2*3.5 is integral, so it survives
    # the ceil() exactly.)
    comp = [{"w": 10.0, "h": 10.0, "is_terminal": False}]
    w0, h0 = _wh(_content_aware_size(comp, padding=2.0))
    w1, h1 = _wh(_content_aware_size(comp, padding=2.0, corner_inset_mm=3.5))
    assert (w1, h1) == (w0 + 7, h0)


def test_corner_center_inset_grows_only_the_depth_axis():
    # RFE #1: corner_center_inset_mm (hole-center inset) is reserved on the
    # PERPENDICULAR depth axis only. horizontal -> depth = HEIGHT.
    comp = [{"w": 10.0, "h": 10.0, "is_terminal": False}]
    w0, h0 = _wh(_content_aware_size(comp, padding=2.0))
    w1, h1 = _wh(_content_aware_size(comp, padding=2.0, corner_center_inset_mm=3.5))
    assert (w1, h1) == (w0, h0 + 7)


def test_corner_reservation_follows_the_terminal_edge_axis_on_swap():
    # The seam that matters: full-clearance tracks the terminal edge, center-inset
    # tracks the perpendicular — so on a VERTICAL terminal edge they swap onto the
    # other dimensions (full -> HEIGHT, center -> WIDTH). Pins the axis mapping so
    # a future edit can't silently transpose the two reservations.
    comp = [{"w": 10.0, "h": 10.0, "is_terminal": False}]
    w0, h0 = _wh(_content_aware_size(comp, padding=2.0,
                                     terminal_edge_horizontal=False))
    w1, h1 = _wh(_content_aware_size(comp, padding=2.0,
                                     terminal_edge_horizontal=False,
                                     corner_inset_mm=3.5, corner_center_inset_mm=1.5))
    assert (w1, h1) == (w0 + 3, h0 + 7)   # center 2*1.5 -> width, full 2*3.5 -> height


def test_hole_reserves_vanish_together_when_holes_off():
    # Both reserves are 0 when there are no holes, so a no-holes board sizes
    # exactly as if the params were never passed (mirrors each other's guard).
    assert _hole_corner_clear(None) == 0.0
    assert _hole_center_inset(None) == 0.0
    off = {"count": 0, "inset_mm": 3.5, "drill_mm": 3.2, "keepout_mm": 1.5}
    assert _hole_corner_clear(off) == 0.0
    assert _hole_center_inset(off) == 0.0


def test_center_inset_is_strictly_less_than_full_corner_clear():
    # The whole point of RFE #1: the depth reserve (center inset) is smaller than
    # the along reserve (full keepout clearance) — that gap is the height saved.
    holes = {"count": 4, "inset_mm": 3.5, "drill_mm": 3.2, "keepout_mm": 1.5}
    assert _hole_center_inset(holes) == 3.5
    assert _hole_corner_clear(holes) == 3.5 + 1.6 + 1.5      # 6.6
    assert _hole_center_inset(holes) < _hole_corner_clear(holes)
