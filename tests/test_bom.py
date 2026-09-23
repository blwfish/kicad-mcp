"""
Tests for BOM operations.

After tool consolidation (phase 1): analyze_bom lives on the `analyze`
router (operation="bom"), export_bom_csv on the `export` router
(operation="bom_csv").
"""

import asyncio
import csv
import json

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.analyze import register_analyze_tools
from kicad_mcp.tools.export import register_export_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def analyze_server():
    mcp = FastMCP("test-analyze")
    register_analyze_tools(mcp)
    return mcp


@pytest.fixture
def export_server():
    mcp = FastMCP("test-export")
    register_export_tools(mcp)
    return mcp


@pytest.fixture
def project_with_bom(tmp_path):
    """Create a project directory with a BOM CSV file."""
    name = "testboard"
    pro = tmp_path / f"{name}.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": f"{name}.kicad_pro"}}))
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text('(kicad_pcb)\n')
    sch = tmp_path / f"{name}.kicad_sch"
    sch.write_text('(kicad_sch)\n')

    bom = tmp_path / f"{name}-bom.csv"
    with open(bom, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Reference", "Value", "Footprint", "Quantity"])
        writer.writerow(["R1", "10k", "Resistor_SMD:R_0805", "1"])
        writer.writerow(["R2", "10k", "Resistor_SMD:R_0805", "1"])
        writer.writerow(["C1", "100nF", "Capacitor_SMD:C_0805", "1"])
        writer.writerow(["U1", "ESP32-WROOM-32E", "RF_Module:ESP32", "1"])

    return {"project_path": str(pro), "bom_path": str(bom)}


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- analyze router → bom operation ------------------------------------------

class TestAnalyzeBom:

    def test_project_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="bom", ctx=None,
            project_path="/nonexistent/project.kicad_pro",
        ))
        assert result["status"] == "error"

    def test_no_bom_files(self, analyze_server, tmp_path):
        pro = tmp_path / "empty.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="bom", ctx=None, project_path=str(pro),
        ))
        assert result["status"] == "error"
        assert "No BOM" in result["error"]

    def test_analyzes_bom(self, analyze_server, project_with_bom):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="bom", ctx=None,
            project_path=project_with_bom["project_path"],
        ))
        assert result["status"] == "ok"

    def test_missing_project_path(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="bom", ctx=None))
        assert "error" in result
        assert "project_path" in result["error"]


# -- export router → bom_csv operation ---------------------------------------

class TestExportBomCsv:

    def test_project_not_found(self, export_server):
        fn = _get_tool_fn(export_server, "export")
        result = asyncio.run(fn(
            operation="bom_csv", ctx=None,
            project_path="/nonexistent/project.kicad_pro",
        ))
        assert result["status"] == "error"

    def test_no_schematic(self, export_server, tmp_path):
        """bom_csv needs a schematic to generate from."""
        pro = tmp_path / "noschem.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(export_server, "export")
        result = asyncio.run(fn(
            operation="bom_csv", ctx=None, project_path=str(pro),
        ))
        assert result["status"] == "error"

    def test_missing_project_path(self, export_server):
        fn = _get_tool_fn(export_server, "export")
        result = asyncio.run(fn(operation="bom_csv", ctx=None))
        assert "error" in result
        assert "project_path" in result["error"]


def test_json_bom_unrecognized_container_is_surfaced(tmp_path):
    """A populated JSON BOM with no components/parts key must not silently look
    empty — surface the unrecognized top-level keys."""
    from kicad_mcp.tools.bom import _parse_bom_file
    p = tmp_path / "bom.json"
    p.write_text(json.dumps({"bom": [{"ref": "R1"}], "meta": 1}))
    comps, info = _parse_bom_file(str(p))
    assert comps == []
    assert "bom" in info.get("unrecognized_json_keys", [])


def test_json_bom_refdes_mapping_is_accepted(tmp_path):
    """A {refdes: row} 'components' mapping is read as a component list."""
    from kicad_mcp.tools.bom import _parse_bom_file
    p = tmp_path / "bom.json"
    p.write_text(json.dumps({"components": {"R1": {"value": "10k"}, "R2": {"value": "1k"}}}))
    comps, _ = _parse_bom_file(str(p))
    assert len(comps) == 2 and {c["value"] for c in comps} == {"10k", "1k"}


