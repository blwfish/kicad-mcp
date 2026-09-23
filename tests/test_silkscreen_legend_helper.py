"""Tests for the silkscreen-legend placement decisions in pcb_pipeline.py:
_band_clear, _choose_interior_side, _label_offset, _refdes_offset.

Extracted per the boundary-ops pattern (docs/BOUNDARY_OPS.md) from decision
logic that used to be welded directly into _step_silkscreen_legends's embedded
pcbnew script (review finding kicad-mcp-20260721-af1a#16) -- previously
untestable without a live pcbnew process. Pure functions operating on plain
tuples, unit-agnostic (the numbers below are simple mm-scale values, not
KiCad's internal integer units -- the logic is identical either way).
"""
from kicad_mcp.tools.pcb_pipeline import (
    _SILKSCREEN_LEGEND_HELPER,
    _band_clear,
    _choose_interior_side,
    _label_offset,
    _refdes_offset,
)


# ---------------------------------------------------------------------------
# _SILKSCREEN_LEGEND_HELPER source must define the same functions
# ---------------------------------------------------------------------------

class TestSilkscreenLegendHelperSource:
    """The embedded-script helper string must define every function the
    Python module exports — otherwise the embedded script will NameError at
    runtime (a failure mode no test here can run, since it needs a live
    pcbnew process). Catch any drift between the two sources here."""

    def test_helper_defines_all_four_functions(self):
        for name in ("_band_clear", "_choose_interior_side",
                     "_label_offset", "_refdes_offset"):
            assert f"def {name}" in _SILKSCREEN_LEGEND_HELPER, (
                f"_SILKSCREEN_LEGEND_HELPER missing {name!r} — embedded "
                "scripts will NameError"
            )

    def test_helper_matches_python_at_representative_inputs(self):
        """Exec the helper source and verify it behaves identically to the
        Python module across one case per function."""
        ns: dict = {}
        exec(_SILKSCREEN_LEGEND_HELPER, ns)

        all_pads = [("J2", 20, 0, 30, 10)]
        assert ns["_band_clear"]("J1", (0, 0, 10, 10), all_pads) == \
            _band_clear("J1", (0, 0, 10, 10), all_pads)

        fbb = (0, 0, 5, 20)  # tall/narrow -> R/L candidates
        assert ns["_choose_interior_side"](fbb, ["A", "B"], 0.5, 0.8, "U1", []) == \
            _choose_interior_side(fbb, ["A", "B"], 0.5, 0.8, "U1", [])

        assert ns["_label_offset"](1, 0, None, (0, 0, 10, 10), (5, 5), 2.0, 0.5, 0.8) == \
            _label_offset(1, 0, None, (0, 0, 10, 10), (5, 5), 2.0, 0.5, 0.8)

        assert ns["_refdes_offset"](0, -1, (0, 0, 10, 10), 0.5, 0.8) == \
            _refdes_offset(0, -1, (0, 0, 10, 10), 0.5, 0.8)


# ---------------------------------------------------------------------------
# _band_clear
# ---------------------------------------------------------------------------

class TestBandClear:
    def test_no_other_pads_is_clear(self):
        assert _band_clear("J1", (0, 0, 10, 10), []) is True

    def test_own_pads_excluded_even_if_overlapping(self):
        all_pads = [("J1", 0, 0, 10, 10)]  # same ref as `own_ref`
        assert _band_clear("J1", (0, 0, 10, 10), all_pads) is True

    def test_other_footprints_pad_fully_inside_band_blocks(self):
        all_pads = [("J2", 2, 2, 8, 8)]
        assert _band_clear("J1", (0, 0, 10, 10), all_pads) is False

    def test_touching_edge_counts_as_blocked_not_clear(self):
        """Boundary: a pad exactly touching the band's edge (px0 == x1) is
        NOT clear -- matches this codebase's established non-strict overlap
        convention (utils.geometry.rects_overlap)."""
        all_pads = [("J2", 10, 0, 20, 10)]  # touches band's right edge at x=10
        assert _band_clear("J1", (0, 0, 10, 10), all_pads) is False

    def test_one_unit_away_is_clear(self):
        all_pads = [("J2", 10.001, 0, 20, 10)]
        assert _band_clear("J1", (0, 0, 10, 10), all_pads) is True


# ---------------------------------------------------------------------------
# _choose_interior_side
# ---------------------------------------------------------------------------

