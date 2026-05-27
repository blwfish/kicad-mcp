"""Boundary tests for the canonical geometry primitives in utils/geometry.

These tests pin the NON-STRICT-inequality semantics: touching edges and
coincident boundaries are treated as the violating case (match KiCad
DRC).  The CLAUDE.md Threshold-Boundary Testing Rule requires
at/just-below/just-above coverage for any threshold, and the
consumer-asymmetry rule requires that results are monotonic across the
boundary.

The 1µm epsilon matches the sub-micrometre noise KiCad's ``ToMM``
produces after FromMM/ToMM round-trips; tests at coarser tolerance miss
real-world floating-point edge cases.
"""

import pytest

from kicad_mcp.utils.geometry import (
    GEOMETRY_HELPER,
    aabb_inside,
    aabb_overlap,
    clearance_violation,
    expand_bbox,
    overlap_area,
    rect_inside,
    rects_overlap,
    signed_gap_mm,
)


EPS = 1e-6  # KiCad ToMM round-trip noise scale


def _r(x1, y1, x2, y2):
    """Build a dict-format rect for tests."""
    return {"x_min_mm": x1, "y_min_mm": y1, "x_max_mm": x2, "y_max_mm": y2}


# ---------------------------------------------------------------------------
# rects_overlap — non-strict (touching counts as overlap)
# ---------------------------------------------------------------------------

class TestRectsOverlap:
    @pytest.mark.parametrize("edge,b", [
        ("right",  _r(10, 0, 20, 10)),
        ("left",   _r(-10, 0, 0, 10)),
        ("bottom", _r(0, 10, 10, 20)),
        ("top",    _r(0, -10, 10, 0)),
    ])
    def test_touching_each_edge_returns_true(self, edge, b):
        a = _r(0, 0, 10, 10)
        assert rects_overlap(a, b) is True, f"Touching {edge} edge must overlap (non-strict)"

    @pytest.mark.parametrize("corner,b", [
        ("top-right",    _r(10, -10, 20, 0)),
        ("top-left",     _r(-10, -10, 0, 0)),
        ("bottom-right", _r(10, 10, 20, 20)),
        ("bottom-left",  _r(-10, 10, 0, 20)),
    ])
    def test_touching_each_corner_returns_true(self, corner, b):
        a = _r(0, 0, 10, 10)
        assert rects_overlap(a, b) is True, f"Touching {corner} corner must overlap (non-strict)"

    def test_overlap_by_epsilon_returns_true(self):
        a = _r(0, 0, 10, 10)
        b = _r(10 - EPS, 0, 20, 10)
        assert rects_overlap(a, b) is True

    def test_separated_by_epsilon_returns_false(self):
        a = _r(0, 0, 10, 10)
        b = _r(10 + EPS, 0, 20, 10)
        assert rects_overlap(a, b) is False

    def test_exact_match_overlaps(self):
        rect = _r(0, 0, 10, 10)
        assert rects_overlap(rect, rect) is True


# ---------------------------------------------------------------------------
# rect_inside — non-strict (touching boundary counts as inside)
# ---------------------------------------------------------------------------

class TestRectInside:
    def test_exact_match_returns_true(self):
        """Non-strict containment: a rect equal to its outer counts as inside."""
        rect = _r(0, 0, 100, 100)
        assert rect_inside(rect, rect) is True

    @pytest.mark.parametrize("edge,inner", [
        ("left",   _r(0, 50, 10, 60)),
        ("right",  _r(90, 50, 100, 60)),
        ("top",    _r(50, 0, 60, 10)),
        ("bottom", _r(50, 90, 60, 100)),
    ])
    def test_flush_against_each_edge_returns_true(self, edge, inner):
        outer = _r(0, 0, 100, 100)
        assert rect_inside(inner, outer) is True, (
            f"Inner flush against {edge} edge counts as inside (non-strict)"
        )

    def test_inner_strictly_inside_returns_true(self):
        outer = _r(0, 0, 100, 100)
        inner = _r(1, 1, 99, 99)
        assert rect_inside(inner, outer) is True

    def test_inner_outside_by_epsilon_returns_false(self):
        outer = _r(0, 0, 100, 100)
        inner = _r(0 - EPS, 0, 100, 100)
        assert rect_inside(inner, outer) is False


# ---------------------------------------------------------------------------
# overlap_area — touching = 0.0 (touching has no shared area even if classified as overlap)
# ---------------------------------------------------------------------------

