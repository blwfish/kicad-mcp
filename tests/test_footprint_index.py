"""Tests for the footprint index and search_footprints tool."""

import os
import sqlite3
import tempfile

import pytest

from kicad_mcp.utils.footprint_index import FootprintIndex, _parse_kicad_mod


# ---------------------------------------------------------------------------
# Sample .kicad_mod content for testing
# ---------------------------------------------------------------------------

SAMPLE_RESISTOR_MOD = '''\
(footprint "R_0603_1608Metric"
\t(version 20241229)
\t(generator "kicad-footprint-generator")
\t(layer "F.Cu")
\t(descr "Resistor SMD 0603 (1608 Metric), square end terminal")
\t(tags "resistor 0603")
\t(property "Reference" "REF**"
\t\t(at 0 -1.43 0)
\t\t(layer "F.SilkS")
\t)
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


@pytest.fixture
def mock_lib_dir(tmp_path):
    """Create a temporary footprint library directory."""
    # Create library directories
    resistor_lib = tmp_path / "Resistor_SMD.pretty"
    resistor_lib.mkdir()
    (resistor_lib / "R_0603_1608Metric.kicad_mod").write_text(SAMPLE_RESISTOR_MOD)

    package_lib = tmp_path / "Package_SO.pretty"
    package_lib.mkdir()
    (package_lib / "SOIC-8_3.9x4.9mm_P1.27mm.kicad_mod").write_text(SAMPLE_SOIC_MOD)

    sot_lib = tmp_path / "Package_TO_SOT_SMD.pretty"
    sot_lib.mkdir()
    (sot_lib / "SOT-23.kicad_mod").write_text(SAMPLE_SOT23_MOD)

    terminal_lib = tmp_path / "TerminalBlock_Phoenix.pretty"
    terminal_lib.mkdir()
    (terminal_lib / "TerminalBlock_Phoenix_MKDS-1,5-2_1x2_P5.00mm.kicad_mod").write_text(
        SAMPLE_TERMINAL_MOD
    )

    return tmp_path


@pytest.fixture
def index(mock_lib_dir, tmp_path):
    """Create a FootprintIndex with mock library data."""
    db_path = str(tmp_path / "test_index.db")
    idx = FootprintIndex(db_path=db_path, lib_path=str(mock_lib_dir))
    idx.rebuild_index()
    return idx


# ---------------------------------------------------------------------------
# _parse_kicad_mod tests
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
        assert "SOIC" in result["description"]
        assert result["pad_count"] == 8

    def test_parse_sot23(self, tmp_path):
        f = tmp_path / "test.kicad_mod"
        f.write_text(SAMPLE_SOT23_MOD)
        result = _parse_kicad_mod(str(f))
        assert result["name"] == "SOT-23"
        assert result["pad_count"] == 3

    def test_parse_missing_file(self):
        result = _parse_kicad_mod("/nonexistent/path.kicad_mod")
        assert result["name"] == ""
        assert result["pad_count"] == 0

    def test_parse_terminal_block(self, tmp_path):
        f = tmp_path / "test.kicad_mod"
        f.write_text(SAMPLE_TERMINAL_MOD)
        result = _parse_kicad_mod(str(f))
        assert result["name"] == "TerminalBlock_Phoenix_MKDS-1,5-2_1x2_P5.00mm"
        assert "phoenix" in result["tags"].lower()
        assert result["pad_count"] == 2


# ---------------------------------------------------------------------------
# FootprintIndex tests
# ---------------------------------------------------------------------------


class TestFootprintIndex:
    def test_rebuild_creates_db(self, mock_lib_dir, tmp_path):
        db_path = str(tmp_path / "new_index.db")
        assert not os.path.exists(db_path)

        idx = FootprintIndex(db_path=db_path, lib_path=str(mock_lib_dir))
        count = idx.rebuild_index()

        assert os.path.exists(db_path)
        assert count == 4  # 4 footprints across 4 libraries

    def test_is_stale_no_db(self, mock_lib_dir, tmp_path):
        idx = FootprintIndex(
            db_path=str(tmp_path / "missing.db"), lib_path=str(mock_lib_dir)
        )
        assert idx.is_stale() is True

    def test_is_stale_after_rebuild(self, index):
        assert index.is_stale() is False

    def test_is_stale_after_lib_change(self, index, mock_lib_dir):
        # Touch a library directory to make it newer
        new_mod = mock_lib_dir / "Resistor_SMD.pretty" / "R_0402.kicad_mod"
        new_mod.write_text('(footprint "R_0402"\n(descr "test")\n(tags "resistor")\n)')
        # Update parent dir mtime
        os.utime(str(mock_lib_dir / "Resistor_SMD.pretty"))
        assert index.is_stale() is True

    def test_rebuild_bad_path(self, tmp_path):
        idx = FootprintIndex(
            db_path=str(tmp_path / "bad.db"), lib_path="/nonexistent/path"
        )
        with pytest.raises(RuntimeError, match="not found"):
            idx.rebuild_index()

    def test_metadata_stored(self, index):
        conn = index._connect()
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'footprint_count'"
        ).fetchone()
        assert row is not None
        assert int(row["value"]) == 4
        conn.close()


# ---------------------------------------------------------------------------
# Search tests
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_by_name(self, index):
        results = index.search("SOT-23")
        assert len(results) >= 1
        assert results[0]["name"] == "SOT-23"
        assert results[0]["library"] == "Package_TO_SOT_SMD"

    def test_search_by_tags(self, index):
        results = index.search("resistor")
        assert len(results) >= 1
        assert any(r["library"] == "Resistor_SMD" for r in results)

    def test_search_by_description(self, index):
        results = index.search("JEDEC")
        assert len(results) >= 1
        assert any("SOIC" in r["name"] for r in results)

    def test_search_full_name_returned(self, index):
        results = index.search("SOIC")
        assert len(results) >= 1
        assert results[0]["full_name"] == "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"

    def test_search_pad_count(self, index):
        results = index.search("SOIC-8")
        assert len(results) >= 1
        assert results[0]["pad_count"] == 8

    def test_search_with_library_filter(self, index):
        results = index.search("resistor", library="Resistor_SMD")
        assert len(results) >= 1
        assert all(r["library"] == "Resistor_SMD" for r in results)

    def test_search_with_wrong_library(self, index):
        results = index.search("resistor", library="Package_SO")
        assert len(results) == 0

    def test_search_limit(self, index):
        results = index.search("pad", limit=2)
        assert len(results) <= 2

    def test_search_empty_query(self, index):
        results = index.search("")
        assert results == []

    def test_search_no_results(self, index):
        results = index.search("xyznonexistent")
        assert results == []

    def test_search_phoenix(self, index):
        results = index.search("phoenix terminal")
        assert len(results) >= 1
        assert "TerminalBlock_Phoenix" in results[0]["library"]

    def test_search_partial_prefix(self, index):
        """Last term should match as prefix."""
        results = index.search("SOI")
        assert len(results) >= 1
        assert any("SOIC" in r["name"] for r in results)