class TestChooseInteriorSide:
    # margin/size use small even integers throughout this class (not mm-scale
    # floats): the real code does `size // 2` / `lw // 2` (floor division,
    # exact for pcbnew's large internal-unit integers) -- on a small float like
    # 0.8, `0.8 // 2 == 0.0`, not 0.4, which would make hand-computed
    # expected values silently wrong. Even integers sidestep that entirely.
    MARGIN, SIZE = 1, 2

    def test_vertical_footprint_prefers_right_or_left(self):
        # height (20) > width (5) -> R/L candidates only
        fbb = (0, 0, 5, 20)
        assert _choose_interior_side(fbb, ["A"], self.MARGIN, self.SIZE, "U1", []) in ("R", "L")

    def test_horizontal_footprint_prefers_up_or_down(self):
        # width (20) > height (5) -> U/D candidates only
        fbb = (0, 0, 20, 5)
        assert _choose_interior_side(fbb, ["A"], self.MARGIN, self.SIZE, "U1", []) in ("U", "D")

    def test_square_footprint_tie_goes_to_vertical_branch(self):
        """Boundary: height == width exactly. `(bottom-top) >= (right-left)`
        pins the tie to the R/L (vertical) branch, not U/D."""
        fbb = (0, 0, 10, 10)
        assert _choose_interior_side(fbb, ["A"], self.MARGIN, self.SIZE, "U1", []) in ("R", "L")

    def test_right_blocked_falls_back_to_left(self):
        # d = margin(1) + maxw(len("A")*2=2) + size//2(1) = 4.
        # R band = (right+margin, top, right+d, bottom) = (6, 0, 9, 20).
        # L band = (left-d, top, left-margin, bottom) = (-4, 0, -1, 20).
        fbb = (0, 0, 5, 20)
        all_pads = [("J2", 6.5, 0, 15, 20)]  # overlaps R band only
        assert _choose_interior_side(fbb, ["A"], self.MARGIN, self.SIZE, "U1", all_pads) == "L"

    def test_both_sides_blocked_returns_none(self):
        fbb = (0, 0, 5, 20)
        all_pads = [("J2", 6.5, 0, 15, 20), ("J3", -15, 0, -2, 20)]
        assert _choose_interior_side(fbb, ["A"], self.MARGIN, self.SIZE, "U1", all_pads) is None


# ---------------------------------------------------------------------------
# _label_offset
# ---------------------------------------------------------------------------

class TestLabelOffset:
    FBB = (0, 0, 10, 10)  # left, top, right, bottom
    PAD = (5, 5)          # (px, py)
    # Even integers (not mm-scale floats) so `size // 2` / `lw // 2` (floor
    # division -- exact for pcbnew's large internal-unit integers in
    # production) behave as a clean halving here too. margin=1, size=2 (->
    # size//2=1), lw=4 (-> lw//2=2).
    MARGIN, SIZE, LW = 1, 2, 4

    def test_edge_inboard_right(self):
        # ix > 0: cross-axis is the pad's own y; offset axis clears fbb's right edge.
        assert _label_offset(1, 0, None, self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (10 + 1 + 1, 5)

    def test_edge_inboard_left(self):
        assert _label_offset(-1, 0, None, self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (0 - 1 - 1, 5)

    def test_edge_inboard_down(self):
        assert _label_offset(0, 1, None, self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (5, 10 + 1 + 1)

    def test_edge_inboard_up(self):
        assert _label_offset(0, -1, None, self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (5, 0 - 1 - 1)

    def test_interior_side_right_uses_label_width_not_glyph_size(self):
        # side="R": offset uses lw (this label's width, //2 = 2), not size (//2 = 1).
        assert _label_offset(0, 0, "R", self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (10 + 1 + 2, 5)

    def test_interior_side_left_uses_label_width_not_glyph_size(self):
        assert _label_offset(0, 0, "L", self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (0 - 1 - 2, 5)

    def test_interior_side_up_uses_glyph_size_not_label_width(self):
        """Asymmetry pin: interior U/D use `size` (the glyph size), NOT `lw`
        (the label width) that R/L use -- an easy transcription slip between
        the two axis pairs. If this used lw//2 (2) instead of size//2 (1) the
        offset would be 0-1-2=-3, not -2."""
        assert _label_offset(0, 0, "U", self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (5, 0 - 1 - 1)

    def test_interior_side_down_uses_glyph_size_not_label_width(self):
        assert _label_offset(0, 0, "D", self.FBB, self.PAD, self.LW, self.MARGIN, self.SIZE) == \
            (5, 10 + 1 + 1)


# ---------------------------------------------------------------------------
# _refdes_offset
# ---------------------------------------------------------------------------

class TestRefdesOffset:
    FBB = (0, 0, 10, 20)  # left, top, right, bottom -> center (5, 10)

    def test_inboard_up_edge(self):
        assert _refdes_offset(0, -1, self.FBB, 0.5, 0.8) == (5, 0 - (0.5 + 1.6))

    def test_inboard_down_edge(self):
        assert _refdes_offset(0, 1, self.FBB, 0.5, 0.8) == (5, 20 + (0.5 + 1.6))

    def test_inboard_right_edge(self):
        assert _refdes_offset(1, 0, self.FBB, 0.5, 0.8) == (10 + (0.5 + 1.6), 10)

    def test_inboard_left_edge(self):
        assert _refdes_offset(-1, 0, self.FBB, 0.5, 0.8) == (0 - (0.5 + 1.6), 10)
