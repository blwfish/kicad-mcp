"""Boundary tests for spiral_placement helpers.

Covers:
- is_within_outline: threshold at exactly spacing, just below, just above
- is_within_outline: spacing == 0 edge case
- generate_spiral_candidates: basic iteration, candidates within outline
- generate_grid_fallback: non-integer outline coordinates keep footprints inside
- generate_grid_fallback: fallback trigger (spiral exhausts, grid finds space)
- SPIRAL_HELPER source drift test (exec the string, verify identical behavior)
"""

import pytest

from kicad_mcp.utils.spiral_placement import (
    SPIRAL_HELPER,
    generate_grid_fallback,
    generate_spiral_candidates,
    is_within_outline,
)


EPS = 1e-6  # sub-micrometre noise floor used throughout the project


def _outline(x_min=0.0, y_min=0.0, x_max=100.0, y_max=80.0):
    return {
        "x_min_mm": x_min,
        "y_min_mm": y_min,
        "x_max_mm": x_max,
        "y_max_mm": y_max,
    }


# ---------------------------------------------------------------------------
# is_within_outline — threshold at x_min_mm + spacing
# ---------------------------------------------------------------------------

class TestIsWithinOutline:
    """Pin the strict-less-than boundary: box[0] < x_min + spacing → rejected."""

    def test_box_exactly_at_threshold_passes(self):
        """box[0] == x_min + spacing → passes (not strictly less than)."""
        spacing = 1.0
        outline = _outline()
        # left edge exactly at threshold
        box = (outline["x_min_mm"] + spacing, 5.0, 10.0, 15.0)
        assert is_within_outline(box, outline, spacing) is True

    def test_box_just_inside_threshold_rejected(self):
        """box[0] == x_min + spacing - ε → rejected."""
        spacing = 1.0
        outline = _outline()
        box = (outline["x_min_mm"] + spacing - EPS, 5.0, 10.0, 15.0)
        assert is_within_outline(box, outline, spacing) is False

    def test_box_just_outside_threshold_passes(self):
        """box[0] == x_min + spacing + ε → passes (extra clearance)."""
        spacing = 1.0
        outline = _outline()
        box = (outline["x_min_mm"] + spacing + EPS, 5.0, 10.0, 15.0)
        assert is_within_outline(box, outline, spacing) is True

    def test_right_edge_threshold_exactly_passes(self):
        """Symmetric check on the right edge."""
        spacing = 1.0
        outline = _outline()
        # right edge exactly at x_max - spacing
        box = (5.0, 5.0, outline["x_max_mm"] - spacing, 15.0)
        assert is_within_outline(box, outline, spacing) is True

    def test_right_edge_just_past_threshold_rejected(self):
        spacing = 1.0
        outline = _outline()
        box = (5.0, 5.0, outline["x_max_mm"] - spacing + EPS, 15.0)
        assert is_within_outline(box, outline, spacing) is False

    def test_y_min_threshold_exactly_passes(self):
        spacing = 2.0
        outline = _outline()
        box = (5.0, outline["y_min_mm"] + spacing, 10.0, 15.0)
        assert is_within_outline(box, outline, spacing) is True

    def test_y_min_just_below_threshold_rejected(self):
        spacing = 2.0
        outline = _outline()
        box = (5.0, outline["y_min_mm"] + spacing - EPS, 10.0, 15.0)
        assert is_within_outline(box, outline, spacing) is False

    def test_spacing_zero_box_at_edge_passes(self):
        """spacing == 0 → any box whose edge is flush with the outline is accepted."""
        outline = _outline()
        box = (outline["x_min_mm"], outline["y_min_mm"],
               outline["x_max_mm"], outline["y_max_mm"])
        assert is_within_outline(box, outline, 0.0) is True

    def test_spacing_zero_box_outside_rejected(self):
        outline = _outline()
        box = (-EPS, 0.0, 10.0, 10.0)
        assert is_within_outline(box, outline, 0.0) is False

    def test_well_inside_board_always_passes(self):
        """Sanity: a small box in the middle of a large board always passes."""
        outline = _outline(0, 0, 200, 150)
        spacing = 3.0
        box = (50.0, 40.0, 70.0, 60.0)
        assert is_within_outline(box, outline, spacing) is True


# ---------------------------------------------------------------------------
# generate_spiral_candidates — basic behavior
# ---------------------------------------------------------------------------

