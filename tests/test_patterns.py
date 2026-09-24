"""Tests for circuit pattern recognition operations (analyze router,
operation="circuit_patterns"/"project_patterns"). This file previously
didn't exist -- zero test coverage of these operations.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.analyze import register_analyze_tools


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


class TestCircuitPatterns:

    def test_file_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="circuit_patterns", ctx=None,
            schematic_path="/nonexistent/test.kicad_sch",
        ))
        assert result["status"] == "error"

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_returns_identified_patterns(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {
            "component_count": 0, "net_count": 0,
            "components": {}, "nets": {},
        }
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="circuit_patterns", ctx=None, schematic_path=sch_file))
        assert result["status"] == "ok"
        assert "identified_patterns" in result
        assert result["total_patterns_found"] == 0

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_handles_extraction_error(self, mock_extract, analyze_server, sch_file):
        mock_extract.return_value = {"error": "Failed to parse schematic"}
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="circuit_patterns", ctx=None, schematic_path=sch_file))
        assert result["status"] == "error"

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_forwards_regex_fallback_incompleteness(self, mock_extract, analyze_server, sch_file):
        """Regression: unlike netlist.py::_op_extract_netlist (which already
        forwards these), this operation never forwarded parser_path/
        incomplete/incomplete_reason from netlist_data. On the regex-fallback
        parser path (no kicad-cli), every net's pin list is empty, so every
        connectivity-dependent pattern classifier here silently finds
        nothing while this op still reports status="ok" -- with no
        forwarded fields, a caller can't tell "genuinely no patterns" from
        "parser couldn't see the wiring."."""
        mock_extract.return_value = {
            "component_count": 1, "net_count": 0,
            "components": {"U1": {"reference": "U1", "value": "LM358"}}, "nets": {},
            "parser_path": "regex",
            "incomplete": True,
            "incomplete_reason": "regex fallback: hierarchical sub-schematics not resolved",
        }
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="circuit_patterns", ctx=None, schematic_path=sch_file))
        assert result["status"] == "ok"
        assert result["parser_path"] == "regex"
        assert result["incomplete"] is True
        assert "hierarchical" in result["incomplete_reason"]

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_complete_parse_has_no_incompleteness_fields(self, mock_extract, analyze_server, sch_file):
        """A clean kicad-cli parse must NOT carry parser_path/incomplete --
        those fields are only present when the source data actually says so."""
        mock_extract.return_value = {
            "component_count": 0, "net_count": 0,
            "components": {}, "nets": {},
        }
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="circuit_patterns", ctx=None, schematic_path=sch_file))
        assert "parser_path" not in result
        assert "incomplete" not in result


class TestProjectCircuitPatterns:

    def test_project_not_found(self, analyze_server):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="project_patterns", ctx=None,
            project_path="/nonexistent/test.kicad_pro",
        ))
        assert result["status"] == "error"

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    @patch("kicad_mcp.tools.patterns.get_project_files")
    def test_forwards_incompleteness_through_project_wrapper(
        self, mock_files, mock_extract, analyze_server, sch_file, tmp_path,
    ):
        """The project-level wrapper delegates to _op_identify_circuit_patterns
        and must not drop the incompleteness fields it forwards."""
        mock_files.return_value = {"schematic": sch_file}
        mock_extract.return_value = {
            "component_count": 0, "net_count": 0,
            "components": {}, "nets": {},
            "parser_path": "regex", "incomplete": True,
            "incomplete_reason": "regex fallback",
        }
        pro_path = str(tmp_path / "test.kicad_pro")
        with open(pro_path, "w") as f:
            f.write("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(operation="project_patterns", ctx=None, project_path=pro_path))
        assert result["status"] == "ok"
        assert result["incomplete"] is True
        assert result["parser_path"] == "regex"
