"""Wire-entry family table + geometry rule (spec §1, H2, Phase 2).

Boundary-focused per CLAUDE.md Threshold-Boundary Rule: the ~0.4 mm asymmetry
threshold is contract, so every band edge is pinned at/below/above. The geometry
rule is exercised on SYNTHETIC bboxes (no KiCad dependency); a real-footprint
reproduction lives in the integration tier / the generator self-check.
"""
import pytest

from kicad_mcp.utils.placement.wire_entry import (
    HIGH_MIN_ASYMMETRY_MM, LOW_MIN_ASYMMETRY_MM, MIN_ROW_AXIS_SPAN_MM,
    WIRE_ENTRY, classify_confidence, derive_wire_entry, lookup, normalize_family,
)

EPS = 1e-6


# --- normalize_family ---------------------------------------------------------

def test_normalize_strips_library_prefix():
    assert normalize_family("TerminalBlock_Phoenix:Foo_1x03_Horizontal") == "Foo_NxN_Horizontal"


def test_normalize_collapses_pin_count_variants_to_one_key():
    # The single source-of-truth guarantee: every size variant shares a key.
    keys = {
        normalize_family(
            f"TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_1x{n:02d}_P5.08mm_Horizontal"
        )
        for n in (2, 3, 8, 16)
    }
    assert keys == {"TerminalBlock_Phoenix_MKDS-1,5-N-5.08_NxN_P5.08mm_Horizontal"}


def test_normalize_preserves_orientation_suffix():
    # Horizontal vs Vertical is the whole point — must NOT be collapsed away.
    h = normalize_family("PinHeader_1x03_P2.54mm_Horizontal")
    v = normalize_family("PinHeader_1x03_P2.54mm_Vertical")
    assert h != v
    assert h == "PinHeader_NxN_P2.54mm_Horizontal"
    assert v == "PinHeader_NxN_P2.54mm_Vertical"


def test_normalize_no_pincount_token_unchanged():
    assert normalize_family("SomeBlock_P5.08mm_Horizontal") == "SomeBlock_P5.08mm_Horizontal"


# --- classify_confidence (threshold boundaries) -------------------------------

@pytest.mark.parametrize("asym,expected", [
    (HIGH_MIN_ASYMMETRY_MM + EPS, "high"),
    (HIGH_MIN_ASYMMETRY_MM, "high"),            # >= is the contract
    (HIGH_MIN_ASYMMETRY_MM - EPS, "low"),
    (LOW_MIN_ASYMMETRY_MM + EPS, "low"),
    (LOW_MIN_ASYMMETRY_MM, "low"),
    (LOW_MIN_ASYMMETRY_MM - EPS, "skip"),
    (0.0, "skip"),
])
def test_classify_confidence_bands(asym, expected):
    assert classify_confidence(asym) == expected


# --- derive_wire_entry --------------------------------------------------------

def _row_along_x(over_neg_y, over_pos_y, n_pads=3, pitch=5.08, pad_half=1.3):
    """Pads in a row along X (y=0); courtyard overhangs the pad row by the given
    amounts on -Y / +Y. Returns (pad_bboxes, courtyard_bbox)."""
    pads = [(-pad_half + i * pitch, -pad_half, pad_half + i * pitch, pad_half)
            for i in range(n_pads)]
    pxmin = min(b[0] for b in pads); pxmax = max(b[2] for b in pads)
    cy = (pxmin - 1.0, -pad_half - over_neg_y, pxmax + 1.0, pad_half + over_pos_y)
    return pads, cy


def test_mkds_like_geometry_wire_enters_negative_y():
    # The verified MKDS case: -Y overhang 4.41, +Y overhang 3.80 -> asym 0.61.
    pads, cy = _row_along_x(over_neg_y=4.41, over_pos_y=3.80)
    vec, asym, conf = derive_wire_entry(pads, cy)
    assert conf == "high"
    assert vec == (0.0, -1.0)
    assert asym == pytest.approx(0.61, abs=1e-3)


def test_positive_y_dominant_flips_vector():
    pads, cy = _row_along_x(over_neg_y=3.0, over_pos_y=4.0)
    vec, _, conf = derive_wire_entry(pads, cy)
    assert conf == "high" and vec == (0.0, 1.0)


def test_symmetric_courtyard_skips_no_vector():
    # A vertical header's perpendicular axis is symmetric -> not a clamp family.
    pads, cy = _row_along_x(over_neg_y=3.7, over_pos_y=3.7)
    vec, asym, conf = derive_wire_entry(pads, cy)
    assert conf == "skip" and vec is None and asym == 0.0


@pytest.mark.parametrize("asym_target,expected_conf,expect_vec", [
    (HIGH_MIN_ASYMMETRY_MM, "high", True),          # exactly 0.4 -> high
    (HIGH_MIN_ASYMMETRY_MM - 0.01, "low", False),   # just below -> low, NOT shipped
    (LOW_MIN_ASYMMETRY_MM - 0.01, "skip", False),   # below low -> skip
])
def test_derive_threshold_boundaries(asym_target, expected_conf, expect_vec):
    pads, cy = _row_along_x(over_neg_y=4.0, over_pos_y=4.0 - asym_target)
    vec, asym, conf = derive_wire_entry(pads, cy)
    assert asym == pytest.approx(asym_target, abs=1e-3)
    assert conf == expected_conf
    assert (vec is not None) == expect_vec


def test_square_cluster_has_no_row_axis():
    # |span_x - span_y| < MIN_ROW_AXIS_SPAN_MM -> ambiguous -> skip.
    pads = [(-1.0, -1.0, 1.0, 1.0)]  # single square pad
    cy = (-3.0, -3.0, 3.0, 3.0)
    assert abs((1.0 - -1.0) - (1.0 - -1.0)) < MIN_ROW_AXIS_SPAN_MM
    _, _, conf = derive_wire_entry(pads, cy)
    assert conf == "skip"


def test_missing_geometry_skips():
    assert derive_wire_entry([], (0, 0, 1, 1))[2] == "skip"
    assert derive_wire_entry([(0, 0, 1, 1)], None)[2] == "skip"


# --- shipped table ------------------------------------------------------------

def test_mkds_family_shipped_negative_y():
    key = "TerminalBlock_Phoenix_MKDS-1,5-N-5.08_NxN_P5.08mm_Horizontal"
    assert WIRE_ENTRY[key] == (0.0, -1.0)


def test_lookup_resolves_real_synthesized_footprint_name():
    # The exact name connectors.synthesize_connector emits (lib:item form).
    name = "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3-5.08_1x03_P5.08mm_Horizontal"
    assert lookup(name) == (0.0, -1.0)


def test_lookup_unknown_family_returns_none():
    assert lookup("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical") is None
