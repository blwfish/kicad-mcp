"""
Tests for netlist extraction and pattern recognition operations.

After tool consolidation (phase 1):
  - extract_netlist → analyze router, operation="netlist"
  - identify_circuit_patterns → analyze router, operation="circuit_patterns"
  - analyze_project_circuit_patterns → analyze router, operation="project_patterns"

`find_component_connections` still lives on `netlist` module until phase 5
(schematic router).
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.analyze import register_analyze_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def analyze_server():
    mcp = FastMCP("test-analyze")
    register_analyze_tools(mcp)
    return mcp


@pytest.fixture
def sch_file(tmp_path):
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(
        '(kicad_sch (version 20230121) (generator "test")\n'
        "  (lib_symbols)\n"
        ")\n"
    )
    return str(sch)


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- analyze.netlist (was: extract_netlist) — schematic input ---------------

class TestExtractNetlistSchematic:

    def test_file_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="netlist", ctx=None,
            path="/nonexistent/test.kicad_sch",
        ))
        assert result["success"] is False
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_returns_netlist(self, mock_extract, mock_analyze, analyze_server, sch_file):
        mock_extract.return_value = {
            "component_count": 3,
            "net_count": 5,
            "components": [{"reference": "R1", "value": "10k"}],
            "nets": {"GND": [], "VCC": []},
        }
        mock_analyze.return_value = {"summary": "3 components, 5 nets"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert result["success"] is True

    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_handles_extraction_error(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {"error": "Failed to parse schematic"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert result["success"] is False


# -- analyze.netlist — project input ----------------------------------------

class TestExtractNetlistProject:

    def test_project_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="netlist", ctx=None,
            path="/nonexistent/project.kicad_pro",
        ))
        assert result["success"] is False

    def test_no_schematic(self, analyze_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=str(pro)))
        assert result["success"] is False
        assert "schematic" in result["error"].lower()

    def test_unsupported_extension(self, analyze_server, tmp_path):
        other = tmp_path / "test.txt"
        other.write_text("not a kicad file")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=str(other)))
        assert result["success"] is False
        assert "Unsupported" in result["error"]


# -- analyze.circuit_patterns (was: identify_circuit_patterns) ---------------

class TestIdentifyCircuitPatterns:

    def test_file_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="circuit_patterns", ctx=None,
            schematic_path="/nonexistent/test.kicad_sch",
        ))
        assert result["success"] is False

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_identifies_patterns(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {
            "component_count": 5,
            "net_count": 4,
            "components": {
                "U1": {"reference": "U1", "value": "LM7805", "lib_id": "Regulator_Linear:L7805"},
                "C1": {"reference": "C1", "value": "100nF", "lib_id": "Device:C"},
                "C2": {"reference": "C2", "value": "10uF", "lib_id": "Device:C"},
                "R1": {"reference": "R1", "value": "10k", "lib_id": "Device:R"},
            },
            "nets": {"GND": [], "VCC": [], "+5V": []},
            "labels": [],
        }
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="circuit_patterns", ctx=None, schematic_path=sch_file,
        ))
        assert result["success"] is True

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_handles_extraction_error(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {"error": "Parse failure"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="circuit_patterns", ctx=None, schematic_path=sch_file,
        ))
        assert result["success"] is False


# -- analyze.project_patterns (was: analyze_project_circuit_patterns) -------

class TestAnalyzeProjectPatterns:

    def test_project_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="project_patterns", ctx=None,
            project_path="/nonexistent/project.kicad_pro",
        ))
        assert result["success"] is False

    def test_no_schematic(self, analyze_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="project_patterns", ctx=None, project_path=str(pro),
        ))
        assert result["success"] is False


# -- _unescape_sexpr unit tests ----------------------------------------------


class TestUnescapeSexpr:
    """Unit tests for the KiCad S-expression string unescaper."""

    @pytest.fixture(autouse=True)
    def import_fn(self):
        from kicad_mcp.utils.netlist_parser import _unescape_sexpr
        self._fn = _unescape_sexpr

    def test_no_escapes_unchanged(self):
        """Plain string with no backslash is returned as-is."""
        assert self._fn("hello") == "hello"

    def test_escaped_double_quote(self):
        r"""\" → "  (escaped quote)."""
        assert self._fn('a\\"b') == 'a"b'

    def test_escaped_backslash(self):
        r"""\\ → \  (escaped backslash)."""
        assert self._fn("a\\\\b") == "a\\b"

    def test_escaped_newline(self):
        r"""\n → newline character."""
        assert self._fn("a\\nb") == "a\nb"

    def test_unknown_escape_takes_literal_next_char(self):
        r"""Unknown escape \x → 'x' (take the literal next char, drop the backslash)."""
        assert self._fn("\\x") == "x"

    def test_empty_string(self):
        """Empty string passes through without error."""
        assert self._fn("") == ""

    def test_trailing_backslash(self):
        """Lone trailing backslash (malformed input): kept as-is since i+1 is OOB."""
        # Per the implementation: `if c == "\\" and i + 1 < len(s)` — trailing
        # backslash falls into the else branch and is appended literally.
        assert self._fn("abc\\") == "abc\\"


# -- _parse_kicadxml: field-survival of the production data-capture path --------

KICADXML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="R1" dnp="1">
      <value>10k</value>
      <footprint>Resistor_SMD:R_0805</footprint>
      <datasheet>http://example.com/r.pdf</datasheet>
      <description>Chip resistor</description>
      <fields>
        <field name="MPN">RC0805FR-0710KL</field>
      </fields>
      <libsource lib="Device" part="R"/>
      <property name="Sheetname" value="root"/>
    </comp>
    <comp ref="C1" dnp="false">
      <value>100nF</value>
      <libsource lib="Device" part="C" description="Unpolarized capacitor"/>
    </comp>
    <comp>
      <value>orphan-no-ref</value>
    </comp>
  </components>
  <nets>
    <net name="/SDA">
      <node ref="R1" pin="1" pinfunction="A" pintype="passive"/>
      <node ref="C1" pin="2"/>
    </net>
    <net>
      <node ref="X1" pin="1"/>
    </net>
    <net name="unconnected-(U1-Pad3)">
      <node ref="U1" pin="3"/>
    </net>
  </nets>
</export>
"""


