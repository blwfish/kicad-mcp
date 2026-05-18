"""Boundary tests for the AABB / in_board helpers embedded in pcb_drc_fix.py.

drc_autofix and auto_fix_placement embed two pure-Python geometry helpers
into their pcbnew subprocess scripts:

    _aabb_hit(a, bx_min, by_min, bx_max, by_max)    # strict <  / >
    in_board(text_bbox)                              # non-strict <=, >=
    bbox_inside_board(bx0, by0, bx1, by1)            # non-strict <=, >=

Same mixed-strictness pattern as the rect helpers in keepout_helpers.py,
same downstream-consumer risk: a silkscreen text positioned at exactly
the pad boundary is reported as "not overlapping" by `_aabb_hit`, but
KiCad's DRC then flags the layout as `silk_over_copper`.

These tests extract the helper source from the file (via grep) and exec
in an isolated namespace -- the same approach test_keepout_rect_helpers.py
uses.  If the helper source is renamed or rewritten without keeping the
function signature stable, the extraction asserts catch it before we
silently test stale logic.
"""

import re
from pathlib import Path

import pytest


PCB_DRC_FIX_PATH = (
    Path(__file__).parent.parent
    / "src"
    / "kicad_mcp"
    / "tools"
    / "pcb_drc_fix.py"
)


class _FakeBBox:
    """Minimal stand-in for pcbnew.BOX2I -- only the four accessors used."""

    def __init__(self, x_min, y_min, x_max, y_max):
        self._x = x_min
        self._y = y_min
        self._right = x_max
        self._bottom = y_max

    def GetX(self):
        return self._x

    def GetY(self):
        return self._y

    def GetRight(self):
        return self._right

    def GetBottom(self):
        return self._bottom


@pytest.fixture(scope="module")
def helpers():
    """Extract the three geometry helpers and exec them in a private namespace.

    The pattern: locate each `def name(...)` block in the source, take
    everything up to the next blank-line-followed-by-non-indented-text.
    Because all three are nested inside f-strings, we read them straight
    from the file rather than importing.
    """
    src = PCB_DRC_FIX_PATH.read_text()

    # _aabb_hit: 3 lines starting at `def _aabb_hit`
    aabb_match = re.search(
        r"^def _aabb_hit\(a, bx_min, by_min, bx_max, by_max\):\n"
        r"^    return \([^\n]+\n"
        r"^            [^\n]+\)\n",
        src,
        re.MULTILINE,
    )
    assert aabb_match, "Could not locate _aabb_hit source in pcb_drc_fix.py"

    # in_board: a 5-line block under `def in_board(text_bbox)`
    in_board_match = re.search(
        r"^def in_board\(text_bbox\):\n"
        r"^    if not board_valid:\n"
        r"^        return True\n"
        r"^    return \([^\n]+\n"
        r"(?:^            [^\n]+\n){2,3}",
        src,
        re.MULTILINE,
    )
    assert in_board_match, "Could not locate in_board source in pcb_drc_fix.py"

    # bbox_inside_board: 4 lines
    bbox_match = re.search(
        r"^def bbox_inside_board\(bx0, by0, bx1, by1\):\n"
        r"^    if outline is None:\n"
        r"^        return True\n"
        r"^    return [^\n]+\n",
        src,
        re.MULTILINE,
    )
    assert bbox_match, "Could not locate bbox_inside_board source in pcb_drc_fix.py"

    # Set up the global state in_board / bbox_inside_board close over.
    namespace = {
        "board_valid": True,
        "board_bb": _FakeBBox(0, 0, 100, 100),
        "outline": (0.0, 0.0, 100.0, 100.0),
    }
    for blob in (aabb_match.group(0), in_board_match.group(0), bbox_match.group(0)):
        exec(blob, namespace)

    return namespace


# ---------------------------------------------------------------------------
# _aabb_hit: strict-inequality semantics around touching edges
# ---------------------------------------------------------------------------

class TestAabbHitBoundary:
    """_aabb_hit decides whether silkscreen text overlaps a pad.  Touching
    edges report False -- which means a label flush against a pad edge
    is "not overlapping" here, but KiCad's DRC will flag it as
    silk_over_copper because the silkscreen mask edge is on the pad's
    copper edge."""

    def test_touching_edge_is_not_hit(self, helpers):
        a = _FakeBBox(0, 0, 10, 10)
        # b touches a's right edge
        assert helpers["_aabb_hit"](a, 10, 0, 20, 10) is False

    def test_one_unit_overlap_is_hit(self, helpers):
        a = _FakeBBox(0, 0, 10, 10)
        assert helpers["_aabb_hit"](a, 9, 0, 19, 10) is True

    def test_microscopic_overlap_is_hit(self, helpers):
        """1 internal unit overlap (pcbnew uses int internal units --
        a single-unit overlap is the smallest detectable case)."""
        a = _FakeBBox(0, 0, 10, 10)
        assert helpers["_aabb_hit"](a, 9, 0, 11, 10) is True

    def test_full_containment_is_hit(self, helpers):
        a = _FakeBBox(0, 0, 100, 100)
        assert helpers["_aabb_hit"](a, 10, 10, 20, 20) is True

    def test_corner_touch_is_not_hit(self, helpers):
        """Touching only at a corner -- not overlapping."""
        a = _FakeBBox(0, 0, 10, 10)
        assert helpers["_aabb_hit"](a, 10, 10, 20, 20) is False

    def test_disjoint_far_is_not_hit(self, helpers):
        a = _FakeBBox(0, 0, 10, 10)
        assert helpers["_aabb_hit"](a, 50, 50, 60, 60) is False


# ---------------------------------------------------------------------------
# in_board: non-strict semantics, plus the "board invalid" early return
# ---------------------------------------------------------------------------

