"""
Tests for PCB routing tools: traces, vias, and routing management.

Unit tests mock run_pcbnew_script to test tool logic without requiring
KiCad's Python 3.9 / pcbnew bindings.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_routing import register_pcb_routing_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def routing_server():
    mcp = FastMCP("test-routing")
    register_pcb_routing_tools(mcp)
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


# -- add_trace tests ---------------------------------------------------------

class TestAddTrace:

    def test_file_not_found(self, routing_server):
        fn = _get_tool_fn(routing_server, "add_trace")
        result = fn("/nonexistent/board.kicad_pcb", 0, 0, 10, 10)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_returns_trace_info(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "trace": {
                "start": [100.0, 80.0],
                "end": [110.0, 80.0],
                "width_mm": 0.25,
                "layer": "F.Cu",
                "net": "VCC",
            },
        }
        fn = _get_tool_fn(routing_server, "add_trace")
        result = fn(pcb_file, 100.0, 80.0, 110.0, 80.0, net_name="VCC")
        assert result["status"] == "ok"
        assert result["trace"]["start"] == [100.0, 80.0]
        assert result["trace"]["net"] == "VCC"

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {"status": "ok", "trace": {}}
        fn = _get_tool_fn(routing_server, "add_trace")
        fn(pcb_file, 1, 2, 3, 4, width_mm=0.5, layer="B.Cu", net_name="GND")
        params = mock_run.call_args[1]["params"]
        assert params["start_x_mm"] == 1
        assert params["start_y_mm"] == 2
        assert params["end_x_mm"] == 3
        assert params["end_y_mm"] == 4
        assert params["width_mm"] == 0.5
        assert params["layer"] == "B.Cu"
        assert params["net_name"] == "GND"

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_default_width_and_layer(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {"status": "ok", "trace": {}}
        fn = _get_tool_fn(routing_server, "add_trace")
        fn(pcb_file, 0, 0, 10, 10)
        params = mock_run.call_args[1]["params"]
        assert params["width_mm"] == 0.25
        assert params["layer"] == "F.Cu"
        assert params["net_name"] == ""


# -- add_via tests -----------------------------------------------------------

class TestAddVia:

    def test_file_not_found(self, routing_server):
        fn = _get_tool_fn(routing_server, "add_via")
        result = fn("/nonexistent/board.kicad_pcb", 50, 50)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_returns_via_info(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "via": {
                "x_mm": 50.0, "y_mm": 60.0,
                "drill_mm": 0.3, "size_mm": 0.6,
                "type": "through", "net": "",
            },
        }
        fn = _get_tool_fn(routing_server, "add_via")
        result = fn(pcb_file, 50.0, 60.0)
        assert result["status"] == "ok"
        assert result["via"]["x_mm"] == 50.0
        assert result["via"]["type"] == "through"

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_custom_via_params(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {"status": "ok", "via": {}}
        fn = _get_tool_fn(routing_server, "add_via")
        fn(pcb_file, 10, 20, drill_mm=0.4, size_mm=0.8,
           net_name="GND", via_type="blind_buried")
        params = mock_run.call_args[1]["params"]
        assert params["drill_mm"] == 0.4
        assert params["size_mm"] == 0.8
        assert params["net_name"] == "GND"
        assert params["via_type"] == "blind_buried"


# -- edit_trace_width tests --------------------------------------------------

class TestEditTraceWidth:

    def test_file_not_found(self, routing_server):
        fn = _get_tool_fn(routing_server, "edit_trace_width")
        result = fn("/nonexistent/board.kicad_pcb", 0.5)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_update_all_traces(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "updated": 15,
            "skipped": 3,
            "new_width_mm": 0.5,
            "net_filter": "(all)",
            "layer_filter": "(all)",
        }
        fn = _get_tool_fn(routing_server, "edit_trace_width")
        result = fn(pcb_file, 0.5)
        assert result["updated"] == 15
        assert result["skipped"] == 3

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_filter_by_net_and_layer(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {"status": "ok", "updated": 5, "skipped": 10,
                                  "new_width_mm": 0.4, "net_filter": "VCC",
                                  "layer_filter": "F.Cu"}
        fn = _get_tool_fn(routing_server, "edit_trace_width")
        fn(pcb_file, 0.4, net_name="VCC", layer="F.Cu")
        params = mock_run.call_args[1]["params"]
        assert params["net_name"] == "VCC"
        assert params["layer"] == "F.Cu"


# -- clear_routing tests -----------------------------------------------------

class TestClearRouting:

    def test_file_not_found(self, routing_server):
        fn = _get_tool_fn(routing_server, "clear_routing")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_default_clears_tracks_and_vias(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "tracks_removed": 20,
            "vias_removed": 5,
            "zones_removed": 0,
        }
        fn = _get_tool_fn(routing_server, "clear_routing")
        result = fn(pcb_file)
        assert result["tracks_removed"] == 20
        assert result["vias_removed"] == 5
        assert result["zones_removed"] == 0
        params = mock_run.call_args[1]["params"]
        assert params["clear_tracks"] is True
        assert params["clear_vias"] is True
        assert params["clear_zones"] is False

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_clear_zones_too(self, mock_run, routing_server, pcb_file):
        mock_run.return_value = {"status": "ok", "tracks_removed": 0,
                                  "vias_removed": 0, "zones_removed": 2}
        fn = _get_tool_fn(routing_server, "clear_routing")
        fn(pcb_file, clear_tracks=False, clear_vias=False, clear_zones=True)
        params = mock_run.call_args[1]["params"]
        assert params["clear_tracks"] is False
        assert params["clear_vias"] is False
        assert params["clear_zones"] is True
