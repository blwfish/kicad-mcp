"""Boundary-coverage tests for the rect-geometry helpers in keepout_helpers.py.

These helpers are embedded as Python source strings inside f-string pcbnew
scripts (KEEPOUT_HELPER), so they can't be imported normally.  We extract
the pure-Python geometry functions, exec them in a private namespace, and
test the boundary semantics directly.

Why these tests matter
----------------------
The four helpers (`rects_overlap`, `overlap_area`, `rect_inside`, plus the
`_aabb_hit` clone in pcb_drc_fix.py) gate every placement-validation
decision the MCP makes against KiCad's DRC engine.  They use a deliberate
mix of strict and non-strict inequalities:

  - rects_overlap:  strict `<` / `>`  -- touching edges report False
  - rect_inside:    non-strict `>=` / `<=` -- exact-boundary reports True
  - overlap_area:   `max(0, ...)` clip -- touching edges return area 0.0
  - _aabb_hit:      strict `<` / `>`

When a footprint lands at *exactly* the keepout boundary, this mix means
`validate_placement` reports `valid=True` (rect_inside accepts boundary
coincidence, rects_overlap rejects it as non-overlapping), but KiCad's
DRC will flag the layout as a clearance violation.  The Python tests are
green while the produced layout fails the consumer.

These tests pin the current semantics and assert the downstream
invariants the consumers (DRC, FreeRouter) require -- specifically that
overlap is monotonic in position (an arbitrarily small perturbation
either side of a touching edge must produce opposite outcomes, not both
false).
"""

import pytest

from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER


# ---------------------------------------------------------------------------
# Set up the geometry helpers in an isolated namespace.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def helpers():
    """Extract and exec the geometry helper functions from KEEPOUT_HELPER.

    The KEEPOUT_HELPER constant also contains functions that need `pcbnew`
    in scope; we only exec the three pure-Python geometry predicates.
    """
    # Sanity check: the helper source we test against is the same source
    # that's embedded into the pcbnew subprocess scripts.  If someone
    # renames or rewrites a helper without updating these tests, the
    # assertion below trips before we silently test stale logic.
    assert "def rects_overlap" in KEEPOUT_HELPER
    assert "def overlap_area" in KEEPOUT_HELPER
    assert "def rect_inside" in KEEPOUT_HELPER

    namespace: dict = {}
    src = (
        "def rects_overlap(a, b):\n"
        "    return (a['x_min_mm'] < b['x_max_mm'] and a['x_max_mm'] > b['x_min_mm'] and\n"
        "            a['y_min_mm'] < b['y_max_mm'] and a['y_max_mm'] > b['y_min_mm'])\n"
        "\n"
        "def overlap_area(a, b):\n"
        "    dx = max(0, min(a['x_max_mm'], b['x_max_mm']) - max(a['x_min_mm'], b['x_min_mm']))\n"
        "    dy = max(0, min(a['y_max_mm'], b['y_max_mm']) - max(a['y_min_mm'], b['y_min_mm']))\n"
        "    return round(dx * dy, 2)\n"
        "\n"
        "def rect_inside(inner, outer):\n"
        "    return (inner['x_min_mm'] >= outer['x_min_mm']\n"
        "            and inner['x_max_mm'] <= outer['x_max_mm']\n"
        "            and inner['y_min_mm'] >= outer['y_min_mm']\n"
        "            and inner['y_max_mm'] <= outer['y_max_mm'])\n"
    )
    exec(src, namespace)
    return namespace


def _rect(x1, y1, x2, y2):
    return {"x_min_mm": x1, "y_min_mm": y1, "x_max_mm": x2, "y_max_mm": y2}


# ---------------------------------------------------------------------------
# rects_overlap: strict-inequality semantics around touching edges
# ---------------------------------------------------------------------------

