"""
Tests for PCB board management tools: load, create, outline, design rules.

Unit tests mock run_pcbnew_script to test tool logic without requiring
KiCad's Python 3.9 / pcbnew bindings.
"""

import asyncio
import json
import os
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_board import register_pcb_board_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def board_server():
    """Create a FastMCP server with only board tools registered."""
    mcp = FastMCP("test-board")
    register_pcb_board_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


@pytest.fixture
def pcb_with_project(tmp_path):
    """PCB file with a companion .kicad_pro for set_design_rules."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    pro = tmp_path / "test.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": "test.kicad_pro"}}, indent=2))
    return {"pcb_path": str(pcb), "pro_path": str(pro)}


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- load_pcb tests ----------------------------------------------------------

class TestLoadPcb:

    def test_file_not_found(self, board_server):
        fn = _get_tool_fn(board_server, "load_pcb")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"].lower()

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_returns_board_summary(self, mock_run, board_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "file": pcb_file,
            "footprint_count": 3,
            "track_count": 12,
            "footprints": [
                {"reference": "R1", "value": "10k", "footprint": "R_0805",
                 "x_mm": 100.0, "y_mm": 80.0, "layer": "F.Cu"},
            ],
        }
        fn = _get_tool_fn(board_server, "load_pcb")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["footprint_count"] == 3
        assert result["track_count"] == 12
        mock_run.assert_called_once()

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_passes_path_via_params(self, mock_run, board_server, pcb_file):
        mock_run.return_value = {"status": "ok", "file": pcb_file,
                                  "footprint_count": 0, "track_count": 0,
                                  "footprints": []}
        fn = _get_tool_fn(board_server, "load_pcb")
        fn(pcb_file)
        params = mock_run.call_args[1]["params"]
        assert params["pcb_path"] == pcb_file


# -- create_pcb tests --------------------------------------------------------

class TestCreatePcb:

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_create_returns_ok(self, mock_run, board_server, tmp_path):
        pcb_path = str(tmp_path / "new.kicad_pcb")
        mock_run.return_value = {"status": "ok", "file": pcb_path}
        fn = _get_tool_fn(board_server, "create_pcb")
        result = fn(pcb_path)
        assert result["status"] == "ok"

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_passes_path_via_params(self, mock_run, board_server, tmp_path):
        pcb_path = str(tmp_path / "new.kicad_pcb")
        mock_run.return_value = {"status": "ok", "file": pcb_path}
        fn = _get_tool_fn(board_server, "create_pcb")
        fn(pcb_path)
        params = mock_run.call_args[1]["params"]
        assert params["pcb_path"] == pcb_path


# -- add_board_outline tests -------------------------------------------------

class TestAddBoardOutline:

    def test_file_not_found(self, board_server):
        fn = _get_tool_fn(board_server, "add_board_outline")
        result = fn("/nonexistent/board.kicad_pcb", 0, 0, 50, 30)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_returns_outline_info(self, mock_run, board_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "previous_edge_cuts_removed": 0,
            "outline": {
                "x_mm": 100.0, "y_mm": 80.0,
                "width_mm": 50.0, "height_mm": 30.0,
            },
        }
        fn = _get_tool_fn(board_server, "add_board_outline")
        result = fn(pcb_file, 100.0, 80.0, 50.0, 30.0)
        assert result["status"] == "ok"
        assert result["outline"]["width_mm"] == 50.0
        assert result["outline"]["height_mm"] == 30.0

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, board_server, pcb_file):
        mock_run.return_value = {"status": "ok", "previous_edge_cuts_removed": 4,
                                  "outline": {"x_mm": 10, "y_mm": 20,
                                              "width_mm": 60, "height_mm": 40}}
        fn = _get_tool_fn(board_server, "add_board_outline")
        fn(pcb_file, 10, 20, 60, 40)
        params = mock_run.call_args[1]["params"]
        assert params["x_mm"] == 10
        assert params["y_mm"] == 20
        assert params["width_mm"] == 60
        assert params["height_mm"] == 40

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_removes_existing_outline(self, mock_run, board_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "previous_edge_cuts_removed": 4,
            "outline": {"x_mm": 0, "y_mm": 0, "width_mm": 50, "height_mm": 30},
        }
        fn = _get_tool_fn(board_server, "add_board_outline")
        result = fn(pcb_file, 0, 0, 50, 30)
        assert result["previous_edge_cuts_removed"] == 4


# -- set_design_rules tests --------------------------------------------------

class TestSetDesignRules:

    def test_file_not_found(self, board_server):
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_returns_design_rules(self, mock_run, board_server, pcb_with_project):
        pcb_path = pcb_with_project["pcb_path"]
        mock_run.return_value = {
            "status": "ok",
            "design_rules": {
                "min_track_width_mm": 0.25,
                "min_clearance_mm": 0.2,
                "min_via_diameter_mm": 0.6,
                "min_via_drill_mm": 0.3,
                "min_hole_to_hole_mm": 0.25,
                "min_through_hole_diameter_mm": 0.3,
                "min_copper_edge_clearance_mm": 0.5,
            },
        }
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn(pcb_path, min_track_width_mm=0.25)
        assert result["status"] == "ok"
        assert result["design_rules"]["min_track_width_mm"] == 0.25

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_updates_project_file(self, mock_run, board_server, pcb_with_project):
        pcb_path = pcb_with_project["pcb_path"]
        pro_path = pcb_with_project["pro_path"]
        mock_run.return_value = {
            "status": "ok",
            "design_rules": {
                "min_track_width_mm": 0.3,
                "min_clearance_mm": 0.25,
                "min_via_diameter_mm": 0.6,
                "min_via_drill_mm": 0.3,
                "min_hole_to_hole_mm": 0.25,
                "min_through_hole_diameter_mm": 0.2,
                "min_copper_edge_clearance_mm": 0.0,
            },
        }
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn(
            pcb_path,
            min_track_width_mm=0.3,
            min_clearance_mm=0.25,
            min_through_hole_diameter_mm=0.2,
            min_copper_edge_clearance_mm=0.0,
        )
        assert result["project_rules_updated"] is True

        # Verify project file was written
        with open(pro_path) as f:
            project = json.load(f)
        rules = project["board"]["design_settings"]["rules"]
        assert rules["min_through_hole_diameter"] == 0.2
        assert rules["min_copper_edge_clearance"] == 0.0
        assert rules["min_track_width"] == 0.3

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_no_project_file(self, mock_run, board_server, pcb_file):
        """set_design_rules works even without a .kicad_pro file."""
        mock_run.return_value = {
            "status": "ok",
            "design_rules": {
                "min_track_width_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_via_diameter_mm": 0.6,
                "min_via_drill_mm": 0.3,
                "min_hole_to_hole_mm": 0.25,
                "min_through_hole_diameter_mm": 0.3,
                "min_copper_edge_clearance_mm": 0.5,
            },
        }
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["project_rules_updated"] is False
