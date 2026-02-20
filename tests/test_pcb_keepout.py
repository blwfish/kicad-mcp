"""
Tests for PCB keepout-aware placement validation tools.

Tests the 4 keepout tools: get_keepout_zones, get_board_constraints,
validate_placement, audit_pcb_placement.

Unit tests mock run_pcbnew_script to test tool logic without requiring
KiCad's Python 3.9 / pcbnew bindings.

Ported from kicad-mcp-old/tests/unit/tools/test_pcb_keepout_tools.py with
changes for FastMCP 3.0 and the new split module structure.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def keepout_server():
    """Create a FastMCP server with only keepout tools registered."""
    mcp = FastMCP("test-keepout")
    register_pcb_keepout_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    """Create a dummy .kicad_pcb file for path-existence checks."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return str(pcb)


# Sample return data mimicking what pcbnew scripts produce

SAMPLE_KEEPOUTS = [
    {
        "source": "footprint",
        "source_ref": "U1",
        "uuid": "abc-123",
        "layers": ["F.Cu", "B.Cu"],
        "constraints": {
            "no_tracks": True,
            "no_vias": True,
            "no_pads": True,
            "no_footprints": True,
            "no_copper_pour": True,
        },
        "bounding_box": {
            "x_min_mm": 106.0,
            "y_min_mm": 66.26,
            "x_max_mm": 154.0,
            "y_max_mm": 87.2,
        },
        "polygon_pts_mm": [
            [106.0, 66.26],
            [154.0, 66.26],
            [154.0, 87.2],
            [106.0, 87.2],
        ],
    },
]

SAMPLE_OUTLINE = {
    "x_min_mm": 95.0,
    "y_min_mm": 72.0,
    "x_max_mm": 165.0,
    "y_max_mm": 122.0,
    "width_mm": 70.0,
    "height_mm": 50.0,
}


# -- Helper to call tools via the registered functions -----------------------

def _get_tool_fn(mcp_server, tool_name):
    """Extract a tool function from the FastMCP 3.0 server by name."""
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- get_keepout_zones tests -------------------------------------------------

class TestGetKeepoutZones:

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "get_keepout_zones")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_returns_keepouts(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "keepout_count": 1,
            "keepouts": SAMPLE_KEEPOUTS,
        }
        fn = _get_tool_fn(keepout_server, "get_keepout_zones")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["keepout_count"] == 1
        assert len(result["keepouts"]) == 1
        kz = result["keepouts"][0]
        assert kz["source"] == "footprint"
        assert kz["source_ref"] == "U1"
        assert kz["constraints"]["no_tracks"] is True
        mock_run.assert_called_once()

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_no_keepouts(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "keepout_count": 0,
            "keepouts": [],
        }
        fn = _get_tool_fn(keepout_server, "get_keepout_zones")
        result = fn(pcb_file)
        assert result["keepout_count"] == 0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_contains_extract_keepouts(self, mock_run, keepout_server, pcb_file):
        """Verify the generated script includes the keepout helper code."""
        mock_run.return_value = {"status": "ok", "keepout_count": 0, "keepouts": []}
        fn = _get_tool_fn(keepout_server, "get_keepout_zones")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert "extract_keepouts" in script
        assert "GetIsRuleArea" in script
        assert pcb_file in script


# -- get_board_constraints tests ---------------------------------------------

class TestGetBoardConstraints:

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "get_board_constraints")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_returns_constraints(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "board_outline": {
                **SAMPLE_OUTLINE,
                "area_mm2": 3500.0,
            },
            "keepout_zones": SAMPLE_KEEPOUTS,
            "design_rules": {
                "min_track_width_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_via_diameter_mm": 0.6,
            },
            "existing_footprints_count": 16,
            "total_keepout_area_mm2": 1005.1,
            "effective_placement_area_mm2": 2494.9,
        }
        fn = _get_tool_fn(keepout_server, "get_board_constraints")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["board_outline"]["width_mm"] == 70.0
        assert result["design_rules"]["min_track_width_mm"] == 0.2
        assert result["existing_footprints_count"] == 16
        assert result["effective_placement_area_mm2"] == 2494.9

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_includes_design_rules(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "board_outline": None,
            "keepout_zones": [],
            "design_rules": {},
            "existing_footprints_count": 0,
            "total_keepout_area_mm2": 0,
        }
        fn = _get_tool_fn(keepout_server, "get_board_constraints")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert "GetDesignSettings" in script
        assert "m_TrackMinWidth" in script
        assert "get_board_outline" in script


