"""
Tests for PCB copper zone tools: add zones and fill zones.

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


# -- add_zone tests ----------------------------------------------------------

class TestAddZone:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_zone",
                    pcb_path="/nonexistent/board.kicad_pcb", net_name="GND")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_returns_zone_info(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "zone": {
                "net": "GND",
                "layer": "B.Cu",
                "corners": [[0, 0], [50, 0], [50, 30], [0, 30]],
                "clearance_mm": 0.3,
                "min_width_mm": 0.2,
                "connect_pads": "thermal",
                "priority": 0,
            },
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_zone",
                    pcb_path=pcb_file, net_name="GND", layer="B.Cu",
                    corners=[[0, 0], [50, 0], [50, 30], [0, 30]])
        assert result["status"] == "ok"
        assert result["zone"]["net"] == "GND"
        assert result["zone"]["layer"] == "B.Cu"
        assert len(result["zone"]["corners"]) == 4

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_auto_outline_when_no_corners(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "auto_outline": True,
            "note": "Zone corners auto-derived from board outline (Edge.Cuts)",
            "zone": {"net": "GND", "layer": "F.Cu", "corners": [],
                     "clearance_mm": 0.3, "min_width_mm": 0.2,
                     "connect_pads": "thermal", "priority": 0},
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_zone", pcb_path=pcb_file, net_name="GND")
        assert result["status"] == "ok"
        # Verify empty corners is passed (auto-outline in pcbnew script)
        params = mock_run.call_args[1]["params"]
        assert params["corners"] == []

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "zone": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("add_zone",
           pcb_path=pcb_file, net_name="VCC", layer="F.Cu",
           corners=[[0, 0], [10, 0], [10, 10], [0, 10]],
           zone_clearance_mm=0.5, min_width_mm=0.3,
           connect_pads="solid", priority=1)
        params = mock_run.call_args[1]["params"]
        assert params["net_name"] == "VCC"
        assert params["layer"] == "F.Cu"
        assert params["clearance_mm"] == 0.5
        assert params["min_width_mm"] == 0.3
        assert params["connect_pads"] == "solid"
        assert params["priority"] == 1

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_uses_60s_timeout(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "zone": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("add_zone", pcb_path=pcb_file, net_name="GND")
        assert mock_run.call_args[1]["timeout"] == 60.0


# -- fill_zones tests --------------------------------------------------------

class TestFillZones:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("fill_zones", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_returns_fill_results(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "fill_success": True,
            "zones_filled": 2,
            "zones": [
                {"net": "GND", "layer": "B.Cu", "filled": True,
                 "filled_area_mm2": 1500.0},
                {"net": "VCC", "layer": "F.Cu", "filled": True,
                 "filled_area_mm2": 800.0},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("fill_zones", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["zones_filled"] == 2
        assert result["fill_success"] is True

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_no_zones_to_fill(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "message": "No copper zones to fill",
            "zones_filled": 0,
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("fill_zones", pcb_path=pcb_file)
        assert result["zones_filled"] == 0

    @patch("kicad_mcp.tools.pcb_zones.run_pcbnew_script")
    def test_uses_60s_timeout(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "zones_filled": 0}
        fn = _get_pcb_fn(pcb_server)
        fn("fill_zones", pcb_path=pcb_file)
        assert mock_run.call_args[1]["timeout"] == 60.0


# ---------------------------------------------------------------------------
# Real-KiCad regression: add_zone should not depend on refilling every prior
# zone to leave each zone correctly filled.
# ---------------------------------------------------------------------------

@pytest.mark.requires_kicad
class TestAddZoneFillsOnlyItsOwnZoneIntegration:
    """finding #21 (Phase 1, 2026-09-23 full review): _op_add_zone called
    filler.Fill(board.Zones()) -- refilling EVERY zone already on the board,
    not just the one this call added -- O(N^2) as zones accumulate one
    add_zone call at a time. Fixed to filler.Fill([zone]) (fills only the
    new zone; _op_fill_zones remains the explicit "refill everything"
    operation). This test pins the CORRECTNESS side (every zone added still
    ends up filled) since the O(N^2) claim itself is a timing property, not
    independently benchmarked here."""

    @pytest.fixture(autouse=True)
    def skip_if_unavailable(self):
        from .conftest import pcbnew_available
        if not pcbnew_available():
            pytest.skip("pcbnew not importable under KiCad's Python")

    _COUNT_FILLED_SCRIPT = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])
if board is None:
    print(json.dumps({"error": "load failed"}))
    sys.exit(0)

filled = [z.IsFilled() for z in board.Zones()]
print(json.dumps({"status": "ok", "filled": filled, "count": len(filled)}))
"""

    def test_each_added_zone_ends_up_filled(self, tmp_path):
        from kicad_mcp.tools.pcb_board import _op_create
        from kicad_mcp.tools.pcb_nets import _op_add_net
        from kicad_mcp.tools.pcb_zones import _op_add_zone
        from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

        pcb_path = str(tmp_path / "zones_test.kicad_pcb")
        assert _op_create(pcb_path).get("status") == "ok"
        assert _op_add_net(pcb_path, "GND").get("status") == "ok"

        # Three non-overlapping zones added one at a time (same net -- a
        # second net with nothing yet connected to it gets pruned by
        # pcbnew's own board.Save() between calls, unrelated to this fix) --
        # each add_zone call must leave EVERY zone filled, not just the one
        # it happened to touch last.
        r1 = _op_add_zone(pcb_path, net_name="GND", layer="F.Cu",
                          corners=[[0, 0], [10, 0], [10, 10], [0, 10]])
        assert r1.get("status") == "ok", r1
        r2 = _op_add_zone(pcb_path, net_name="GND", layer="B.Cu",
                          corners=[[20, 0], [30, 0], [30, 10], [20, 10]])
        assert r2.get("status") == "ok", r2
        r3 = _op_add_zone(pcb_path, net_name="GND", layer="F.Cu",
                          corners=[[0, 20], [10, 20], [10, 30], [0, 30]])
        assert r3.get("status") == "ok", r3

        check = run_pcbnew_script(self._COUNT_FILLED_SCRIPT, params={"pcb_path": pcb_path})
        assert check["count"] == 3, check
        assert all(check["filled"]), check
