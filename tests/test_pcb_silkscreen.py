"""
Tests for PCB silkscreen tools: add text, list items, update items,
check overlaps, auto-fix.

Unit tests mock run_pcbnew_script.
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


# -- add_text tests ----------------------------------------------------------

class TestAddText:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_text",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    text="Hello", x_mm=100, y_mm=80)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_adds_text(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "text": "Rev 1.0",
            "x_mm": 110.0,
            "y_mm": 115.0,
            "layer": "F.SilkS",
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_text",
                    pcb_path=pcb_file, text="Rev 1.0", x_mm=110, y_mm=115)
        assert result["status"] == "ok"
        assert result["text"] == "Rev 1.0"

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "text": "X", "x_mm": 0,
                                  "y_mm": 0, "layer": "B.SilkS"}
        fn = _get_pcb_fn(pcb_server)
        fn("add_text",
           pcb_path=pcb_file, text="X", x_mm=10, y_mm=20,
           layer="B.SilkS", text_size_mm=2.0,
           thickness_mm=0.2, rotation_deg=45)
        params = mock_run.call_args[1]["params"]
        assert params["text"] == "X"
        assert params["layer"] == "B.SilkS"
        assert params["size_mm"] == 2.0
        assert params["thickness_mm"] == 0.2
        assert params["rotation_deg"] == 45


# -- list_silkscreen tests ---------------------------------------------------

class TestListSilkscreen:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("list_silkscreen", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_lists_items(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "item_count": 3,
            "items": [
                {"type": "reference", "component": "R1", "text": "R1",
                 "visible": True, "layer": "F.SilkS",
                 "x_mm": 100, "y_mm": 80, "size_mm": 1.0},
                {"type": "value", "component": "R1", "text": "10k",
                 "visible": False, "layer": "F.Fab",
                 "x_mm": 100, "y_mm": 81, "size_mm": 1.0},
                {"type": "standalone", "component": None, "text": "Rev 1.0",
                 "visible": True, "layer": "F.SilkS",
                 "x_mm": 110, "y_mm": 115, "size_mm": 1.0},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("list_silkscreen", pcb_path=pcb_file)
        assert result["item_count"] == 3
        refs = [i for i in result["items"] if i["type"] == "reference"]
        assert len(refs) == 1


# -- update_silkscreen tests -------------------------------------------------

class TestUpdateSilkscreen:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("update_silkscreen",
                    pcb_path="/nonexistent/board.kicad_pcb", reference="R1")
        # Should return error (either file not found or no modifications)
        assert "error" in result

    def test_invalid_field(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("update_silkscreen",
                    pcb_path=pcb_file, reference="R1", silk_field="invalid")
        assert "error" in result
        assert "reference" in result["error"] or "value" in result["error"]

    def test_no_modifications(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("update_silkscreen",
                    pcb_path=pcb_file, reference="R1")
        assert "error" in result
        assert "No modifications" in result["error"]

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_update_visibility(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "reference": "R1",
            "field": "reference",
            "changes": {"visible": False},
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("update_silkscreen",
                    pcb_path=pcb_file, reference="R1", visible=False)
        assert result["status"] == "ok"


# -- check_silkscreen_overlaps tests -----------------------------------------

class TestCheckSilkscreenOverlaps:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("check_silkscreen_overlaps",
                    pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_no_overlaps(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "overlap_count": 0,
            "overlaps": [],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("check_silkscreen_overlaps", pcb_path=pcb_file)
        assert result["overlap_count"] == 0

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_reports_overlaps(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "overlap_count": 2,
            "overlaps": [
                {"item1": "R1 ref", "item2": "R2 ref", "type": "text-text"},
                {"item1": "C1 ref", "item2": "pad U1:1", "type": "text-pad"},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("check_silkscreen_overlaps", pcb_path=pcb_file)
        assert result["overlap_count"] == 2


# -- auto_fix_silkscreen tests -----------------------------------------------

class TestAutoFixSilkscreen:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("auto_fix_silkscreen", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_fixes_overlaps(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "overlaps_before": 5,
            "overlaps_after": 1,
            "fixes_applied": 4,
            "fixes": [
                {"reference": "R1", "action": "moved", "offset_mm": 0.5},
                {"reference": "R2", "action": "hidden", "reason": "persistent overlap"},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("auto_fix_silkscreen", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["fixes_applied"] == 4


# -- edit_text tests --------------------------------------------------------

class TestEditText:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_text", pcb_path="/nonexistent/board.kicad_pcb",
                    text="Rev 1.0", new_text="Rev 2.0")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_silkscreen.run_pcbnew_script")
    def test_edits_text(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "old_text": "Rev 1.0",
            "new_text": "Rev 2.0",
            "items_updated": 1,
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("edit_text", pcb_path=pcb_file,
                    text="Rev 1.0", new_text="Rev 2.0")
        assert result["status"] == "ok"
