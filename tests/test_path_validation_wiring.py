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

    def test_circuit_patterns_rejects_out_of_root_path_under_strict_mode(self, tmp_path, monkeypatch):
        """Regression: patterns.py's _op_identify_circuit_patterns used
        os.path.exists() only -- unlike every _op_* in the pcb_*.py
        routers, it never went through validate_project_path, bypassing
        the traversal defense entirely."""
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        outsider = tmp_path / "outsider"
        outsider.mkdir(exist_ok=True)
        target = outsider / "board.kicad_sch"
        target.write_text('(kicad_sch (version 20230121))\n')

        mcp = create_server()
        fn = _get_tool_fn(mcp, "analyze")
        result = asyncio.run(fn(operation="circuit_patterns", ctx=None, schematic_path=str(target)))

        assert "error" in result
        assert "outside the configured" in result["error"]

    def test_netlist_rejects_out_of_root_path_under_strict_mode(self, tmp_path, monkeypatch):
        """Regression: netlist.py had 3 separate os.path.exists()-only
        entry points (the main netlist-extraction dispatch, connection
        analysis, and find_component_connections) bypassing the same
        traversal defense."""
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        outsider = tmp_path / "outsider"
        outsider.mkdir(exist_ok=True)
        target = outsider / "board.kicad_sch"
        target.write_text('(kicad_sch (version 20230121))\n')

        mcp = create_server()
        fn = _get_tool_fn(mcp, "analyze")
        result = asyncio.run(fn(operation="netlist", ctx=None, path=str(target)))

        assert "error" in result
        assert "outside the configured" in result["error"]

    def test_pcb_create_rejects_out_of_root_path_under_strict_mode(self, tmp_path, monkeypatch):
        """Regression: _op_create (unlike every other _op_* in pcb_board.py)
        never called validate_project_path at all -- a new board could be
        created outside the configured roots even under strict mode."""
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        outsider = tmp_path / "outsider"
        outsider.mkdir(exist_ok=True)
        target = str(outsider / "new.kicad_pcb")  # doesn't exist yet -- this is a create

        mcp = create_server()
        fn = _get_tool_fn(mcp, "pcb")
        result = fn("create", pcb_path=target)

        assert "error" in result
        assert "outside the configured" in result["error"]
