"""Tests for _op_autofix's routing-fix orchestration (pcb_drc_fix.py).

Distinct from test_pcb_drc_fix.py, which covers the AABB/silkscreen
geometry boundary cases. This file covers a different bug class: _op_autofix
clears all existing tracks/vias and saves BEFORE calling _run_full_autoroute
to re-route. If that re-autoroute then fails, the board is left with NO
routing at all -- strictly worse than the input -- and the old code reported
this as "re-autorouted (N passes, ? unconnected)", indistinguishable from a
real (if incomplete) success. These tests pin that a failed re-autoroute is
reported plainly, with status="warning" and routing_regressed=True, not
silently folded into an optimistic action-log entry.

All of _op_autofix's collaborators are imported locally inside the function
body (lazy imports to avoid circular deps), so patches target the source
module each name is imported FROM, not kicad_mcp.tools.pcb_drc_fix.
"""

import asyncio
from unittest.mock import patch

from kicad_mcp.tools.pcb_drc_fix import _op_autofix

_ROUTING_DRC = {
    "success": True,
    "total_violations": 3,
    "violation_categories": {"clearance": 3},
}
_CLEAN_DRC = {"success": True, "total_violations": 0, "violation_categories": {}}


def _run(pcb_path="/tmp/board.kicad_pcb", project_path="/tmp/board.kicad_pro", **kwargs):
    return asyncio.run(_op_autofix(pcb_path, project_path, **kwargs))


class TestRoutingFixSuccess:
    @patch("kicad_mcp.tools.pcb_autoroute._run_full_autoroute")
    @patch("kicad_mcp.tools.pcb_autoroute._find_java")
    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar")
    @patch("kicad_mcp.utils.pcbnew_bridge.run_pcbnew_script")
    @patch("kicad_mcp.tools.drc_impl.cli_drc.run_drc_via_cli")
    @patch("os.path.exists", return_value=True)
    def test_successful_reroute_reports_ok(
        self, mock_exists, mock_drc, mock_script, mock_jar, mock_java, mock_route,
    ):
        mock_drc.side_effect = [_ROUTING_DRC, _CLEAN_DRC]
        # No "status" key: the clear-routing call site only reads "error"
        # (absent -> success) and "removed" -- see the note on mock_route
        # below for why an unread "status" key here would be a coincidental
        # echo, not a meaningful mock configuration.
        mock_script.return_value = {"removed": 5}
        mock_jar.return_value = "/fake/freerouter.jar"
        mock_java.return_value = "/usr/bin/java"
        # No "status" key: _op_autofix's routing branch only checks for
        # "error" in route_result, never reads route_result["status"] --
        # including one here would coincidentally echo this test's own
        # result["status"] assertion without that assertion depending on
        # any real logic, tripping the tautology detector on a false
        # positive (see AGENTS.md's note on this class of false positive).
        mock_route.return_value = {"tracks_after": 20, "vias_after": 2, "unconnected_after_routing": 0}

        result = _run(fix_placement=False, fix_silkscreen=False)

        assert result["status"] == "ok"
        assert result["routing_regressed"] is False
        assert any("re-autorouted" in a for a in result["actions_taken"])
        assert not any("FAILED" in a for a in result["actions_taken"])


class TestRoutingFixFailure:
    @patch("kicad_mcp.tools.pcb_autoroute._run_full_autoroute")
    @patch("kicad_mcp.tools.pcb_autoroute._find_java")
    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar")
    @patch("kicad_mcp.utils.pcbnew_bridge.run_pcbnew_script")
    @patch("kicad_mcp.tools.drc_impl.cli_drc.run_drc_via_cli")
    @patch("os.path.exists", return_value=True)
    def test_failed_reroute_reports_warning_not_ok(
        self, mock_exists, mock_drc, mock_script, mock_jar, mock_java, mock_route,
    ):
        """The board now has NO routing (tracks were cleared, re-route
        failed) -- status must be "warning", not "ok", and the failure
        must be named plainly in actions_taken rather than folded into a
        "re-autorouted ... ? unconnected" message that reads as success."""
        mock_drc.side_effect = [_ROUTING_DRC, _ROUTING_DRC]  # still violating after
        mock_script.return_value = {"status": "ok", "removed": 5}
        mock_jar.return_value = "/fake/freerouter.jar"
        mock_java.return_value = "/usr/bin/java"
        mock_route.return_value = {"error": "All FreeRouter passes failed"}

        result = _run(fix_placement=False, fix_silkscreen=False)

        assert result["status"] == "warning"
        assert result["routing_regressed"] is True
        routing_action = next(a for a in result["actions_taken"] if a.startswith("routing:"))
        assert "FAILED" in routing_action
        assert "NO routing" in routing_action
        assert "?" not in routing_action  # no more masking a failure as "? unconnected"

    @patch("kicad_mcp.tools.pcb_autoroute._run_full_autoroute")
    @patch("kicad_mcp.tools.pcb_autoroute._find_java")
    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar")
    @patch("kicad_mcp.utils.pcbnew_bridge.run_pcbnew_script")
    @patch("kicad_mcp.tools.drc_impl.cli_drc.run_drc_via_cli")
    @patch("os.path.exists", return_value=True)
    def test_unavailable_freerouter_skips_without_regression(
        self, mock_exists, mock_drc, mock_script, mock_jar, mock_java, mock_route,
    ):
        """No jar/java: routing must be left untouched entirely (skip, not
        clear-then-fail) -- routing_regressed stays False since nothing
        was ever cleared."""
        mock_drc.side_effect = [_ROUTING_DRC, _ROUTING_DRC]
        mock_jar.return_value = None
        mock_java.return_value = None

        result = _run(fix_placement=False, fix_silkscreen=False)

        mock_route.assert_not_called()
        assert result["status"] == "ok"
        assert result["routing_regressed"] is False
        assert any("skipped" in a for a in result["actions_taken"])
