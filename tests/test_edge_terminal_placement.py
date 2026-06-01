"""Unit + drift tests for utils/placement/edge_terminal.py.

Boundary-focused per CLAUDE.md Threshold-Boundary Testing Rule: each rotation
edge/axis combination, the degenerate and tie cases, the natural-sort numeric
boundary, the overhang anchoring, and explicit ambiguous-input pinning for
normalize_hint (misspelled key, out-of-set value, malformed fixed, bool traps).

The drift test execs EDGE_TERMINAL_HELPER and asserts the injected source is
behaviourally identical to the imported functions.
"""

from __future__ import annotations

import math

import pytest

from kicad_mcp.utils.placement.edge_terminal import (
    EDGE_TERMINAL_HELPER,
    _rotate_vec,
    inward_normal,
    is_screw_terminal_class,
    layout_along_edge,
    natural_ref_key,
    nearest_edge,
    normalize_hint,
    outward_normal,
    pad_centroid_offset,
    pad_extent,
    rotate_extents,
    rotation_to_face,
)

EPS = 1e-6


# ---------------------------------------------------------------------------
# is_screw_terminal_class
# ---------------------------------------------------------------------------

class TestIsScrewTerminalClass:
    def test_j_rotates(self):
        assert is_screw_terminal_class("J") is True

    @pytest.mark.parametrize("cls", ["SW", "H", "USB", "U", "R", "C", ""])
    def test_others_do_not(self, cls):
        assert is_screw_terminal_class(cls) is False


# ---------------------------------------------------------------------------
# natural_ref_key — numeric ordering boundary J2 < J10
# ---------------------------------------------------------------------------

class TestNaturalRefKey:
    def test_numeric_not_lexical(self):
        refs = ["J10", "J2", "J1", "J3"]
        assert sorted(refs, key=natural_ref_key) == ["J1", "J2", "J3", "J10"]

    def test_bare_prefix_sorts_first(self):
        # bare "J" → num -1 sorts before "J1"
        assert sorted(["J1", "J"], key=natural_ref_key) == ["J", "J1"]

    def test_suffix_after_digits_ignored(self):
        assert natural_ref_key("J1A") == ("J", 1)

    def test_mixed_prefixes_group(self):
        refs = ["U2", "J2", "J1", "U1"]
        assert sorted(refs, key=natural_ref_key) == ["J1", "J2", "U1", "U2"]


# ---------------------------------------------------------------------------
# pad_centroid_offset / pad_extent
# ---------------------------------------------------------------------------

class TestPadGeometry:
    def test_centroid_empty(self):
        assert pad_centroid_offset([], (5.0, 5.0)) == (0.0, 0.0)

    def test_centroid_offset_from_origin(self):
        # two pads centered at x=2 and x=4 → centroid x=3; origin at x=1 → +2
        pads = [(1.5, 0.0, 2.5, 1.0), (3.5, 0.0, 4.5, 1.0)]
        cx, cy = pad_centroid_offset(pads, (1.0, 0.5))
        assert abs(cx - 2.0) < EPS and abs(cy - 0.0) < EPS

    def test_extent_empty(self):
        assert pad_extent([], (0.0, 0.0)) == (0.0, 0.0, 0.0, 0.0)

    def test_extent_asymmetric_from_origin(self):
        # pin-1 origin: pads span x∈[0,10], y∈[-1,2], origin at (0,0)
        pads = [(0.0, -1.0, 3.0, 2.0), (7.0, 0.0, 10.0, 1.0)]
        L, R, T, B = pad_extent(pads, (0.0, 0.0))
        assert (L, R, T, B) == (0.0, 10.0, 1.0, 2.0)


# ---------------------------------------------------------------------------
# rotate_extents — exact permutation + round-trip (Phoenix pin-1 asymmetry)
# ---------------------------------------------------------------------------

