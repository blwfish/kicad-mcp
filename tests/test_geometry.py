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
