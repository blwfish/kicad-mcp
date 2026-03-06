"""
Tests for check_pad_clearances tool.

Unit tests mock run_pcbnew_script to test tool registration and argument
handling.  Integration-style tests verify the embedded pcbnew script logic
by checking the generated script content.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def keepout_server():
    """Create a FastMCP server with keepout tools registered."""
    mcp = FastMCP("test-keepout")
    register_pcb_keepout_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    """Create a dummy .kicad_pcb file."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return str(pcb)


def _get_tool_fn(mcp_server, tool_name):
    """Extract a tool function from the FastMCP 3.0 server by name."""
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- Tool registration tests -------------------------------------------------

class TestCheckPadClearancesRegistration:

    def test_tool_registered(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        assert fn is not None

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]


# -- Script content tests ---------------------------------------------------

class TestCheckPadClearancesScript:

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_iterates_pads(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 0,
            "min_clearance_mm": 0.2,
            "violation_count": 0,
            "footprint_pairs_affected": 0,
            "footprint_pair_summary": [],
            "violations": [],
            "violations_truncated": False,
            "summary": "All inter-footprint pad clearances >= 0.2mm",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert "fp.Pads()" in script
        assert "pad.GetPosition()" in script
        assert "pad.GetSize()" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_uses_board_design_rules_when_zero(self, mock_run, keepout_server, pcb_file):
        """When min_clearance_mm=0, script falls back to board design rules."""
        mock_run.return_value = {
            "status": "ok", "total_pads": 0, "violation_count": 0,
            "min_clearance_mm": 0.2, "footprint_pairs_affected": 0,
            "footprint_pair_summary": [], "violations": [],
            "violations_truncated": False, "summary": "",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        fn(pcb_file, min_clearance_mm=0.0)
        script = mock_run.call_args[0][0]
        assert "m_MinClearance" in script
        assert "min_cl = 0" in script or "min_cl = 0.0" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_uses_explicit_clearance(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_pads": 0, "violation_count": 0,
            "min_clearance_mm": 0.5, "footprint_pairs_affected": 0,
            "footprint_pair_summary": [], "violations": [],
            "violations_truncated": False, "summary": "",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        fn(pcb_file, min_clearance_mm=0.5)
        script = mock_run.call_args[0][0]
        assert "min_cl = 0.5" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_skips_same_footprint(self, mock_run, keepout_server, pcb_file):
        """Script must skip pad pairs within the same footprint."""
        mock_run.return_value = {
            "status": "ok", "total_pads": 0, "violation_count": 0,
            "min_clearance_mm": 0.2, "footprint_pairs_affected": 0,
            "footprint_pair_summary": [], "violations": [],
            "violations_truncated": False, "summary": "",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert 'a["ref"] == b["ref"]' in script


# -- Mock result processing tests -------------------------------------------

class TestCheckPadClearancesResults:

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_no_violations(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 20,
            "min_clearance_mm": 0.2,
            "violation_count": 0,
            "footprint_pairs_affected": 0,
            "footprint_pair_summary": [],
            "violations": [],
            "violations_truncated": False,
            "summary": "All inter-footprint pad clearances >= 0.2mm (20 pads checked)",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["violation_count"] == 0
        assert result["total_pads"] == 20

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_violations_found(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 50,
            "min_clearance_mm": 0.2,
            "violation_count": 3,
            "footprint_pairs_affected": 2,
            "footprint_pair_summary": [
                {"ref_a": "R1", "ref_b": "U1", "pad_violations": 2, "min_gap_mm": 0.05},
                {"ref_a": "D1", "ref_b": "U1", "pad_violations": 1, "min_gap_mm": 0.1},
            ],
            "violations": [
                {
                    "pad_a": "R1:1", "pad_b": "U1:4",
                    "net_a": "SDA", "net_b": "",
                    "gap_mm": 0.05, "min_clearance_mm": 0.2,
                    "overlap": False,
                    "pad_a_center": [32.0, 44.0],
                    "pad_b_center": [32.5, 44.3],
                },
                {
                    "pad_a": "R1:2", "pad_b": "U1:5",
                    "net_a": "SCL", "net_b": "",
                    "gap_mm": 0.08, "min_clearance_mm": 0.2,
                    "overlap": False,
                    "pad_a_center": [32.0, 45.6],
                    "pad_b_center": [32.6, 45.9],
                },
                {
                    "pad_a": "D1:1", "pad_b": "U1:25",
                    "net_a": "PIEZO_ADC", "net_b": "IO0",
                    "gap_mm": 0.1, "min_clearance_mm": 0.2,
                    "overlap": False,
                    "pad_a_center": [50.0, 56.0],
                    "pad_b_center": [50.3, 56.2],
                },
            ],
            "violations_truncated": False,
            "summary": "3 pad clearance violation(s) across 2 footprint pair(s) (min_clearance=0.2mm)",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        result = fn(pcb_file)
        assert result["violation_count"] == 3
        assert result["footprint_pairs_affected"] == 2
        assert result["footprint_pair_summary"][0]["min_gap_mm"] == 0.05

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_pad_overlap_detected(self, mock_run, keepout_server, pcb_file):
        """When pads physically overlap, gap=0 and overlap=True."""
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 10,
            "min_clearance_mm": 0.2,
            "violation_count": 1,
            "footprint_pairs_affected": 1,
            "footprint_pair_summary": [
                {"ref_a": "R1", "ref_b": "U1", "pad_violations": 1, "min_gap_mm": 0.0},
            ],
            "violations": [
                {
                    "pad_a": "R1:1", "pad_b": "U1:4",
                    "net_a": "SDA", "net_b": "",
                    "gap_mm": 0.0, "min_clearance_mm": 0.2,
                    "overlap": True,
                    "pad_a_center": [32.0, 44.0],
                    "pad_b_center": [32.0, 44.0],
                },
            ],
            "violations_truncated": False,
            "summary": "1 pad clearance violation(s)",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        result = fn(pcb_file)
        assert result["violations"][0]["overlap"] is True
        assert result["violations"][0]["gap_mm"] == 0.0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_truncation(self, mock_run, keepout_server, pcb_file):
        """More than 50 violations should be truncated."""
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 200,
            "min_clearance_mm": 0.2,
            "violation_count": 75,
            "footprint_pairs_affected": 10,
            "footprint_pair_summary": [],
            "violations": [{"pad_a": f"R{i}:1", "pad_b": "U1:1"} for i in range(50)],
            "violations_truncated": True,
            "summary": "75 violations",
        }
        fn = _get_tool_fn(keepout_server, "check_pad_clearances")
        result = fn(pcb_file)
        assert result["violations_truncated"] is True
        assert len(result["violations"]) == 50