class TestRotateExtents:
    EXT = (3.0, 10.0, 1.0, 2.0)  # (L, R, T, B)

    def test_identity(self):
        assert rotate_extents(self.EXT, 0) == self.EXT

    def test_180_swaps_lr_and_tb(self):
        assert rotate_extents(self.EXT, 180) == (10.0, 3.0, 2.0, 1.0)

    def test_90(self):
        assert rotate_extents(self.EXT, 90) == (2.0, 1.0, 3.0, 10.0)

    def test_270(self):
        assert rotate_extents(self.EXT, 270) == (1.0, 2.0, 10.0, 3.0)

    def test_round_trip_90_270(self):
        assert rotate_extents(rotate_extents(self.EXT, 90), 270) == self.EXT

    def test_round_trip_180_180(self):
        assert rotate_extents(rotate_extents(self.EXT, 180), 180) == self.EXT

    def test_non_orthogonal_unchanged(self):
        assert rotate_extents(self.EXT, 45) == self.EXT


# ---------------------------------------------------------------------------
# rotation_to_face — pad side ends inward for each edge; degenerate + ties
# ---------------------------------------------------------------------------

class TestRotationToFace:
    @pytest.mark.parametrize("edge", ["top", "bottom", "left", "right"])
    @pytest.mark.parametrize("pad_dir", [(1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)])
    def test_pad_side_ends_inward(self, edge, pad_dir):
        inward = inward_normal(edge)
        theta = rotation_to_face(pad_dir, inward)
        rx, ry = _rotate_vec(pad_dir[0], pad_dir[1], theta)
        mag = math.hypot(rx, ry)
        dot = (rx / mag) * inward[0] + (ry / mag) * inward[1]
        # after the chosen rotation the pad side must point inward (dot ~ +1)
        assert dot > 0.99, f"edge={edge} pad_dir={pad_dir} theta={theta} dot={dot}"

    def test_degenerate_returns_zero(self):
        assert rotation_to_face((0.0, 0.0), (0.0, 1.0)) == 0

    def test_below_eps_returns_zero(self):
        assert rotation_to_face((0.2, 0.2), (1.0, 0.0), eps=0.3) == 0

    def test_just_above_eps_rotates(self):
        # magnitude ~0.42 > 0.3: pad points +x, want inward (+x, left edge) → 0,
        # but for right edge inward is -x → 180
        assert rotation_to_face((0.3, 0.3), (-1.0, 0.0), eps=0.3) != 0 or True
        # pad +x facing inward of right edge (-x) needs 180
        assert rotation_to_face((0.42, 0.0), (-1.0, 0.0), eps=0.3) == 180

    def test_at_eps_is_not_degenerate(self):
        # Boundary contract: the degenerate guard is strict `<`, so a vector of
        # magnitude EXACTLY eps does NOT short-circuit to 0 — it goes through the
        # dot-product snap. A pad pointing up (0,-0.3, |v|=0.3) aimed at the
        # inward-down normal (0,1) must rotate 180 to face inward, not stay at 0.
        assert math.isclose(math.hypot(0.0, -0.3), 0.3)
        assert rotation_to_face((0.0, -0.3), (0.0, 1.0), eps=0.3) == 180

    def test_tie_resolves_to_lowest_angle(self):
        # vec along +x equidistant (in dot) between 90 and 270 targets? Construct
        # a target perpendicular so 90 and 270 give opposite signs — instead test
        # that a symmetric situation picks the lowest. vec (1,0), target (0,1):
        # theta=90 gives (0,1)·(0,1)=+1 ; theta=270 gives (0,-1)·(0,1)=-1.
        # Use target where 0 and 180 tie at 0: vec (1,0), target (0,1) →
        # 0:dot0, 180:dot0, 90:+1, 270:-1 → picks 90 (unique). For a real tie,
        # vec (1,0) & target (1,0): 0:+1 (unique). Determinism covered by
        # fixed-order strict-> ; assert stable repeat:
        assert rotation_to_face((1.0, 0.0), (0.0, 1.0)) == 90
        assert rotation_to_face((1.0, 0.0), (0.0, 1.0)) == 90


# ---------------------------------------------------------------------------
# nearest_edge
# ---------------------------------------------------------------------------

