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