class TestOverlapArea:
    def test_touching_returns_zero(self):
        """Touching rects classify as overlapping (rects_overlap True) but
        share zero area — sign of overlap lives in signed_gap_mm, depth
        of overlap lives in overlap_area."""
        assert overlap_area(_r(0, 0, 10, 10), _r(10, 0, 20, 10)) == 0.0

    def test_disjoint_returns_zero(self):
        assert overlap_area(_r(0, 0, 10, 10), _r(50, 50, 60, 60)) == 0.0

    def test_unit_overlap(self):
        assert overlap_area(_r(0, 0, 10, 10), _r(9, 9, 20, 20)) == 1.0

    def test_subpixel_overlap_rounds_to_zero(self):
        # 1µm × 1µm = 1e-12 mm² → rounds to 0.00 at 2 decimals
        a = _r(0, 0, 10, 10)
        b = _r(10 - EPS, 10 - EPS, 20, 20)
        assert overlap_area(a, b) == 0.0


# ---------------------------------------------------------------------------
# signed_gap_mm — sign distinguishes touching from embedded
# ---------------------------------------------------------------------------

class TestSignedGap:
    def test_touching_returns_zero(self):
        assert signed_gap_mm(_r(0, 0, 10, 10), _r(10, 0, 20, 10)) == 0.0

    def test_separated_returns_positive(self):
        assert signed_gap_mm(_r(0, 0, 10, 10), _r(15, 0, 25, 10)) == pytest.approx(5.0)

    def test_overlap_returns_negative(self):
        """Both axes overlap → gap is negative penetration depth."""
        # A: (0,0)-(10,10); B: (8,8)-(18,18). Overlap region (8,8)-(10,10).
        # gap_x = max(0, 8) - min(10, 18) = -2; gap_y = -2
        # Both negative → max(-2, -2) = -2 (penetration depth)
        assert signed_gap_mm(_r(0, 0, 10, 10), _r(8, 8, 18, 18)) == pytest.approx(-2.0)

    def test_separated_on_one_axis_returns_axial_gap(self):
        """Overlap on y, separated on x → gap is the x-separation."""
        # A: (0,0)-(10,10); B: (15,5)-(25,8). x_gap = 5; y overlaps.
        assert signed_gap_mm(_r(0, 0, 10, 10), _r(15, 5, 25, 8)) == pytest.approx(5.0)

    def test_sign_distinguishes_touching_from_embedded(self):
        """The whole point of signed_gap: gap=0 (touching) ≠ gap<0 (embedded)."""
        touching = signed_gap_mm(_r(0, 0, 10, 10), _r(10, 0, 20, 10))
        embedded_1 = signed_gap_mm(_r(0, 0, 10, 10), _r(9, 0, 19, 10))
        embedded_5 = signed_gap_mm(_r(0, 0, 10, 10), _r(5, 0, 15, 10))
        assert touching == 0.0
        assert embedded_1 == pytest.approx(-1.0)
        assert embedded_5 == pytest.approx(-5.0)
        # Monotonic: deeper embedment → more negative
        assert embedded_5 < embedded_1 < touching


# ---------------------------------------------------------------------------
# aabb_overlap / aabb_inside — tuple format, same non-strict semantics
# ---------------------------------------------------------------------------

class TestAabbTuple:
    def test_overlap_touching_returns_true(self):
        assert aabb_overlap((0, 0, 10, 10), (10, 0, 20, 10)) is True

    def test_overlap_just_inside_returns_true(self):
        assert aabb_overlap((0, 0, 10, 10), (10 - EPS, 0, 20, 10)) is True

    def test_overlap_just_past_returns_false(self):
        assert aabb_overlap((0, 0, 10, 10), (10 + EPS, 0, 20, 10)) is False

    def test_inside_flush_returns_true(self):
        assert aabb_inside((0, 50, 10, 60), (0, 0, 100, 100)) is True

    def test_inside_strictly_returns_true(self):
        assert aabb_inside((1, 1, 99, 99), (0, 0, 100, 100)) is True

    def test_inside_past_edge_returns_false(self):
        assert aabb_inside((-EPS, 0, 100, 100), (0, 0, 100, 100)) is False


# ---------------------------------------------------------------------------
# GEOMETRY_HELPER source must define the same functions
# ---------------------------------------------------------------------------