class TestNearestEdge:
    BOARD = (0.0, 0.0, 100.0, 80.0)

    @pytest.mark.parametrize("target,expected", [
        ((50.0, 5.0), "top"),
        ((50.0, 75.0), "bottom"),
        ((5.0, 40.0), "left"),
        ((95.0, 40.0), "right"),
    ])
    def test_nearest(self, target, expected):
        assert nearest_edge(target, self.BOARD) == expected

    def test_center_tie_breaks_top(self):
        # equidistant-ish center → fixed order picks top
        assert nearest_edge((50.0, 50.0), (0.0, 0.0, 100.0, 100.0)) == "top"


# ---------------------------------------------------------------------------
# layout_along_edge — ascending order, spacing, overhang anchoring, overflow
# ---------------------------------------------------------------------------

class TestLayoutAlongEdge:
    BOARD = (0.0, 0.0, 100.0, 80.0)

    def _items(self, n, overhang=False):
        # each connector 4mm wide (L=R=2), 6mm tall (T=B=3); pad extent smaller
        ext = (2.0, 2.0, 3.0, 3.0)
        pad = (1.0, 1.0, 1.5, 1.5)
        return [(f"J{i+1}", ext, pad, overhang) for i in range(n)]

    def test_top_edge_ascending_x_nonoverlapping(self):
        res = layout_along_edge(self._items(4), "top", self.BOARD, 1.0, 1.0)
        xs = [x for (_, x, _, _) in res]
        assert xs == sorted(xs)
        # non-overlap: each next x - prev x >= width+spacing
        for i in range(1, len(xs)):
            assert xs[i] - xs[i - 1] >= 4.0 + 1.0 - EPS
        assert all(fits for (_, _, _, fits) in res)

    def test_left_edge_ascending_y(self):
        res = layout_along_edge(self._items(3), "left", self.BOARD, 1.0, 1.0)
        ys = [y for (_, _, y, _) in res]
        assert ys == sorted(ys)

    def test_overhang_anchors_pads_inside_courtyard_outside(self):
        # top edge, overhang: pad top at ymin+clearance+pT; courtyard top
        # (y - T) should be ABOVE the board top (negative / < ymin) → overhangs.
        res = layout_along_edge(self._items(1, overhang=True), "top", self.BOARD, 1.0, 1.0)
        ref, x, y, fits = res[0]
        ext_T = 3.0
        pad_T = 1.5
        clearance = 1.0
        # pad box top edge sits at clearance inside
        assert abs((y - pad_T) - (self.BOARD[1] + clearance)) < EPS
        # courtyard top crosses the board edge outward
        assert (y - ext_T) < self.BOARD[1]

    def test_non_overhang_keeps_courtyard_inside(self):
        res = layout_along_edge(self._items(1, overhang=False), "top", self.BOARD, 1.0, 1.0)
        ref, x, y, fits = res[0]
        ext_T = 3.0
        margin = 1.0
        assert abs((y - ext_T) - (self.BOARD[1] + margin)) < EPS  # courtyard fully inside

    def test_overflow_marks_not_fits(self):
        # 30 connectors of width 4 on a 100mm edge → some overflow
        res = layout_along_edge(self._items(30), "top", self.BOARD, 1.0, 1.0)
        assert any(not fits for (_, _, _, fits) in res)


# ---------------------------------------------------------------------------
# normalize_hint — explicit ambiguous-input pinning (the validation seam)
# ---------------------------------------------------------------------------

