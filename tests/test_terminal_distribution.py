"""Pure tests for ``distribute_terminals`` (SPEC_Multi_Edge_Terminal_Distribution
§8.1). Boundary-focused per CLAUDE.md: the regression lock (single_edge ≡ today's
``_content_aware_size``), the 4-antenna-sides axis mapping, the spill heuristic,
and the degenerate counts (0, 1, near-square)."""
from __future__ import annotations

import pytest

from kicad_mcp.tools.pcb_pipeline import _content_aware_size
from kicad_mcp.utils.placement.edge_terminal import distribute_terminals

# A reusable cluster + a wide terminal roster (the audio-remote shape: many
# terminals → a wide letterbox in single-edge mode).
_INT = [{"ref": "U1", "w": 18.0, "h": 18.0, "is_terminal": False},
        {"ref": "U2", "w": 6.0, "h": 5.0, "is_terminal": False}]
_TERMS = [{"ref": f"J{i}", "w": 10.0, "h": 12.0, "is_terminal": True} for i in range(1, 7)]


def _wh(size):
    return size["width_mm"], size["height_mm"]


# --- THE regression lock: single_edge must equal _content_aware_size exactly ----

@pytest.mark.parametrize("antenna,horiz", [
    ("top", True), ("bottom", True), ("left", False), ("right", False), (None, True),
])
@pytest.mark.parametrize("ci,cc", [(0.0, 0.0), (6.6, 3.5), (3.0, 3.0)])
def test_single_edge_equals_content_aware_size(antenna, horiz, ci, cc):
    comps = _INT + _TERMS
    got = distribute_terminals(comps, antenna, mode="single_edge",
                               corner_inset_mm=ci, corner_center_inset_mm=cc)["size"]
    want = _content_aware_size(comps, terminal_edge_horizontal=horiz,
                               corner_inset_mm=ci, corner_center_inset_mm=cc)
    assert got == want


def test_single_edge_no_terminals_matches_baseline():
    got = distribute_terminals(_INT, "top", mode="single_edge")
    assert got["edge_of"] == {}
    assert got["size"] == _content_aware_size(_INT, terminal_edge_horizontal=True)


# --- edge assignment: opposite the antenna, never the antenna edge --------------

@pytest.mark.parametrize("antenna,primary", [
    ("top", "bottom"), ("bottom", "top"), ("left", "right"), ("right", "left"),
])
def test_primary_is_opposite_antenna(antenna, primary):
    got = distribute_terminals(_INT + _TERMS, antenna, mode="single_edge")
    assert got["primary_edge"] == primary
    assert set(got["edge_of"].values()) == {primary}


@pytest.mark.parametrize("antenna", ["top", "bottom", "left", "right"])
def test_multi_edge_never_uses_antenna_edge(antenna):
    got = distribute_terminals(_INT + _TERMS, antenna, mode="multi_edge",
                               corner_inset_mm=6.6, corner_center_inset_mm=3.5)
    assert antenna not in set(got["edge_of"].values())
    # only the opposite + the two perpendicular sides are ever used
    sides = {"top", "bottom", "left", "right"} - {antenna}
    assert set(got["edge_of"].values()) <= sides


# --- the heuristic: spill only when it helps ------------------------------------

def test_multi_edge_spills_a_wide_letterbox_and_squares_it():
    comps = _INT + _TERMS
    single = distribute_terminals(comps, "top", mode="single_edge",
                                  corner_inset_mm=6.6, corner_center_inset_mm=3.5)
    multi = distribute_terminals(comps, "top", mode="multi_edge",
                                 corner_inset_mm=6.6, corner_center_inset_mm=3.5)
    sw, sh = _wh(single["size"]); mw, mh = _wh(multi["size"])
    assert len(set(multi["edge_of"].values())) >= 2, "expected a spill to side edges"
    assert abs(mw - mh) < abs(sw - sh), "multi-edge should be squarer"
    assert mw * mh <= sw * sh, "multi-edge area must not be worse"


def test_multi_edge_noops_when_already_near_square():
    # A roster that single-edges to a near-square board: spilling must NOT happen.
    comps = _INT + [{"ref": "J1", "w": 10.0, "h": 12.0, "is_terminal": True}]
    single = distribute_terminals(comps, "top", mode="single_edge")
    multi = distribute_terminals(comps, "top", mode="multi_edge")
    assert multi["edge_of"] == single["edge_of"]
    assert multi["size"] == single["size"]


def test_one_terminal_is_never_split():
    comps = _INT + [{"ref": "J1", "w": 10.0, "h": 12.0, "is_terminal": True}]
    got = distribute_terminals(comps, "top", mode="multi_edge")
    assert set(got["edge_of"].values()) == {"bottom"}


def test_zero_terminals_multi_edge():
    got = distribute_terminals(_INT, "top", mode="multi_edge")
    assert got["edge_of"] == {}


# --- ordering preserved per side (the layout gate requires it) ------------------

def test_peeled_sides_keep_natural_ref_order():
    from kicad_mcp.utils.placement.edge_terminal import natural_ref_key
    got = distribute_terminals(_INT + _TERMS, "top", mode="multi_edge",
                               corner_inset_mm=6.6, corner_center_inset_mm=3.5)
    by_edge: dict = {}
    for ref, edge in got["edge_of"].items():
        by_edge.setdefault(edge, []).append(ref)
    # edge_of is built by iterating terminals in natural order, so each edge's refs
    # come out already sorted — exactly what the integration layout gate asserts.
    for edge, refs in by_edge.items():
        assert refs == sorted(refs, key=natural_ref_key), \
            f"{edge} refs out of natural order: {refs}"


def test_deterministic():
    a = distribute_terminals(_INT + _TERMS, "top", mode="multi_edge",
                             corner_inset_mm=6.6, corner_center_inset_mm=3.5)
    b = distribute_terminals(_INT + _TERMS, "top", mode="multi_edge",
                             corner_inset_mm=6.6, corner_center_inset_mm=3.5)
    assert a == b


def test_vertical_antenna_transposes_axes():
    # Same roster, antenna on top (horizontal terminals) vs left (vertical): the
    # single-edge sizes are transposes of each other.
    comps = _INT + _TERMS
    horiz = distribute_terminals(comps, "top", mode="single_edge")["size"]
    vert = distribute_terminals(comps, "left", mode="single_edge")["size"]
    assert (horiz["width_mm"], horiz["height_mm"]) == (vert["height_mm"], vert["width_mm"])
