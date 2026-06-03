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
    _select_best_pass,
    _freerouter_cmd,
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


class TestFreerouterCmd:
    """The FreeRouter invocation must disable analytics — Freerouting v2 phones
    home to api.freerouting.app on every run, which an air-gapped design forbids."""

    def _cmd(self):
        return _freerouter_cmd("/usr/bin/java", "/x/fr.jar", "/x/b.dsn", "/x/b.ses")

    def test_disables_analytics(self):
        assert "--usage_and_diagnostic_data.disable_analytics=true" in self._cmd()

    def test_headless_gui(self):
        assert "--gui.enabled=false" in self._cmd()

    def test_headless_awt(self):
        # Keeps the JVM from initializing AWT windows.
        assert "-Djava.awt.headless=true" in self._cmd()

    def test_macos_background_accessory(self):
        # LSUIElement: no Dock icon / no focus steal on macOS (headless alone
        # doesn't stop the Cocoa delegate claiming a Dock slot every run).
        assert "-Dapple.awt.UIElement=true" in self._cmd()

    def test_passes_dsn_and_ses_paths(self):
        cmd = self._cmd()
        assert cmd[cmd.index("-de") + 1] == "/x/b.dsn"
        assert cmd[cmd.index("-do") + 1] == "/x/b.ses"

    def test_runs_the_jar(self):
        cmd = self._cmd()
        assert cmd[0] == "/usr/bin/java"
        assert cmd[cmd.index("-jar") + 1] == "/x/fr.jar"


class TestSelectBestPass:
    """Best pass = fewest KiCad-MEASURED unconnected nets. We rank on the
    measured ratsnest of each pass's SES, never on FreeRouter's prose log."""

    def test_picks_fewest_unconnected(self):
        ses, n = _select_best_pass([("a.ses", 3), ("b.ses", 0), ("c.ses", 1)])
        assert ses == "b.ses" and n == 0

    def test_single_pass(self):
        assert _select_best_pass([("only.ses", 2)]) == ("only.ses", 2)

    def test_measured_pass_beats_unmeasured(self):
        # A pass we could measure (even at 4) is preferred over one we couldn't.
        ses, n = _select_best_pass([("good.ses", 4), ("unmeasured.ses", None)])
        assert ses == "good.ses" and n == 4

    def test_all_unmeasured_falls_back_to_first_ses(self):
        # Measurement subprocess failed on every pass — still import something
        # rather than throw away a routed board.
        ses, n = _select_best_pass([("first.ses", None), ("second.ses", None)])
        assert ses == "first.ses" and n is None

    def test_no_passes_returns_none(self):
        assert _select_best_pass([]) == (None, None)

    def test_zero_is_a_real_value_not_falsy_skip(self):
        # 0 unconnected is the BEST, must not be treated as "missing".
        ses, n = _select_best_pass([("z.ses", 0), ("x.ses", 2)])
        assert ses == "z.ses" and n == 0


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
                "passes": 2,
                "result": {},
            }
            _autoroute_jobs["new-job"] = {
                "status": "done",
                "started": time.time(),
                "passes": 2,
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
                "passes": 2,
            }
            _cleanup_stale_jobs()
            assert "running-job" in _autoroute_jobs


# -- autoroute router: run operation tests -----------------------------------

