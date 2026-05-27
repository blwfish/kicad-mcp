"""Tests for the AABB overlap semantics used by pcb_drc_fix.py.

The overlap detection logic at pcb_drc_fix.py line ~201 runs inside an
embedded pcbnew subprocess script.  It cannot be unit-tested by importing
the tool directly; a full integration test would require a live KiCad
installation and is marked @pytest.mark.requires_kicad.

What we CAN test here:
  1. The ``aabb_overlap`` function from ``geometry.py`` — the Python-side
     canonical source that the injected ``GEOMETRY_HELPER`` string must
     match byte-for-byte.
  2. The ``GEOMETRY_HELPER`` source string itself (executed via ``exec``) to
     verify the injected version in the subprocess behaves identically.

Both sets of tests cover the three boundary cases mandated by the
Threshold-Boundary Testing Rule: touching (gap=0), 1-unit overlap,
1-unit gap.
"""

import pytest
from kicad_mcp.utils.geometry import aabb_overlap, GEOMETRY_HELPER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _injected_aabb_overlap(a, b):
    """Execute aabb_overlap as it will be in the embedded subprocess."""
    ns = {}
    exec(GEOMETRY_HELPER, ns)
    return ns["aabb_overlap"](a, b)


# ---------------------------------------------------------------------------
# aabb_overlap — Python-side canonical function
# ---------------------------------------------------------------------------

class TestAabbOverlapBoundaries:
    """Boundary cases: gap=0, gap=-1, gap=+1 on each axis."""

    # --- X-axis boundaries ---

    def test_touching_right_edge_x(self):
        # a's right edge exactly meets b's left edge — should count as overlap
        a = (0, 0, 5, 5)
        b = (5, 0, 10, 5)
        assert aabb_overlap(a, b) is True

    def test_one_unit_overlap_x(self):
        # a's right edge is 1 unit inside b — clear overlap
        a = (0, 0, 6, 5)
        b = (5, 0, 10, 5)
        assert aabb_overlap(a, b) is True

    def test_one_unit_gap_x(self):
        # a's right edge is 1 unit short of b's left edge — no overlap
        a = (0, 0, 4, 5)
        b = (5, 0, 10, 5)
        assert aabb_overlap(a, b) is False

    # --- Y-axis boundaries ---

    def test_touching_bottom_edge_y(self):
        # a's bottom edge exactly meets b's top edge
        a = (0, 0, 5, 5)
        b = (0, 5, 5, 10)
        assert aabb_overlap(a, b) is True

    def test_one_unit_overlap_y(self):
        # a's bottom extends 1 unit into b
        a = (0, 0, 5, 6)
        b = (0, 5, 5, 10)
        assert aabb_overlap(a, b) is True

    def test_one_unit_gap_y(self):
        # a's bottom is 1 unit short of b's top
        a = (0, 0, 5, 4)
        b = (0, 5, 5, 10)
        assert aabb_overlap(a, b) is False

    # --- Corner touch ---

    def test_corner_touch(self):
        # Rects share a single corner point — counts as overlap (non-strict)
        a = (0, 0, 3, 3)
        b = (3, 3, 6, 6)
        assert aabb_overlap(a, b) is True

    # --- Fully separated ---

    def test_fully_separated_x(self):
        a = (0, 0, 2, 2)
        b = (5, 0, 8, 2)
        assert aabb_overlap(a, b) is False

    def test_fully_separated_y(self):
        a = (0, 0, 2, 2)
        b = (0, 5, 2, 8)
        assert aabb_overlap(a, b) is False

    # --- Full containment ---

    def test_inner_fully_inside_outer(self):
        # A rect inside another is an overlap
        inner = (1, 1, 3, 3)
        outer = (0, 0, 5, 5)
        assert aabb_overlap(inner, outer) is True

    # --- Input NOT from any docstring example ---

    def test_asymmetric_rects_diagonal_gap(self):
        # Two narrow rects that share no x-extent at all
        a = (10, 10, 20, 11)  # wide horizontal strip
        b = (21, 10, 30, 11)  # adjacent strip, 1-unit gap
        assert aabb_overlap(a, b) is False

    def test_asymmetric_rects_diagonal_touch(self):
        a = (10, 10, 20, 11)
        b = (20, 10, 30, 11)  # exactly touching
        assert aabb_overlap(a, b) is True


# ---------------------------------------------------------------------------
# GEOMETRY_HELPER injected string — must match canonical function
# ---------------------------------------------------------------------------

class TestGeometryHelperInjected:
    """Verify the GEOMETRY_HELPER source string (used in embedded subprocess)
    is byte-equivalent in behaviour to the Python-side function above."""

    def test_injected_touching_x(self):
        assert _injected_aabb_overlap((0, 0, 5, 5), (5, 0, 10, 5)) is True

    def test_injected_one_unit_overlap_x(self):
        assert _injected_aabb_overlap((0, 0, 6, 5), (5, 0, 10, 5)) is True

    def test_injected_one_unit_gap_x(self):
        assert _injected_aabb_overlap((0, 0, 4, 5), (5, 0, 10, 5)) is False

    def test_injected_touching_y(self):
        assert _injected_aabb_overlap((0, 0, 5, 5), (0, 5, 5, 10)) is True

    def test_injected_one_unit_overlap_y(self):
        assert _injected_aabb_overlap((0, 0, 5, 6), (0, 5, 5, 10)) is True

    def test_injected_one_unit_gap_y(self):
        assert _injected_aabb_overlap((0, 0, 5, 4), (0, 5, 5, 10)) is False

    def test_injected_corner_touch(self):
        assert _injected_aabb_overlap((0, 0, 3, 3), (3, 3, 6, 6)) is True

    def test_injected_fully_separated(self):
        assert _injected_aabb_overlap((0, 0, 2, 2), (5, 5, 8, 8)) is False


# ---------------------------------------------------------------------------
# Integration-level note (subprocess boundary)
# ---------------------------------------------------------------------------
# The full ``auto_fix_placement`` tool invokes ``run_pcbnew_script`` which
# spawns a separate Python process (KiCad's bundled interpreter).  Testing
# the overlap gate at the tool level requires a live KiCad installation and
# is marked ``@pytest.mark.requires_kicad``.  The tests above exercise the
# injected helper string at the same semantic level the subprocess uses,
# which is the closest we can get without a KiCad install.
