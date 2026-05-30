"""
Tests for the audit domain router (phase 3 consolidation of pcb_keepout tools).

Covers operations: keepouts, constraints, validate_one, placement,
footprint_overlaps, all (summary + full), pre_route_check, auto_fix_placement.

Unit tests mock run_pcbnew_script to test router logic without requiring
KiCad's Python 3.9 / pcbnew bindings.
"""

import asyncio
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def audit_server():
    """Create a FastMCP server with only the audit router registered."""
    mcp = FastMCP("test-audit")
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


# -- Helper to call the audit router -----------------------------------------

def _get_audit_fn(mcp_server):
    """Extract the audit tool function from the FastMCP 3.0 server."""
    tool = asyncio.run(mcp_server.get_tool("audit"))
    if tool is None:
        raise ValueError("Tool 'audit' not found")
    return tool.fn


# -- Router registration / unknown-op / detail-validation -------------------

class TestAuditRouterBasics:

    def test_audit_tool_registered(self, audit_server):
        fn = _get_audit_fn(audit_server)
        assert fn is not None

    def test_unknown_operation(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("nonexistent_op", pcb_path="/some/path.kicad_pcb")
        assert "error" in result
        assert "unknown operation" in result["error"]
        assert "nonexistent_op" in result["error"]

    def test_invalid_detail_value(self, audit_server, pcb_file):
        fn = _get_audit_fn(audit_server)
        result = fn("all", pcb_path=pcb_file, detail="verbose")
        assert "error" in result
        assert "detail" in result["error"]
        assert "verbose" in result["error"]

    def test_missing_pcb_path_for_all(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("all")
        assert "error" in result
        assert "pcb_path" in result["error"]

    def test_missing_pcb_path_for_keepouts(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("keepouts")
        assert "error" in result
        assert "pcb_path" in result["error"]

    def test_file_not_found_returns_error(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("all", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]


# -- operation="keepouts" ----------------------------------------------------

class TestAuditKeepouts:

    def test_file_not_found(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("keepouts", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_returns_keepouts(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "keepout_count": 1,
            "keepouts": SAMPLE_KEEPOUTS,
        }
        fn = _get_audit_fn(audit_server)
        result = fn("keepouts", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["keepout_count"] == 1
        assert len(result["keepouts"]) == 1
        kz = result["keepouts"][0]
        assert kz["source"] == "footprint"
        assert kz["source_ref"] == "U1"
        assert kz["constraints"]["no_tracks"] is True
        mock_run.assert_called_once()

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_no_keepouts(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "keepout_count": 0,
            "keepouts": [],
        }
        fn = _get_audit_fn(audit_server)
        result = fn("keepouts", pcb_path=pcb_file)
        assert result["keepout_count"] == 0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_contains_extract_keepouts(self, mock_run, audit_server, pcb_file):
        """Verify the generated script includes the keepout helper code."""
        mock_run.return_value = {"status": "ok", "keepout_count": 0, "keepouts": []}
        fn = _get_audit_fn(audit_server)
        fn("keepouts", pcb_path=pcb_file)
        script = mock_run.call_args[0][0]
        assert "extract_keepouts" in script
        assert "GetIsRuleArea" in script
        params = mock_run.call_args[1]["params"]
        assert params["pcb_path"] == pcb_file


# -- operation="constraints" -------------------------------------------------

class TestAuditConstraints:

    def test_file_not_found(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("constraints", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_returns_constraints(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("constraints", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["board_outline"]["width_mm"] == 70.0
        assert result["design_rules"]["min_track_width_mm"] == 0.2
        assert result["existing_footprints_count"] == 16
        assert result["effective_placement_area_mm2"] == 2494.9

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_includes_design_rules(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "board_outline": None,
            "keepout_zones": [],
            "design_rules": {},
            "existing_footprints_count": 0,
            "total_keepout_area_mm2": 0,
        }
        fn = _get_audit_fn(audit_server)
        fn("constraints", pcb_path=pcb_file)
        script = mock_run.call_args[0][0]
        assert "GetDesignSettings" in script
        assert "m_TrackMinWidth" in script
        assert "get_board_outline" in script


# -- operation="validate_one" ------------------------------------------------

class TestAuditValidateOne:

    def test_file_not_found(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path="/nonexistent/board.kicad_pcb",
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric",
                    x_mm=130.0, y_mm=80.0)
        assert "error" in result

    def test_missing_library(self, audit_server, pcb_file):
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    footprint_name="R_0805_2012Metric", x_mm=130.0, y_mm=80.0)
        assert "error" in result
        assert "library" in result["error"]

    def test_missing_footprint_name(self, audit_server, pcb_file):
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Resistor_SMD", x_mm=130.0, y_mm=80.0)
        assert "error" in result
        assert "footprint_name" in result["error"]

    def test_missing_x_mm(self, audit_server, pcb_file):
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric",
                    y_mm=80.0)
        assert "error" in result
        assert "x_mm" in result["error"]

    def test_missing_y_mm(self, audit_server, pcb_file):
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric",
                    x_mm=130.0)
        assert "error" in result
        assert "y_mm" in result["error"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_valid_placement(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric",
                    x_mm=100.0, y_mm=111.0)
        assert result["valid"] is True
        assert result["violations"] == []

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_placement_in_keepout(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric",
                    x_mm=130.0, y_mm=79.0)
        assert result["valid"] is False
        assert len(result["violations"]) == 1
        assert result["violations"][0]["type"] == "keepout_overlap"
        assert result["violations"][0]["keepout_ref"] == "U1"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_placement_outside_board(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric",
                    x_mm=166.0, y_mm=111.0)
        assert result["valid"] is False
        assert result["violations"][0]["type"] == "outside_board"
        assert result["violations"][0]["overhang"]["right_mm"] == 5.0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_routing_warning_not_violation(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="Capacitor_SMD", footprint_name="C_0805_2012Metric",
                    x_mm=110.0, y_mm=97.0)
        assert result["valid"] is True
        assert len(result["warnings"]) == 1
        assert result["warnings"][0]["type"] == "routing_keepout_overlap"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_loads_footprint_from_library(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "valid": True,
            "violations": [],
            "warnings": [],
            "footprint_bbox_mm": {},
            "board_outline_mm": None,
        }
        fn = _get_audit_fn(audit_server)
        fn("validate_one", pcb_path=pcb_file,
           library="Resistor_SMD", footprint_name="R_0805_2012Metric",
           x_mm=100.0, y_mm=100.0, rotation_deg=45.0)
        script = mock_run.call_args[0][0]
        assert "FootprintLoad" in script
        assert "SetPosition" in script
        params = mock_run.call_args[1]["params"]
        assert params["library"] == "Resistor_SMD"
        assert params["footprint_name"] == "R_0805_2012Metric"
        assert params["rotation_deg"] == 45.0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_library_not_found(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {"error": "Library 'FakeLib' not found"}
        fn = _get_audit_fn(audit_server)
        result = fn("validate_one", pcb_path=pcb_file,
                    library="FakeLib", footprint_name="FakeFP",
                    x_mm=100.0, y_mm=100.0)
        assert "error" in result


# -- operation="placement" ---------------------------------------------------

class TestAuditPlacement:

    def test_file_not_found(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("placement", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_all_clean(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 8,
            "violations_count": 0,
            "clean_count": 8,
            "violations": [],
            "summary": "All 8 footprints pass placement checks",
        }
        fn = _get_audit_fn(audit_server)
        result = fn("placement", pcb_path=pcb_file)
        assert result["violations_count"] == 0
        assert result["clean_count"] == 8
        assert "pass" in result["summary"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_violations_found(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("placement", pcb_path=pcb_file)
        assert result["violations_count"] == 12
        assert result["clean_count"] == 4
        assert len(result["violations"]) == 2
        d1 = result["violations"][0]
        assert d1["reference"] == "D1"
        assert d1["issues"][0]["severity"] == "violation"
        bz1 = result["violations"][1]
        assert bz1["reference"] == "BZ1"
        assert bz1["issues"][0]["type"] == "outside_board"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_skips_own_keepout(self, mock_run, audit_server, pcb_file):
        """Verify the script skips a footprint's own embedded keepout zone."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 1,
            "violations_count": 0,
            "clean_count": 1,
            "violations": [],
            "summary": "All 1 footprints pass placement checks",
        }
        fn = _get_audit_fn(audit_server)
        fn("placement", pcb_path=pcb_file)
        script = mock_run.call_args[0][0]
        assert "source_ref" in script
        assert "continue" in script

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_error_propagated(self, mock_run, audit_server, pcb_file):
        mock_run.side_effect = RuntimeError("pcbnew crashed")
        fn = _get_audit_fn(audit_server)
        with pytest.raises(RuntimeError, match="pcbnew crashed"):
            fn("placement", pcb_path=pcb_file)


# -- Shared helper logic tests (pure Python, no mocking needed) ---------------

class TestHelperLogic:
    """Test the pure-Python helper functions embedded in KEEPOUT_HELPER."""

    @pytest.fixture(autouse=True)
    def setup_helpers(self):
        from kicad_mcp.utils.geometry import GEOMETRY_HELPER
        from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER

        namespace: dict = {}
        exec(GEOMETRY_HELPER, namespace)
        self.rects_overlap = namespace["rects_overlap"]
        self.overlap_area = namespace["overlap_area"]
        self.rect_inside = namespace["rect_inside"]

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

    def test_touching_edge_is_overlap(self):
        """Non-strict semantics: rects sharing an edge count as overlapping."""
        a = self._rect(0, 0, 10, 10)
        b = self._rect(10, 0, 20, 10)
        assert self.rects_overlap(a, b) is True

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
        """Non-strict semantics: a rect coincident with its outer IS inside."""
        inner = self._rect(0, 0, 100, 100)
        outer = self._rect(0, 0, 100, 100)
        assert self.rect_inside(inner, outer) is True

    def test_completely_outside(self):
        inner = self._rect(200, 200, 210, 210)
        outer = self._rect(0, 0, 100, 100)
        assert self.rect_inside(inner, outer) is False


# -- extract_keepouts field capture (no-KiCad, duck-typed pcbnew) -------------

class _FakeBBox:
    def __init__(self, x, y, right, bottom):
        self._x, self._y, self._r, self._b = x, y, right, bottom
    def GetX(self): return self._x
    def GetY(self): return self._y
    def GetRight(self): return self._r
    def GetBottom(self): return self._b


class _FakeOutline:
    def __init__(self, pts):
        self._pts = pts
    def PointCount(self): return len(self._pts)
    def CPoint(self, i):
        from types import SimpleNamespace
        return SimpleNamespace(x=self._pts[i][0], y=self._pts[i][1])


class _FakePolySet:
    def __init__(self, pts):
        self._outline = _FakeOutline(pts)
    def OutlineCount(self): return 1
    def Outline(self, i): return self._outline


class _FakeLayerSet:
    def __init__(self, ids):
        self._ids = ids
    def Seq(self): return self._ids


class _FakeUuid:
    def AsString(self): return "uuid-1234"


class _FakeZone:
    """Duck-typed pcbnew ZONE exposing exactly the surface extract_keepouts
    touches, with each constraint flag independently settable so we can prove
    every flag is captured (not hardcoded)."""
    def __init__(self, *, is_rule_area=True, constraints=None, layer_ids=(0,),
                 pts=((0, 0), (1_000_000, 1_000_000)), has_zonefills=True):
        self._is_rule_area = is_rule_area
        c = constraints or {}
        self._tracks = c.get("no_tracks", False)
        self._vias = c.get("no_vias", False)
        self._pads = c.get("no_pads", False)
        self._footprints = c.get("no_footprints", False)
        self._pour = c.get("no_copper_pour", False)
        self._layer_ids = list(layer_ids)
        self._pts = list(pts)
        self.m_Uuid = _FakeUuid()
        # KiCad 10 exposes GetDoNotAllowZoneFills; KiCad 9 only the older
        # GetDoNotAllowCopperPour. Install exactly one so hasattr() reflects
        # the simulated API version.
        if has_zonefills:
            self.GetDoNotAllowZoneFills = lambda: self._pour
        else:
            self.GetDoNotAllowCopperPour = lambda: self._pour
    def GetIsRuleArea(self): return self._is_rule_area
    def GetBoundingBox(self): return _FakeBBox(0, 0, 1_000_000, 1_000_000)
    def GetLayerSet(self): return _FakeLayerSet(self._layer_ids)
    def Outline(self): return _FakePolySet(self._pts)
    def GetDoNotAllowTracks(self): return self._tracks
    def GetDoNotAllowVias(self): return self._vias
    def GetDoNotAllowPads(self): return self._pads
    def GetDoNotAllowFootprints(self): return self._footprints


class _FakeBoard:
    def __init__(self, zones):
        self._zones = zones
    def Zones(self): return self._zones
    def GetFootprints(self): return []
    def GetLayerName(self, lid): return {0: "F.Cu", 31: "B.Cu"}.get(lid, f"L{lid}")


class TestExtractKeepoutsFieldCapture:
    """The actual field-capture extractor (where KiCad rule-area data lands in
    the constraint dict) was only covered behind requires_kicad + a hardcoded
    path. Exec KEEPOUT_HELPER with a duck-typed pcbnew so the no-KiCad suite
    verifies every constraint flag and field is captured (m-keepout-extract-test)."""

    @pytest.fixture(autouse=True)
    def _exec_helper(self, monkeypatch):
        import sys
        from types import SimpleNamespace
        from kicad_mcp.utils.keepout_helpers import KEEPOUT_HELPER
        # extract_keepouts does `import pcbnew` internally and calls ToMM.
        fake_pcbnew = SimpleNamespace(ToMM=lambda v: v / 1_000_000.0)
        monkeypatch.setitem(sys.modules, "pcbnew", fake_pcbnew)
        ns: dict = {}
        exec(KEEPOUT_HELPER, ns)
        self.extract_keepouts = ns["extract_keepouts"]

    def test_all_constraint_flags_captured_true(self):
        zone = _FakeZone(constraints={
            "no_tracks": True, "no_vias": True, "no_pads": True,
            "no_footprints": True, "no_copper_pour": True})
        out = self.extract_keepouts(_FakeBoard([zone]))
        assert len(out) == 1
        c = out[0]["constraints"]
        assert c == {"no_tracks": True, "no_vias": True, "no_pads": True,
                     "no_footprints": True, "no_copper_pour": True}

    def test_flags_are_not_hardcoded(self):
        """A zone that allows everything must report every flag False — proves
        the extractor reads the zone, not a constant."""
        zone = _FakeZone(constraints={})  # all False
        c = self.extract_keepouts(_FakeBoard([zone]))[0]["constraints"]
        assert c == {"no_tracks": False, "no_vias": False, "no_pads": False,
                     "no_footprints": False, "no_copper_pour": False}

    def test_each_flag_independent(self):
        """Only no_vias set — exactly that key True, others False (no bleed)."""
        zone = _FakeZone(constraints={"no_vias": True})
        c = self.extract_keepouts(_FakeBoard([zone]))[0]["constraints"]
        assert c["no_vias"] is True
        assert all(c[k] is False for k in c if k != "no_vias")

    def test_metadata_fields_captured(self):
        zone = _FakeZone(layer_ids=[0, 31])
        info = self.extract_keepouts(_FakeBoard([zone]))[0]
        assert info["source"] == "board"
        assert info["uuid"] == "uuid-1234"
        assert info["layers"] == ["F.Cu", "B.Cu"]
        assert info["bounding_box"] == {
            "x_min_mm": 0.0, "y_min_mm": 0.0, "x_max_mm": 1.0, "y_max_mm": 1.0}
        assert info["polygon_pts_mm"] == [[0.0, 0.0], [1.0, 1.0]]

    def test_non_rule_area_zone_skipped(self):
        zone = _FakeZone(is_rule_area=False)
        assert self.extract_keepouts(_FakeBoard([zone])) == []

    def test_copper_pour_falls_back_when_zonefills_absent(self):
        """KiCad 9 lacks GetDoNotAllowZoneFills — extractor must fall back to
        GetDoNotAllowCopperPour, not crash."""
        z9 = _FakeZone(constraints={"no_copper_pour": True}, has_zonefills=False)
        assert not hasattr(z9, "GetDoNotAllowZoneFills")
        c = self.extract_keepouts(_FakeBoard([z9]))[0]["constraints"]
        assert c["no_copper_pour"] is True


# -- operation="footprint_overlaps" ------------------------------------------

class TestAuditFootprintOverlaps:

    def test_file_not_found(self, audit_server):
        fn = _get_audit_fn(audit_server)
        result = fn("footprint_overlaps", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_no_overlaps(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("footprint_overlaps", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["overlap_count"] == 0
        assert result["pairs_checked"] == 10
        assert "clear" in result["summary"]

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_physical_overlap_detected(self, mock_run, audit_server, pcb_file):
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
        fn = _get_audit_fn(audit_server)
        result = fn("footprint_overlaps", pcb_path=pcb_file)
        assert result["overlap_count"] == 1
        overlap = result["overlaps"][0]
        assert overlap["ref_a"] == "J6"
        assert overlap["overlap"] is True
        assert overlap["severity"] == "error"

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_clearance_warning(self, mock_run, audit_server, pcb_file):
        """Footprints within min_clearance but not overlapping produce a warning."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 2,
            "pairs_checked": 1,
            "overlap_count": 1,
            "error_count": 0,
            "warning_count": 1,
            "overlaps": [{
                "ref_a": "R1", "ref_b": "R2",
                "overlap": False, "overlap_mm2": 0.0,
                "gap_mm": 0.15, "severity": "warning",
                "value_a": "4.7k", "value_b": "4.7k",
                "message": "R1 and R2 are only 0.15 mm apart (min clearance: 0.5 mm)",
                "bbox_a": {}, "bbox_b": {},
            }],
            "summary": "1 overlap(s) found among 2 footprints (0 collisions, 1 clearance warnings)",
        }
        fn = _get_audit_fn(audit_server)
        result = fn("footprint_overlaps", pcb_path=pcb_file, min_clearance_mm=0.5)
        assert result["warning_count"] == 1
        assert result["error_count"] == 0
        overlap = result["overlaps"][0]
        assert overlap["overlap"] is False
        assert overlap["severity"] == "warning"
        assert overlap["gap_mm"] == 0.15

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_default_clearance_zero(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_footprints": 2, "pairs_checked": 1,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [], "summary": "All 2 footprints are clear of each other",
        }
        fn = _get_audit_fn(audit_server)
        fn("footprint_overlaps", pcb_path=pcb_file)
        params = mock_run.call_args[1]["params"]
        assert params["min_clearance_mm"] == 0.0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_use_courtyard_default_true(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_footprints": 0, "pairs_checked": 0,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [], "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("footprint_overlaps", pcb_path=pcb_file)
        script = mock_run.call_args[0][0]
        assert "get_courtyard_bbox" in script
        assert "CrtYd" in script
        params = mock_run.call_args[1]["params"]
        assert params["use_courtyard"] is True

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_use_courtyard_false(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_footprints": 0, "pairs_checked": 0,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [], "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("footprint_overlaps", pcb_path=pcb_file, use_courtyard=False)
        params = mock_run.call_args[1]["params"]
        assert params["use_courtyard"] is False

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_uses_clearance_violation_helper(self, mock_run, audit_server, pcb_file):
        """The generated script uses clearance_violation() instead of inline expand+gate."""
        mock_run.return_value = {
            "status": "ok", "total_footprints": 0, "pairs_checked": 0,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [], "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("footprint_overlaps", pcb_path=pcb_file, min_clearance_mm=0.5)
        script = mock_run.call_args[0][0]
        assert "clearance_violation" in script
        assert "min_clearance > 0 and rects_overlap" not in script

    # -- min_clearance boundary tests ----------------------------------------

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_min_clearance_zero_param_passed_through(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_footprints": 2, "pairs_checked": 1,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [], "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("footprint_overlaps", pcb_path=pcb_file, min_clearance_mm=0.0)
        params = mock_run.call_args[1]["params"]
        assert params["min_clearance_mm"] == 0.0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_min_clearance_epsilon_param_passed_through(self, mock_run, audit_server, pcb_file):
        mock_run.return_value = {
            "status": "ok", "total_footprints": 2, "pairs_checked": 1,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [], "summary": "",
        }
        fn = _get_audit_fn(audit_server)
        fn("footprint_overlaps", pcb_path=pcb_file, min_clearance_mm=1e-6)
        params = mock_run.call_args[1]["params"]
        assert params["min_clearance_mm"] == pytest.approx(1e-6)

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_clearance_violation_at_threshold_produces_no_warning(
            self, mock_run, audit_server, pcb_file):
        """Gap exactly equal to min_clearance is clean — no warning expected."""
        mock_run.return_value = {
            "status": "ok", "total_footprints": 2, "pairs_checked": 1,
            "overlap_count": 0, "error_count": 0, "warning_count": 0,
            "overlaps": [],
            "summary": "All 2 footprints are clear of each other (min clearance 0.5 mm)",
        }
        fn = _get_audit_fn(audit_server)
        result = fn("footprint_overlaps", pcb_path=pcb_file, min_clearance_mm=0.5)
        assert result["warning_count"] == 0
        assert result["overlap_count"] == 0


# -- operation="all" detail flag tests ----------------------------------------

class TestAuditAllDetailFlag:

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_all_summary_default(self, mock_run, audit_server, pcb_file):
        """detail='summary' (default) uses the combined single-subprocess script."""
        mock_run.return_value = {
            "status": "ok",
            "total_footprints": 5,
            "total_issues": 0,
            "footprint_overlaps": [],
            "keepout_violations": [],
            "silkscreen_overlaps": [],
            "silkscreen_text_overlaps": [],
            "summary": "All 5 footprints pass all checks",
        }
        fn = _get_audit_fn(audit_server)
        result = fn("all", pcb_path=pcb_file)
        assert result["status"] == "ok"
        assert result["total_footprints"] == 5
        # summary detail: one subprocess call
        assert mock_run.call_count == 1

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_all_full_calls_each_sub_op(self, mock_run, audit_server, pcb_file):
        """detail='full' calls each sub-op and aggregates their full output."""
        # Each sub-op (_op_placement, _op_footprint_overlaps, _op_pad_clearances,
        # _op_keepouts) makes exactly one run_pcbnew_script call.
        mock_run.return_value = {"status": "ok"}
        # Provide per-op return values that the full path expects
        placement_result = {
            "status": "ok",
            "total_footprints": 3,
            "violations_count": 0,
            "clean_count": 3,
            "violations": [],
            "summary": "All 3 footprints pass placement checks",
        }
        overlaps_result = {
            "status": "ok",
            "total_footprints": 3,
            "pairs_checked": 3,
            "overlap_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "overlaps": [],
            "summary": "All 3 footprints are clear of each other",
        }
        pad_cl_result = {
            "status": "ok",
            "total_pads": 6,
            "min_clearance_mm": 0.2,
            "min_clearance_source": "board",
            "violation_count": 0,
            "footprint_pairs_affected": 0,
            "footprint_pair_summary": [],
            "violations": [],
            "violations_truncated": False,
            "summary": "All inter-footprint pad clearances >= 0.2mm (6 pads checked)",
        }
        keepouts_result = {
            "status": "ok",
            "keepout_count": 1,
            "keepouts": SAMPLE_KEEPOUTS,
        }
        mock_run.side_effect = [
            placement_result, overlaps_result, pad_cl_result, keepouts_result
        ]

        fn = _get_audit_fn(audit_server)
        result = fn("all", pcb_path=pcb_file, detail="full")

        # Four separate subprocess calls (one per sub-op)
        assert mock_run.call_count == 4

        # Each sub-op result is nested under its key
        assert result["status"] == "ok"
        assert result["placement"]["violations_count"] == 0
        assert result["footprint_overlaps"]["overlap_count"] == 0
        assert result["pad_clearances"]["violation_count"] == 0
        assert result["keepouts"]["keepout_count"] == 1

        # Aggregate summary fields
        assert "total_issues" in result
        assert "summary" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_all_summary_vs_full_different_shape(self, mock_run, audit_server, pcb_file):
        """summary and full return structurally different output."""
        summary_result = {
            "status": "ok",
            "total_footprints": 2,
            "total_issues": 1,
            "footprint_overlaps": [{"ref_a": "R1", "ref_b": "R2", "overlap": True, "overlap_mm2": 1.0}],
            "keepout_violations": [],
            "silkscreen_overlaps": [],
            "silkscreen_text_overlaps": [],
            "summary": "1 footprint overlap(s)",
        }
        placement_result = {
            "status": "ok", "total_footprints": 2, "violations_count": 0,
            "clean_count": 2, "violations": [], "summary": "",
        }
        overlaps_result = {
            "status": "ok", "total_footprints": 2, "pairs_checked": 1,
            "overlap_count": 1, "error_count": 1, "warning_count": 0,
            "overlaps": [{"ref_a": "R1", "ref_b": "R2", "overlap": True,
                          "overlap_mm2": 1.0, "gap_mm": -0.5,
                          "bbox_a": {}, "bbox_b": {}, "value_a": "10k", "value_b": "10k",
                          "bbox_source_a": "body", "bbox_source_b": "body",
                          "severity": "error", "message": "R1 and R2 physically overlap"}],
            "summary": "1 overlap(s) found",
        }
        pad_cl_result = {
            "status": "ok", "total_pads": 4, "min_clearance_mm": 0.2,
            "min_clearance_source": "board", "violation_count": 0,
            "footprint_pairs_affected": 0, "footprint_pair_summary": [],
            "violations": [], "violations_truncated": False, "summary": "",
        }
        keepouts_result = {
            "status": "ok", "keepout_count": 0, "keepouts": [],
        }

        fn = _get_audit_fn(audit_server)

        # summary: single call
        mock_run.side_effect = [summary_result]
        result_summary = fn("all", pcb_path=pcb_file, detail="summary")
        assert "footprint_overlaps" in result_summary  # abridged list
        assert "placement" not in result_summary        # no nested ops

        # full: four calls
        mock_run.side_effect = [placement_result, overlaps_result, pad_cl_result, keepouts_result]
        result_full = fn("all", pcb_path=pcb_file, detail="full")
        assert "placement" in result_full               # nested op present
        assert "footprint_overlaps" in result_full      # nested op present
        assert "pad_clearances" in result_full          # nested op present
        # full result has per-footprint detail (violations list)
        assert "violations" in result_full["placement"]
        assert "overlaps" in result_full["footprint_overlaps"]
