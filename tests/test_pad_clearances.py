"""
Tests for operation="pad_clearances" on the audit router.

Unit tests mock run_pcbnew_script to test tool registration and argument
handling.  Integration-style tests verify the embedded pcbnew script logic
by checking the generated script content.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def audit_server():
    """Create a FastMCP server with the audit router registered."""
    mcp = FastMCP("test-audit")
    register_pcb_keepout_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    """Create a dummy .kicad_pcb file."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return str(pcb)


def _get_audit_fn(mcp_server):
    tool = asyncio.run(mcp_server.get_tool("audit"))
    if tool is None:
        raise ValueError("Tool 'audit' not found")
    return tool.fn


# -- Tool registration tests -------------------------------------------------

class TestPadClearancesRegistration:

    def test_audit_tool_registered(self, audit_server):
        fn = _get_audit_fn(audit_server)
        assert fn is not None

    def test_file_not_found(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("pad_clearances", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]


# -- Script content tests ---------------------------------------------------

class TestPadClearancesScript:

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_iterates_pads(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 0,
            "min_clearance_mm": 0.2,
            "violation_count": 0,
            "footprint_pairs_affected": 0,
            "footprint_pair_summary": [],
            "violations": [],
            "violations_truncated": False,
            "summary": "All inter-footprint pad clearances >= 0.2mm",
        }
        fn = _get_audit_fn(audit_server)
        fn("pad_clearances", pcb_path=pcb_file)
        script = mock_run.call_args[0][0]
        assert "fp.Pads()" in script
        assert "pad.GetPosition()" in script
        assert "pad.GetSize()" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_uses_board_design_rules_when_zero(self, mock_run, audit_server, pcb_file):
        """When min_clearance_mm=0, script falls back to board design rules."""
        mock_run.return_value = {
            "status": "ok", "total_pads": 0, "violation_count": 0,
            "min_clearance_mm": 0.2, "footprint_pairs_affected": 0,
            "footprint_pair_summary": [], "violations": [],
            "violations_truncated": False, "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("pad_clearances", pcb_path=pcb_file, min_clearance_mm=0.0)
        script = mock_run.call_args[0][0]
        assert "m_MinClearance" in script
        assert "min_cl = 0" in script or "min_cl = 0.0" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_uses_explicit_clearance(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_pads": 0, "violation_count": 0,
            "min_clearance_mm": 0.5, "footprint_pairs_affected": 0,
            "footprint_pair_summary": [], "violations": [],
            "violations_truncated": False, "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("pad_clearances", pcb_path=pcb_file, min_clearance_mm=0.5)
        params = mock_run.call_args[1]["params"]
        assert params["min_clearance_mm"] == 0.5

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_skips_same_footprint(self, mock_run, audit_server, pcb_file):
        """Script must skip pad pairs within the same footprint."""
        mock_run.return_value = {
            "status": "ok", "total_pads": 0, "violation_count": 0,
            "min_clearance_mm": 0.2, "footprint_pairs_affected": 0,
            "footprint_pair_summary": [], "violations": [],
            "violations_truncated": False, "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("pad_clearances", pcb_path=pcb_file)
        script = mock_run.call_args[0][0]
        assert 'a["ref"] == b["ref"]' in script


# -- Mock result processing tests -------------------------------------------

class TestPadClearancesResults:

    def test_pad_gap_and_threshold_boundary(self):
        """The gap + `< min_cl` decision is extracted into PAD_GAP_HELPER so it is
        unit-testable in-process (review finding h-test-threshold). a's right edge
        sits at x=0 so the gap equals b's left edge exactly — no float drift at the
        boundary (the `1.2 - 1.0` trap the consumer-tolerance rule warns about).
        Pin the strict side: a gap of exactly 0.2 is NOT a violation."""
        from kicad_mcp.tools.pcb_keepout import PAD_GAP_HELPER
        ns: dict = {}
        exec(PAD_GAP_HELPER, ns)
        gap = ns["pad_signed_gap"]

        def pad(x0, y0, x1, y1):
            return {"x0": x0, "y0": y0, "x1": x1, "y1": y1}

        a = pad(-1, 0, 0, 1)                                  # right edge at x=0
        assert gap(a, pad(0.5, 0, 1.5, 1)) == 0.5            # separated by 0.5mm
        assert gap(a, pad(0.0, 0, 1.0, 1)) == 0.0            # touching -> 0
        assert gap(a, pad(-0.5, 0, 0.5, 1)) < 0             # penetrating -> negative
        assert gap(a, pad(0.1, 0, 1.1, 1)) < 0.2            # below 0.2 -> violation
        assert not (gap(a, pad(0.2, 0, 1.2, 1)) < 0.2)      # AT 0.2: strict < -> not
        assert not (gap(a, pad(0.3, 0, 1.3, 1)) < 0.2)      # above -> not

    def test_pad_gap_diverges_from_signed_gap_mm_when_nested(self):
        """pad_signed_gap deliberately differs from geometry.signed_gap_mm for a
        box nested inside another: overlap EXTENT vs move-to-clear DISTANCE. Pin
        the seam with a test rather than silently unifying two look-alike formulas."""
        from kicad_mcp.tools.pcb_keepout import PAD_GAP_HELPER
        from kicad_mcp.utils.geometry import GEOMETRY_HELPER
        ns: dict = {}
        exec(PAD_GAP_HELPER, ns)
        exec(GEOMETRY_HELPER, ns)
        a = {"x0": 0, "y0": 0, "x1": 10, "y1": 10}
        b = {"x0": 4, "y0": 4, "x1": 6, "y1": 6}
        a_rect = {"x_min_mm": 0, "y_min_mm": 0, "x_max_mm": 10, "y_max_mm": 10}
        b_rect = {"x_min_mm": 4, "y_min_mm": 4, "x_max_mm": 6, "y_max_mm": 6}
        assert ns["pad_signed_gap"](a, b) == -2.0            # overlap extent
        assert ns["signed_gap_mm"](a_rect, b_rect) == -6.0   # separation distance

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_pad_overlap_detected(self, mock_run, audit_server, pcb_file):
        """When pads physically overlap, gap=0 and overlap=True."""
        mock_run.return_value = {
            "status": "ok",
            "total_pads": 10,
            "min_clearance_mm": 0.2,
            "violation_count": 1,
            "footprint_pairs_affected": 1,
            "footprint_pair_summary": [
                {"ref_a": "R1", "ref_b": "U1", "pad_violations": 1, "min_gap_mm": 0.0},
            ],
            "violations": [
                {
                    "pad_a": "R1:1", "pad_b": "U1:4",
                    "net_a": "SDA", "net_b": "",
                    "gap_mm": 0.0, "min_clearance_mm": 0.2,
                    "overlap": True,
                    "pad_a_center": [32.0, 44.0],
                    "pad_b_center": [32.0, 44.0],
                },
            ],
            "violations_truncated": False,
            "summary": "1 pad clearance violation(s)",
        }
        fn = _get_audit_fn(audit_server)
        result = fn("pad_clearances", pcb_path=pcb_file)
        assert result["violations"][0]["overlap"] is True
        assert result["violations"][0]["gap_mm"] == 0.0

    def test_truncate_violations_boundary(self):
        """The 50-cap and the strict ``> 50`` flag are a pure in-process
        post-processor now (extracted from the embedded script per the review's
        h-test-truncation finding), so the boundary is unit-testable without a
        mocked subprocess. Pin the strict side: exactly 50 is NOT truncated."""
        from kicad_mcp.tools.pcb_keepout import _truncate_violations

        def result(n):
            return {
                "violation_count": n,
                "violations": [{"pad_a": f"R{i}:1", "pad_b": "U1:1"} for i in range(n)],
            }

        r49 = _truncate_violations(result(49))
        assert r49["violations_truncated"] is False and len(r49["violations"]) == 49
        r50 = _truncate_violations(result(50))  # at the cap: `>` is strict -> not truncated
        assert r50["violations_truncated"] is False and len(r50["violations"]) == 50
        r51 = _truncate_violations(result(51))
        assert r51["violations_truncated"] is True and len(r51["violations"]) == 50

    def test_truncate_violations_passes_through_errors(self):
        """An error / short-circuit result (no 'violations' key) is returned
        untouched — no violations or violations_truncated keys injected."""
        from kicad_mcp.tools.pcb_keepout import _truncate_violations
        assert _truncate_violations({"error": "boom"}) == {"error": "boom"}