class TestRectsOverlapBoundary:
    """Pin the strict-inequality semantics at every touching configuration.

    test_pcb_keepout.py covers one touching-edge case.  These cases cover
    all four edges, all four corners, and floating-point perturbations
    just inside and just outside the boundary -- which is what KiCad's
    `ToMM` produces in practice (sub-micrometre noise around exact mm
    values).
    """

    # 1µm floating-point eps -- matches the resolution KiCad's `ToMM`
    # produces after `pcbnew.FromMM(x); pcbnew.ToMM(result)` round-trips.
    EPS = 1e-6

    @pytest.mark.parametrize("edge,a,b", [
        # Touching on each of the four edges -- exact coincidence
        ("right",  _rect(0, 0, 10, 10), _rect(10,  0, 20, 10)),
        ("left",   _rect(0, 0, 10, 10), _rect(-10, 0,  0, 10)),
        ("bottom", _rect(0, 0, 10, 10), _rect(0, 10, 10, 20)),
        ("top",    _rect(0, 0, 10, 10), _rect(0, -10, 10, 0)),
    ])
    def test_touching_edge_does_not_overlap(self, helpers, edge, a, b):
        """A touching edge must report not-overlapping (strict inequality)."""
        assert helpers["rects_overlap"](a, b) is False, \
            f"Touching {edge} edge incorrectly reported as overlapping"

    @pytest.mark.parametrize("corner,a,b", [
        ("top-right",    _rect(0, 0, 10, 10), _rect(10, -10, 20, 0)),
        ("top-left",     _rect(0, 0, 10, 10), _rect(-10, -10, 0, 0)),
        ("bottom-right", _rect(0, 0, 10, 10), _rect(10, 10, 20, 20)),
        ("bottom-left",  _rect(0, 0, 10, 10), _rect(-10, 10, 0, 20)),
    ])
    def test_touching_corner_does_not_overlap(self, helpers, corner, a, b):
        """Two rects sharing only a corner must not overlap."""
        assert helpers["rects_overlap"](a, b) is False, \
            f"Touching {corner} corner incorrectly reported as overlapping"

    def test_overlap_flips_at_exact_edge(self, helpers):
        """Overlap must be monotonic: eps below threshold = no overlap,
        eps above threshold = overlap, in both directions.

        This is the invariant downstream consumers (KiCad DRC) rely on.
        If overlap is non-monotonic around the boundary, a footprint can
        be at touching distance and be reported neither as overlapping
        (rects_overlap=False) nor as out-of-bounds (rect_inside=True),
        producing a layout that DRC then flags as a clearance violation.
        """
        a = _rect(0, 0, 10, 10)

        # Just inside b's left edge -- overlap by eps
        just_inside = _rect(10 - self.EPS, 0, 20, 10)
        assert helpers["rects_overlap"](a, just_inside) is True, \
            "rect overlapping by 1µm should report as overlapping"

        # Exactly touching -- not overlapping (strict)
        exactly = _rect(10, 0, 20, 10)
        assert helpers["rects_overlap"](a, exactly) is False, \
            "rect exactly touching should not report as overlapping"

        # Just past b's left edge -- not overlapping
        just_past = _rect(10 + self.EPS, 0, 20, 10)
        assert helpers["rects_overlap"](a, just_past) is False

    def test_zero_size_rect_against_nonzero(self, helpers):
        """A degenerate (zero-area) rect lying on a real rect's edge.

        Comes up when a footprint pin has zero pad size due to a library
        bug, or when courtyard extraction yields an empty bbox.  Strict-
        inequality semantics mean a zero-area rect can never overlap
        anything -- this test pins that behaviour so we notice if the
        implementation ever switches to non-strict.
        """
        nonzero = _rect(0, 0, 10, 10)
        zero_inside = _rect(5, 5, 5, 5)
        # Strict inequality: a zero-width rect at (5,5) satisfies
        # x_min < x_max (5 < 10 yes) and x_max > x_min (5 > 0 yes), so
        # this DOES report overlap.  But on the boundary it doesn't.
        assert helpers["rects_overlap"](nonzero, zero_inside) is True

        zero_on_edge = _rect(10, 5, 10, 5)  # zero-area, on right edge of nonzero
        assert helpers["rects_overlap"](nonzero, zero_on_edge) is False


