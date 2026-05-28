"""
Tests for PCB routing tools: traces, vias, and routing management.

Unit tests mock run_pcbnew_script to test tool logic without requiring
KiCad's Python 3.9 / pcbnew bindings.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb import register_pcb_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def pcb_server():
    mcp = FastMCP("test-pcb")
    register_pcb_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


def _get_pcb_fn(mcp_server):
    tool = asyncio.run(mcp_server.get_tool("pcb"))
    if tool is None:
        raise ValueError("Tool 'pcb' not found")
    return tool.fn


# -- add_trace tests ---------------------------------------------------------

class TestAddTrace:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_trace",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    start_x_mm=0, start_y_mm=0, end_x_mm=10, end_y_mm=10)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_returns_trace_info(self, mock_run, pcb_server, pcb_file):
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
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_trace",
                    pcb_path=pcb_file,
                    start_x_mm=100.0, start_y_mm=80.0,
                    end_x_mm=110.0, end_y_mm=80.0,
                    net_name="VCC")
        assert result["status"] == "ok"
        assert result["trace"]["start"] == [100.0, 80.0]
        assert result["trace"]["net"] == "VCC"

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "trace": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("add_trace",
           pcb_path=pcb_file,
           start_x_mm=1, start_y_mm=2, end_x_mm=3, end_y_mm=4,
           trace_width_mm=0.5, layer="B.Cu", net_name="GND")
        params = mock_run.call_args[1]["params"]
        assert params["start_x_mm"] == 1
        assert params["start_y_mm"] == 2
        assert params["end_x_mm"] == 3
        assert params["end_y_mm"] == 4
        assert params["width_mm"] == 0.5
        assert params["layer"] == "B.Cu"
        assert params["net_name"] == "GND"

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_default_width_and_layer(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "trace": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("add_trace",
           pcb_path=pcb_file,
           start_x_mm=0, start_y_mm=0, end_x_mm=10, end_y_mm=10)
        params = mock_run.call_args[1]["params"]
        assert params["width_mm"] == 0.25
        assert params["layer"] == "F.Cu"
        assert params["net_name"] == ""


# -- add_via tests -----------------------------------------------------------

class TestAddVia:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    x_mm=50, y_mm=50)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_returns_via_info(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "via": {
                "x_mm": 50.0, "y_mm": 60.0,
                "drill_mm": 0.3, "size_mm": 0.6,
                "type": "through", "net": "",
            },
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=50.0, y_mm=60.0)
        assert result["status"] == "ok"
        assert result["via"]["x_mm"] == 50.0
        assert result["via"]["type"] == "through"

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_custom_via_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "via": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("add_via",
           pcb_path=pcb_file, x_mm=10, y_mm=20,
           drill_mm=0.4, size_mm=0.8,
           net_name="GND", via_type="blind_buried")
        params = mock_run.call_args[1]["params"]
        assert params["drill_mm"] == 0.4
        assert params["size_mm"] == 0.8
        assert params["net_name"] == "GND"
        assert params["via_type"] == "blind_buried"


# -- edit_trace_width tests --------------------------------------------------

class TestEditTraceWidth:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_trace_width",
                    pcb_path="/nonexistent/board.kicad_pcb", new_width_mm=0.5)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_update_all_traces(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "updated": 15,
            "skipped": 3,
            "new_width_mm": 0.5,
            "net_filter": "(all)",
            "layer_filter": "(all)",
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_trace_width", pcb_path=pcb_file, new_width_mm=0.5)
        assert result["updated"] == 15
        assert result["skipped"] == 3

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_filter_by_net_and_layer(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "updated": 5, "skipped": 10,
                                  "new_width_mm": 0.4, "net_filter": "VCC",
                                  "layer_filter": "F.Cu"}
        fn = _get_pcb_fn(pcb_server)
        fn("edit_trace_width",
           pcb_path=pcb_file, new_width_mm=0.4,
           net_filter="VCC", layer_filter="F.Cu")
        params = mock_run.call_args[1]["params"]
        assert params["net_name"] == "VCC"
        assert params["layer"] == "F.Cu"


# -- clear_routing tests -----------------------------------------------------

class TestClearRouting:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("clear_routing", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_default_clears_tracks_and_vias(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "tracks_removed": 20,
            "vias_removed": 5,
            "zones_removed": 0,
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("clear_routing", pcb_path=pcb_file)
        assert result["tracks_removed"] == 20
        assert result["vias_removed"] == 5
        assert result["zones_removed"] == 0
        params = mock_run.call_args[1]["params"]
        assert params["clear_tracks"] is True
        assert params["clear_vias"] is True
        assert params["clear_zones"] is False

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_clear_zones_too(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "tracks_removed": 0,
                                  "vias_removed": 0, "zones_removed": 2}
        fn = _get_pcb_fn(pcb_server)
        fn("clear_routing",
           pcb_path=pcb_file, clear_tracks=False, clear_vias=False,
           clear_zones_flag=True)
        params = mock_run.call_args[1]["params"]
        assert params["clear_tracks"] is False
        assert params["clear_vias"] is False
        assert params["clear_zones"] is True


# -- Threshold-boundary tests ------------------------------------------------
# Each `<= 0` guard needs three cases: value=0 (boundary, rejected),
# value=-0.1 (below, rejected), value=0.1 (above, accepted).
# The `drill >= size` guard needs: drill==size (boundary, rejected),
# drill slightly above size (rejected), drill slightly below size (accepted).


class TestAddTraceWidthBoundary:
    """Threshold tests for add_trace.width_mm <= 0 guard."""

    def test_zero_width_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_trace", pcb_path=pcb_file,
                    start_x_mm=0, start_y_mm=0, end_x_mm=10, end_y_mm=10,
                    trace_width_mm=0)
        assert "error" in result
        assert "width_mm" in result["error"]

    def test_negative_width_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_trace", pcb_path=pcb_file,
                    start_x_mm=0, start_y_mm=0, end_x_mm=10, end_y_mm=10,
                    trace_width_mm=-0.1)
        assert "error" in result
        assert "width_mm" in result["error"]

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_small_positive_width_accepted(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "trace": {}}
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_trace", pcb_path=pcb_file,
                    start_x_mm=0, start_y_mm=0, end_x_mm=10, end_y_mm=10,
                    trace_width_mm=0.01)
        assert "error" not in result


class TestEditTraceWidthBoundary:
    """Threshold tests for edit_trace_width.new_width_mm <= 0 guard."""

    def test_zero_new_width_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_trace_width", pcb_path=pcb_file, new_width_mm=0)
        assert "error" in result
        assert "new_width_mm" in result["error"]

    def test_negative_new_width_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_trace_width", pcb_path=pcb_file, new_width_mm=-0.1)
        assert "error" in result
        assert "new_width_mm" in result["error"]

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_small_positive_new_width_accepted(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "updated": 0, "skipped": 0,
                                  "new_width_mm": 0.01, "net_filter": "(all)",
                                  "layer_filter": "(all)"}
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_trace_width", pcb_path=pcb_file, new_width_mm=0.01)
        assert "error" not in result


class TestAddViaBoundary:
    """Threshold tests for add_via guards:
      - drill_mm <= 0
      - size_mm <= 0
      - drill_mm >= size_mm
    """

    # drill_mm <= 0
    def test_zero_drill_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0, size_mm=0.6)
        assert "error" in result
        assert "drill_mm" in result["error"]

    def test_negative_drill_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=-0.1, size_mm=0.6)
        assert "error" in result
        assert "drill_mm" in result["error"]

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_small_positive_drill_accepted(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "via": {}}
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.01, size_mm=0.6)
        assert "error" not in result

    # size_mm <= 0
    def test_zero_size_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.3, size_mm=0)
        assert "error" in result
        assert "size_mm" in result["error"]

    def test_negative_size_rejected(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.3, size_mm=-0.1)
        assert "error" in result
        assert "size_mm" in result["error"]

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_small_positive_size_accepted(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "via": {}}
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.01, size_mm=0.1)
        assert "error" not in result

    # drill_mm >= size_mm: drill must be strictly less than size
    def test_drill_equal_size_rejected(self, pcb_server, pcb_file):
        """At the boundary: drill == size → no annular ring → rejected."""
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.5, size_mm=0.5)
        assert "error" in result
        assert "annular" in result["error"].lower() or "drill" in result["error"].lower()

    def test_drill_exceeds_size_rejected(self, pcb_server, pcb_file):
        """Just above boundary: drill > size → rejected."""
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.6, size_mm=0.5)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_routing.run_pcbnew_script")
    def test_drill_just_below_size_accepted(self, mock_run, pcb_server, pcb_file):
        """Just below boundary: drill < size → accepted."""
        mock_run.return_value = {"status": "ok", "via": {}}
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_via", pcb_path=pcb_file, x_mm=10, y_mm=10,
                    drill_mm=0.49, size_mm=0.5)
        assert "error" not in result
