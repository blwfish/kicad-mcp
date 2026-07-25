"""Tests for pin resolution (CI-safe, fake symbols) + schematic generation
(gated on a real KiCad symbol library being present)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_mcp.utils.firmware.mcu_pinmap import gpio_to_pin_number, pin_number_by_name

# --- fake symbol for pin-map unit tests (no KiCad needed) --------------------

@dataclass
class _Pin:
    name: str
    number: str

@dataclass
class _Sym:
    pins: list


_ESP_LIKE = _Sym(pins=[
    _Pin("IO21", "33"), _Pin("IO22", "36"), _Pin("IO16", "27"),
    _Pin("RXD0/IO3", "34"), _Pin("TXD0/IO1", "35"), _Pin("IO0", "25"),
    _Pin("SENSOR_VP", "5"),    # input-only, no IO token
])


@pytest.mark.parametrize("gpio,expected", [
    (21, "33"), (22, "36"), (16, "27"),
    (3, "34"),   # RXD0/IO3
    (1, "35"),   # TXD0/IO1
    (0, "25"),
])
def test_gpio_to_pin_number(gpio, expected):
    assert gpio_to_pin_number(_ESP_LIKE, gpio) == expected

def test_gpio_token_does_not_partial_match():
    # gpio 2 must NOT match "IO21"/"IO22"; gpio 3 must NOT match nonexistent IO35.
    assert gpio_to_pin_number(_ESP_LIKE, 2) is None
    assert gpio_to_pin_number(_ESP_LIKE, 5) is None  # SENSOR_VP has no IO token

def test_pin_number_by_name():
    sym = _Sym(pins=[_Pin("SDA", "13"), _Pin("SCK", "12"), _Pin("INTA", "20")])
    assert pin_number_by_name(sym, "SDA") == "13"
    assert pin_number_by_name(sym, "INTA") == "20"
    assert pin_number_by_name(sym, "MISSING") is None
    assert pin_number_by_name(sym, None) is None

def test_pin_map_handles_none_symbol():
    assert gpio_to_pin_number(None, 5) is None
    assert pin_number_by_name(None, "SDA") is None


# --- build-status contract (CI-safe; pure decision, no KiCad) ----------------

@pytest.mark.parametrize("errs,unres,any_placed,expected", [
    # clean build -> ok (the only "ok" cases: BOTH failure lists empty)
    ([],            [],           True,  "ok"),
    ([],            [],           False, "ok"),    # no components, no failures: pinned ok
    # a dropped component (the MCP23017-rename failure mode) is NOT ok
    ([{"ref": "U3"}], [],         True,  "partial"),  # others still placed
    ([{"ref": "U1"}], [],         False, "error"),    # nothing placed (e.g. bad MCU)
    # an endpoint that should have wired but didn't is NOT ok
    ([],            [{"net": "X"}], True,  "partial"),
    ([],            [{"net": "X"}], False, "error"),
    # both failure signals present
    ([{"ref": "U3"}], [{"net": "X"}], True,  "partial"),
    ([{"ref": "U3"}], [{"net": "X"}], False, "error"),
])
def test_build_status(errs, unres, any_placed, expected):
    from kicad_mcp.utils.firmware.generate import _build_status
    assert _build_status(errs, unres, any_placed) == expected


# --- compact table layout (CI-safe; pure geometry, no KiCad symbols needed) --

def test_snap_rounds_to_grid_including_exact_halfway():
    from kicad_mcp.utils.firmware.generate import _GRID_MM, _snap
    assert _snap(0.0) == 0.0
    assert _snap(_GRID_MM) == _GRID_MM
    # Exact halfway between two grid points is decided by Python's round-half-
    # to-even, not "always round up" — pin the actual behavior so a rounding-
    # mode change (e.g. a switch to math.floor(x+0.5)) is visible as a diff.
    assert _snap(_GRID_MM * 0.5) == 0.0            # 0.5 -> 0 (even)
    assert _snap(_GRID_MM * 1.5) == _GRID_MM * 2   # 1.5 -> 2 (even)


@pytest.mark.parametrize("cols,extents,expected_w,expected_h", [
    # single column: no horizontal gutter (cols-1==0 boundary) — only the
    # vertical row gutter between the two stacked symbols applies.
    (1, [(0, 0, 10, 5), (0, 0, 10, 5)], 10, 5 + 5 + 11.0),
    # single row: no vertical gutter (rows-1==0 boundary) — only the
    # horizontal column gutter between the two side-by-side symbols applies.
    (2, [(0, 0, 10, 5), (0, 0, 10, 5)], 10 + 10 + 16.0, 5),
])
def test_grid_dims_boundary_gutter_only_between_cells(cols, extents, expected_w, expected_h):
    # Regression: a 1-col or 1-row grid must NOT add a gutter for a
    # nonexistent second column/row.
    from kicad_mcp.utils.firmware.generate import _grid_dims
    _cw, _rh, w, h = _grid_dims(extents, cols)
    assert w == pytest.approx(expected_w)
    assert h == pytest.approx(expected_h)


def test_choose_cols_empty_and_single():
    from kicad_mcp.utils.firmware.generate import _choose_cols
    # n=0: defensive default. generate_schematic never actually calls this with
    # an empty list (it special-cases "no placed components" itself), but the
    # function's own contract shouldn't divide by zero or return 0 columns.
    assert _choose_cols([]) == 1
    assert _choose_cols([(0, 0, 10, 10)]) == 1


def test_choose_cols_picks_true_aspect_minimum():
    from kicad_mcp.utils.firmware.generate import _TARGET_ASPECT, _choose_cols, _grid_dims
    extents = [(0, 0, 10, 10)] * 12

    def dev(cols):
        _cw, _rh, w, h = _grid_dims(extents, cols)
        return abs((w / h) - _TARGET_ASPECT)

    chosen = _choose_cols(extents)
    chosen_dev = dev(chosen)
    # Must be a true global argmin over every candidate column count, not an
    # off-by-one or first-local-minimum stopping point.
    assert all(chosen_dev <= dev(c) for c in range(1, len(extents) + 1))


# --- exact page sizing (CI-safe; pure geometry, no KiCad symbol libraries) ---

def test_finalize_page_empty_schematic_leaves_default_untouched():
    import kicad_sch_api as ksa

    from kicad_mcp.utils.firmware.generate import _finalize_page
    sch = ksa.create_schematic("t")
    assert _finalize_page(sch) == (0.0, 0.0)


def test_finalize_page_shifts_negative_content_to_margin():
    import kicad_sch_api as ksa

    from kicad_mcp.utils.firmware.generate import _PAGE_MARGIN_MM, _finalize_page
    sch = ksa.create_schematic("t")
    sch.add_wire(start=(-5.0, -3.0), end=(10.0, 10.0))
    sch.add_label("NET1", (10.0, 10.0))

    w, h = _finalize_page(sch)
    assert w > 0 and h > 0

    # The leftmost/topmost point (-5, -3) must land exactly at the margin —
    # this is what keeps negative-coordinate content from clipping on export.
    wire = list(sch.wires)[0]
    assert min(p.x for p in wire.points) == pytest.approx(_PAGE_MARGIN_MM)
    assert min(p.y for p in wire.points) == pytest.approx(_PAGE_MARGIN_MM)


def test_finalize_page_reserves_titleblock_corner_for_small_content():
    """Regression: KiCad renders the title block anchored to the bottom-right
    page corner at a FIXED footprint (measured ~108x34mm), independent of page
    size. A page sized tight to content with only the plain margin on every
    side packs components straight into that corner — this was caught by eye
    on a real 3-component render (U3 sat directly under "Size: User / Date:").
    A page must always leave the full reserved footprint clear, even for
    content far smaller than the title block itself."""
    import kicad_sch_api as ksa

    from kicad_mcp.utils.firmware.generate import (
        _PAGE_MARGIN_MM,
        _TITLEBLOCK_H_MM,
        _TITLEBLOCK_W_MM,
        _finalize_page,
    )
    sch = ksa.create_schematic("t")
    # A single small label — far smaller than the title block's own footprint.
    sch.add_label("NET1", (0.0, 0.0))

    w, h = _finalize_page(sch)

    assert w >= _TITLEBLOCK_W_MM + _PAGE_MARGIN_MM
    assert h >= (0.0 - 0.0) + _PAGE_MARGIN_MM + _TITLEBLOCK_H_MM
    # The content itself still sits at the plain margin (unaffected) — only
    # the page's own bottom/right padding grew to clear the title block.
    label = list(sch.labels)[0]
    assert label.position.x == pytest.approx(_PAGE_MARGIN_MM)
    assert label.position.y == pytest.approx(_PAGE_MARGIN_MM)


# --- generation integration (needs real KiCad symbol libraries) --------------

SPEEDCAL_CONFIG = Path(
    "/Volumes/Files/claude/mr-esp32/speed-cal/firmware/include/config.h"
)


def _kicad_symbols_available() -> bool:
    try:
        from kicad_sch_api.library.cache import get_symbol_cache
        return get_symbol_cache().get_symbol("RF_Module:ESP32-WROOM-32E") is not None
    except Exception:
        return False


@pytest.mark.skipif(not _kicad_symbols_available(),
                    reason="KiCad symbol libraries not available")
@pytest.mark.skipif(not SPEEDCAL_CONFIG.exists(), reason="speed-cal config.h not present")
def test_generate_speedcal_end_to_end(tmp_path):
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
    from kicad_mcp.utils.firmware.parse import parse_defines, partition
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

    parsed = partition(parse_defines(SPEEDCAL_CONFIG.read_text()))
    intent = build_intent(parsed, firmware_path=str(SPEEDCAL_CONFIG),
                          board_id=find_board_id(str(SPEEDCAL_CONFIG)))
    out = tmp_path / "gen.kicad_sch"
    res = generate_schematic(intent, str(out))

    assert res["status"] == "ok"
    # PIEZO and TRACK are terminal-only cards (realize: terminal) — intrinsically
    # off-board, so generate excludes them from schematic placement even on this
    # raw (non-expanded) intent. Only the on-board ICs are placed; their terminal
    # signals are not flagged unresolved (a screw terminal carries them at expand).
    assert res["components_placed"] == 3        # ESP32 + HX711 + MCP23017
    assert not res["component_errors"] and not res["unresolved_endpoints"]

    nl = extract_netlist_via_cli(str(out))
    assert nl is not None

    def members(name):
        return {f"{x['component']}.{x['pin']}" for x in nl["nets"].get(name, [])}

    # bus + peripheral nets join the correct pins (matches v5 connectivity)
    assert members("I2C_SDA") == {"U1.33", "U3.13"}      # ESP32 IO21 ↔ MCP SDA
    assert members("I2C_SCL") == {"U1.36", "U3.12"}      # ESP32 IO22 ↔ MCP SCL(=SCK pin)
    assert members("HX711_DOUT") == {"U1.27", "U2.12"}
    assert members("HX711_SCK") == {"U1.28", "U2.11"}    # firmware SCK -> PD_SCK pin
    assert members("MCP23017_INT") == {"U1.16", "U3.20"} # -> INTA pin
    # orphan net: MCU pin only, far end open
    assert members("I2S_SCK") == {"U1.30"}


@pytest.mark.skipif(not _kicad_symbols_available(),
                    reason="KiCad symbol libraries not available")
@pytest.mark.skipif(not SPEEDCAL_CONFIG.exists(), reason="speed-cal config.h not present")
def test_unresolvable_symbol_flips_status_off_ok(tmp_path):
    """A component whose lib_id doesn't resolve must NOT report status 'ok'.

    Regression for the MCP23017 cross-version rename: KiCad 10 renamed the
    symbol family (``MCP23017_SO`` -> ``MCP23017x-x-SO``), so the card's
    KiCad-10-only lib_id silently dropped U3 on KiCad 9 while the build still
    reported ``ok``. Here we simulate that by corrupting an on-board
    peripheral's lib_id to a name that resolves on NO version.
    """
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
    from kicad_mcp.utils.firmware.knowledge import is_terminal_card_type
    from kicad_mcp.utils.firmware.parse import parse_defines, partition

    parsed = partition(parse_defines(SPEEDCAL_CONFIG.read_text()))
    intent = build_intent(parsed, firmware_path=str(SPEEDCAL_CONFIG),
                          board_id=find_board_id(str(SPEEDCAL_CONFIG)))
    # Pick an on-board peripheral (terminal cards are never placed, so corrupting
    # one would not produce a component_error).
    target = next(p for p in intent.peripherals
                  if p.lib_id and not is_terminal_card_type(p.type))
    target.lib_id = "Interface_Expansion:MCP23017_DOES_NOT_EXIST"

    out = tmp_path / "gen.kicad_sch"
    res = generate_schematic(intent, str(out))

    assert res["status"] == "partial"          # the bug: this used to be "ok"
    assert any(e["ref"] == target.ref for e in res["component_errors"])
    assert res["components_placed"] >= 1        # the MCU + other IC still placed


@pytest.mark.skipif(not _kicad_symbols_available(),
                    reason="KiCad symbol libraries not available")
@pytest.mark.skipif(not SPEEDCAL_CONFIG.exists(), reason="speed-cal config.h not present")
def test_generated_schematic_fits_page(tmp_path):
    """Every placed symbol must sit inside the sheet at positive coordinates.

    Regression: the old fixed-pitch grid pushed content onto a >600 mm strip
    (and above/left of the origin) while the sheet stayed A4 297x210, so SVG/PDF
    export silently clipped most of the drawing. The generator now packs to the
    content and sizes the page exactly to fit it."""
    import re

    import kicad_sch_api as ksa
    from kicad_sch_api.core.component_bounds import get_component_bounding_box

    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
    from kicad_mcp.utils.firmware.parse import parse_defines, partition

    parsed = partition(parse_defines(SPEEDCAL_CONFIG.read_text()))
    intent = build_intent(parsed, firmware_path=str(SPEEDCAL_CONFIG),
                          board_id=find_board_id(str(SPEEDCAL_CONFIG)))
    out = tmp_path / "gen.kicad_sch"
    res = generate_schematic(intent, str(out))
    assert res["status"] == "ok"

    page_w, page_h = res["page_size_mm"]
    assert page_w > 0 and page_h > 0

    # The saved sheet carries an explicit User size matching the reported dims.
    m = re.search(r'\(paper "User" ([\d.]+) ([\d.]+)\)', out.read_text())
    assert m, 'expected an explicit (paper "User" W H) sheet'
    assert abs(float(m.group(1)) - page_w) < 0.1
    assert abs(float(m.group(2)) - page_h) < 0.1

    # No symbol may fall outside the viewBox or into negative coordinates —
    # that is exactly what clips on export. Includes the power-marker band.
    sch = ksa.load_schematic(str(out))
    for comp in sch.components:
        bb = get_component_bounding_box(comp, include_properties=False)
        assert bb.min_x >= 0 and bb.min_y >= 0, (comp.reference, bb.min_x, bb.min_y)
        assert bb.max_x <= page_w and bb.max_y <= page_h, (
            comp.reference, bb.max_x, bb.max_y, page_w, page_h)

    # Nor may a symbol sit inside KiCad's title block — the reserved corner is
    # invisible to bare page-bounds checks (it's blank space, not a component),
    # which is exactly how the first version of this fix shipped with U3
    # rendering directly under the title block's "Size / Date" row.
    from kicad_mcp.utils.firmware.generate import _TITLEBLOCK_H_MM, _TITLEBLOCK_W_MM
    titleblock_left = page_w - _TITLEBLOCK_W_MM
    titleblock_top = page_h - _TITLEBLOCK_H_MM
    for comp in sch.components:
        bb = get_component_bounding_box(comp, include_properties=False)
        in_titleblock_band = bb.max_x > titleblock_left and bb.max_y > titleblock_top
        assert not in_titleblock_band, (comp.reference, bb.min_x, bb.min_y,
                                        bb.max_x, bb.max_y, titleblock_left, titleblock_top)