class TestGeometryHelperSource:
    """The embedded-script helper string must define every primitive the
    Python module exports — otherwise embedded scripts will NameError at
    runtime. Catch any drift between the two sources here.
    """

    @pytest.mark.parametrize("name", [
        "aabb_overlap",
        "aabb_inside",
        "rects_overlap",
        "rect_inside",
        "overlap_area",
        "signed_gap_mm",
        "expand_bbox",
        "clearance_violation",
    ])
    def test_helper_defines(self, name):
        assert f"def {name}" in GEOMETRY_HELPER, (
            f"GEOMETRY_HELPER missing {name!r} — embedded scripts will NameError"
        )

    def test_helper_non_strict_semantics(self):
        """Exec the helper source and verify it behaves identically to the Python module."""
        namespace: dict = {}
        exec(GEOMETRY_HELPER, namespace)
        a = _r(0, 0, 10, 10)
        b = _r(10, 0, 20, 10)
        # Touching: both Python and helper agree (both True under non-strict)
        assert namespace["rects_overlap"](a, b) is rects_overlap(a, b)
        assert namespace["rects_overlap"](a, b) is True
        # Self-containment: both True under non-strict
        assert namespace["rect_inside"](a, a) is rect_inside(a, a)
        assert namespace["rect_inside"](a, a) is True
        # Touching area: 0.0 regardless of strictness
        assert namespace["overlap_area"](a, b) == overlap_area(a, b)
        # Signed gap: 0.0 for touching, sign preserved
        assert namespace["signed_gap_mm"](a, b) == signed_gap_mm(a, b)

    def test_helper_expand_bbox_identical(self):
        """expand_bbox in GEOMETRY_HELPER must match Python module."""
        namespace: dict = {}
        exec(GEOMETRY_HELPER, namespace)
        rect = _r(0, 0, 10, 10)
        for margin in (0.0, 1.0, -1.0, 0.5):
            expected = expand_bbox(rect, margin)
            got = namespace["expand_bbox"](rect, margin)
            assert got == expected, f"expand_bbox mismatch at margin={margin}"

    def test_helper_clearance_violation_identical(self):
        """clearance_violation in GEOMETRY_HELPER must match Python module."""
        namespace: dict = {}
        exec(GEOMETRY_HELPER, namespace)
        a = _r(0, 0, 10, 10)
        # touching, gap=0.5, gap=1e-6
        cases = [
            (_r(10, 0, 20, 10), 0.0),   # touching, zero clearance
            (_r(10, 0, 20, 10), 0.5),   # touching, positive clearance
            (_r(10.5, 0, 20, 10), 0.5), # gap == clearance (threshold)
            (_r(15, 0, 25, 10), 0.0),   # well separated, zero clearance
        ]
        for b, cl in cases:
            expected = clearance_violation(a, b, cl)
            got = namespace["clearance_violation"](a, b, cl)
            assert got == expected, f"clearance_violation mismatch: b={b}, cl={cl}"


# ---------------------------------------------------------------------------
# expand_bbox — boundary tests
# ---------------------------------------------------------------------------

class TestExpandBbox:
    def test_zero_margin_is_noop(self):
        rect = _r(2, 3, 8, 9)
        result = expand_bbox(rect, 0.0)
        assert result == rect
        assert result is not rect  # returns a copy

    def test_positive_margin_expands_all_sides(self):
        result = expand_bbox(_r(2, 3, 8, 9), 1.0)
        assert result["x_min_mm"] == pytest.approx(1.0)
        assert result["y_min_mm"] == pytest.approx(2.0)
        assert result["x_max_mm"] == pytest.approx(9.0)
        assert result["y_max_mm"] == pytest.approx(10.0)

    def test_negative_margin_shrinks_all_sides(self):
        """Negative margin shrinks; if margin > half-width the rect inverts."""
        result = expand_bbox(_r(0, 0, 10, 10), -1.0)
        assert result["x_min_mm"] == pytest.approx(1.0)
        assert result["y_min_mm"] == pytest.approx(1.0)
        assert result["x_max_mm"] == pytest.approx(9.0)
        assert result["y_max_mm"] == pytest.approx(9.0)

    def test_negative_margin_larger_than_half_produces_inverted_rect(self):
        """Document: margin > half-width → x_min_mm > x_max_mm (inverted).
        Callers must guard against this if using the result with rects_overlap."""
        result = expand_bbox(_r(0, 0, 4, 4), -3.0)
        # x_min_mm = 0 - (-3) = 3; x_max_mm = 4 + (-3) = 1
        assert result["x_min_mm"] > result["x_max_mm"]

    def test_epsilon_margin(self):
        result = expand_bbox(_r(0, 0, 10, 10), EPS)
        assert result["x_min_mm"] == pytest.approx(-EPS)
        assert result["x_max_mm"] == pytest.approx(10 + EPS)

    def test_expand_preserves_center(self):
        """Expanding uniformly should not shift the center."""
        rect = _r(0, 0, 10, 10)
        expanded = expand_bbox(rect, 2.0)
        orig_cx = (rect["x_min_mm"] + rect["x_max_mm"]) / 2
        orig_cy = (rect["y_min_mm"] + rect["y_max_mm"]) / 2
        exp_cx = (expanded["x_min_mm"] + expanded["x_max_mm"]) / 2
        exp_cy = (expanded["y_min_mm"] + expanded["y_max_mm"]) / 2
        assert exp_cx == pytest.approx(orig_cx)
        assert exp_cy == pytest.approx(orig_cy)


