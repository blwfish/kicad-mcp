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
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_returns_netlist(self, mock_extract, mock_analyze, analyze_server, sch_file):
        # components/nets are dicts keyed by reference/net name in both real
        # parser paths (SchematicParser.component_info, the cli-path
        # component_info/nets in netlist_parser.py) -- match that shape,
        # not a list, so this mock stays representative of production data.
        mock_extract.return_value = {
            "component_count": 3,
            "net_count": 5,
            "components": {"R1": {"reference": "R1", "value": "10k"}},
            "nets": {"GND": [], "VCC": []},
        }
        mock_analyze.return_value = {"summary": "3 components, 5 nets"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert result["status"] == "ok"
        assert result["components"] == {"R1": {"reference": "R1", "value": "10k"}}
        assert result["components_truncated"] is False
        assert result["nets_truncated"] is False

    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_handles_extraction_error(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {"error": "Failed to parse schematic"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert result["status"] == "error"

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_forwards_regex_fallback_incompleteness(
        self, mock_extract, mock_analyze, analyze_server, sch_file
    ):
        # When kicad-cli is unavailable the parser falls back to regex, which
        # can't trace connectivity → empty pin lists. The result MUST carry
        # parser_path/incomplete/incomplete_reason so the caller doesn't trust a
        # clean-looking success over silently-missing connectivity.
        mock_extract.return_value = {
            "component_count": 1, "net_count": 0,
            "components": {}, "nets": {},
            "parser_path": "regex",
            "incomplete": True,
            "incomplete_reason": "regex fallback: hierarchical sub-schematics not resolved",
        }
        mock_analyze.return_value = {"summary": "incomplete"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert result["status"] == "ok"
        assert result["parser_path"] == "regex"
        assert result["incomplete"] is True
        assert "hierarchical" in result["incomplete_reason"]


# -- analyze.netlist — pagination (components/nets capped at `limit`) -------

class TestExtractNetlistPagination:
    """components/nets are capped at `limit` entries -- component_count/
    net_count/analysis must always reflect the TRUE, untruncated netlist.
    Threshold boundary per CLAUDE.md's Testing rule: total == limit (not
    truncated) and total == limit + 1 (truncated, smallest case)."""

    def _mock_netlist(self, n):
        return {
            "component_count": n,
            "net_count": n,
            "components": {f"R{i}": {"reference": f"R{i}", "value": "10k"} for i in range(n)},
            "nets": {f"NET{i}": [] for i in range(n)},
        }

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_default_limit_is_one_hundred(self, mock_extract, mock_analyze, analyze_server, sch_file):
        mock_extract.return_value = self._mock_netlist(150)
        mock_analyze.return_value = {}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert len(result["components"]) == 100
        assert len(result["nets"]) == 100
        assert result["components_truncated"] is True
        assert result["nets_truncated"] is True
        # Full counts and analysis are NEVER truncated, only the raw echo-back.
        assert result["component_count"] == 150
        assert result["net_count"] == 150

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_total_exactly_equal_to_limit_is_not_truncated(self, mock_extract, mock_analyze, analyze_server, sch_file):
        mock_extract.return_value = self._mock_netlist(100)
        mock_analyze.return_value = {}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert len(result["components"]) == 100
        assert result["components_truncated"] is False
        assert result["nets_truncated"] is False

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_total_one_more_than_limit_is_truncated(self, mock_extract, mock_analyze, analyze_server, sch_file):
        mock_extract.return_value = self._mock_netlist(101)
        mock_analyze.return_value = {}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file))
        assert len(result["components"]) == 100
        assert result["components_truncated"] is True

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_custom_limit_is_honored(self, mock_extract, mock_analyze, analyze_server, sch_file):
        mock_extract.return_value = self._mock_netlist(10)
        mock_analyze.return_value = {}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file, limit=3))
        assert len(result["components"]) == 3
        assert result["components_truncated"] is True

    def test_limit_zero_is_rejected(self, analyze_server, sch_file):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file, limit=0))
        assert "error" in result

    def test_negative_limit_is_rejected(self, analyze_server, sch_file):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=sch_file, limit=-5))
        assert "error" in result

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_analysis_covers_the_full_netlist_not_the_truncated_view(
        self, mock_extract, mock_analyze, analyze_server, sch_file,
    ):
        """analyze_netlist() must be called with the FULL netlist_data,
        not a pre-truncated copy -- truncation is only applied to the
        response's raw components/nets echo-back."""
        full = self._mock_netlist(150)
        mock_extract.return_value = full
        mock_analyze.return_value = {"summary": "ok"}
        fn = _get_tool_fn(analyze_server, "analyze")
        asyncio.run(fn(operation="netlist", ctx=None, path=sch_file, limit=3))
        called_with = mock_analyze.call_args[0][0]
        assert len(called_with["components"]) == 150


