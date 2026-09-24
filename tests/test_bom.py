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


def test_csv_semicolon_delimiter_with_comma_in_text_field_not_misdetected(tmp_path):
    """Regression: the delimiter was picked by character presence in the
    sample, not the actual separator -- a semicolon-delimited BOM whose
    Description field contains a comma used to always misdetect "," as the
    delimiter (comma is present SOMEWHERE in the sample), misaligning every
    column. csv.Sniffer actually parses quoting, so it isn't fooled by that."""
    from kicad_mcp.tools.bom import _parse_bom_file
    p = tmp_path / "bom.csv"
    p.write_text(
        'Reference;Value;Description\n'
        'R1;10k;"Resistor, 5% tolerance"\n'
        'R2;1k;"Resistor, 1% tolerance"\n'
    )
    comps, info = _parse_bom_file(str(p))
    assert info["delimiter"] == ";"
    assert len(comps) == 2
    assert comps[0]["Reference"] == "R1"
    assert comps[0]["Value"] == "10k"


def test_csv_sniffer_failure_falls_back_to_substring_heuristic(tmp_path):
    """A single-row (no structural repetition for Sniffer to key off of) CSV
    must still parse via the substring fallback, not raise."""
    from kicad_mcp.tools.bom import _parse_bom_file
    p = tmp_path / "bom.csv"
    p.write_text("R1\n")
    comps, info = _parse_bom_file(str(p))
    assert info["delimiter"] == ","  # no delimiter char present -> default


def test_xml_unrecognized_tags_surfaced_not_silently_empty(tmp_path):
    """Regression: an XML BOM using a different vendor's tag name (<Part>
    instead of <component>/<Component>) used to silently yield zero
    components, indistinguishable from a genuinely empty file."""
    from kicad_mcp.tools.bom import _parse_bom_file
    p = tmp_path / "bom.xml"
    p.write_text("<BOM><Part><Reference>R1</Reference></Part></BOM>")
    comps, info = _parse_bom_file(str(p))
    assert comps == []
    assert "Part" in info["unrecognized_xml_tags"]