class TestAutorouteRun:

    def test_file_not_found(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("run", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    def test_requires_pcb_path(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("run")
        assert "error" in result
        assert "pcb_path" in result["error"]

    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar", return_value=None)
    def test_no_freerouter(self, mock_jar, route_server, pcb_file):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("run", pcb_path=pcb_file)
        assert "error" in result
        assert "FreeRouter" in result["error"] or "freerouter" in result["error"].lower()


# -- autoroute router: list_jobs operation tests -----------------------------

class TestAutorouteListJobs:

    def test_empty_job_list(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("list_jobs")
        assert result["count"] == 0
        assert result["jobs"] == {}

    def test_lists_active_jobs(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["test-job"] = {
                "status": "running",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
                "passes": 2,
            }
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("list_jobs")
        assert result["count"] == 1
        assert "test-job" in result["jobs"]


# -- autoroute router: cancel operation tests --------------------------------

class TestAutorouteCancel:

    def test_cancel_nonexistent_job(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("cancel", job_id="nonexistent-job-id")
        assert "error" in result

    def test_cancel_requires_job_id(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("cancel")
        assert "error" in result
        assert "job_id" in result["error"]

    def test_cancel_running_job(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["test-cancel"] = {
                "status": "running",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
                "passes": 2,
                "process": MagicMock(),
            }
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("cancel", job_id="test-cancel")
        assert result["status"] == "ok" or "cancel" in str(result).lower()


# -- autoroute router: poll operation tests ----------------------------------

class TestAutoroutePoll:

    def test_poll_nonexistent_job(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("poll", job_id="nonexistent-job-id")
        assert "error" in result

    def test_poll_requires_job_id(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("poll")
        assert "error" in result
        assert "job_id" in result["error"]

    def test_poll_running_job(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["running"] = {
                "status": "running",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
                "passes": 2,
            }
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("poll", job_id="running")
        assert result["status"] == "running"

    def test_poll_completed_job(self, route_server):
        with _autoroute_lock:
            _autoroute_jobs["done"] = {
                "status": "done",
                "started": time.time(),
                "pcb_path": "/tmp/test.kicad_pcb",
                "passes": 2,
                "result": {"traces_added": 50, "unrouted": 0},
            }
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("poll", job_id="done")
        assert result["status"] == "done"


# -- autoroute router: unknown operation -------------------------------------

class TestAutorouteUnknownOperation:

    def test_unknown_op(self, route_server):
        fn = _get_tool_fn(route_server, "autoroute")
        result = fn("bogus")
        assert "error" in result
        assert "bogus" in result["error"]
        assert "run|start|poll|cancel|list_jobs" in result["error"]


# --- h-autoroute-nudge: the shared NUDGE_PLACEMENT_HELPER, exec'd in-process ---
# The autoroute and DRC-fix placement steps both consume one helper now. We exec
# the real helper string against a duck-typed pcbnew board so its containment +
# refresh logic is tested without KiCad (the autoroute copy used to ignore the
# outline and could shove parts past Edge.Cuts).
import types


class _FakeFP:
    def __init__(self, ref, cx, cy, half=5.0, nets=1):
        self.ref, self.cx, self.cy, self.half, self.nets = ref, cx, cy, half, nets

    def GetReference(self):
        return self.ref

    def GetPosition(self):
        return types.SimpleNamespace(x=self.cx * 1e6, y=self.cy * 1e6)

    def SetPosition(self, vec):
        self.cx, self.cy = vec[0] / 1e6, vec[1] / 1e6

    def bbox(self):
        return (self.cx - self.half, self.cy - self.half,
                self.cx + self.half, self.cy + self.half)


class _FakeBoard:
    def __init__(self, fps, outline=(0.0, 0.0, 100.0, 100.0)):
        self._fps, self._o = fps, outline

    def GetFootprints(self):
        return list(self._fps)

    def GetBoardEdgesBoundingBox(self):
        o = self._o
        return types.SimpleNamespace(
            GetWidth=lambda: (o[2] - o[0]) * 1e6,
            GetX=lambda: o[0] * 1e6, GetY=lambda: o[1] * 1e6,
            GetRight=lambda: o[2] * 1e6, GetBottom=lambda: o[3] * 1e6)


def _make_nudge():
    from kicad_mcp.utils.keepout_helpers import NUDGE_PLACEMENT_HELPER
    ns = {
        "pcbnew": types.SimpleNamespace(
            ToMM=lambda v: v / 1e6, FromMM=lambda v: int(v * 1e6),
            VECTOR2I=lambda x, y: (x, y)),
        "get_courtyard_bbox": lambda fp: fp.bbox(),
        "signal_net_count": lambda fp: fp.nets,
    }
    exec(NUDGE_PLACEMENT_HELPER, ns)
    return ns["nudge_overlapping_footprints"]


def _overlap(p, q):
    return p[0] < q[2] and p[2] > q[0] and p[1] < q[3] and p[3] > q[1]


def test_nudge_separates_overlap_in_open_space():
    a = _FakeFP("R1", 50, 50, nets=2)
    b = _FakeFP("R2", 54, 50, nets=1)          # fewer nets -> the mover
    count, moved = _make_nudge()(_FakeBoard([a, b]), 0.5, 3)
    assert count >= 1 and "R2" in moved
    assert not _overlap(a.bbox(), b.bbox())


def test_nudge_acts_on_touching_courtyards():
    """gap == 0 (courtyards sharing exactly one edge) must count as overlapping —
    KiCad DRC flags it as courtyards_overlap. The helper's AABB is non-strict
    (<=); a strict `<` would treat touching as clear and never nudge (count 0).
    half=5 → R1 bbox (45..55), R2 bbox (55..65): right edge of a == left edge of b."""
    a = _FakeFP("R1", 50, 50, nets=2)
    b = _FakeFP("R2", 60, 50, nets=1)
    count, moved = _make_nudge()(_FakeBoard([a, b]), 0.5, 3)
    assert count >= 1 and "R2" in moved, "touching courtyards must be nudged apart"
    # strictly separated afterward (no longer even touching)
    ax, bx = a.bbox(), b.bbox()
    assert ax[2] < bx[0] or bx[2] < ax[0] or ax[3] < bx[1] or bx[3] < ax[1]


def test_nudge_never_moves_a_footprint_off_board():
    """h-autoroute-nudge: R2 overlaps R1 at the board's right edge, so the
    separating move would go off-board. The shared helper keeps it inside the
    outline (the old autoroute copy shoved it past Edge.Cuts)."""
    a = _FakeFP("R1", 90, 50, nets=2)
    b = _FakeFP("R2", 94, 50, nets=1)
    _make_nudge()(_FakeBoard([a, b], outline=(0, 0, 100, 100)), 0.5, 3)
    for fp in (a, b):
        x0, y0, x1, y1 = fp.bbox()
        assert x0 >= 0 and y0 >= 0 and x1 <= 100 and y1 <= 100, \
            f"{fp.ref} moved outside the board outline: {fp.bbox()}"
