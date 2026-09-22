"""End-to-end proof that pcb()'s write-lock wiring (pcb.py's _MUTATING_OPS +
_dispatch closure) actually reaches through the real tool dispatch: a
mutating operation is rejected when the lock is already held, and a
read-only operation is NOT -- serializing reads too would defeat
AGENT-INSTRUCTIONS.md's "read-only calls are safe to run in parallel"
design. See utils/pcb_lock.py for what this does and does not cover.
"""
import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb import register_pcb_tools
from kicad_mcp.utils.pcb_lock import pcb_write_lock


@pytest.fixture
def pcb_server():
    mcp = FastMCP("test-pcb-lock")
    register_pcb_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


def _get_pcb_fn(mcp_server):
    tool = asyncio.run(mcp_server.get_tool("pcb"))
    assert tool is not None
    return tool.fn


class TestMutatingOpsRespectTheLock:
    def test_add_net_rejected_while_lock_held(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        with pcb_write_lock(pcb_file) as held:
            assert held is True
            with patch("kicad_mcp.tools.pcb_nets.run_pcbnew_script") as mock_run:
                result = fn("add_net", pcb_path=pcb_file, net_name="VCC")
                mock_run.assert_not_called()

        assert result["status"] == "error"
        assert "already in progress" in result["error"]

    def test_place_footprint_rejected_while_lock_held(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        with pcb_write_lock(pcb_file) as held:
            assert held is True
            with patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script") as mock_run:
                result = fn(
                    "place_footprint", pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0603",
                    reference="R1", value="10k", x_mm=10, y_mm=10,
                )
                mock_run.assert_not_called()

        assert result["status"] == "error"
        assert "already in progress" in result["error"]

    def test_lock_released_after_a_call_completes(self, pcb_server, pcb_file):
        """The lock must not leak across calls, regardless of whether the
        call's own business logic succeeded -- pcb_write_lock always
        releases in its own finally, independent of _dispatch()'s result."""
        fn = _get_pcb_fn(pcb_server)
        with patch("kicad_mcp.tools.pcb_nets.run_pcbnew_script", return_value={"status": "ok"}):
            fn("add_net", pcb_path=pcb_file, net_name="VCC")

        with pcb_write_lock(pcb_file) as held:
            assert held is True  # not still held by the first call


class TestReadOnlyOpsIgnoreTheLock:
    """Read-only operations must proceed even while another call holds the
    write-lock -- this is the entire point of distinguishing them."""

    def test_list_nets_proceeds_while_lock_held(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        with pcb_write_lock(pcb_file) as held:
            assert held is True
            with patch(
                "kicad_mcp.tools.pcb_nets.run_pcbnew_script",
                return_value={"status": "ok", "nets": []},
            ) as mock_run:
                result = fn("list_nets", pcb_path=pcb_file)
                mock_run.assert_called_once()

        assert "error" not in result

    def test_load_proceeds_while_lock_held(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        with pcb_write_lock(pcb_file) as held:
            assert held is True
            with patch(
                "kicad_mcp.tools.pcb_board.run_pcbnew_script",
                return_value={"status": "ok"},
            ) as mock_run:
                result = fn("load", pcb_path=pcb_file)
                mock_run.assert_called_once()

        assert "error" not in result