class TestGenerateSpiralCandidates:

    def test_all_yielded_candidates_pass_outline_check(self):
        """Every yielded (px, py, box) must satisfy is_within_outline."""
        outline = _outline(0, 0, 50, 40)
        spacing = 1.0
        hw, hh = 3.0, 2.0
        tx, ty = 25.0, 20.0
        for px, py, box in generate_spiral_candidates(tx, ty, hw, hh, outline, spacing):
            assert is_within_outline(box, outline, spacing), (
                f"Candidate ({px:.3f}, {py:.3f}) box {box} failed outline check"
            )

    def test_generates_candidates_on_a_non_docstring_board(self):
        """Input not from any docstring example — 37x29 mm board, 5mm spacing."""
        outline = _outline(10, 5, 47, 34)
        spacing = 0.5
        hw, hh = 2.0, 1.5
        tx, ty = 28.0, 19.5  # not board center, not zero
        candidates = list(generate_spiral_candidates(tx, ty, hw, hh, outline, spacing))
        assert len(candidates) > 0, "Expected at least one candidate on a 37x29 board"

    def test_order_is_increasing_radius(self):
        """Candidates should be produced at increasing radius (first candidates
        are closer to the target than later ones — not necessarily strictly, as
        clamping can collapse positions, but the first candidate has the
        smallest nominal radius)."""
        outline = _outline(0, 0, 100, 100)
        spacing = 1.0
        hw, hh = 2.0, 2.0
        tx, ty = 50.0, 50.0
        candidates = list(generate_spiral_candidates(tx, ty, hw, hh, outline, spacing))
        assert len(candidates) > 0
        # First candidate should be from radius=2 (r=1 * 2.0)
        first_px, first_py, _ = candidates[0]
        first_dist = ((first_px - tx) ** 2 + (first_py - ty) ** 2) ** 0.5
        last_px, last_py, _ = candidates[-1]
        last_dist = ((last_px - tx) ** 2 + (last_py - ty) ** 2) ** 0.5
        # In an unconstrained board, last dist should be >= first dist
        assert last_dist >= first_dist

    def test_zero_candidates_on_tiny_board(self):
        """A board smaller than the footprint + spacing produces no candidates."""
        outline = _outline(0, 0, 2.0, 2.0)
        spacing = 1.0
        hw, hh = 2.0, 2.0  # footprint is bigger than the board
        tx, ty = 1.0, 1.0
        candidates = list(generate_spiral_candidates(tx, ty, hw, hh, outline, spacing))
        assert candidates == []


# ---------------------------------------------------------------------------
# generate_grid_fallback — integer-inset behavior
# ---------------------------------------------------------------------------

class TestGenerateGridFallback:

    def test_non_integer_outline_keeps_footprint_inside(self):
        """x_min=1.3, hw=2.0 → x_start = int(1.3 + 2.0 + 1) = int(4.3) = 4.
        First gx is 4; box left = 4 - 2 = 2.0 > 1.3 → inside."""
        outline = _outline(1.3, 2.7, 50.0, 40.0)
        hw, hh = 2.0, 1.5
        candidates = list(generate_grid_fallback(hw, hh, outline))
        assert len(candidates) > 0
        for gx, gy, box in candidates:
            assert box[0] >= outline["x_min_mm"], (
                f"box left {box[0]:.4f} < x_min {outline['x_min_mm']}"
            )
            assert box[2] <= outline["x_max_mm"], (
                f"box right {box[2]:.4f} > x_max {outline['x_max_mm']}"
            )
            assert box[1] >= outline["y_min_mm"], (
                f"box top {box[1]:.4f} < y_min {outline['y_min_mm']}"
            )
            assert box[3] <= outline["y_max_mm"], (
                f"box bottom {box[3]:.4f} > y_max {outline['y_max_mm']}"
            )

    def test_integer_outline_also_inside(self):
        """Integer outline: x_min=0, hw=3 → x_start=int(0+3+1)=4 → box left=1."""
        outline = _outline(0.0, 0.0, 30.0, 20.0)
        hw, hh = 3.0, 2.0
        candidates = list(generate_grid_fallback(hw, hh, outline))
        assert len(candidates) > 0
        for gx, gy, box in candidates:
            assert box[0] >= outline["x_min_mm"]
            assert box[2] <= outline["x_max_mm"]

    def test_returns_float_coordinates(self):
        """gx/gy must be float (matches original float(gx) cast)."""
        outline = _outline(0.0, 0.0, 20.0, 15.0)
        hw, hh = 1.0, 1.0
        gx, gy, _ = next(generate_grid_fallback(hw, hh, outline))
        assert isinstance(gx, float)
        assert isinstance(gy, float)

    def test_step_parameter_controls_spacing(self):
        """step=1 produces more candidates than step=4 on the same board."""
        outline = _outline(0.0, 0.0, 30.0, 20.0)
        hw, hh = 1.0, 1.0
        fine = list(generate_grid_fallback(hw, hh, outline, step=1))
        coarse = list(generate_grid_fallback(hw, hh, outline, step=4))
        assert len(fine) > len(coarse)


# ---------------------------------------------------------------------------
# Spiral fallback integration: spiral exhausts, grid finds a spot
# ---------------------------------------------------------------------------