def test_xml_recognized_tags_no_unrecognized_key(tmp_path):
    from kicad_mcp.tools.bom import _parse_bom_file
    p = tmp_path / "bom.xml"
    p.write_text('<BOM><component ref="R1"><value>10k</value></component></BOM>')
    comps, info = _parse_bom_file(str(p))
    assert len(comps) == 1
    assert "unrecognized_xml_tags" not in info


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

    def test_new_field_probes_detected_and_extracted(self):
        """Regression: dnp/notes/supplier/vendor_code/installed/revision/
        tolerance/alternate_mpn weren't even in the probe list -- not
        detected at all, unlike mpn/manufacturer (detected but not
        extracted, a different bug). Now both detected AND extracted."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [
            {"reference": "R1", "value": "10k", "dnp": "Y", "notes": "hand-select",
             "supplier": "Digi-Key", "vendor_code": "DK-123", "installed": "yes",
             "revision": "B", "tolerance": "1%", "alternate_mpn": "RC0805-ALT"},
        ]
        results = _analyze_bom_data(components, {})
        for field in ("dnp", "notes", "supplier", "vendor_code", "installed",
                      "revision", "tolerance", "alternate_mpn"):
            assert results["detected_fields"][field] == field
        entry = results["supplier_info"][0]
        assert entry["dnp"] == "Y"
        assert entry["supplier"] == "Digi-Key"
        assert entry["alternate_mpn"] == "RC0805-ALT"

    def test_unparseable_quantity_counted_not_silent(self):
        """Regression: a non-numeric quantity silently became 1 via
        fillna(1) with no counter -- total_component_count (a sum) could be
        quietly wrong with nothing to show a value was defaulted."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [
            {"reference": "R1", "value": "10k", "quantity": "2"},
            {"reference": "R2", "value": "10k", "quantity": "N/A"},
        ]
        results = _analyze_bom_data(components, {})
        assert "quantity" in results["stage_errors"]
        assert "1 row" in results["stage_errors"]["quantity"]
        assert results["total_component_count"] == 3  # 2 + defaulted 1

    def test_wellformed_quantity_no_stage_error(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [{"reference": "R1", "value": "10k", "quantity": "2"}]
        results = _analyze_bom_data(components, {})
        assert "quantity" not in results.get("stage_errors", {})

    def test_unparseable_cost_counted_and_excluded(self):
        """Regression: a malformed cost value was silently dropped via
        dropna() before summing total_cost, with no stage_errors signal --
        the only symptom was a total_cost that was quietly too low."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [
            {"reference": "R1", "value": "10k", "cost": "0.10"},
            {"reference": "R2", "value": "10k", "cost": "quote-required"},
        ]
        results = _analyze_bom_data(components, {})
        assert "cost" in results["stage_errors"]
        assert "1 row" in results["stage_errors"]["cost"]
        assert results["total_cost"] == 0.10  # R2's cost excluded, not zero-filled

    def test_wellformed_cost_no_stage_error(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [{"reference": "R1", "value": "10k", "cost": "0.10"}]
        results = _analyze_bom_data(components, {})
        assert "cost" not in results.get("stage_errors", {})

    def test_missing_category_tallied_as_unknown_not_dropped(self):
        """Regression: value_counts() drops NaN by default -- a component
        with no category value used to vanish from the summary instead of
        being tallied, so sum(categories.values()) could undercount the
        true component total with nothing to explain the gap."""
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [
            {"reference": "R1", "value": "10k", "category": "Resistor"},
            {"reference": "R2", "value": "10k", "category": None},
        ]
        results = _analyze_bom_data(components, {})
        assert results["categories"]["(unknown)"] == 1
        assert sum(results["categories"].values()) == 2

    def test_missing_value_tallied_as_unknown_in_most_common(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        components = [
            {"reference": "R1", "value": "10k"},
            {"reference": "R2", "value": None},
        ]
        results = _analyze_bom_data(components, {})
        assert results["most_common_values"]["(unknown)"] == 1
        assert results["distinct_value_count"] == 2


class TestColumnMapValidation:
    """Regression: column_map's KEYS must be one of the canonical field
    names (_BOM_FIELD_PROBES) -- a typo'd key (e.g. "refrence" instead of
    "reference") never matched anything in the detection loop and was
    silently never consulted at all, with zero signal the override had no
    effect. Distinct from the existing column_map.<field> stage_error, which
    only fires when the field name IS recognized but the requested VALUE
    (the column header) doesn't exist in the BOM."""

    def _components(self):
        return [{"Part Number": "R1", "value": "10k"}]

    def test_unrecognized_field_name_key_flagged(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(
            self._components(), {}, column_map={"refrence": "Part Number"},
        )
        assert "column_map" in results["stage_errors"]
        assert "refrence" in results["stage_errors"]["column_map"]

    def test_unrecognized_field_name_key_does_not_set_the_field(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(
            self._components(), {}, column_map={"refrence": "Part Number"},
        )
        assert "reference" not in results["detected_fields"]

    def test_recognized_field_name_key_still_works(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(
            self._components(), {}, column_map={"reference": "Part Number"},
        )
        assert "column_map" not in results.get("stage_errors", {})
        assert results["detected_fields"]["reference"] == "part number"

    def test_mix_of_recognized_and_unrecognized_keys(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(
            self._components(),
            {},
            column_map={"reference": "Part Number", "vlaue": "value"},
        )
        assert results["detected_fields"]["reference"] == "part number"
        assert "column_map" in results["stage_errors"]
        assert "vlaue" in results["stage_errors"]["column_map"]

    def test_no_column_map_no_stage_error(self):
        from kicad_mcp.tools.bom import _analyze_bom_data
        results = _analyze_bom_data(self._components(), {})
        assert "column_map" not in results.get("stage_errors", {})