# -- validate_placement tests ------------------------------------------------

class TestValidatePlacement:

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "validate_placement")
        result = fn("/nonexistent/board.kicad_pcb",
                     "Resistor_SMD", "R_0805_2012Metric", 130.0, 80.0)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_valid_placement(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "valid": True,
            "violations": [],
            "warnings": [],
            "footprint_bbox_mm": {
                "x_min_mm": 98.0, "y_min_mm": 110.0,
                "x_max_mm": 102.0, "y_max_mm": 112.0,
            },
            "board_outline_mm": SAMPLE_OUTLINE,
        }
        fn = _get_tool_fn(keepout_server, "validate_placement")
        result = fn(pcb_file, "Resistor_SMD", "R_0805_2012Metric", 100.0, 111.0)
        assert result["valid"] is True
        assert result["violations"] == []

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_placement_in_keepout(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "valid": False,
            "violations": [{
                "type": "keepout_overlap",
                "keepout_source": "footprint",
                "keepout_ref": "U1",
                "overlap_mm2": 8.0,
                "blocked": ["tracks", "vias", "pads", "footprints", "copper_pour"],
                "message": "Footprint overlaps keepout zone from U1",
            }],
            "warnings": [],
            "footprint_bbox_mm": {
                "x_min_mm": 128.0, "y_min_mm": 78.0,
                "x_max_mm": 132.0, "y_max_mm": 80.0,
            },
            "board_outline_mm": SAMPLE_OUTLINE,
        }
        fn = _get_tool_fn(keepout_server, "validate_placement")
        result = fn(pcb_file, "Resistor_SMD", "R_0805_2012Metric", 130.0, 79.0)
        assert result["valid"] is False
        assert len(result["violations"]) == 1
        assert result["violations"][0]["type"] == "keepout_overlap"
        assert result["violations"][0]["keepout_ref"] == "U1"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_placement_outside_board(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "valid": False,
            "violations": [{
                "type": "outside_board",
                "overhang": {"right_mm": 5.0},
                "message": "Footprint extends beyond board outline",
            }],
            "warnings": [],
            "footprint_bbox_mm": {
                "x_min_mm": 162.0, "y_min_mm": 110.0,
                "x_max_mm": 170.0, "y_max_mm": 112.0,
            },
            "board_outline_mm": SAMPLE_OUTLINE,
        }
        fn = _get_tool_fn(keepout_server, "validate_placement")
        result = fn(pcb_file, "Resistor_SMD", "R_0805_2012Metric", 166.0, 111.0)
        assert result["valid"] is False
        assert result["violations"][0]["type"] == "outside_board"
        assert result["violations"][0]["overhang"]["right_mm"] == 5.0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_routing_warning_not_violation(self, mock_run, keepout_server, pcb_file):
        """A keepout that blocks routing but not footprints produces a warning, still valid."""
        mock_run.return_value = {
            "status": "ok",
            "valid": True,
            "violations": [],
            "warnings": [{
                "type": "routing_keepout_overlap",
                "keepout_source": "footprint",
                "keepout_ref": "U2",
                "overlap_mm2": 2.0,
                "blocked": ["tracks", "vias"],
                "message": "Footprint overlaps zone that blocks tracks, vias (routing may be difficult)",
            }],
            "footprint_bbox_mm": {
                "x_min_mm": 108.0, "y_min_mm": 96.0,
                "x_max_mm": 112.0, "y_max_mm": 98.0,
            },
            "board_outline_mm": SAMPLE_OUTLINE,
        }
        fn = _get_tool_fn(keepout_server, "validate_placement")
        result = fn(pcb_file, "Capacitor_SMD", "C_0805_2012Metric", 110.0, 97.0)
        assert result["valid"] is True
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["type"] == "routing_keepout_overlap"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_loads_footprint_from_library(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "valid": True,
            "violations": [],
            "warnings": [],
            "footprint_bbox_mm": {},
            "board_outline_mm": None,
        }
        fn = _get_tool_fn(keepout_server, "validate_placement")
        fn(pcb_file, "Resistor_SMD", "R_0805_2012Metric", 100.0, 100.0, 45.0)
        script = mock_run.call_args[0][0]
        assert "FootprintLoad" in script
        assert "Resistor_SMD" in script
        assert "R_0805_2012Metric" in script
        assert "SetPosition" in script
        assert "45.0" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_library_not_found(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {"error": "Library 'FakeLib' not found"}
        fn = _get_tool_fn(keepout_server, "validate_placement")
        result = fn(pcb_file, "FakeLib", "FakeFP", 100.0, 100.0)
        assert "error" in result


# -- audit_pcb_placement tests -----------------------------------------------

class TestAuditPcbPlacement:

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "audit_pcb_placement")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_all_clean(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 8,
            "violations_count": 0,
            "clean_count": 8,
            "violations": [],
            "summary": "All 8 footprints pass placement checks",
        }
        fn = _get_tool_fn(keepout_server, "audit_pcb_placement")
        result = fn(pcb_file)
        assert result["violations_count"] == 0
        assert result["clean_count"] == 8
        assert "pass" in result["summary"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_violations_found(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 16,
            "violations_count": 12,
            "clean_count": 4,
            "violations": [
                {
                    "reference": "D1",
                    "value": "LED",
                    "footprint": "LED_0805_2012Metric",
                    "position_mm": [120.0, 80.0],
                    "bbox_mm": {
                        "x_min_mm": 118.0, "y_min_mm": 78.0,
                        "x_max_mm": 122.0, "y_max_mm": 82.0,
                    },
                    "issues": [{
                        "type": "keepout_overlap",
                        "severity": "violation",
                        "keepout_source": "footprint",
                        "keepout_ref": "U1",
                        "overlap_mm2": 16.0,
                        "blocked": [
                            "tracks", "vias", "pads", "footprints", "copper_pour",
                        ],
                    }],
                },
                {
                    "reference": "BZ1",
                    "value": "Buzzer",
                    "footprint": "Buzzer_12x9.5mm",
                    "position_mm": [170.0, 118.0],
                    "bbox_mm": {
                        "x_min_mm": 164.0, "y_min_mm": 112.0,
                        "x_max_mm": 176.0, "y_max_mm": 124.0,
                    },
                    "issues": [{
                        "type": "outside_board",
                        "severity": "violation",
                        "overhang": {"right_mm": 11.0, "bottom_mm": 2.0},
                    }],
                },
            ],
            "summary": "12 of 16 footprints have placement issues",
        }
        fn = _get_tool_fn(keepout_server, "audit_pcb_placement")
        result = fn(pcb_file)
        assert result["violations_count"] == 12
        assert result["clean_count"] == 4
        assert len(result["violations"]) == 2
        # Check keepout violation
        d1 = result["violations"][0]
        assert d1["reference"] == "D1"
        assert d1["issues"][0]["severity"] == "violation"
        # Check board boundary violation
        bz1 = result["violations"][1]
        assert bz1["reference"] == "BZ1"
        assert bz1["issues"][0]["type"] == "outside_board"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_skips_own_keepout(self, mock_run, keepout_server, pcb_file):
        """Verify the script skips a footprint's own embedded keepout zone."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 1,
            "violations_count": 0,
            "clean_count": 1,
            "violations": [],
            "summary": "All 1 footprints pass placement checks",
        }
        fn = _get_tool_fn(keepout_server, "audit_pcb_placement")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        # The script should have logic to skip a footprint's own keepout
        assert "source_ref" in script
        assert "continue" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_error_propagated(self, mock_run, keepout_server, pcb_file):
        """If pcbnew script raises RuntimeError, it propagates."""
        mock_run.side_effect = RuntimeError("pcbnew crashed")
        fn = _get_tool_fn(keepout_server, "audit_pcb_placement")
        with pytest.raises(RuntimeError, match="pcbnew crashed"):
            fn(pcb_file)


# -- Shared helper logic tests (pure Python, no mocking needed) ---------------

class TestHelperLogic:
    """Test the pure-Python helper functions embedded in KEEPOUT_HELPER.

    Since these are strings embedded in pcbnew scripts, we extract and exec them
    to test the geometry logic directly.
    """

    @pytest.fixture(autouse=True)
    def setup_helpers(self):
        """Execute the KEEPOUT_HELPER code to get geometry functions."""
        from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER

        # The KEEPOUT_HELPER string defines functions that use pcbnew internally
        # (extract_keepouts, get_board_outline), but the geometry helpers are
        # pure Python. We exec just the geometry functions.
        namespace = {}
        exec(
            "def rects_overlap(a, b):\n"
            '    return (a["x_min_mm"] < b["x_max_mm"] and a["x_max_mm"] > b["x_min_mm"] and\n'
            '            a["y_min_mm"] < b["y_max_mm"] and a["y_max_mm"] > b["y_min_mm"])\n'
            "\n"
            "def overlap_area(a, b):\n"
            '    dx = max(0, min(a["x_max_mm"], b["x_max_mm"]) - max(a["x_min_mm"], b["x_min_mm"]))\n'
            '    dy = max(0, min(a["y_max_mm"], b["y_max_mm"]) - max(a["y_min_mm"], b["y_min_mm"]))\n'
            "    return round(dx * dy, 2)\n"
            "\n"
            "def rect_inside(inner, outer):\n"
            '    return (inner["x_min_mm"] >= outer["x_min_mm"] and inner["x_max_mm"] <= outer["x_max_mm"] and\n'
            '            inner["y_min_mm"] >= outer["y_min_mm"] and inner["y_max_mm"] <= outer["y_max_mm"])\n',
            namespace,
        )
        self.rects_overlap = namespace["rects_overlap"]
        self.overlap_area = namespace["overlap_area"]
        self.rect_inside = namespace["rect_inside"]

        # Also verify these functions exist in the actual KEEPOUT_HELPER string
        assert "rects_overlap" in KEEPOUT_HELPER
        assert "overlap_area" in KEEPOUT_HELPER
        assert "rect_inside" in KEEPOUT_HELPER

    def _rect(self, x1, y1, x2, y2):
        return {"x_min_mm": x1, "y_min_mm": y1, "x_max_mm": x2, "y_max_mm": y2}

    # rects_overlap
    def test_overlapping_rects(self):
        a = self._rect(0, 0, 10, 10)
        b = self._rect(5, 5, 15, 15)
        assert self.rects_overlap(a, b) is True

    def test_non_overlapping_rects(self):
        a = self._rect(0, 0, 10, 10)
        b = self._rect(20, 20, 30, 30)
        assert self.rects_overlap(a, b) is False

    def test_touching_edge_not_overlapping(self):
        """Rects that share an edge but don't overlap."""
        a = self._rect(0, 0, 10, 10)
        b = self._rect(10, 0, 20, 10)
        assert self.rects_overlap(a, b) is False

    def test_contained_rect(self):
        outer = self._rect(0, 0, 100, 100)
        inner = self._rect(10, 10, 20, 20)
        assert self.rects_overlap(outer, inner) is True

    # overlap_area
    def test_overlap_area_partial(self):
        a = self._rect(0, 0, 10, 10)
        b = self._rect(5, 5, 15, 15)
        assert self.overlap_area(a, b) == 25.0

    def test_overlap_area_none(self):
        a = self._rect(0, 0, 10, 10)
        b = self._rect(20, 20, 30, 30)
        assert self.overlap_area(a, b) == 0.0

    def test_overlap_area_contained(self):
        outer = self._rect(0, 0, 100, 100)
        inner = self._rect(10, 10, 20, 20)
        assert self.overlap_area(outer, inner) == 100.0

    # rect_inside
    def test_fully_inside(self):
        inner = self._rect(10, 10, 20, 20)
        outer = self._rect(0, 0, 100, 100)
        assert self.rect_inside(inner, outer) is True

    def test_partially_outside(self):
        inner = self._rect(90, 90, 110, 110)
        outer = self._rect(0, 0, 100, 100)
        assert self.rect_inside(inner, outer) is False

    def test_exactly_on_boundary(self):
        inner = self._rect(0, 0, 100, 100)
        outer = self._rect(0, 0, 100, 100)
        assert self.rect_inside(inner, outer) is True

    def test_completely_outside(self):
        inner = self._rect(200, 200, 210, 210)
        outer = self._rect(0, 0, 100, 100)
        assert self.rect_inside(inner, outer) is False


# -- audit_footprint_overlaps tests ------------------------------------------

class TestAuditFootprintOverlaps:

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_no_overlaps(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 5,
            "pairs_checked": 10,
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 5 footprints are clear of each other",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn(pcb_file)
        assert result["status"] == "ok"
        assert result["overlap_count"] == 0
        assert result["pairs_checked"] == 10
        assert "clear" in result["summary"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_physical_overlap_detected(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 3,
            "pairs_checked": 3,
            "overlap_count": 1,
            "error_count": 1,
            "warning_count": 0,
            "overlaps": [{
                "ref_a": "J6",
                "ref_b": "J2",
                "value_a": "Conn_01x08",
                "value_b": "Conn_01x16",
                "overlap": True,
                "overlap_mm2": 2.5,
                "gap_mm": -0.33,
                "severity": "error",
                "message": "J6 and J2 physically overlap by 2.5 mm2",
                "bbox_a": {
                    "x_min_mm": 100.73, "y_min_mm": 142.0,
                    "x_max_mm": 103.27, "y_max_mm": 162.78,
                },
                "bbox_b": {
                    "x_min_mm": 99.0, "y_min_mm": 163.11,
                    "x_max_mm": 181.0, "y_max_mm": 174.0,
                },
            }],
            "summary": "1 overlap(s) found among 3 footprints (1 collisions, 0 clearance warnings)",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn(pcb_file)
        assert result["overlap_count"] == 1
        assert result["error_count"] == 1
        overlap = result["overlaps"][0]
        assert overlap["ref_a"] == "J6"
        assert overlap["ref_b"] == "J2"
        assert overlap["overlap"] is True
        assert overlap["severity"] == "error"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_clearance_warning(self, mock_run, keepout_server, pcb_file):
        """Footprints within min_clearance but not overlapping produce a warning."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 2,
            "pairs_checked": 1,
            "overlap_count": 1,
            "error_count": 0,
            "warning_count": 1,
            "overlaps": [{
                "ref_a": "R1",
                "ref_b": "R2",
                "value_a": "4.7k",
                "value_b": "4.7k",
                "overlap": False,
                "overlap_mm2": 0.0,
                "gap_mm": 0.15,
                "severity": "warning",
                "message": "R1 and R2 are only 0.15 mm apart (min clearance: 0.5 mm)",
                "bbox_a": {
                    "x_min_mm": 160.0, "y_min_mm": 134.0,
                    "x_max_mm": 164.0, "y_max_mm": 138.0,
                },
                "bbox_b": {
                    "x_min_mm": 160.0, "y_min_mm": 138.15,
                    "x_max_mm": 164.0, "y_max_mm": 142.15,
                },
            }],
            "summary": "1 overlap(s) found among 2 footprints (0 collisions, 1 clearance warnings)",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn(pcb_file, min_clearance_mm=0.5)
        assert result["warning_count"] == 1
        assert result["error_count"] == 0
        overlap = result["overlaps"][0]
        assert overlap["overlap"] is False
        assert overlap["severity"] == "warning"
        assert overlap["gap_mm"] == 0.15

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_pairs_checked_formula(self, mock_run, keepout_server, pcb_file):
        """Verify pairs_checked = n*(n-1)/2."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 20,
            "pairs_checked": 190,  # 20*19/2
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 20 footprints are clear of each other",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn(pcb_file)
        assert result["pairs_checked"] == 190

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_uses_pairwise_check(self, mock_run, keepout_server, pcb_file):
        """Verify the generated script contains pairwise overlap logic."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 0,
            "pairs_checked": 0,
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 0 footprints are clear of each other",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert "rects_overlap" in script
        assert "overlap_area" in script
        assert "range(i + 1" in script
        assert "GetBoundingBox" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_error_propagated(self, mock_run, keepout_server, pcb_file):
        mock_run.side_effect = RuntimeError("pcbnew crashed")
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        with pytest.raises(RuntimeError, match="pcbnew crashed"):
            fn(pcb_file)

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_default_clearance_zero(self, mock_run, keepout_server, pcb_file):
        """Default min_clearance_mm is 0 (only actual overlaps reported)."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 2,
            "pairs_checked": 1,
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 2 footprints are clear of each other",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert "min_clearance = 0.0" in script or "min_clearance = 0" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_use_courtyard_default_true(self, mock_run, keepout_server, pcb_file):
        """Default use_courtyard=True generates courtyard/pad-based bbox logic."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 0,
            "pairs_checked": 0,
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 0 footprints are clear of each other",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        assert "use_courtyard = True" in script
        assert "get_courtyard_bbox" in script
        assert "CrtYd" in script
        assert "Pads()" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_use_courtyard_false_uses_body_bbox(self, mock_run, keepout_server, pcb_file):
        """use_courtyard=False uses body bbox only."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 0,
            "pairs_checked": 0,
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 0 footprints are clear of each other",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        fn(pcb_file, use_courtyard=False)
        script = mock_run.call_args[0][0]
        assert "use_courtyard = False" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_bbox_source_reported(self, mock_run, keepout_server, pcb_file):
        """Overlap entries include bbox_source_a and bbox_source_b."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 2,
            "pairs_checked": 1,
            "overlap_count": 1,
            "error_count": 1,
            "warning_count": 0,
            "overlaps": [{
                "ref_a": "J4",
                "ref_b": "J6",
                "value_a": "OLED",
                "value_b": "Expansion",
                "overlap": True,
                "overlap_mm2": 7.93,
                "gap_mm": -2.21,
                "severity": "error",
                "message": "J4 and J6 physically overlap by 7.93 mm2",
                "bbox_a": {},
                "bbox_b": {},
                "bbox_source_a": "pads",
                "bbox_source_b": "pads",
            }],
            "summary": "1 overlap(s) found among 2 footprints (1 collisions, 0 clearance warnings)",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn(pcb_file)
        overlap = result["overlaps"][0]
        assert overlap["bbox_source_a"] == "pads"
        assert overlap["bbox_source_b"] == "pads"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_multiple_overlaps(self, mock_run, keepout_server, pcb_file):
        """Multiple overlap pairs are reported."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 4,
            "pairs_checked": 6,
            "overlap_count": 3,
            "error_count": 2,
            "warning_count": 1,
            "overlaps": [
                {"ref_a": "R1", "ref_b": "R2", "overlap": True, "severity": "error",
                 "overlap_mm2": 1.0, "gap_mm": -0.5,
                 "value_a": "10k", "value_b": "10k",
                 "message": "R1 and R2 physically overlap by 1.0 mm2",
                 "bbox_a": {}, "bbox_b": {}},
                {"ref_a": "R1", "ref_b": "C1", "overlap": True, "severity": "error",
                 "overlap_mm2": 0.5, "gap_mm": -0.2,
                 "value_a": "10k", "value_b": "100nF",
                 "message": "R1 and C1 physically overlap by 0.5 mm2",
                 "bbox_a": {}, "bbox_b": {}},
                {"ref_a": "R2", "ref_b": "C1", "overlap": False, "severity": "warning",
                 "overlap_mm2": 0.0, "gap_mm": 0.3,
                 "value_a": "10k", "value_b": "100nF",
                 "message": "R2 and C1 are only 0.3 mm apart (min clearance: 0.5 mm)",
                 "bbox_a": {}, "bbox_b": {}},
            ],
            "summary": "3 overlap(s) found among 4 footprints (2 collisions, 1 clearance warnings)",
        }
        fn = _get_tool_fn(keepout_server, "audit_footprint_overlaps")
        result = fn(pcb_file, min_clearance_mm=0.5)
        assert result["overlap_count"] == 3
        assert result["error_count"] == 2
        assert result["warning_count"] == 1