# -- analyze.netlist — project input ----------------------------------------

class TestExtractNetlistProject:

    def test_project_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="netlist", ctx=None,
            path="/nonexistent/project.kicad_pro",
        ))
        assert result["status"] == "error"

    def test_no_schematic(self, analyze_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=str(pro)))
        assert result["status"] == "error"
        assert "schematic" in result["error"].lower()

    def test_unsupported_extension(self, analyze_server, tmp_path):
        other = tmp_path / "test.txt"
        other.write_text("not a kicad file")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=str(other)))
        assert result["status"] == "error"
        assert "Unsupported" in result["error"]


# -- analyze.circuit_patterns (was: identify_circuit_patterns) ---------------

class TestIdentifyCircuitPatterns:

    def test_file_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="circuit_patterns", ctx=None,
            schematic_path="/nonexistent/test.kicad_sch",
        ))
        assert result["status"] == "error"

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
        assert result["status"] == "ok"

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_handles_extraction_error(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {"error": "Parse failure"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="circuit_patterns", ctx=None, schematic_path=sch_file,
        ))
        assert result["status"] == "error"


# -- analyze.project_patterns (was: analyze_project_circuit_patterns) -------

class TestAnalyzeProjectPatterns:

    def test_project_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="project_patterns", ctx=None,
            project_path="/nonexistent/project.kicad_pro",
        ))
        assert result["status"] == "error"

    def test_no_schematic(self, analyze_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="project_patterns", ctx=None, project_path=str(pro),
        ))
        assert result["status"] == "error"


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
        assert parsed["unconnected_nets_skipped"] == 1  # the "unconnected-(U1-Pad3)" net
        assert parsed["orphan_net_nodes_skipped"] == 0  # every node in a KEPT net has a real component

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

    def test_pins_aggregated_per_component_from_nets(self, parsed):
        """Regression: tools/netlist.py's pin_functions classification reads
        component_info[ref]["pins"], but this parser (the default path whenever
        kicad-cli is available) used to never set that key at all -- only the
        regex-fallback parser did -- so pin_functions silently came back {} on
        the common path. "pinfunction" (KiCad's schematic-assigned pin name)
        stands in for the regex parser's <pin><name>."""
        r1 = parsed["components"]["R1"]
        assert r1["pins"] == [{"num": "1", "name": "A"}]
        # node present but no pinfunction attribute -> name is "", not omitted
        c1 = parsed["components"]["C1"]
        assert c1["pins"] == [{"num": "2", "name": ""}]

    def test_orphan_net_node_is_counted_not_silent(self):
        """Regression: a net node referencing a component missing from
        component_info (e.g. dropped by a malformed-component skip
        elsewhere in the same parse) was silently skipped in the per-
        component pin-aggregation loop with no counter at all. finding #97
        of the 2026-09-23 full review."""
        from kicad_mcp.utils.netlist_parser import _parse_kicadxml
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components>
    <comp ref="R1"><value>10k</value></comp>
  </components>
  <nets>
    <net name="GHOST">
      <node ref="U99" pin="1"/>
    </net>
  </nets>
</export>
"""
        result = _parse_kicadxml(xml)
        assert result["orphan_net_nodes_skipped"] == 1
        assert "R1" in result["components"]

    def test_lstrip_only_removes_one_leading_slash(self):
        """Regression: net_name.lstrip("/") strips EVERY leading slash, not
        just the local-label hierarchy marker -- "///weird" would silently
        become "weird" instead of the correct "//weird". finding #99 of the
        2026-09-23 full review."""
        from kicad_mcp.utils.netlist_parser import _parse_kicadxml
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<export version="E">
  <components><comp ref="R1"><value>10k</value></comp></components>
  <nets>
    <net name="///weird">
      <node ref="R1" pin="1"/>
    </net>
  </nets>
</export>
"""
        result = _parse_kicadxml(xml)
        assert "//weird" in result["nets"]
        assert "weird" not in result["nets"]

    def test_malformed_xml_raises_parse_error(self):
        import xml.etree.ElementTree as ET
        from kicad_mcp.utils.netlist_parser import _parse_kicadxml
        with pytest.raises(ET.ParseError):
            _parse_kicadxml("<export><components></export>")  # unclosed tag


