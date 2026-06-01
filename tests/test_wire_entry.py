"""Wire-entry family table + geometry rule (spec §1, H2, Phase 2).

Boundary-focused per CLAUDE.md Threshold-Boundary Rule: the ~0.4 mm asymmetry
threshold is contract, so every band edge is pinned at/below/above. The geometry
rule is exercised on SYNTHETIC bboxes (no KiCad dependency); a real-footprint
reproduction lives in the integration tier / the generator self-check.
"""
import pytest

from kicad_mcp.utils.placement.wire_entry import (
    HIGH_MIN_ASYMMETRY_MM, LOW_MIN_ASYMMETRY_MM, MIN_ROW_AXIS_SPAN_MM,
    WIRE_ENTRY, WIRE_ENTRY_HELPER, classify_confidence, derive_wire_entry, lookup,
    normalize_family,
)

EPS = 1e-6


# --- normalize_family ---------------------------------------------------------

def test_normalize_strips_library_prefix():
    assert normalize_family("TerminalBlock_Phoenix:Foo_1x03_Horizontal") == "Foo_1xN_Horizontal"


def test_normalize_collapses_pin_count_variants_to_one_key():
    # The single source-of-truth guarantee: every size variant shares a key.
    keys = {
        normalize_family(
            f"TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_1x{n:02d}_P5.08mm_Horizontal"
        )
        for n in (2, 3, 8, 16)
    }
    assert keys == {"TerminalBlock_Phoenix_MKDS-1,5-N-5.08_1xN_P5.08mm_Horizontal"}


def test_normalize_preserves_orientation_suffix():
    # Horizontal vs Vertical is the whole point — must NOT be collapsed away.
    h = normalize_family("PinHeader_1x03_P2.54mm_Horizontal")
    v = normalize_family("PinHeader_1x03_P2.54mm_Vertical")
    assert h != v
    assert h == "PinHeader_1xN_P2.54mm_Horizontal"
    assert v == "PinHeader_1xN_P2.54mm_Vertical"


def test_normalize_no_pincount_token_unchanged():
    assert normalize_family("SomeBlock_P5.08mm_Horizontal") == "SomeBlock_P5.08mm_Horizontal"


def test_normalize_keeps_row_count_distinct():
    # 1-row and 2-row are physically different connectors (different wire-entry
    # geometry) — they must NOT collapse to one family.
    one = normalize_family("PinHeader_1x04_P2.54mm_Vertical")
    two = normalize_family("PinHeader_2x04_P2.54mm_Vertical")
    assert one == "PinHeader_1xN_P2.54mm_Vertical"
    assert two == "PinHeader_2xN_P2.54mm_Vertical"
    assert one != two


def test_normalize_does_not_eat_physical_dimension_token():
    # "5x8mm" is a size, not a pin array — must be left intact.
    assert normalize_family("Lug_5x8mm_Horizontal") == "Lug_5x8mm_Horizontal"


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


def test_square_two_pad_cluster_has_no_row_axis():
    # Two pads but a ~square cluster: |span_x - span_y| < threshold -> skip.
    pads = [(-1.0, -1.0, 1.0, 1.0), (-1.0, -1.0, 1.0, 1.0)]
    cy = (-3.0, -3.0, 3.0, 3.0)
    _, _, conf = derive_wire_entry(pads, cy)
    assert conf == "skip"


@pytest.mark.parametrize("sx,sy,skips", [
    (2.5, 2.0, False),    # |span_x - span_y| == 0.5 == threshold -> strict < -> NOT skip
    (2.499, 2.0, True),   # 0.499 < 0.5 -> skip (no clear row axis)
    (2.501, 2.0, False),  # 0.501 -> has a row axis -> proceeds
])
def test_row_axis_span_boundary(sx, sy, skips):
    # Two pads spanning (sx, sy); a clear -Y courtyard overhang so a non-skip
    # resolves to high. Pins the MIN_ROW_AXIS_SPAN_MM strict-< boundary.
    pads = [(0.0, 0.0, 1.0, sy), (sx - 1.0, 0.0, sx, sy)]
    cy = (-1.0, -5.0, sx + 1.0, sy + 1.0)
    _, _, conf = derive_wire_entry(pads, cy)
    assert (conf == "skip") == skips
    assert MIN_ROW_AXIS_SPAN_MM == 0.5   # the boundary the cases are built around


def test_single_anisotropic_pad_skips():
    # A LONE oval pad has an anisotropic shape that passes the row-axis span
    # guard — but a single pad has no ROW, so it must skip (else a lug/clip could
    # synthesize a confident-but-wrong vector from pad shape).
    pads = [(-0.85, -1.7, 0.85, 1.7)]      # 1.7 x 3.4 oval -> span diff 1.7 > guard
    cy = (-1.5, -5.0, 3.5, 4.0)            # asymmetric courtyard
    vec, _, conf = derive_wire_entry(pads, cy)
    assert conf == "skip" and vec is None


def test_missing_geometry_skips():
    assert derive_wire_entry([], (0, 0, 1, 1))[2] == "skip"
    assert derive_wire_entry([(0, 0, 1, 1)], None)[2] == "skip"


# --- shipped table ------------------------------------------------------------

def test_mkds_family_shipped_negative_y():
    key = "TerminalBlock_Phoenix_MKDS-1,5-N-5.08_1xN_P5.08mm_Horizontal"
    assert WIRE_ENTRY[key] == (0.0, -1.0)


def test_lookup_resolves_real_synthesized_footprint_name():
    # The exact name connectors.synthesize_connector emits (lib:item form).
    name = "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3-5.08_1x03_P5.08mm_Horizontal"
    assert lookup(name) == (0.0, -1.0)


def test_lookup_unknown_family_returns_none():
    assert lookup("Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical") is None


# --- bridge helper drift (the injected normalize_family must not diverge) ------

def test_helper_normalize_family_matches_import():
    """WIRE_ENTRY_HELPER is exec'd inside pcbnew's interpreter; its copy of
    normalize_family must stay behaviourally identical to the imported one
    (CLAUDE.md Syntactic-Semantic Seam Rule — single source of truth)."""
    ns: dict = {}
    exec(WIRE_ENTRY_HELPER, ns)
    helper_fn = ns["normalize_family"]
    samples = [
        "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3-5.08_1x03_P5.08mm_Horizontal",
        "TerminalBlock_Phoenix_MKDS-1,5-16-5.08_1x16_P5.08mm_Horizontal",
        "PinHeader_1x04_P2.54mm_Vertical",
        "Conn_02x05_Odd_Even",
        # exercises the (?!\d*mm) lookahead: collapse 1x04 but leave 5x8mm intact
        "SomePart_1x04_5x8mm_Horizontal",
        "SomeBlock_P5.08mm_Horizontal",
        "",
    ]
    for s in samples:
        assert helper_fn(s) == normalize_family(s), s
