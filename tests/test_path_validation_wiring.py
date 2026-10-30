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


class TestProjectPathEntryPointsHonourStrictMode:
    """Regression (2026-09-23 review finding #22, remainder): bom.py (2
    entry points), drc.py (run, history), export.py (thumbnail) and
    pcb_drc_fix.py (autofix's project_path) still gated on a bare
    os.path.exists(project_path), bypassing KICAD_MCP_STRICT_PATHS even after
    the netlist/patterns/pcb_pipeline entry points were wired up."""

    def _outside_project(self, tmp_path):
        outsider = tmp_path / "outsider"
        outsider.mkdir(exist_ok=True)
        pro = outsider / "board.kicad_pro"
        pro.write_text("{}")
        return str(pro)

    def _call(self, tool, **kwargs):
        fn = _get_tool_fn(create_server(), tool)
        result = fn(**kwargs)
        return asyncio.run(result) if asyncio.iscoroutine(result) else result

    def _strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        _isolated_roots(monkeypatch, tmp_path)
        return self._outside_project(tmp_path)

    def test_analyze_bom_rejects_out_of_root_project(self, tmp_path, monkeypatch):
        target = self._strict(tmp_path, monkeypatch)
        r = self._call("analyze", operation="bom", ctx=None, project_path=target)
        assert r["status"] == "error"
        assert "outside the configured" in r["error"]

    def test_export_bom_csv_rejects_out_of_root_project(self, tmp_path, monkeypatch):
        target = self._strict(tmp_path, monkeypatch)
        r = self._call("export", operation="bom_csv", ctx=None, project_path=target)
        assert r["status"] == "error"
        assert "outside the configured" in r["error"]

    def test_export_thumbnail_rejects_out_of_root_project(self, tmp_path, monkeypatch):
        target = self._strict(tmp_path, monkeypatch)
        r = self._call("export", operation="thumbnail", ctx=None, project_path=target)
        assert "outside the configured" in r["error"]

    def test_drc_run_rejects_out_of_root_project(self, tmp_path, monkeypatch):
        target = self._strict(tmp_path, monkeypatch)
        r = self._call("drc", operation="run", ctx=None, project_path=target)
        assert r["status"] == "error"
        assert "outside the configured" in r["error"]

    def test_drc_history_rejects_out_of_root_project(self, tmp_path, monkeypatch):
        target = self._strict(tmp_path, monkeypatch)
        r = self._call("drc", operation="history", ctx=None, project_path=target)
        assert r["status"] == "error"
        assert "outside the configured" in r["error"]

    def test_drc_autofix_rejects_out_of_root_project_path(self, tmp_path, monkeypatch):
        """pcb_path is in-root and valid; only project_path is outside."""
        target = self._strict(tmp_path, monkeypatch)
        pcb = tmp_path / "kicad_root" / "board.kicad_pcb"
        pcb.write_text("")
        r = self._call("drc", operation="autofix", ctx=None,
                       pcb_path=str(pcb), project_path=target)
        assert "outside the configured" in r["error"]
        assert "Provide project_path explicitly" in r["error"]

    def test_drc_history_not_rejected_when_strict_mode_off(self, tmp_path, monkeypatch):
        """Same out-of-root path, strict unset: warn-only, so the traversal
        error must NOT appear (the existing must_exist/"not found" behaviour
        is unchanged)."""
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        _isolated_roots(monkeypatch, tmp_path)
        target = self._outside_project(tmp_path)
        r = self._call("drc", operation="history", ctx=None, project_path=target)
        assert "outside the configured" not in str(r)

    def test_missing_project_still_reports_not_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        _isolated_roots(monkeypatch, tmp_path)
        missing = str(tmp_path / "nope" / "x.kicad_pro")
        for tool, op in (("analyze", "bom"), ("export", "bom_csv"),
                         ("export", "thumbnail"), ("drc", "run"), ("drc", "history")):
            r = self._call(tool, operation=op, ctx=None, project_path=missing)
            assert "not found" in r["error"].lower(), (tool, op, r)
