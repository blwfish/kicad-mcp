"""Pure tests for mounting-hole resolution (Phase 5). The footprint/keepout step
itself is integration-gated (real KiCad); this covers the defaults-merge logic.
Boundary-focused per CLAUDE.md: None vs {} vs partial-override vs count=0.
"""
from unittest.mock import patch

from kicad_mcp.tools.pcb_pipeline import _resolve_mounting_holes, _step_add_mounting_holes

_DEFAULT = {"count": 4, "drill_mm": 3.2, "inset_mm": 3.5, "keepout_mm": 1.5}


def test_none_is_full_default():
    assert _resolve_mounting_holes(None) == _DEFAULT


def test_empty_mapping_is_full_default():
    assert _resolve_mounting_holes({}) == _DEFAULT


def test_partial_override_merges_over_defaults():
    assert _resolve_mounting_holes({"count": 2, "drill_mm": 2.7}) == \
        {"count": 2, "drill_mm": 2.7, "inset_mm": 3.5, "keepout_mm": 1.5}


def test_count_zero_preserved_not_defaulted():
    # count=0 (disable) must survive the merge, not get overwritten by default 4.
    assert _resolve_mounting_holes({"count": 0})["count"] == 0


def test_unknown_keys_ignored_only_known_merged():
    # _resolve only copies known keys (validation/rejection is the sidecar's job).
    assert _resolve_mounting_holes({"count": 2, "bogus": 9}) == \
        {"count": 2, "drill_mm": 3.2, "inset_mm": 3.5, "keepout_mm": 1.5}


def test_defaults_and_known_keys_share_one_source_of_truth():
    """Regression: pcb_pipeline._HOLE_DEFAULTS (what _resolve_mounting_holes
    merges over) and sidecar._KNOWN_MOUNTING_HOLES_KEYS (what
    _validate_mounting_holes accepts) used to be two independently-typed
    literals with no import tying them together and no test referencing
    either -- a key added to one would silently leave the other unaware
    (an accepted-but-undefaulted key, or a defaulted-but-rejected one).
    pcb_pipeline now imports _HOLE_DEFAULTS from sidecar.py; this pins that
    they stay the same object, not just equal by coincidence."""
    from kicad_mcp.tools import pcb_pipeline
    from kicad_mcp.utils.firmware import sidecar

    assert pcb_pipeline._HOLE_DEFAULTS is sidecar._HOLE_DEFAULTS
    assert sidecar._KNOWN_MOUNTING_HOLES_KEYS == frozenset(pcb_pipeline._HOLE_DEFAULTS)


@patch("kicad_mcp.tools.pcb_pipeline.run_pcbnew_script")
def test_keepout_allows_pads_blocks_pour(mock_run):
    """The per-hole keepout must NOT disallow pads — the mounting hole's own NPTH
    pad (a bare 3.2mm hole, no copper) would otherwise self-flag as an
    items_not_allowed DRC error. It MUST disallow zone fills (the copper pour) —
    that's what actually keeps the screw-head annulus clear, and was missing."""
    mock_run.return_value = {"status": "ok"}
    _step_add_mounting_holes("/tmp/x.kicad_pcb", {"count": 4, "drill_mm": 3.2,
                                                  "inset_mm": 3.5, "keepout_mm": 1.5})
    script = mock_run.call_args[0][0]
    assert "SetDoNotAllowTracks(True)" in script
    assert "SetDoNotAllowVias(True)" in script
    assert "SetDoNotAllowPads(False)" in script        # NPTH pad must not self-flag
    # pour kept off the annulus, version-robust: KiCad 10 ZoneFills / KiCad 9
    # CopperPour (a 10-only call broke the 9.0 integration job).
    assert "SetDoNotAllowZoneFills(True)" in script
    assert "SetDoNotAllowCopperPour(True)" in script
    assert "SetDoNotAllowPads(True)" not in script


def test_count_zero_emits_no_script():
    # count=0 returns early without building/running a pcbnew script.
    with patch("kicad_mcp.tools.pcb_pipeline.run_pcbnew_script") as mock_run:
        r = _step_add_mounting_holes("/tmp/x.kicad_pcb", {"count": 0})
        assert r["holes_added"] == 0
        mock_run.assert_not_called()


def test_invalid_count_rejected_not_silently_placing_two():
    """Regression: sidecar._validate_mounting_holes enforces count in
    {0, 2, 4} on the board.yaml path, but _step_add_mounting_holes itself
    (called directly by design(operation="import_intent"), which has no
    sidecar validation at all) used to fall through its count==4/else branch
    for ANY other value -- count:3 silently placed 2 holes and reported
    status="ok", with nothing to show fewer holes were placed than asked
    for. Validated at the point of use so both callers are covered."""
    for bad_count in (1, 3, 5, -1, True, "4"):
        with patch("kicad_mcp.tools.pcb_pipeline.run_pcbnew_script") as mock_run:
            r = _step_add_mounting_holes("/tmp/x.kicad_pcb", {"count": bad_count})
            assert r["status"] == "error", f"count={bad_count!r} should be rejected"
            assert str(bad_count) in r["error"] or repr(bad_count) in r["error"]
            mock_run.assert_not_called()


def test_valid_counts_still_accepted():
    for good_count in (0, 2, 4):
        with patch("kicad_mcp.tools.pcb_pipeline.run_pcbnew_script") as mock_run:
            mock_run.return_value = {"status": "ok", "holes_added": good_count, "positions": []}
            r = _step_add_mounting_holes("/tmp/x.kicad_pcb", {
                "count": good_count, "drill_mm": 3.2, "inset_mm": 3.5, "keepout_mm": 1.5,
            })
            assert r["status"] == "ok"
