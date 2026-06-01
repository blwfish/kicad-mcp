"""Content-aware board sizing (spec §4, Phase 4) — pure-math unit tests.

Boundary-focused per CLAUDE.md Threshold-Boundary Rule: terminal-vs-no-terminal,
empty/all-terminal, the perimeter-fit growth edge, and the keepout-exclusion
contract (the old estimator's keepout term over-sized antenna boards).
"""
import math

from kicad_mcp.tools.pcb_pipeline import _content_aware_size


def _wh(suggestions, label="4:3"):
    s = next(x for x in suggestions if x["label"] == label)
    return s["width_mm"], s["height_mm"]


def test_no_terminals_has_no_perimeter_band():
    # One interior part; size is the routed cluster + padding, no terminal depth.
    comps = [{"w": 10.0, "h": 10.0, "is_terminal": False}]
    w, h = _wh(_content_aware_size(comps, routing_factor=2.5, padding=2.0), "square")
    # cluster side = sqrt(100*2.5)=15.81; +0 depth +4 padding -> 19.81 -> ceil 20
    assert (w, h) == (20, 20)


def test_terminals_reserve_depth_on_each_side():
    interior = {"w": 10.0, "h": 10.0, "is_terminal": False}
    # A 5x10 terminal: depth = min = 5, reserved on BOTH sides of each axis.
    with_term = _content_aware_size([interior, {"w": 5.0, "h": 10.0, "is_terminal": True}], padding=2.0)
    without = _content_aware_size([interior], padding=2.0)
    ww, wh = _wh(with_term, "square")
    ow, oh = _wh(without, "square")
    # +2*depth(5) = +10 on each dim (terminal area itself is NOT added to interior).
    assert ww - ow == 10 and wh - oh == 10


def test_keepout_is_never_counted():
    # The function only sees body w/h; an antenna keepout overhangs (spec §2) and
    # must not add area. Extra keys are ignored -> identical size.
    a = _content_aware_size([{"w": 18.0, "h": 25.0, "is_terminal": False}])
    b = _content_aware_size([{"w": 18.0, "h": 25.0, "is_terminal": False,
                              "keepout_area": 500.0}])
    assert a == b


def test_all_terminals_no_interior_cluster():
    comps = [{"w": 5.0, "h": 10.0, "is_terminal": True}]
    w, h = _wh(_content_aware_size(comps, padding=2.0), "square")
    # No interior -> cluster 0; just the depth band + padding: 2*5 + 2*2 = 14.
    assert (w, h) == (14, 14)


def test_all_terminals_board_grows_to_seat_them():
    # Regression: with no interior the cluster perimeter is 0, so growth must key
    # off the BOARD perimeter — else a many-terminal adapter stays 14x14 and the
    # terminals can't be seated (under-size).
    comps = [{"w": 20.0, "h": 5.0, "is_terminal": True} for _ in range(4)]
    w, h = _wh(_content_aware_size(comps, padding=2.0), "square")
    assert 2 * (w + h) >= 4 * 20.0  # total terminal length 80mm fits the perimeter


def test_perimeter_growth_boundary():
    # Pin the `term_len_sum > board_perim` (strict) boundary. Use a ~zero-DEPTH
    # terminal so the depth band doesn't perturb the board — only its LENGTH,
    # which drives the perimeter-fit growth, varies.
    interior = {"w": 20.0, "h": 20.0, "is_terminal": False}
    bw, bh = _wh(_content_aware_size([interior], padding=2.0), "square")
    perim = 2 * (bw + bh)

    def board_perim(term_len):
        comps = [interior, {"w": term_len, "h": 0.0, "is_terminal": True}]
        w, h = _wh(_content_aware_size(comps, padding=2.0), "square")
        return 2 * (w + h)

    # At/below the perimeter -> no meaningful growth (stays ~base, modulo ceil).
    assert board_perim(perim) <= perim + 4
    assert board_perim(perim * 0.5) <= perim + 4
    # Well above -> grows so the perimeter can seat the terminal end-to-end.
    assert board_perim(perim * 2.0) >= perim * 2.0 - 4


def test_empty_components_is_zero_plus_padding():
    w, h = _wh(_content_aware_size([], padding=2.0), "square")
    assert (w, h) == (4, 4)  # 2*padding only


def test_largest_interior_dim_is_a_floor():
    # A single long part must fit even if its area is small.
    comps = [{"w": 40.0, "h": 2.0, "is_terminal": False}]
    w, h = _wh(_content_aware_size(comps), "square")
    assert w >= 40 and h >= 40  # max_interior_dim floors both cluster axes


def test_perimeter_grows_to_seat_many_long_terminals():
    # Tiny interior, but lots of terminal length -> the board grows so the
    # perimeter can actually seat them end-to-end.
    interior = [{"w": 4.0, "h": 4.0, "is_terminal": False}]
    many = interior + [{"w": 30.0, "h": 3.0, "is_terminal": True} for _ in range(8)]
    w, h = _wh(_content_aware_size(many), "square")
    # 8 terminals * 30mm long = 240mm must fit around the perimeter 2*(w+h).
    assert 2 * (w + h) >= 240


def test_suggestions_cover_three_aspects():
    s = _content_aware_size([{"w": 10.0, "h": 10.0, "is_terminal": False}])
    assert {x["label"] for x in s} == {"square", "4:3", "3:2"}
