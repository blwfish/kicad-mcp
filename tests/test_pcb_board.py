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

from kicad_mcp.tools.pcb import register_pcb_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def pcb_server():
    """Create a FastMCP server with the pcb router registered."""
    mcp = FastMCP("test-pcb")
    register_pcb_tools(mcp)
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


def _get_pcb_fn(mcp_server):
    tool = asyncio.run(mcp_server.get_tool("pcb"))
    if tool is None:
        raise ValueError("Tool 'pcb' not found")
    return tool.fn


# -- load tests --------------------------------------------------------------

class TestLoadPcb:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("load", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"].lower()

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_returns_board_summary(self, mock_run, pcb_server, pcb_file):
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
        fn = _get_pcb_fn(pcb_server)
        result = fn("load", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["footprint_count"] == 3
        assert result["track_count"] == 12
        mock_run.assert_called_once()

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_passes_path_via_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "file": pcb_file,
                                  "footprint_count": 0, "track_count": 0,
                                  "footprints": []}
        fn = _get_pcb_fn(pcb_server)
        fn("load", pcb_path=pcb_file)
        params = mock_run.call_args[1]["params"]
        assert params["pcb_path"] == pcb_file


# -- create tests ------------------------------------------------------------

class TestCreatePcb:

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_create_returns_ok(self, mock_run, pcb_server, tmp_path):
        pcb_path = str(tmp_path / "new.kicad_pcb")
        mock_run.return_value = {"status": "ok", "file": pcb_path}
        fn = _get_pcb_fn(pcb_server)
        result = fn("create", pcb_path=pcb_path)
        assert result["status"] == "ok"

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_passes_path_via_params(self, mock_run, pcb_server, tmp_path):
        pcb_path = str(tmp_path / "new.kicad_pcb")
        mock_run.return_value = {"status": "ok", "file": pcb_path}
        fn = _get_pcb_fn(pcb_server)
        fn("create", pcb_path=pcb_path)
        params = mock_run.call_args[1]["params"]
        assert params["pcb_path"] == pcb_path


# -- set_outline tests -------------------------------------------------------

class TestSetOutline:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_outline", pcb_path="/nonexistent/board.kicad_pcb",
                    x_mm=0, y_mm=0, width_mm=50, height_mm=30)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_returns_outline_info(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "previous_edge_cuts_removed": 0,
            "outline": {
                "x_mm": 100.0, "y_mm": 80.0,
                "width_mm": 50.0, "height_mm": 30.0,
            },
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_outline", pcb_path=pcb_file,
                    x_mm=100.0, y_mm=80.0, width_mm=50.0, height_mm=30.0)
        assert result["status"] == "ok"
        assert result["outline"]["width_mm"] == 50.0
        assert result["outline"]["height_mm"] == 30.0

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "previous_edge_cuts_removed": 4,
                                  "outline": {"x_mm": 10, "y_mm": 20,
                                              "width_mm": 60, "height_mm": 40}}
        fn = _get_pcb_fn(pcb_server)
        fn("set_outline", pcb_path=pcb_file,
           x_mm=10, y_mm=20, width_mm=60, height_mm=40)
        params = mock_run.call_args[1]["params"]
        assert params["x_mm"] == 10
        assert params["y_mm"] == 20
        assert params["width_mm"] == 60
        assert params["height_mm"] == 40

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_removes_existing_outline(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "previous_edge_cuts_removed": 4,
            "outline": {"x_mm": 0, "y_mm": 0, "width_mm": 50, "height_mm": 30},
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_outline", pcb_path=pcb_file,
                    x_mm=0, y_mm=0, width_mm=50, height_mm=30)
        assert result["previous_edge_cuts_removed"] == 4


# -- set_design_rules tests --------------------------------------------------

class TestSetDesignRules:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_design_rules", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_returns_design_rules(self, mock_run, pcb_server, pcb_with_project):
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
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_design_rules", pcb_path=pcb_path, min_track_width_mm=0.25)
        assert result["status"] == "ok"
        assert result["design_rules"]["min_track_width_mm"] == 0.25

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_updates_project_file(self, mock_run, pcb_server, pcb_with_project):
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
        fn = _get_pcb_fn(pcb_server)
        result = fn(
            "set_design_rules", pcb_path=pcb_path,
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
    def test_no_project_file(self, mock_run, pcb_server, pcb_file):
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
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_design_rules", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["project_rules_updated"] is False


# -- unknown operation -------------------------------------------------------

class TestUnknownOperation:

    def test_unknown_op_returns_error(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("bogus_op", pcb_path="/some/board.kicad_pcb")
        assert "error" in result
        assert "unknown operation" in result["error"]
        assert "bogus_op" in result["error"]
