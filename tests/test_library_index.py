"""Tests for the unified library index (symbols + footprints)."""

import os
import sqlite3

import pytest

from kicad_mcp.utils.library_index import (
    LibraryIndex,
    _parse_kicad_mod,
    _parse_kicad_sym,
)


# ---------------------------------------------------------------------------
# Sample .kicad_mod content
# ---------------------------------------------------------------------------

SAMPLE_RESISTOR_MOD = '''\
(footprint "R_0603_1608Metric"
\t(version 20241229)
\t(generator "kicad-footprint-generator")
\t(layer "F.Cu")
\t(descr "Resistor SMD 0603 (1608 Metric), square end terminal")
\t(tags "resistor 0603")
\t(pad "1" smd roundrect (at -0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
\t(pad "2" smd roundrect (at 0.825 0) (size 0.8 0.95) (layers "F.Cu" "F.Paste" "F.Mask"))
)
'''

SAMPLE_SOIC_MOD = '''\
(footprint "SOIC-8_3.9x4.9mm_P1.27mm"
\t(version 20241229)
\t(generator "kicad-footprint-generator")
\t(layer "F.Cu")
\t(descr "SOIC, 8 Pin (JEDEC MS-012AA)")
\t(tags "SOIC SO")
\t(pad "1" smd roundrect (at -2.5 -1.905) (size 1.5 0.6))
\t(pad "2" smd roundrect (at -2.5 -0.635) (size 1.5 0.6))
\t(pad "3" smd roundrect (at -2.5 0.635) (size 1.5 0.6))
\t(pad "4" smd roundrect (at -2.5 1.905) (size 1.5 0.6))
\t(pad "5" smd roundrect (at 2.5 1.905) (size 1.5 0.6))
\t(pad "6" smd roundrect (at 2.5 0.635) (size 1.5 0.6))
\t(pad "7" smd roundrect (at 2.5 -0.635) (size 1.5 0.6))
\t(pad "8" smd roundrect (at 2.5 -1.905) (size 1.5 0.6))
)
'''

SAMPLE_SOT23_MOD = '''\
(footprint "SOT-23"
\t(version 20241229)
\t(generator "kicad-footprint-generator")
\t(layer "F.Cu")
\t(descr "SOT-23, 3 Pin transistor package")
\t(tags "SOT-23 transistor")
\t(pad "1" smd roundrect (at -1 -0.95) (size 0.9 0.8))
\t(pad "2" smd roundrect (at -1 0.95) (size 0.9 0.8))
\t(pad "3" smd roundrect (at 1 0) (size 0.9 0.8))
)
'''

SAMPLE_TERMINAL_MOD = '''\
(footprint "TerminalBlock_Phoenix_MKDS-1,5-2_1x2_P5.00mm"
\t(version 20241229)
\t(generator "kicad-footprint-generator")
\t(layer "F.Cu")
\t(descr "Phoenix Contact MKDS 1,5/2 terminal block, 2 pin, 5mm pitch")
\t(tags "phoenix terminal block MKDS")
\t(pad "1" thru_hole circle (at 0 0) (size 2.6 2.6) (drill 1.3))
\t(pad "2" thru_hole circle (at 5 0) (size 2.6 2.6) (drill 1.3))
)
'''


# ---------------------------------------------------------------------------
# Sample .kicad_sym content
# ---------------------------------------------------------------------------

