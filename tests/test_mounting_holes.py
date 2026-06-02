"""Pure tests for mounting-hole resolution (Phase 5). The footprint/keepout step
itself is integration-gated (real KiCad); this covers the defaults-merge logic.
Boundary-focused per CLAUDE.md: None vs {} vs partial-override vs count=0.
"""
from kicad_mcp.tools.pcb_pipeline import _resolve_mounting_holes

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
