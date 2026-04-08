"""
Tests for netlist extraction tools and pattern recognition tools.

Tests netlist.py and patterns.py tools via error paths and mocking.
"""

import asyncio
from unittest.mock import patch, MagicMock

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.netlist import register_netlist_tools
from kicad_mcp.tools.patterns import register_pattern_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def netlist_server():
    mcp = FastMCP("test-netlist")
    register_netlist_tools(mcp)
    return mcp


@pytest.fixture
def pattern_server():
    mcp = FastMCP("test-patterns")
    register_pattern_tools(mcp)
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


# -- extract_schematic_netlist tests -----------------------------------------

class TestExtractSchematicNetlist:

    def test_file_not_found(self, netlist_server):
        fn = _get_tool_fn(netlist_server, "extract_schematic_netlist")
        result = asyncio.run(fn("/nonexistent/test.kicad_sch", None))
        assert result["success"] is False
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.netlist.analyze_netlist")
    @patch("kicad_mcp.tools.netlist.extract_netlist")
    def test_returns_netlist(self, mock_extract, mock_analyze, netlist_server, sch_file):
        mock_extract.return_value = {
            "component_count": 3,
            "net_count": 5,
            "components": [{"reference": "R1", "value": "10k"}],
            "nets": {"GND": [], "VCC": []},
        }
        mock_analyze.return_value = {"summary": "3 components, 5 nets"}
        fn = _get_tool_fn(netlist_server, "extract_schematic_netlist")
        result = asyncio.run(fn(sch_file, None))
        assert result["success"] is True

    @patch("kicad_mcp.tools.netlist.extract_netlist")
    def test_handles_extraction_error(self, mock_extract, netlist_server, sch_file):
        mock_extract.return_value = {"error": "Failed to parse schematic"}
        fn = _get_tool_fn(netlist_server, "extract_schematic_netlist")
        result = asyncio.run(fn(sch_file, None))
        assert result["success"] is False


# -- extract_project_netlist tests -------------------------------------------

class TestExtractProjectNetlist:

    def test_project_not_found(self, netlist_server):
        fn = _get_tool_fn(netlist_server, "extract_project_netlist")
        result = asyncio.run(fn("/nonexistent/project.kicad_pro", None))
        assert result["success"] is False

    def test_no_schematic(self, netlist_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(netlist_server, "extract_project_netlist")
        result = asyncio.run(fn(str(pro), None))
        assert result["success"] is False
        assert "schematic" in result["error"].lower()


# -- identify_circuit_patterns tests -----------------------------------------

class TestIdentifyCircuitPatterns:

    def test_file_not_found(self, pattern_server):
        fn = _get_tool_fn(pattern_server, "identify_circuit_patterns")
        result = asyncio.run(fn("/nonexistent/test.kicad_sch", None))
        assert result["success"] is False

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_identifies_patterns(self, mock_extract, pattern_server, sch_file):
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
        fn = _get_tool_fn(pattern_server, "identify_circuit_patterns")
        result = asyncio.run(fn(sch_file, None))
        assert result["success"] is True

    @patch("kicad_mcp.tools.patterns.extract_netlist")
    def test_handles_extraction_error(self, mock_extract, pattern_server, sch_file):
        mock_extract.return_value = {"error": "Parse failure"}
        fn = _get_tool_fn(pattern_server, "identify_circuit_patterns")
        result = asyncio.run(fn(sch_file, None))
        assert result["success"] is False


# -- analyze_project_circuit_patterns tests ----------------------------------

class TestAnalyzeProjectPatterns:

    def test_project_not_found(self, pattern_server):
        fn = _get_tool_fn(pattern_server, "analyze_project_circuit_patterns")
        result = asyncio.run(fn("/nonexistent/project.kicad_pro", None))
        assert result["success"] is False

    def test_no_schematic(self, pattern_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(pattern_server, "analyze_project_circuit_patterns")
        result = asyncio.run(fn(str(pro), None))
        assert result["success"] is False