SAMPLE_DEVICE_SYM = '''\
(kicad_symbol_lib
\t(version 20241209)
\t(generator "kicad_symbol_editor")
\t(symbol "R"
\t\t(pin_names (offset 0))
\t\t(exclude_from_sim no)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(property "Reference" "R" (at 2.032 0 90))
\t\t(property "Value" "R" (at -1.524 0 90))
\t\t(property "Description" "Resistor" (at 0 0 0))
\t\t(property "ki_keywords" "R res resistor" (at 0 0 0))
\t\t(symbol "R_0_1"
\t\t\t(polyline (pts (xy 0 -2.286) (xy 0 -2.54)))
\t\t)
\t\t(symbol "R_1_1"
\t\t\t(pin passive line (at 0 2.54 270) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "1"))
\t\t\t(pin passive line (at 0 -2.54 90) (length 0) (name "~" (effects (font (size 1.27 1.27)))) (number "2"))
\t\t)
\t)
\t(symbol "C"
\t\t(pin_names (offset 0))
\t\t(property "Reference" "C" (at 2.032 0 90))
\t\t(property "Value" "C" (at -1.524 0 90))
\t\t(property "Description" "Unpolarized capacitor" (at 0 0 0))
\t\t(property "ki_keywords" "cap capacitor" (at 0 0 0))
\t\t(symbol "C_0_1"
\t\t\t(polyline (pts (xy -2.032 -0.762) (xy 2.032 -0.762)))
\t\t)
\t\t(symbol "C_1_1"
\t\t\t(pin passive line (at 0 2.54 270) (length 0) (name "~") (number "1"))
\t\t\t(pin passive line (at 0 -2.54 90) (length 0) (name "~") (number "2"))
\t\t)
\t)
)
'''