# -- _analyze_bom_data: the real pandas path, and supplier-info extraction ---
#
# Regression for two review findings on the SAME underlying gap: pandas is a
# soft runtime dependency (try/except ImportError, degrades to basic counts
# for end users who skip it) -- but that also meant this whole DataFrame-based
# branch never ran in this repo's OWN dev/test environment (pandas wasn't
# installed here either), so it had zero real coverage. Now a dev dependency
# (see pyproject.toml) specifically so these tests exercise the real path
# instead of a mock or a hand-rolled fake pandas.

class TestAnalyzeBomDataPandasPath:

    def _components(self):
        return [
            {"reference": "R1", "value": "10k", "mpn": "RC0805FR-0710KL",
             "manufacturer": "Yageo", "lcsc": "C17414"},
            {"reference": "R2", "value": "10k", "mpn": "RC0805FR-0710KL",
             "manufacturer": "Yageo", "lcsc": "C17414"},
            {"reference": "C1", "value": "100nF"},  # no supplier fields at all
        ]

    def test_pandas_path_actually_runs_not_the_fallback(self):
        """Pin that this test suite exercises the real DataFrame branch, not
        the "pandas not installed" degrade-to-counts-only fallback -- the
        exact gap review finding #09 flagged (this file used to pass either
        way, silently, because pandas was never installed here at all)."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(self._components(), {})
        assert "pandas" not in results.get("stage_errors", {})
        assert results["detected_fields"]["mpn"] == "mpn"

    def test_supplier_info_extracted_for_rows_with_detected_columns(self):
        """Regression for #08: mpn/manufacturer/lcsc were detected into
        detected_fields even before this fix, but the actual values were
        never pulled out of the DataFrame into the analysis output at all."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(self._components(), {})
        by_ref = {e["reference"]: e for e in results["supplier_info"]}
        assert by_ref["R1"] == {
            "reference": "R1", "mpn": "RC0805FR-0710KL",
            "manufacturer": "Yageo", "lcsc": "C17414",
        }
        assert by_ref["R2"]["mpn"] == "RC0805FR-0710KL"

    def test_row_with_no_supplier_values_excluded_not_a_bare_reference_stub(self):
        """C1 has a reference but none of the detected supplier columns
        populated -- it must not show up as a content-free {"reference": ...}
        stub crowding out the rows that actually have supplier data."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(self._components(), {})
        refs = {e["reference"] for e in results["supplier_info"]}
        assert "C1" not in refs

    def test_no_supplier_columns_detected_key_absent_not_empty_list(self):
        """When no BOM column matches mpn/manufacturer/lcsc/datasheet/
        description at all, supplier_info must be absent entirely (matching
        the pattern of every other optional results key here), not an
        always-present empty list."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [{"reference": "R1", "value": "10k"},
                      {"reference": "C1", "value": "100nF"}]
        results = _analyze_bom_data(components, {})
        assert "supplier_info" not in results

    def test_pandas_none_fallback_still_works(self, monkeypatch):
        """The graceful-degradation contract for end users who don't install
        pandas (a soft dependency) must keep working now that pandas is a dev
        dependency here -- otherwise every test in this class would silently
        stop covering the fallback path the same way they silently stopped
        covering the real path before pandas was added to the dev group."""
        import kicad_mcp.tools.bom as bom_mod
        monkeypatch.setattr(bom_mod, "pd", None)
        results = bom_mod._analyze_bom_data(self._components(), {})
        assert results["stage_errors"]["pandas"] == "pandas not installed; counts only"
        assert results["total_component_count"] == 3
        assert "supplier_info" not in results

    def test_blank_supplier_value_not_included_as_empty_string(self):
        """A detected column present in the BOM but blank for a given row
        (common when only some rows have LCSC/JLCPCB data filled in) must be
        omitted from that row's entry, not carried through as ""."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [
            {"reference": "R1", "value": "10k", "mpn": "RC0805FR-0710KL", "lcsc": ""},
            {"reference": "R2", "value": "10k", "mpn": "RC0805FR-0710KL", "lcsc": "C17414"},
        ]
        results = _analyze_bom_data(components, {})
        by_ref = {e["reference"]: e for e in results["supplier_info"]}
        assert "lcsc" not in by_ref["R1"]
        assert by_ref["R2"]["lcsc"] == "C17414"
