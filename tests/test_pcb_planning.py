"""
Tests for PCB planning tools: board size estimation and suggested placement.

Unit tests mock run_pcbnew_script.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_planning import register_pcb_planning_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def planning_server():
    mcp = FastMCP("test-planning")
    register_pcb_planning_tools(mcp)
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


# -- estimate_board_size tests -----------------------------------------------

class TestEstimateBoardSize:

    def test_empty_footprints(self, planning_server):
        fn = _get_tool_fn(planning_server, "estimate_board_size")
        result = fn([])
        assert "error" in result
        assert "No footprints" in result["error"]

    @patch("kicad_mcp.tools.pcb_planning.run_pcbnew_script")
    def test_returns_size_estimate(self, mock_run, planning_server):
        mock_run.return_value = {
            "status": "ok",
            "component_count": 5,
            "total_component_area_mm2": 120.0,
            "suggested_board": {
                "width_mm": 40.0,
                "height_mm": 30.0,
                "area_mm2": 1200.0,
            },
            "components": [
                {"library": "Resistor_SMD", "footprint": "R_0805",
                 "width_mm": 3.0, "height_mm": 1.3, "area_mm2": 3.9},
            ],
        }
        fn = _get_tool_fn(planning_server, "estimate_board_size")
        result = fn([
            {"library": "Resistor_SMD", "footprint_name": "R_0805_2012Metric"},
            {"library": "Capacitor_SMD", "footprint_name": "C_0805_2012Metric"},
        ])
        assert result["status"] == "ok"
        assert "suggested_board" in result
        assert result["suggested_board"]["width_mm"] > 0

    @patch("kicad_mcp.tools.pcb_planning.run_pcbnew_script")
    def test_passes_params(self, mock_run, planning_server):
        mock_run.return_value = {"status": "ok", "component_count": 1,
                                  "suggested_board": {}}
        fn = _get_tool_fn(planning_server, "estimate_board_size")
        fn([{"library": "R", "footprint_name": "R_0805"}],
           padding_mm=3.0, routing_factor=3.0)
        params = mock_run.call_args[1]["params"]
        assert params["padding_mm"] == 3.0
        assert params["routing_factor"] == 3.0
        assert len(params["footprints"]) == 1

    @patch("kicad_mcp.tools.pcb_planning.run_pcbnew_script")
    def test_runtime_error_returns_error_envelope_not_raises(self, mock_run, planning_server):
        """finding #16 (Phase 1.5, 2026-09-23 full review): this call had no
        try/except of its own -- run_pcbnew_script normalizes every
        subprocess failure to RuntimeError (its documented contract), so
        the exception escaped this tool call raw instead of the
        {"error": ...} envelope every other failure path here returns."""
        mock_run.side_effect = RuntimeError("pcbnew script failed (exit 1): boom")
        fn = _get_tool_fn(planning_server, "estimate_board_size")
        result = fn([{"library": "R", "footprint_name": "R_0805"}])   # must not raise
        assert "error" in result
        assert "boom" in result["error"]


# -- suggest_placement tests -------------------------------------------------

class TestSuggestPlacement:

    def test_file_not_found(self, planning_server):
        fn = _get_tool_fn(planning_server, "suggest_placement")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_planning.run_pcbnew_script")
    def test_returns_suggestions(self, mock_run, planning_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "footprint_count": 3,
            "suggestions": [
                {"reference": "U1", "x_mm": 130, "y_mm": 97,
                 "reason": "center of board"},
                {"reference": "C1", "x_mm": 125, "y_mm": 90,
                 "reason": "near U1 pin 1 (decoupling)"},
                {"reference": "R1", "x_mm": 135, "y_mm": 90,
                 "reason": "near U1 pin 3 (pull-up)"},
            ],
        }
        fn = _get_tool_fn(planning_server, "suggest_placement")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert len(result["suggestions"]) == 3
        assert result["suggestions"][0]["reference"] == "U1"

    @patch("kicad_mcp.tools.pcb_planning.run_pcbnew_script")
    def test_runtime_error_returns_error_envelope_not_raises(self, mock_run, planning_server, pcb_file):
        """finding #16 (Phase 1.5, 2026-09-23 full review): this call had no
        try/except of its own -- run_pcbnew_script normalizes every
        subprocess failure to RuntimeError (its documented contract), so
        the exception escaped this tool call raw instead of the
        {"error": ...} envelope every other failure path here returns."""
        mock_run.side_effect = RuntimeError("pcbnew script failed (exit 1): boom")
        fn = _get_tool_fn(planning_server, "suggest_placement")
        result = fn(pcb_file)   # must not raise
        assert "error" in result
        assert "boom" in result["error"]
