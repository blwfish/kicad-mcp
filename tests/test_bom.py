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
        assert result["success"] is False

    def test_no_bom_files(self, analyze_server, tmp_path):
        pro = tmp_path / "empty.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="bom", ctx=None, project_path=str(pro),
        ))
        assert result["success"] is False
        assert "No BOM" in result["error"]

    def test_analyzes_bom(self, analyze_server, project_with_bom):
        fn = _get_tool_fn(analyze_server, "analyze")
        result = asyncio.run(fn(
            operation="bom", ctx=None,
            project_path=project_with_bom["project_path"],
        ))
        assert result["success"] is True

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
        assert result["success"] is False

    def test_no_schematic(self, export_server, tmp_path):
        """bom_csv needs a schematic to generate from."""
        pro = tmp_path / "noschem.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(export_server, "export")
        result = asyncio.run(fn(
            operation="bom_csv", ctx=None, project_path=str(pro),
        ))
        assert result["success"] is False

    def test_missing_project_path(self, export_server):
        fn = _get_tool_fn(export_server, "export")
        result = asyncio.run(fn(operation="bom_csv", ctx=None))
        assert "error" in result
        assert "project_path" in result["error"]
