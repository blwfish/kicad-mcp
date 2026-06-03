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