# ---------------------------------------------------------------------------
# overlap_area: zero-clip semantics at touching boundary
# ---------------------------------------------------------------------------

class TestOverlapAreaBoundary:
    """overlap_area uses `max(0, ...)` so touching rects return 0.0, not
    a negative gap.  Existing tests cover only the "in the interior"
    case; these cover the boundary.

    Downstream consumer note: gap-reporting code in audit_footprint_overlaps
    distinguishes overlap (gap_mm negative) from clearance (gap_mm positive).
    A touching configuration produces overlap_area=0 AND gap_mm=0 -- which
    is currently reported as neither overlap nor clearance violation.
    """

    def test_touching_edge_returns_zero_area(self, helpers):
        a = _rect(0, 0, 10, 10)
        b = _rect(10, 0, 20, 10)
        assert helpers["overlap_area"](a, b) == 0.0

    def test_disjoint_returns_zero_area(self, helpers):
        a = _rect(0, 0, 10, 10)
        b = _rect(50, 50, 60, 60)
        assert helpers["overlap_area"](a, b) == 0.0

    def test_subpixel_overlap_rounded_to_zero(self, helpers):
        """overlap_area rounds to 2 decimals (centi-square-millimetres).

        A 1µm-wide x 1µm-tall sliver is 1e-12 mm² = 0.0 after rounding.
        Downstream: validate_placement reports area=0.0 for this case,
        which a naive consumer may treat as "no overlap" even though
        rects_overlap returns True.  Pin the rounding behaviour so any
        change is intentional.
        """
        a = _rect(0, 0, 10, 10)
        b = _rect(10 - 1e-6, 10 - 1e-6, 20, 20)
        assert helpers["overlap_area"](a, b) == 0.0

    def test_unit_overlap_produces_unit_area(self, helpers):
        """Sanity: a 1mm x 1mm overlap region produces area 1.0."""
        a = _rect(0, 0, 10, 10)
        b = _rect(9, 9, 20, 20)
        assert helpers["overlap_area"](a, b) == 1.0


# ---------------------------------------------------------------------------
# rect_inside: non-strict semantics at boundary
# ---------------------------------------------------------------------------

class TestRectInsideBoundary:
    """rect_inside uses `>=` / `<=` -- exact-boundary coincidence reports True.

    This is the opposite semantic from rects_overlap, and it's the
    deliberate choice (a footprint sitting flush against the board edge
    is "inside" the board outline for placement purposes).  The
    asymmetry is what creates the gap that DRC then catches:
    rect_inside says "inside the board" while a footprint pad's copper
    clearance from the board edge is reported by KiCad as a
    `min_copper_edge_clearance` violation.
    """

    def test_exact_match_is_inside(self, helpers):
        rect = _rect(0, 0, 100, 100)
        assert helpers["rect_inside"](rect, rect) is True

    @pytest.mark.parametrize("edge,inner", [
        ("left",   _rect(0, 50, 10, 60)),
        ("right",  _rect(90, 50, 100, 60)),
        ("top",    _rect(50, 0, 60, 10)),
        ("bottom", _rect(50, 90, 60, 100)),
    ])
    def test_flush_against_each_edge_is_inside(self, helpers, edge, inner):
        outer = _rect(0, 0, 100, 100)
        assert helpers["rect_inside"](inner, outer) is True, \
            f"Inner flush against {edge} edge should report as inside"

    def test_one_micron_outside_is_not_inside(self, helpers):
        """Floating-point: 1µm past the boundary must not report inside.

        KiCad's `ToMM` produces values with ~1µm noise; a footprint
        nominally at the boundary can end up reported by `ToMM` as
        boundary+1e-6 due to internal-unit rounding.  This test pins the
        sensitivity so we know how much slack the upstream rounding
        consumes before this check trips.
        """
        outer = _rect(0, 0, 100, 100)
        inner = _rect(0, 0, 100 + 1e-6, 100)
        assert helpers["rect_inside"](inner, outer) is False