class TestInBoardBoundary:
    """in_board lets silkscreen text sit flush against the board edge.
    Two semantic decisions encoded here that downstream consumers care
    about:

      1. board_valid=False -> always True (silently disables the check).
      2. >= / <= boundary -> exact-coincidence is "inside".

    The first matters because a fresh board with no Edge.Cuts will have
    board_valid=False and silkscreen text outside the (nonexistent)
    board outline will pass.  We pin both behaviours.
    """

    def test_text_inside_board_is_in(self, helpers):
        text = _FakeBBox(10, 10, 50, 20)
        assert helpers["in_board"](text) is True

    def test_text_outside_board_is_not_in(self, helpers):
        text = _FakeBBox(110, 10, 150, 20)
        assert helpers["in_board"](text) is False

    def test_text_flush_against_each_edge_is_in(self, helpers):
        for text in [
            _FakeBBox(0, 50, 10, 60),    # flush left
            _FakeBBox(90, 50, 100, 60),  # flush right
            _FakeBBox(50, 0, 60, 10),    # flush top
            _FakeBBox(50, 90, 60, 100),  # flush bottom
        ]:
            assert helpers["in_board"](text) is True

    def test_text_one_unit_outside_is_not_in(self, helpers):
        text = _FakeBBox(0, 0, 101, 100)
        assert helpers["in_board"](text) is False

    def test_board_invalid_always_returns_true(self, helpers):
        """When the board has no Edge.Cuts, board_valid is False and
        in_board silently accepts every position.  This is intentional
        (the auto-fix runs on boards under construction) but means a
        silkscreen-relocation that produces an outside-the-board
        position passes when there's no outline to check against."""
        text = _FakeBBox(99999, 99999, 100000, 100000)
        helpers["board_valid"] = False
        try:
            assert helpers["in_board"](text) is True
        finally:
            helpers["board_valid"] = True


# ---------------------------------------------------------------------------
# bbox_inside_board: same non-strict semantics, separate helper
# ---------------------------------------------------------------------------

class TestBboxInsideBoardBoundary:
    """The auto_fix_placement script has its own copy of "is this rect
    inside the board outline".  Same semantic as in_board / rect_inside
    -- non-strict.  Pin it independently so a divergence between the
    three would be caught."""

    def test_inside(self, helpers):
        assert helpers["bbox_inside_board"](10, 10, 50, 50) is True

    def test_exact_match_is_inside(self, helpers):
        assert helpers["bbox_inside_board"](0, 0, 100, 100) is True

    def test_flush_each_edge(self, helpers):
        # flush left
        assert helpers["bbox_inside_board"](0, 50, 50, 60) is True
        # flush right
        assert helpers["bbox_inside_board"](50, 50, 100, 60) is True
        # flush top
        assert helpers["bbox_inside_board"](50, 0, 60, 50) is True
        # flush bottom
        assert helpers["bbox_inside_board"](50, 50, 60, 100) is True

    def test_one_unit_past_each_edge_is_outside(self, helpers):
        assert helpers["bbox_inside_board"](-1, 50, 50, 60) is False
        assert helpers["bbox_inside_board"](50, 50, 101, 60) is False
        assert helpers["bbox_inside_board"](50, -1, 60, 50) is False
        assert helpers["bbox_inside_board"](50, 50, 60, 101) is False

    def test_no_outline_always_inside(self, helpers):
        """When no Edge.Cuts has been added yet, outline is None and
        every position is "inside".  Mirrors in_board's board_valid
        early return -- both silent-pass behaviours need to be visible
        in the test suite so a future refactor doesn't accidentally
        flip them."""
        helpers["outline"] = None
        try:
            assert helpers["bbox_inside_board"](-1000, -1000, 1000, 1000) is True
        finally:
            helpers["outline"] = (0.0, 0.0, 100.0, 100.0)


# ---------------------------------------------------------------------------
# Consumer-asymmetry pin: the silkscreen autofix loop's accept condition
# ---------------------------------------------------------------------------

class TestSilkscreenAutofixAcceptCondition:
    """The autofix loop in drc_autofix accepts a new silkscreen position
    iff `not has_any_overlap(...) and in_board(...)`.  Combined, the
    strict-overlap and non-strict-in-board semantics let a position
    sitting exactly on the pad edge AND exactly on the board edge pass
    the accept check.

    KiCad's DRC then flags such a layout for `silk_over_copper` (silkscreen
    on copper edge) and `min_silk_clearance` (silkscreen on board edge),
    even though Python said the move was valid.  This test pins the
    behaviour so anyone reading the autofix loop sees the consumer gap.
    """

    def test_accept_condition_flush_against_pad_and_board_passes(self, helpers):
        """Worst case: silk text flush against pad edge AND board edge."""
        # Pad at (50, 50)-(60, 60); board at (0, 0)-(100, 100).
        # Text spans (60, 0)-(70, 10): flush against pad's right edge
        # AND flush against board's top edge.
        text = _FakeBBox(60, 0, 70, 10)
        pad_x0, pad_y0, pad_x1, pad_y1 = 50, 50, 60, 60
        overlaps_pad = helpers["_aabb_hit"](text, pad_x0, pad_y0, pad_x1, pad_y1)
        inside_board = helpers["in_board"](text)

        # Producer says: this position is acceptable.
        assert overlaps_pad is False
        assert inside_board is True
        producer_accepts = not overlaps_pad and inside_board
        assert producer_accepts is True

        # Consumer (KiCad DRC) would flag: silk text on board edge
        # violates min_silk_to_edge clearance, and being flush against
        # a pad violates silk_over_copper.  Pin the gap explicitly --
        # if a future refactor tightens _aabb_hit to non-strict, this
        # producer_accepts will flip to False and the test fails,
        # prompting an update of this regression-pin.