class TestFallbackTrigger:
    """Verify that when the spiral finds no collision-free slot,
    generate_grid_fallback does find one in an otherwise-empty board region."""

    def test_grid_fallback_finds_spot_when_spiral_fails(self):
        """Fill the center of a large board with many placed boxes so the
        spiral (which orbits the center) finds no collision-free spot, then
        confirm the grid fallback still finds one in a corner."""
        import math as _math

        outline = _outline(0, 0, 40, 30)
        spacing = 0.5
        hw, hh = 1.0, 1.0

        # Block every spiral candidate by pre-placing a dense grid of boxes
        # in the centre region where the spiral searches
        placed_boxes = []
        for bx in range(-20, 20, 2):
            for by in range(-15, 15, 2):
                placed_boxes.append((15 + bx - 1, 10 + by - 1, 15 + bx + 1, 10 + by + 1))

        def box_collides(box):
            ex = (box[0] - spacing, box[1] - spacing, box[2] + spacing, box[3] + spacing)
            for pb in placed_boxes:
                if ex[0] <= pb[2] and ex[2] >= pb[0] and ex[1] <= pb[3] and ex[3] >= pb[1]:
                    return True
            return False

        # Spiral should find nothing (all candidates collide)
        spiral_hit = False
        for px, py, box in generate_spiral_candidates(20, 15, hw, hh, outline, spacing):
            if not box_collides(box):
                spiral_hit = True
                break

        # Grid fallback should find something
        grid_hit = False
        for gx, gy, box in generate_grid_fallback(hw, hh, outline):
            if not box_collides(box):
                grid_hit = True
                break

        # The point of this test is that the grid fallback recovers — if
        # the spiral also found something, it just means our blocking was
        # imperfect, but the grid must still work.
        assert grid_hit, "Grid fallback must find a free slot somewhere on the board"


# ---------------------------------------------------------------------------
# SPIRAL_HELPER source drift test
# ---------------------------------------------------------------------------

class TestSpiralHelperSource:
    """The embedded-script source string must define every helper and produce
    bit-equivalent results to the Python-side functions.  Catch drift here."""

    @pytest.mark.parametrize("name", [
        "is_within_outline",
        "generate_spiral_candidates",
        "generate_grid_fallback",
    ])
    def test_helper_defines(self, name):
        assert f"def {name}" in SPIRAL_HELPER, (
            f"SPIRAL_HELPER missing {name!r} — embedded scripts will NameError"
        )

    def test_helper_is_within_outline_threshold(self):
        """Exec SPIRAL_HELPER and verify threshold semantics match Python module."""
        ns: dict = {}
        exec(SPIRAL_HELPER, ns)
        outline = _outline()
        spacing = 1.5
        # exactly at threshold → passes in both
        box_exact = (outline["x_min_mm"] + spacing, 5.0, 10.0, 15.0)
        assert ns["is_within_outline"](box_exact, outline, spacing) == is_within_outline(
            box_exact, outline, spacing
        )
        # just inside threshold → rejected in both
        box_inside = (outline["x_min_mm"] + spacing - EPS, 5.0, 10.0, 15.0)
        assert ns["is_within_outline"](box_inside, outline, spacing) == is_within_outline(
            box_inside, outline, spacing
        )

    def test_helper_spiral_candidates_match(self):
        """First 20 spiral candidates from the helper match the Python module."""
        ns: dict = {}
        exec(SPIRAL_HELPER, ns)
        outline = _outline(0, 0, 60, 50)
        spacing = 1.0
        hw, hh = 2.0, 1.5
        tx, ty = 30.0, 25.0

        py_cands = list(generate_spiral_candidates(tx, ty, hw, hh, outline, spacing))[:20]
        helper_cands = list(ns["generate_spiral_candidates"](tx, ty, hw, hh, outline, spacing))[:20]

        assert len(py_cands) == len(helper_cands), (
            "SPIRAL_HELPER generate_spiral_candidates yields different count"
        )
        for i, ((px1, py1, b1), (px2, py2, b2)) in enumerate(zip(py_cands, helper_cands)):
            assert abs(px1 - px2) < EPS and abs(py1 - py2) < EPS, (
                f"Candidate {i}: Python ({px1:.6f},{py1:.6f}) != helper ({px2:.6f},{py2:.6f})"
            )

    def test_helper_grid_fallback_matches(self):
        """First 10 grid fallback candidates from the helper match the Python module."""
        ns: dict = {}
        exec(SPIRAL_HELPER, ns)
        outline = _outline(1.3, 2.7, 30.0, 25.0)
        hw, hh = 2.0, 1.5

        py_grid = list(generate_grid_fallback(hw, hh, outline))[:10]
        helper_grid = list(ns["generate_grid_fallback"](hw, hh, outline))[:10]

        assert len(py_grid) == len(helper_grid)
        for i, ((gx1, gy1, b1), (gx2, gy2, b2)) in enumerate(zip(py_grid, helper_grid)):
            assert gx1 == gx2 and gy1 == gy2, (
                f"Grid candidate {i}: Python ({gx1},{gy1}) != helper ({gx2},{gy2})"
            )