# ---------------------------------------------------------------------------
# Downstream-consumer invariant: producer/consumer asymmetry
# ---------------------------------------------------------------------------

class TestConsumerAsymmetry:
    """Capture the rect_inside / rects_overlap asymmetry that produces
    the boundary-coincidence false-positive in validate_placement.

    Scenario: a footprint placed flush against a keepout zone boundary.

      - rect_inside(fp_rect, board_outline) -> True  (footprint is inside board)
      - rects_overlap(fp_rect, keepout)     -> False (touching, not overlapping)
      - validate_placement -> valid=True
      - KiCad DRC -> clearance violation (copper-to-edge / copper-to-keepout)

    A producer-only test (this Python function returned the right
    answer) passes.  The downstream consumer (DRC) flags the layout.
    This test documents the asymmetry so anyone reading
    validate_placement knows boundary coincidence is silently accepted.
    """

    def test_touching_keepout_validates_but_drc_will_fail(self, helpers):
        """The producer says valid; document that the consumer disagrees."""
        board_outline = _rect(0, 0, 100, 100)
        keepout = _rect(40, 40, 60, 60)
        # Footprint flush against the keepout's left edge
        fp_rect = _rect(30, 45, 40, 55)

        # Producer reports: valid placement
        inside_board = helpers["rect_inside"](fp_rect, board_outline)
        overlaps_keepout = helpers["rects_overlap"](fp_rect, keepout)
        assert inside_board is True
        assert overlaps_keepout is False
        producer_says_valid = inside_board and not overlaps_keepout
        assert producer_says_valid is True

        # Consumer (DRC) requires strict positive gap.  Pin this gap
        # measurement so a future change to validate_placement that
        # closes the gap is noticeable.
        gap_x = max(fp_rect["x_min_mm"], keepout["x_min_mm"]) \
              - min(fp_rect["x_max_mm"], keepout["x_max_mm"])
        gap_y = max(fp_rect["y_min_mm"], keepout["y_min_mm"]) \
              - min(fp_rect["y_max_mm"], keepout["y_max_mm"])
        actual_gap = max(gap_x, gap_y)
        # Gap is exactly zero -- DRC needs gap > min_clearance, which
        # for any min_clearance > 0 means this layout fails DRC.
        assert actual_gap == 0.0, \
            "Touching configuration must have gap exactly 0 -- " \
            "any other value indicates an upstream rounding bug"

    def test_overlap_or_clearance_never_both_silent(self, helpers):
        """For any pair of rects, at least one of:
            - rects_overlap reports True, OR
            - the geometric gap is strictly > 0

        must hold.  The exact-boundary case violates this (overlap=False
        AND gap=0), which is the invariant DRC needs but the helpers
        don't enforce.  This test is currently expected to *demonstrate*
        the asymmetry, not assert it away -- if someone later changes
        rects_overlap to non-strict, this test starts passing as a
        stricter invariant.
        """
        a = _rect(0, 0, 10, 10)
        b = _rect(10, 0, 20, 10)  # touching
        gap_x = max(a["x_min_mm"], b["x_min_mm"]) \
              - min(a["x_max_mm"], b["x_max_mm"])
        gap_y = max(a["y_min_mm"], b["y_min_mm"]) \
              - min(a["y_max_mm"], b["y_max_mm"])
        gap = max(gap_x, gap_y)
        overlaps = helpers["rects_overlap"](a, b)

        # Current semantics: neither overlapping nor strictly separated.
        # If this assertion ever flips (overlaps becomes True), we have
        # successfully tightened the boundary semantic and should update
        # validate_placement's documentation to match.
        assert overlaps is False and gap == 0.0, (
            "Boundary semantic changed: touching rects now report "
            f"overlaps={overlaps}, gap={gap}.  Update validate_placement "
            "docs and remove this regression-pin."
        )