# -- find_component_connections: pin_functions on the default (cli) path -----

class TestPinFunctionsClassificationOnCliPath:
    """End-to-end regression for the same finding as
    TestParseKicadxml.test_pins_aggregated_per_component_from_nets: confirm
    pin_functions actually comes back populated (not {}) when the netlist data
    has the shape _parse_kicadxml now produces, exercising the full
    _op_find_component_connections classification logic in tools/netlist.py."""

    @pytest.fixture
    def project(self, tmp_project_dir):
        return tmp_project_dir

    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_pin_functions_populated_from_cli_shaped_netlist(self, mock_parse, project):
        from kicad_mcp.tools.netlist import _op_find_component_connections

        mock_parse.return_value = {
            "parser_path": "cli",
            "components": {
                "U1": {
                    "reference": "U1",
                    "pins": [
                        {"num": "1", "name": "VCC"},
                        {"num": "2", "name": "GND"},
                        {"num": "3", "name": "MISO"},
                    ],
                },
            },
            "nets": {
                "VCC": [{"component": "U1", "pin": "1"}, {"component": "R1", "pin": "1"}],
                "GND": [{"component": "U1", "pin": "2"}],
                "SPI_MISO": [{"component": "U1", "pin": "3"}],
            },
        }

        result = asyncio.run(
            _op_find_component_connections(project["project_path"], "U1", None)
        )
        assert result["status"] == "ok"
        assert result["pin_functions"] == {
            "1": {"name": "VCC", "type": "power"},
            "2": {"name": "GND", "type": "power"},
            "3": {"name": "MISO", "type": "unknown"},
        }


# -- is_power_net: signed VOLTAGE rails vs signed SIGNAL nets -----------------

class TestIsPowerNet:
    def test_signed_voltage_is_power_signal_is_not(self):
        from kicad_mcp.utils.netlist_parser import is_power_net
        for power in ("+3V3", "+5V", "-12V", "+3.3V", "VCC", "GND", "VDD_A", "VBUS"):
            assert is_power_net(power), power
        # sign-then-LETTER is an active-low / signed signal net, NOT a rail
        for signal in ("-RESET", "+CS", "-CS", "-SIGNAL", "SDA", "GPIO0"):
            assert not is_power_net(signal), signal

    def test_helper_matches_python(self):
        """The embedded POWER_NET_HELPER must agree with the Python is_power_net —
        the dup was guarded only by a comment otherwise."""
        from kicad_mcp.utils.netlist_parser import POWER_NET_HELPER, is_power_net
        ns: dict = {}
        exec(POWER_NET_HELPER, ns)
        for n in ("+3V3", "-12V", "VCC", "GND", "-RESET", "+CS", "SDA", "VBUS", "+"):
            assert ns["is_power_net"](n) == is_power_net(n), n


# -- analyze.connections: component_types tally -------------------------------

class TestAnalyzeConnectionsComponentTypes:
    """Regression: _op_analyze_schematic_connections' component_types tally
    used to re-encode its own copy of the reference-prefix-extraction regex,
    identical to (and independently maintained from) netlist_parser.py's own
    copy and component_utils.py's canonical get_component_type_from_reference.
    finding #6 of the 2026-09-23 full review. No test exercised the
    "connections" operation at all before this fix."""

    @patch("kicad_mcp.tools.netlist._parse_netlist")
    def test_tallies_by_reference_prefix(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {
            "component_count": 3, "net_count": 1,
            "components": {"R1": {}, "R2": {}, "U1": {}},
            "nets": {"GND": [{}, {}]},
        }
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="connections", ctx=None, schematic_path=sch_file))
        assert result["status"] == "ok"
        assert result["analysis"]["component_types"] == {"R": 2, "U": 1}
