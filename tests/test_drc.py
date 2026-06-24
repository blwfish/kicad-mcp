"""
Tests for DRC tools and DRC history utilities.

Tests drc.py router and utils/drc_history.py functions.
"""

import asyncio
import json
from unittest.mock import patch

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


def test_history_path_is_deterministic_not_process_hash():
    """h-drc-hash: the history filename must be stable across processes. Built-in
    hash() is per-process randomized for strings, so history was lost on every
    restart. Pin it to the deterministic sha1 so a revert to hash() fails here."""
    import hashlib
    p = "/projects/demo/board.kicad_pro"
    assert get_project_history_path(p) == get_project_history_path(p)   # deterministic
    expected = hashlib.sha1(p.encode("utf-8")).hexdigest()[:8]
    assert expected in get_project_history_path(p)                      # sha1, not hash()
    assert "board.kicad_pro" in get_project_history_path(p)


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

    def test_nine_entries_all_kept(self, tmp_path, monkeypatch):
        """Just-below boundary: 9 entries saved → all 9 kept (no trim)."""
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        for i in range(9):
            save_drc_result(project, {"total_violations": i, "violation_categories": {}})
        files = list(tmp_path.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert len(data["entries"]) == 9

    def test_exactly_10_entries_all_kept(self, tmp_path, monkeypatch):
        """At boundary: exactly 10 entries saved → all 10 kept."""
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        for i in range(10):
            save_drc_result(project, {"total_violations": i, "violation_categories": {}})
        files = list(tmp_path.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert len(data["entries"]) == 10

    def test_11_entries_trimmed_to_10(self, tmp_path, monkeypatch):
        """Just-above boundary: 11 entries saved → trimmed to exactly 10."""
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        project = "/fake/project.kicad_pro"
        for i in range(11):
            save_drc_result(project, {"total_violations": i, "violation_categories": {}})
        files = list(tmp_path.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)
        assert len(data["entries"]) == 10

    def test_12_entries_newest_10_kept_oldest_dropped(self, tmp_path, monkeypatch):
        """Verify 'drop oldest' ordering: after 12 saves, the first write is gone
        and the last 10 (violations 2..11) are retained.

        We patch `time.time` in the drc_history module so each call returns a
        strictly-incrementing timestamp, making entry ordering deterministic.
        """
        monkeypatch.setattr("kicad_mcp.utils.drc_history.DRC_HISTORY_DIR", str(tmp_path))
        import kicad_mcp.utils.drc_history as drc_hist_mod

        project = "/fake/project.kicad_pro"
        call_counter = [0]
        base_time = 1_000_000.0

        def fake_time():
            t = base_time + call_counter[0]
            call_counter[0] += 1
            return t

        monkeypatch.setattr(drc_hist_mod.time, "time", fake_time)

        for i in range(12):
            save_drc_result(project, {"total_violations": i, "violation_categories": {}})

        files = list(tmp_path.glob("*.json"))
        with open(files[0]) as f:
            data = json.load(f)

        violations = {e["total_violations"] for e in data["entries"]}
        assert len(data["entries"]) == 10
        # The very first save (violations=0, lowest timestamp) must be dropped
        assert 0 not in violations
        # The last 10 saves (violations 2..11) must all be present
        assert violations == set(range(2, 12))


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


# -- drc router: history operation tests -------------------------------------

class TestDrcHistoryOperation:

    def test_project_not_found(self, drc_server):
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("history", None, project_path="/nonexistent/project.kicad_pro"))
        assert result["success"] is False

    def test_requires_project_path(self, drc_server):
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("history", None))
        assert "error" in result
        assert "project_path" in result["error"]

    @patch("kicad_mcp.tools.drc.get_drc_history")
    def test_returns_history(self, mock_hist, drc_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_hist.return_value = [
            {"timestamp": 1000, "total_violations": 5},
            {"timestamp": 900, "total_violations": 10},
        ]
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("history", None, project_path=str(pro)))
        assert result["success"] is True
        assert result["entry_count"] == 2
        assert result["trend"] == "improving"

    @patch("kicad_mcp.tools.drc.get_drc_history")
    def test_no_trend_with_single_entry(self, mock_hist, drc_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_hist.return_value = [{"timestamp": 1000, "total_violations": 5}]
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("history", None, project_path=str(pro)))
        assert result["trend"] is None


# -- drc router: run operation tests -----------------------------------------

class TestDrcRunOperation:

    def test_project_not_found(self, drc_server):
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("run", None, project_path="/nonexistent/project.kicad_pro"))
        assert result["success"] is False

    def test_requires_project_path(self, drc_server):
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("run", None))
        assert "error" in result
        assert "project_path" in result["error"]

    def test_no_pcb_file(self, drc_server, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("run", None, project_path=str(pro)))
        assert result["success"] is False
        assert "PCB file not found" in result["error"]


# -- drc router: unknown operation -------------------------------------------

class TestDrcUnknownOperation:

    def test_unknown_op(self, drc_server):
        fn = _get_tool_fn(drc_server, "drc")
        result = asyncio.run(fn("bogus", None))
        assert "error" in result
        assert "bogus" in result["error"]
        assert "run|autofix|history" in result["error"]