# ---------------------------------------------------------------------------
# clearance_violation — boundary tests
# ---------------------------------------------------------------------------

class TestClearanceViolation:
    """Boundary coverage per CLAUDE.md Threshold-Boundary Testing Rule.

    The threshold is min_clearance_mm; three cases per value: at, below, above.
    Also tests that min_clearance_mm=0 is strictly equivalent to rects_overlap.
    """

    # -- min_clearance=0: must equal rects_overlap ---------------------------

    def test_zero_clearance_touching_is_violation(self):
        """gap=0 (touching) with min_clearance=0 must be a violation (non-strict)."""
        a = _r(0, 0, 10, 10)
        b = _r(10, 0, 20, 10)
        assert clearance_violation(a, b, 0.0) is True
        assert clearance_violation(a, b, 0.0) is rects_overlap(a, b)

    def test_zero_clearance_overlapping_is_violation(self):
        a = _r(0, 0, 10, 10)
        b = _r(8, 0, 18, 10)
        assert clearance_violation(a, b, 0.0) is True
        assert clearance_violation(a, b, 0.0) is rects_overlap(a, b)

    def test_zero_clearance_separated_is_not_violation(self):
        a = _r(0, 0, 10, 10)
        b = _r(10 + EPS, 0, 20, 10)
        assert clearance_violation(a, b, 0.0) is False
        assert clearance_violation(a, b, 0.0) is rects_overlap(a, b)

    @pytest.mark.parametrize("edge,b", [
        ("right",  _r(10, 0, 20, 10)),
        ("left",   _r(-10, 0, 0, 10)),
        ("bottom", _r(0, 10, 10, 20)),
        ("top",    _r(0, -10, 10, 0)),
    ])
    def test_zero_clearance_matches_rects_overlap_all_edges(self, edge, b):
        a = _r(0, 0, 10, 10)
        assert clearance_violation(a, b, 0.0) is rects_overlap(a, b)

    # -- min_clearance=0.5: threshold at/below/above -------------------------

    def test_gap_equals_clearance_is_not_violation(self):
        """gap == min_clearance: the limit is exclusive — exactly at threshold is clean."""
        a = _r(0, 0, 10, 10)
        b = _r(10.5, 0, 20, 10)  # gap = 0.5
        assert signed_gap_mm(a, b) == pytest.approx(0.5)
        assert clearance_violation(a, b, 0.5) is False

    def test_gap_just_below_clearance_is_violation(self):
        """gap < min_clearance by epsilon: violation."""
        a = _r(0, 0, 10, 10)
        b = _r(10.5 - EPS, 0, 20, 10)  # gap = 0.5 - 1e-6
        assert clearance_violation(a, b, 0.5) is True

    def test_gap_just_above_clearance_is_not_violation(self):
        """gap > min_clearance by epsilon: no violation."""
        a = _r(0, 0, 10, 10)
        b = _r(10.5 + EPS, 0, 20, 10)  # gap = 0.5 + 1e-6
        assert clearance_violation(a, b, 0.5) is False

    def test_touching_with_positive_clearance_is_violation(self):
        """gap=0 (touching) with min_clearance=0.5: violation (gap < 0.5)."""
        a = _r(0, 0, 10, 10)
        b = _r(10, 0, 20, 10)
        assert clearance_violation(a, b, 0.5) is True

    def test_overlap_with_positive_clearance_is_violation(self):
        """Overlapping rects always violate any non-negative clearance."""
        a = _r(0, 0, 10, 10)
        b = _r(8, 0, 18, 10)  # gap = -2
        assert clearance_violation(a, b, 0.5) is True
        assert clearance_violation(a, b, 0.0) is True

    def test_large_gap_no_violation(self):
        a = _r(0, 0, 10, 10)
        b = _r(100, 0, 110, 10)
        assert clearance_violation(a, b, 0.5) is False
        assert clearance_violation(a, b, 0.0) is False

    # -- monotonicity: increasing clearance produces more violations ----------

    def test_monotone_in_clearance(self):
        """A gap of 1.0 mm: no violation at cl=0.99-eps, violation at cl=1.0+eps."""
        a = _r(0, 0, 10, 10)
        b = _r(11.0, 0, 20, 10)  # gap = 1.0
        cl = 1.0
        assert signed_gap_mm(a, b) == pytest.approx(cl)
        # At threshold: clean
        assert clearance_violation(a, b, cl) is False
        # Just above threshold: violation
        assert clearance_violation(a, b, cl + EPS) is True
        # Just below threshold: clean
        assert clearance_violation(a, b, cl - EPS) is False
