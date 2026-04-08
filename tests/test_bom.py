"""
Tests for BOM (Bill of Materials) tools.

Tests bom.py tools and the export_bom_csv tool.
"""

import asyncio
import csv
import json
import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.bom import register_bom_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def bom_server():
    mcp = FastMCP("test-bom")
    register_bom_tools(mcp)
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

    # Create a BOM CSV file
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


# -- analyze_bom tests -------------------------------------------------------

class TestAnalyzeBom:

    def test_project_not_found(self, bom_server):
        fn = _get_tool_fn(bom_server, "analyze_bom")
        result = asyncio.run(fn("/nonexistent/project.kicad_pro", None))
        assert result["success"] is False

    def test_no_bom_files(self, bom_server, tmp_path):
        pro = tmp_path / "empty.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(bom_server, "analyze_bom")
        result = asyncio.run(fn(str(pro), None))
        assert result["success"] is False
        assert "No BOM" in result["error"]

    def test_analyzes_bom(self, bom_server, project_with_bom):
        fn = _get_tool_fn(bom_server, "analyze_bom")
        result = asyncio.run(fn(project_with_bom["project_path"], None))
        assert result["success"] is True


# -- export_bom_csv tests ---------------------------------------------------

class TestExportBomCsv:

    def test_project_not_found(self, bom_server):
        fn = _get_tool_fn(bom_server, "export_bom_csv")
        result = asyncio.run(fn("/nonexistent/project.kicad_pro", None))
        assert result["success"] is False

    def test_no_schematic(self, bom_server, tmp_path):
        """export_bom_csv needs a schematic to generate from."""
        pro = tmp_path / "noschem.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(bom_server, "export_bom_csv")
        result = asyncio.run(fn(str(pro), None))
        assert result["success"] is False