class TestParseKicadxml:
    """Field-survival for the kicad-cli kicadxml parser — the authoritative
    data-capture path. It was extracted into a pure function (review finding
    h-test-netlist) so every field is assertable here without KiCad; the default
    no-KiCad suite never reached this logic before (it lived behind ET.parse of a
    subprocess output)."""

    @pytest.fixture(scope="class")
    def parsed(self):
        from kicad_mcp.utils.netlist_parser import _parse_kicadxml
        return _parse_kicadxml(KICADXML_SAMPLE)

    def test_parser_path_and_skip_counters(self, parsed):
        assert parsed["parser_path"] == "cli"
        assert parsed["component_count"] == 2          # R1, C1; the no-ref comp skipped
        assert parsed["malformed_components_skipped"] == 1
        assert parsed["net_count"] == 1                # SDA; unconnected + no-name excluded
        assert parsed["malformed_nets_skipped"] == 1   # the <net> with no name

    def test_all_component_fields_survive(self, parsed):
        r1 = parsed["components"]["R1"]
        assert r1["reference"] == "R1"
        assert r1["dnp"] is True                       # "1" -> True
        assert r1["value"] == "10k"
        assert r1["footprint"] == "Resistor_SMD:R_0805"
        assert r1["datasheet"] == "http://example.com/r.pdf"
        assert r1["description"] == "Chip resistor"
        assert r1["lib_id"] == "Device:R"
        # both <fields><field> and inline <property> land in properties
        assert r1["properties"]["MPN"] == "RC0805FR-0710KL"
        assert r1["properties"]["Sheetname"] == "root"

    def test_dnp_coercion_and_libsource_description_fallback(self, parsed):
        c1 = parsed["components"]["C1"]
        assert c1["dnp"] is False                      # "false" -> False
        # no <description> child -> falls back to the libsource attribute
        assert c1["description"] == "Unpolarized capacitor"

    def test_net_name_stripped_and_node_attrs_captured(self, parsed):
        assert "SDA" in parsed["nets"] and "/SDA" not in parsed["nets"]
        nodes = {n["component"]: n for n in parsed["nets"]["SDA"]}
        assert nodes["R1"]["pin"] == "1"
        # extra node attributes survive — not dropped to {component, pin}
        assert nodes["R1"]["pinfunction"] == "A"
        assert nodes["R1"]["pintype"] == "passive"
        assert "pinfunction" not in nodes["C1"]        # absent stays absent

    def test_unconnected_nets_excluded(self, parsed):
        assert not any(k.startswith("unconnected-") for k in parsed["nets"])

    def test_malformed_xml_raises_parse_error(self):
        import xml.etree.ElementTree as ET
        from kicad_mcp.utils.netlist_parser import _parse_kicadxml
        with pytest.raises(ET.ParseError):
            _parse_kicadxml("<export><components></export>")  # unclosed tag
