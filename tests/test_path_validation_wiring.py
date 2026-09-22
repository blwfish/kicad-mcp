"""End-to-end proof that validate_project_path's STRICT-mode traversal
rejection (not just its must_exist check, which the existing per-tool
"file not found" tests already exercise) actually reaches through a real
tool call, for a representative sample of the 12 files newly wired to it.

Every mutating PCB tool previously gated only on os.path.exists(pcb_path)
-- validate_project_path's must_exist check produces the same visible
"not found" behavior (already covered by each tool's own existing tests,
all still passing), but its traversal/out-of-root rejection under
KICAD_MCP_STRICT_PATHS=1 was never exercised through any tool before this
wiring existed. This file closes that specific gap.
"""
import asyncio

from kicad_mcp.server import create_server

from tests.test_path_validation import _isolated_roots


def _get_tool_fn(mcp_server, name):
    tool = asyncio.run(mcp_server.get_tool(name))
    assert tool is not None, f"tool {name!r} not registered"
    return tool.fn


class TestStrictModeRejectionReachesRealTools:
    """A handful of representative tools (one per newly-wired file) proving
    the STRICT-mode branch -- not just must_exist -- is really reachable
    through the registered MCP tool, not just present as an unused import."""

    def _outside_path(self, tmp_path):
        outsider = tmp_path / "outsider"
        outsider.mkdir(exist_ok=True)
        f = outsider / "board.kicad_pcb"
        f.write_text("")
        return str(f)

    def test_pcb_load_rejects_out_of_root_path_under_strict_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        target = self._outside_path(tmp_path)

        mcp = create_server()
        fn = _get_tool_fn(mcp, "pcb")
        result = fn("load", pcb_path=target)

        assert "error" in result
        assert "outside the configured" in result["error"]

    def test_pcb_load_allows_same_path_when_strict_mode_off(self, tmp_path, monkeypatch):
        """Same out-of-root path, strict mode unset -- must NOT be rejected
        (warn-only default), proving this isn't just accidentally always
        erroring regardless of the env var."""
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        _isolated_roots(monkeypatch, tmp_path)
        target = self._outside_path(tmp_path)

        mcp = create_server()
        fn = _get_tool_fn(mcp, "pcb")
        result = fn("load", pcb_path=target)

        # Warn-only: validate_project_path returns None (no rejection), so
        # this reaches run_pcbnew_script and fails for an unrelated reason
        # (no real .kicad_pcb content / no KiCad Python in this env) --
        # the point is it's NOT the path-validation error.
        if "error" in result:
            assert "outside the configured" not in result["error"]

    def test_autoroute_run_rejects_out_of_root_path_under_strict_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        target = self._outside_path(tmp_path)

        mcp = create_server()
        fn = _get_tool_fn(mcp, "autoroute")
        result = fn("run", pcb_path=target)

        assert "error" in result
        assert "outside the configured" in result["error"]

    def test_export_rejects_out_of_root_path_under_strict_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        target = self._outside_path(tmp_path)

        mcp = create_server()
        fn = _get_tool_fn(mcp, "export")
        result = asyncio.run(fn("gerbers", None, pcb_path=target))

        assert "error" in result
        assert "outside the configured" in result["error"]
