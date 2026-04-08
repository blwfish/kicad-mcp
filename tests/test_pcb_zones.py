"""
Tests for PCB copper zone tools: add zones and fill zones.

Unit tests mock run_pcbnew_script to test tool logic without requiring
KiCad's Python 3.9 / pcbnew bindings.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_zones import register_pcb_zone_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def zone_server():
    mcp = FastMCP("test-zones")
    register_pcb_zone_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- add_copper_zone tests ---------------------------------------------------

class TestAddCopperZone:

    def test_file_not_found(self, zone_server):
        fn = _get_tool_fn(zone_server, "add_copper_zone")
        result = fn("/nonexistent/board.kicad_pcb", "GND")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_returns_zone_info(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "zone": {
                "net": "GND",
                "layer": "B.Cu",
                "corners": [[0, 0], [50, 0], [50, 30], [0, 30]],
                "clearance_mm": 0.3,
                "min_width_mm": 0.2,
                "connect_pads": "thermal",
                "priority": 0,
            },
        }
        fn = _get_tool_fn(zone_server, "add_copper_zone")
        result = fn(pcb_file, "GND", layer="B.Cu",
                    corners=[[0, 0], [50, 0], [50, 30], [0, 30]])
        assert result["status"] == "ok"
        assert result["zone"]["net"] == "GND"
        assert result["zone"]["layer"] == "B.Cu"
        assert len(result["zone"]["corners"]) == 4

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_auto_outline_when_no_corners(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "auto_outline": True,
            "note": "Zone corners auto-derived from board outline (Edge.Cuts)",
            "zone": {"net": "GND", "layer": "F.Cu", "corners": [],
                     "clearance_mm": 0.3, "min_width_mm": 0.2,
                     "connect_pads": "thermal", "priority": 0},
        }
        fn = _get_tool_fn(zone_server, "add_copper_zone")
        result = fn(pcb_file, "GND")
        assert result["status"] == "ok"
        # Verify empty corners is passed (auto-outline in pcbnew script)
        params = mock_run.call_args[1]["params"]
        assert params["corners"] == []

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {"status": "ok", "zone": {}}
        fn = _get_tool_fn(zone_server, "add_copper_zone")
        fn(pcb_file, "VCC", layer="F.Cu",
           corners=[[0, 0], [10, 0], [10, 10], [0, 10]],
           clearance_mm=0.5, min_width_mm=0.3,
           connect_pads="solid", priority=1)
        params = mock_run.call_args[1]["params"]
        assert params["net_name"] == "VCC"
        assert params["layer"] == "F.Cu"
        assert params["clearance_mm"] == 0.5
        assert params["min_width_mm"] == 0.3
        assert params["connect_pads"] == "solid"
        assert params["priority"] == 1

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_uses_60s_timeout(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {"status": "ok", "zone": {}}
        fn = _get_tool_fn(zone_server, "add_copper_zone")
        fn(pcb_file, "GND")
        assert mock_run.call_args[1]["timeout"] == 60.0


# -- fill_zones tests --------------------------------------------------------

class TestFillZones:

    def test_file_not_found(self, zone_server):
        fn = _get_tool_fn(zone_server, "fill_zones")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_returns_fill_results(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "fill_success": True,
            "zones_filled": 2,
            "zones": [
                {"net": "GND", "layer": "B.Cu", "filled": True,
                 "filled_area_mm2": 1500.0},
                {"net": "VCC", "layer": "F.Cu", "filled": True,
                 "filled_area_mm2": 800.0},
            ],
        }
        fn = _get_tool_fn(zone_server, "fill_zones")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["zones_filled"] == 2
        assert result["fill_success"] is True

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_no_zones_to_fill(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "message": "No copper zones to fill",
            "zones_filled": 0,
        }
        fn = _get_tool_fn(zone_server, "fill_zones")
        result = fn(pcb_file)
        assert result["zones_filled"] == 0

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_uses_60s_timeout(self, mock_run, zone_server, pcb_file):
        mock_run.return_value = {"status": "ok", "zones_filled": 0}
        fn = _get_tool_fn(zone_server, "fill_zones")
        fn(pcb_file)
        assert mock_run.call_args[1]["timeout"] == 60.0