SAMPLE_AMPLIFIER_SYM = '''\
(kicad_symbol_lib
\t(version 20241209)
\t(generator "kicad_symbol_editor")
\t(symbol "LM358"
\t\t(pin_names (offset 1.016))
\t\t(property "Reference" "U" (at 0 5.08 0))
\t\t(property "Value" "LM358" (at 0 -5.08 0))
\t\t(property "Description" "Low-Power, Dual Operational Amplifier" (at 0 0 0))
\t\t(property "ki_keywords" "dual opamp op-amp amplifier" (at 0 0 0))
\t\t(symbol "LM358_1_1"
\t\t\t(pin output line (at 5.08 0 180) (length 1.27) (name "~") (number "1"))
\t\t\t(pin input line (at -5.08 -2.54 0) (length 1.27) (name "-") (number "2"))
\t\t\t(pin input line (at -5.08 2.54 0) (length 1.27) (name "+") (number "3"))
\t\t)
\t\t(symbol "LM358_2_1"
\t\t\t(pin output line (at 5.08 0 180) (length 1.27) (name "~") (number "7"))
\t\t\t(pin input line (at -5.08 -2.54 0) (length 1.27) (name "-") (number "6"))
\t\t\t(pin input line (at -5.08 2.54 0) (length 1.27) (name "+") (number "5"))
\t\t)
\t\t(symbol "LM358_3_1"
\t\t\t(pin power_in line (at -2.54 5.08 270) (length 2.54) (name "V+") (number "8"))
\t\t\t(pin power_in line (at -2.54 -5.08 90) (length 2.54) (name "V-") (number "4"))
\t\t)
\t)
)
'''


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_fp_lib(tmp_path):
    """Create temporary footprint libraries."""
    resistor = tmp_path / "fp" / "Resistor_SMD.pretty"
    resistor.mkdir(parents=True)
    (resistor / "R_0603_1608Metric.kicad_mod").write_text(SAMPLE_RESISTOR_MOD)

    package_so = tmp_path / "fp" / "Package_SO.pretty"
    package_so.mkdir()
    (package_so / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").write_text(SAMPLE_SOIC_MOD)

    sot = tmp_path / "fp" / "Package_TO_SOT_SMD.pretty"
    sot.mkdir()
    (sot / "SOT-23.kicad_mod").write_text(SAMPLE_SOT23_MOD)

    terminal = tmp_path / "fp" / "TerminalBlock_Phoenix.pretty"
    terminal.mkdir()
    (terminal / "TerminalBlock_Phoenix_MKDS-1,5-2_1x2_P5.00mm.kicad_mod").write_text(
        SAMPLE_TERMINAL_MOD
    )

    return str(tmp_path / "fp")


@pytest.fixture
def mock_sym_lib(tmp_path):
    """Create temporary symbol libraries."""
    sym_dir = tmp_path / "sym"
    sym_dir.mkdir()
    (sym_dir / "Device.kicad_sym").write_text(SAMPLE_DEVICE_SYM)
    (sym_dir / "Amplifier_Operational.kicad_sym").write_text(SAMPLE_AMPLIFIER_SYM)
    return str(sym_dir)


@pytest.fixture
def index(mock_fp_lib, mock_sym_lib, tmp_path):
    """Create a LibraryIndex with both mock libraries."""
    db_path = str(tmp_path / "test_index.db")
    idx = LibraryIndex(
        db_path=db_path,
        footprint_lib_path=mock_fp_lib,
        symbol_lib_path=mock_sym_lib,
    )
    idx.rebuild_footprints()
    idx.rebuild_symbols()
    return idx


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestParseKicadMod:
    def test_parse_resistor(self, tmp_path):
        f = tmp_path / "test.kicad_mod"
        f.write_text(SAMPLE_RESISTOR_MOD)
        result = _parse_kicad_mod(str(f))
        assert result["name"] == "R_0603_1608Metric"
        assert "Resistor SMD 0603" in result["description"]
        assert "resistor" in result["tags"]
        assert result["pad_count"] == 2

    def test_parse_soic(self, tmp_path):
        f = tmp_path / "test.kicad_mod"
        f.write_text(SAMPLE_SOIC_MOD)
        result = _parse_kicad_mod(str(f))
        assert result["name"] == "SOIC-8_3.9x4.9mm_P1.27mm"
        assert result["pad_count"] == 8

    def test_parse_missing_file(self):
        result = _parse_kicad_mod("/nonexistent/path.kicad_mod")
        assert result["name"] == ""
        assert result["pad_count"] == 0


class TestParseKicadSym:
    def test_parse_device_lib(self, tmp_path):
        f = tmp_path / "Device.kicad_sym"
        f.write_text(SAMPLE_DEVICE_SYM)
        results = _parse_kicad_sym(str(f))
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert "R" in names
        assert "C" in names

    def test_parse_resistor_symbol(self, tmp_path):
        f = tmp_path / "Device.kicad_sym"
        f.write_text(SAMPLE_DEVICE_SYM)
        results = _parse_kicad_sym(str(f))
        r = next(s for s in results if s["name"] == "R")
        assert r["library"] == "Device"
        assert r["description"] == "Resistor"
        assert "resistor" in r["keywords"]
        assert r["pin_count"] >= 2

    def test_parse_opamp_skips_subunits(self, tmp_path):
        f = tmp_path / "Amplifier_Operational.kicad_sym"
        f.write_text(SAMPLE_AMPLIFIER_SYM)
        results = _parse_kicad_sym(str(f))
        # Should find LM358 but not LM358_1_1, LM358_2_1, etc.
        assert len(results) == 1
        assert results[0]["name"] == "LM358"
        assert "opamp" in results[0]["keywords"]

    def test_parse_missing_file(self):
        results = _parse_kicad_sym("/nonexistent/path.kicad_sym")
        assert results == []


# ---------------------------------------------------------------------------
# Unified index tests
# ---------------------------------------------------------------------------


class TestLibraryIndex:
    def test_single_database(self, index):
        """Both tables live in one DB file."""
        conn = index._connect()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "footprints" in tables
        assert "symbols" in tables
        assert "metadata" in tables
        conn.close()

    def test_footprint_count(self, index):
        conn = index._connect()
        count = conn.execute("SELECT COUNT(*) FROM footprints").fetchone()[0]
        assert count == 4
        conn.close()

    def test_symbol_count(self, index):
        conn = index._connect()
        count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        assert count == 3  # R, C, LM358
        conn.close()

    def test_footprints_not_stale(self, index):
        assert index.footprints_stale() is False

    def test_symbols_not_stale(self, index):
        assert index.symbols_stale() is False

    def test_stale_after_fp_change(self, index, mock_fp_lib):
        new_mod = os.path.join(mock_fp_lib, "Resistor_SMD.pretty", "R_0402.kicad_mod")
        with open(new_mod, "w") as f:
            f.write('(footprint "R_0402"\n(descr "test")\n(tags "resistor")\n)')
        os.utime(os.path.join(mock_fp_lib, "Resistor_SMD.pretty"))
        assert index.footprints_stale() is True
        # Symbol index should still be fresh
        assert index.symbols_stale() is False

    def test_stale_after_user_lib_symbol_edit(self, index, tmp_path):
        """h-library-stale: editing a symbol in a USER library (an extra dir, not
        the system path) must mark the index stale. The staleness check used to
        scan only the system path, so user-library edits were never detected."""
        import time
        user_dir = tmp_path / "userlib"
        user_dir.mkdir()
        user_sym = user_dir / "MyParts.kicad_sym"
        user_sym.write_text(SAMPLE_DEVICE_SYM)
        index.extra_symbol_dirs = [str(user_dir)]
        index.rebuild_symbols()                      # now indexes system + user dir
        assert index.symbols_stale() is False        # fresh after rebuild
        os.utime(str(user_sym), (time.time() + 10000, time.time() + 10000))
        assert index.symbols_stale() is True         # edit detected via the extra dir

    def test_stale_after_sym_change(self, index, mock_sym_lib):
        new_sym = os.path.join(mock_sym_lib, "NewLib.kicad_sym")
        with open(new_sym, "w") as f:
            f.write('(kicad_symbol_lib\n\t(symbol "X"\n\t\t(property "Description" "test")\n\t)\n)')
        assert index.symbols_stale() is True
        # Footprint index should still be fresh
        assert index.footprints_stale() is False

    def test_rebuild_preserves_other_tables(self, index):
        """Rebuilding footprints doesn't destroy symbols, and vice versa."""
        index.rebuild_footprints()
        conn = index._connect()
        sym_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        assert sym_count == 3  # Still there
        conn.close()

        index.rebuild_symbols()
        conn = index._connect()
        fp_count = conn.execute("SELECT COUNT(*) FROM footprints").fetchone()[0]
        assert fp_count == 4  # Still there
        conn.close()


# ---------------------------------------------------------------------------
# Footprint search tests
# ---------------------------------------------------------------------------


class TestSearchFootprints:
    def test_search_by_name(self, index):
        results = index.search_footprints("SOT-23")
        assert len(results) >= 1
        assert results[0]["name"] == "SOT-23"

    def test_search_by_tags(self, index):
        results = index.search_footprints("resistor")
        assert len(results) >= 1
        assert any(r["library"] == "Resistor_SMD" for r in results)

    def test_search_full_name(self, index):
        results = index.search_footprints("SOIC")
        assert len(results) >= 1
        assert results[0]["full_name"] == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"

    def test_search_with_library_filter(self, index):
        results = index.search_footprints("resistor", library="Resistor_SMD")
        assert all(r["library"] == "Resistor_SMD" for r in results)

    def test_search_empty_query(self, index):
        assert index.search_footprints("") == []

    def test_search_no_results(self, index):
        assert index.search_footprints("xyznonexistent") == []

    def test_search_prefix(self, index):
        results = index.search_footprints("SOI")
        assert any("SOIC" in r["name"] for r in results)

    def test_search_phoenix(self, index):
        results = index.search_footprints("phoenix terminal")
        assert len(results) >= 1
        assert "TerminalBlock_Phoenix" in results[0]["library"]


# ---------------------------------------------------------------------------
# Symbol search tests
# ---------------------------------------------------------------------------


class TestSearchSymbols:
    def test_search_by_name(self, index):
        results = index.search_symbols("LM358")
        assert len(results) >= 1
        assert results[0]["name"] == "LM358"

    def test_search_by_keywords(self, index):
        results = index.search_symbols("opamp")
        assert len(results) >= 1
        assert any(r["name"] == "LM358" for r in results)

    def test_search_by_description(self, index):
        results = index.search_symbols("capacitor")
        assert len(results) >= 1
        assert any(r["name"] == "C" for r in results)

    def test_search_lib_id_format(self, index):
        results = index.search_symbols("resistor")
        assert len(results) >= 1
        r = next(x for x in results if x["name"] == "R")
        assert r["lib_id"] == "Device:R"

    def test_search_with_library_filter(self, index):
        results = index.search_symbols("amplifier", library="Amplifier_Operational")
        assert all(r["library"] == "Amplifier_Operational" for r in results)

    def test_search_empty_query(self, index):
        assert index.search_symbols("") == []

    def test_search_no_results(self, index):
        assert index.search_symbols("xyznonexistent") == []

    def test_search_prefix(self, index):
        results = index.search_symbols("LM3")
        assert any(r["name"] == "LM358" for r in results)


# ---------------------------------------------------------------------------
# _op_search truncated boundary tests
# ---------------------------------------------------------------------------


class TestSearchTruncatedBoundary:
    """Pin the `truncated` flag in _op_search at the limit boundary.

    The implementation uses `len(results) == limit`; the SQL LIMIT prevents
    the index from returning more than `limit` rows, so `len > limit` is not
    reachable in practice.  These tests verify the three boundary cases:
      - count < limit  → truncated: False
      - count == limit → truncated: True
      - (count > limit is unreachable via the SQL layer — not tested)

    `get_library_index` is imported locally inside `_op_search`, so we patch
    the canonical module path `kicad_mcp.utils.library_index.get_library_index`.
    """

    def test_below_limit_footprint_not_truncated(self, index):
        """Footprints: result < limit → truncated False.

        "resistor" matches exactly 1 footprint; limit=5 → count(1) < limit(5).
        """
        from unittest.mock import patch
        from kicad_mcp.tools.library import _op_search

        with patch("kicad_mcp.utils.library_index.get_library_index", return_value=index):
            result = _op_search("resistor", type="footprint", limit=5)
        assert "error" not in result
        assert result["count"] < 5
        assert result["truncated"] is False

    def test_at_limit_footprint_is_truncated(self, index):
        """Footprints: result == limit → truncated True.

        "pin" matches 3 footprints (SOIC-8, SOT-23, TerminalBlock); set limit=3.
        """
        from unittest.mock import patch
        from kicad_mcp.tools.library import _op_search

        with patch("kicad_mcp.utils.library_index.get_library_index", return_value=index):
            all_fps = index.search_footprints("pin", limit=100)
            n = len(all_fps)
            if n == 0:
                pytest.skip("No matching footprints for boundary test")
            result = _op_search("pin", type="footprint", limit=n)
        assert result["count"] == n
        assert result["truncated"] is True

    def test_below_limit_symbol_not_truncated(self, index):
        """Symbols: result < limit → truncated False."""
        from unittest.mock import patch
        from kicad_mcp.tools.library import _op_search

        with patch("kicad_mcp.utils.library_index.get_library_index", return_value=index):
            all_syms = index.search_symbols("R", limit=100)
            n = len(all_syms)
            assert n > 0
            result = _op_search("R", type="symbol", limit=n + 1)
        assert result.get("truncated") is False

    def test_at_limit_symbol_is_truncated(self, index):
        """Symbols: result == limit → truncated True."""
        from unittest.mock import patch
        from kicad_mcp.tools.library import _op_search

        with patch("kicad_mcp.utils.library_index.get_library_index", return_value=index):
            all_syms = index.search_symbols("R", limit=100)
            n = len(all_syms)
            if n == 0:
                pytest.skip("No matching symbols for boundary test")
            result = _op_search("R", type="symbol", limit=n)
        assert result["count"] == n
        assert result["truncated"] is True