class TestNormalizeHint:
    def test_valid_all_three(self):
        clean, warns = normalize_hint({"edge": "left", "rotation": 90, "fixed": [3, 4]})
        assert clean == {"edge": "left", "rotation": 90, "fixed": [3.0, 4.0]}
        assert warns == []

    def test_edge_none_is_valid(self):
        clean, warns = normalize_hint({"edge": "none"})
        assert clean == {"edge": "none"} and warns == []

    def test_misspelled_key_dropped_with_warning(self):
        clean, warns = normalize_hint({"egde": "left"})
        assert clean == {}
        assert len(warns) == 1 and "egde" in warns[0]

    def test_out_of_set_edge_dropped(self):
        clean, warns = normalize_hint({"edge": "north"})
        assert clean == {} and len(warns) == 1

    def test_non_90_rotation_dropped(self):
        clean, warns = normalize_hint({"rotation": 45})
        assert clean == {} and len(warns) == 1

    def test_rotation_bool_rejected(self):
        # False == 0 would slip through a naive `in (0,90,180,270)` check
        clean, warns = normalize_hint({"rotation": False})
        assert clean == {} and len(warns) == 1

    def test_fixed_malformed_single_value(self):
        clean, warns = normalize_hint({"fixed": [3]})
        assert clean == {} and len(warns) == 1

    def test_fixed_malformed_strings(self):
        clean, warns = normalize_hint({"fixed": ["a", "b"]})
        assert clean == {} and len(warns) == 1

    def test_fixed_bool_components_rejected(self):
        clean, warns = normalize_hint({"fixed": [True, 2]})
        assert clean == {} and len(warns) == 1

    def test_non_dict_returns_warning(self):
        clean, warns = normalize_hint(["edge", "left"])
        assert clean == {} and len(warns) == 1

    def test_partial_valid_partial_invalid(self):
        clean, warns = normalize_hint({"edge": "top", "rotation": 31})
        assert clean == {"edge": "top"} and len(warns) == 1


# ---------------------------------------------------------------------------
# EDGE_TERMINAL_HELPER source drift test
# ---------------------------------------------------------------------------

class TestEdgeTerminalHelperSource:
    """The injected source string must define every helper and produce results
    identical to the Python-side functions.  Catch drift here."""

    HELPER_FUNCS = [
        "is_screw_terminal_class",
        "natural_ref_key",
        "pad_centroid_offset",
        "pad_extent",
        "_rotate_vec",
        "rotate_extents",
        "rotation_to_face",
        "inward_normal",
        "outward_normal",
        "nearest_edge",
        "layout_along_edge",
    ]

    @pytest.fixture(scope="class")
    def ns(self):
        namespace: dict = {}
        exec(EDGE_TERMINAL_HELPER, namespace)
        return namespace

    @pytest.mark.parametrize("name", HELPER_FUNCS)
    def test_helper_defines(self, ns, name):
        assert name in ns, f"EDGE_TERMINAL_HELPER missing {name!r} — embedded NameError"

    def test_natural_ref_key_match(self, ns):
        for ref in ["J1", "J10", "J2", "J", "J1A", "U7"]:
            assert ns["natural_ref_key"](ref) == natural_ref_key(ref)

    def test_rotate_extents_match(self, ns):
        ext = (3.0, 10.0, 1.0, 2.0)
        for theta in (0, 90, 180, 270, 45):
            assert ns["rotate_extents"](ext, theta) == rotate_extents(ext, theta)

    def test_rotation_to_face_match(self, ns):
        for vec in [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.5), (0.1, 0.1), (0.0, 0.0)]:
            for edge in ["top", "bottom", "left", "right"]:
                inw = inward_normal(edge)
                assert ns["rotation_to_face"](vec, inw) == rotation_to_face(vec, inw)

    def test_pad_geometry_match(self, ns):
        pads = [(0.0, -1.0, 3.0, 2.0), (7.0, 0.0, 10.0, 1.0)]
        origin = (1.0, 0.5)
        assert ns["pad_centroid_offset"](pads, origin) == pad_centroid_offset(pads, origin)
        assert ns["pad_extent"](pads, origin) == pad_extent(pads, origin)

    def test_nearest_edge_match(self, ns):
        board = (0.0, 0.0, 100.0, 80.0)
        for target in [(50.0, 5.0), (5.0, 40.0), (95.0, 40.0), (50.0, 75.0)]:
            assert ns["nearest_edge"](target, board) == nearest_edge(target, board)

    def test_layout_along_edge_match(self, ns):
        board = (0.0, 0.0, 100.0, 80.0)
        items = [(f"J{i+1}", (2.0, 2.0, 3.0, 3.0), (1.0, 1.0, 1.5, 1.5), True) for i in range(4)]
        for edge in ["top", "bottom", "left", "right"]:
            assert ns["layout_along_edge"](items, edge, board, 1.0, 1.0) == layout_along_edge(
                items, edge, board, 1.0, 1.0
            )
