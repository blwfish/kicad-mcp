"""
Tests for DRC tools and DRC history utilities.

Tests drc.py tools and utils/drc_history.py functions.
"""

import asyncio
import json
import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.drc import register_drc_tools
from kicad_mcp.utils.drc_history import (
    get_project_history_path,
    save_drc_result,
    get_drc_history,
    compare_with_previous,
)


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def drc_server():
    mcp = FastMCP("test-drc")
    register_drc_tools(mcp)
    return mcp


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- DRC history utility tests -----------------------------------------------

class TestGetProjectHistoryPath:

    def test_returns_path_with_hash(self):
        path = get_project_history_path("/home/user/my_board.kicad_pro")
        assert "my_board" in path
        assert path.endswith(".json")

    def test_different_projects_get_different_paths(self):
        p1 = get_project_history_path("/home/user/board1.kicad_pro")
        p2 = get_project_history_path("/home/user/board2.kicad_pro")
        assert p1 != p2


class TestSaveDrcResult:

    def test_saves_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        save_drc_result("/fake/project.kicad_pro", {
            "total_violations": 5,
            "violation_categories": {"clearance": 3, "unconnected": 2},
        })
        # Verify a history file was created
        files = list(tmp_path.glob("*.json"))
        assert len(files) == 1
        with open(files[0]) as f:
            data = json.load(f)
        assert len(data["entries"]) == 1
        assert data["entries"][0]["total_violations"] == 5

    def test_appends_to_existing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        save_drc_result(project, {"total_violations": 5, "violation_categories": {}})
        save_drc_result(project, {"total_violations": 3, "violation_categories": {}})
        files = list(tmp_path.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert len(data["entries"]) == 2

    def test_caps_at_10_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        for i in range(12):
            save_drc_result(project, {"total_violations": i, "violation_categories": {}})
        files = list(tmp_path.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert len(data["entries"]) <= 10


class TestGetDrcHistory:

    def test_no_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        entries = get_drc_history("/fake/no_history.kicad_pro")
        assert entries == []

    def test_returns_sorted_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        save_drc_result(project, {"total_violations": 10, "violation_categories": {}})
        save_drc_result(project, {"total_violations": 5, "violation_categories": {}})
        entries = get_drc_history(project)
        assert len(entries) == 2
        # Newest first
        assert entries[0]["total_violations"] == 5


class TestCompareWithPrevious:

    def test_no_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        result = compare_with_previous("/fake/project.kicad_pro",
                                        {"total_violations": 5})
        assert result is None

    def test_compares_violations(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        save_drc_result(project, {
            "total_violations": 10,
            "violation_categories": {"clearance": 5, "unconnected": 5},
        })
        save_drc_result(project, {
            "total_violations": 7,
            "violation_categories": {"clearance": 3, "unconnected": 4},
        })
        comparison = compare_with_previous(project, {
            "total_violations": 3,
            "violation_categories": {"clearance": 2, "shorting": 1},
        })
        assert comparison is not None
        assert comparison["current_violations"] == 3
        assert "resolved_categories" in comparison


# -- get_drc_history_tool tests ----------------------------------------------

class TestGetDrcHistoryTool:

    def test_project_not_found(self, drc_server):
        fn = _get_tool_fn(drc_server, "get_drc_history_tool")
        result = fn("/nonexistent/project.kicad_pro")
        assert result["success"] is False

    @patch("kicad_mcp.tools.drc.get_drc_history")
    def test_returns_history(self, mock_hist, drc_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_hist.return_value = [
            {"timestamp": 1000, "total_violations": 5},
            {"timestamp": 900, "total_violations": 10},
        ]
        fn = _get_tool_fn(drc_server, "get_drc_history_tool")
        result = fn(str(pro))
        assert result["success"] is True
        assert result["entry_count"] == 2
        assert result["trend"] == "improving"

    @patch("kicad_mcp.tools.drc.get_drc_history")
    def test_no_trend_with_single_entry(self, mock_hist, drc_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_hist.return_value = [{"timestamp": 1000, "total_violations": 5}]
        fn = _get_tool_fn(drc_server, "get_drc_history_tool")
        result = fn(str(pro))
        assert result["trend"] is None


# -- run_drc_check tests -----------------------------------------------------

class TestRunDrcCheck:

    def test_project_not_found(self, drc_server):
        fn = _get_tool_fn(drc_server, "run_drc_check")
        result = asyncio.run(fn("/nonexistent/project.kicad_pro", None))
        assert result["success"] is False

    def test_no_pcb_file(self, drc_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(drc_server, "run_drc_check")
        result = asyncio.run(fn(str(pro), None))
        assert result["success"] is False
        assert "PCB file not found" in result["error"]
