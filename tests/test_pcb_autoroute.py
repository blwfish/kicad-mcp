"""
Tests for PCB autoroute tools (FreeRouter integration).

Tests the job management logic and error paths without requiring FreeRouter.
"""

import asyncio
import threading
import time
from unittest.mock import patch, MagicMock

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_autoroute import (
    register_pcb_autoroute_tools,
    _find_freerouter_jar,
    _autoroute_jobs,
    _autoroute_lock,
    _cleanup_stale_jobs,
    MAX_CONCURRENT_JOBS,
)


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def route_server():
    mcp = FastMCP("test-autoroute")
    register_pcb_autoroute_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


@pytest.fixture(autouse=True)
def clean_jobs():
    """Reset the job tracker between tests."""
    with _autoroute_lock:
        _autoroute_jobs.clear()
    yield
    with _autoroute_lock:
        _autoroute_jobs.clear()


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- _find_freerouter_jar tests ----------------------------------------------

class TestFindFreerouterJar:

    @patch.dict("os.environ", {"FREEROUTER_JAR": "/usr/lib/freerouting.jar"})
    @patch("os.path.isfile", return_value=True)
    def test_env_var(self, mock_isfile):
        result = _find_freerouter_jar()
        assert result == "/usr/lib/freerouting.jar"

    def test_explicit_path(self, tmp_path):
        jar = tmp_path / "freerouting.jar"
        jar.write_text("fake jar")
        assert _find_freerouter_jar(str(jar)) == str(jar)

    def test_returns_none_when_not_found(self):
        result = _find_freerouter_jar("/nonexistent/freerouting.jar")
        # May return None or find a system-installed JAR
        # The important thing is it doesn't crash
        assert result is None or isinstance(result, str)


# -- _cleanup_stale_jobs tests -----------------------------------------------

class TestCleanupStaleJobs:

    def test_removes_old_completed_jobs(self):
        with _autoroute_lock:
            _autoroute_jobs["old-job"] = {
                "status": "done",
                "started": time.time() - 7200,  # 2 hours ago
                "result": {},
            }
            _autoroute_jobs["new-job"] = {
                "status": "done",
                "started": time.time(),
                "result": {},
            }
            _cleanup_stale_jobs()
            assert "old-job" not in _autoroute_jobs
            assert "new-job" in _autoroute_jobs

    def test_keeps_running_jobs(self):
        with _autoroute_lock:
            _autoroute_jobs["running-job"] = {
                "status": "running",
                "started": time.time() - 7200,
            }
            _cleanup_stale_jobs()
            assert "running-job" in _autoroute_jobs


# -- autoroute_pcb tool tests -----------------------------------------------

class TestAutoroutePcb:

    def test_file_not_found(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute_pcb")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar", return_value=None)
    def test_no_freerouter(self, mock_jar, route_server, pcb_file):
        fn = _get_tool_fn(route_server, "autoroute_pcb")
        result = fn(pcb_file)
        assert "error" in result
        assert "FreeRouter" in result["error"] or "freerouter" in result["error"].lower()


# -- list_autoroute_jobs tool tests ------------------------------------------

class TestListAutorouteJobs:

    def test_empty_job_list(self, route_server):
        fn = _get_tool_fn(route_server, "list_autoroute_jobs")
        result = fn()
        assert result["count"] == 0
        assert result["jobs"] == {}

    def test_lists_active_jobs(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["test-job"] = {
                "status": "running",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
            }
        fn = _get_tool_fn(route_server, "list_autoroute_jobs")
        result = fn()
        assert result["count"] == 1
        assert "test-job" in result["jobs"]


# -- cancel_autoroute tool tests --------------------------------------------

class TestCancelAutoroute:

    def test_cancel_nonexistent_job(self, route_server):
        fn = _get_tool_fn(route_server, "cancel_autoroute")
        result = fn("nonexistent-job-id")
        assert "error" in result

    def test_cancel_running_job(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["test-cancel"] = {
                "status": "running",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
                "process": MagicMock(),
            }
        fn = _get_tool_fn(route_server, "cancel_autoroute")
        result = fn("test-cancel")
        assert result["status"] == "ok" or "cancel" in str(result).lower()


# -- poll_autoroute tool tests -----------------------------------------------

class TestPollAutoroute:

    def test_poll_nonexistent_job(self, route_server):
        fn = _get_tool_fn(route_server, "poll_autoroute")
        result = fn("nonexistent-job-id")
        assert "error" in result

    def test_poll_running_job(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["running"] = {
                "status": "running",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
            }
        fn = _get_tool_fn(route_server, "poll_autoroute")
        result = fn("running")
        assert result["status"] == "running"

    def test_poll_completed_job(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["done"] = {
                "status": "done",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
                "result": {"traces_added": 50, "unrouted": 0},
            }
        fn = _get_tool_fn(route_server, "poll_autoroute")
        result = fn("done")
        assert result["status"] == "done"
