"""
Tests for new tools: check_pin_collisions, get_footprint_dimensions,
pre_route_check, set_design_rules project file updates, and export_gerbers.
"""

import asyncio
import json
import os
import zipfile
from unittest.mock import patch, MagicMock

import pytest
from fastmcp import FastMCP

from kicad_mcp.server import create_server
from kicad_mcp.tools.pcb_board import register_pcb_board_tools
from kicad_mcp.tools.pcb_footprints import register_pcb_footprint_tools
from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools
from kicad_mcp.tools.export import register_export_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def mcp_server():
    return create_server()


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return str(pcb)


@pytest.fixture
def pcb_with_pro(tmp_path):
    """Create a PCB file with accompanying .kicad_pro file."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    pro = tmp_path / "test.kicad_pro"
    pro.write_text(json.dumps({
        "board": {
            "design_settings": {
                "rules": {
                    "min_clearance": 0.2,
                    "min_through_hole_diameter": 0.3,
                    "min_copper_edge_clearance": 0.5,
                    "min_track_width": 0.2,
                    "min_hole_to_hole": 0.25,
                    "min_via_diameter": 0.6,
                }
            }
        }
    }, indent=2))
    return str(pcb), str(pro)


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- check_pin_collisions tests ---------------------------------------------

class TestCheckPinCollisions:

    def test_tool_registered(self, mcp_server):
        fn = _get_tool_fn(mcp_server, "check_pin_collisions")
        assert fn is not None

    def test_requires_schematic(self, mcp_server):
        """Should raise RuntimeError if no schematic is loaded."""
        fn = _get_tool_fn(mcp_server, "check_pin_collisions")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn()

    def test_no_collisions_empty_schematic(self, mcp_server):
        """Creating an empty schematic should return no collisions."""
        create_fn = _get_tool_fn(mcp_server, "create_schematic")
        create_fn("test")
        fn = _get_tool_fn(mcp_server, "check_pin_collisions")
        result = fn()
        assert result["status"] == "ok"
        assert result["collision_count"] == 0

    def test_no_collisions_separate_components(self, mcp_server):
        """Two resistors placed far apart should have no pin collisions."""
        create_fn = _get_tool_fn(mcp_server, "create_schematic")
        create_fn("test")
        add_fn = _get_tool_fn(mcp_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[50, 50])
        add_fn(lib_id="Device:R", reference="R2", value="10k", position=[100, 50])
        fn = _get_tool_fn(mcp_server, "check_pin_collisions")
        result = fn()
        assert result["status"] == "ok"
        assert result["collision_count"] == 0

    def test_detects_collision(self, mcp_server):
        """Two components placed at the same position should have pin collisions."""
        create_fn = _get_tool_fn(mcp_server, "create_schematic")
        create_fn("test")
        add_fn = _get_tool_fn(mcp_server, "add_component")
        # Place two resistors at exact same position — their pins will collide
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[50, 50])
        add_fn(lib_id="Device:R", reference="R2", value="10k", position=[50, 50])
        fn = _get_tool_fn(mcp_server, "check_pin_collisions")
        result = fn()
        assert result["status"] == "ok"
        assert result["collision_count"] > 0
        # Each collision should involve pins from different components
        for collision in result["collisions"]:
            refs = {p["reference"] for p in collision["pins"]}
            assert len(refs) >= 2


# -- get_footprint_dimensions tests ------------------------------------------

class TestGetFootprintDimensions:

    @pytest.fixture
    def fp_server(self):
        mcp = FastMCP("test-fp")
        register_pcb_footprint_tools(mcp)
        return mcp

    def test_tool_registered(self, fp_server):
        fn = _get_tool_fn(fp_server, "get_footprint_dimensions")
        assert fn is not None

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_script_loads_footprint(self, mock_run, fp_server):
        mock_run.return_value = {
            "status": "ok",
            "library": "Resistor_SMD",
            "footprint": "R_0603_1608Metric",
            "rotation_deg": 0,
            "pad_count": 2,
            "body_bbox": {
                "x_min_mm": -1.0, "y_min_mm": -0.5,
                "x_max_mm": 1.0, "y_max_mm": 0.5,
                "width_mm": 2.0, "height_mm": 1.0,
            },
            "pad_span": {
                "x_min_mm": -0.8, "y_min_mm": -0.4,
                "x_max_mm": 0.8, "y_max_mm": 0.4,
                "width_mm": 1.6, "height_mm": 0.8,
            },
        }
        fn = _get_tool_fn(fp_server, "get_footprint_dimensions")
        result = fn(library="Resistor_SMD", footprint_name="R_0603_1608Metric")
        assert result["status"] == "ok"
        assert result["pad_count"] == 2
        assert "body_bbox" in result
        assert "pad_span" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_script_handles_keepout_zones(self, mock_run, fp_server):
        """ESP32-WROOM-32E should report embedded keepout zones."""
        mock_run.return_value = {
            "status": "ok",
            "library": "RF_Module",
            "footprint": "ESP32-WROOM-32E",
            "rotation_deg": 0,
            "pad_count": 39,
            "body_bbox": {
                "x_min_mm": -9.0, "y_min_mm": -12.75,
                "x_max_mm": 9.0, "y_max_mm": 12.75,
                "width_mm": 18.0, "height_mm": 25.5,
            },
            "pad_span": {
                "x_min_mm": -8.0, "y_min_mm": -11.0,
                "x_max_mm": 8.0, "y_max_mm": 11.0,
                "width_mm": 16.0, "height_mm": 22.0,
            },
            "keepout_zones": [
                {
                    "bounding_box": {
                        "x_min_mm": -9.0, "y_min_mm": -12.75,
                        "x_max_mm": 9.0, "y_max_mm": -5.0,
                        "width_mm": 18.0, "height_mm": 7.75,
                    },
                    "constraints": {
                        "no_tracks": True, "no_vias": True,
                        "no_pads": True, "no_copper_pour": True,
                        "no_footprints": True,
                    },
                }
            ],
            "keepout_count": 1,
        }
        fn = _get_tool_fn(fp_server, "get_footprint_dimensions")
        result = fn(library="RF_Module", footprint_name="ESP32-WROOM-32E")
        assert "keepout_zones" in result
        assert result["keepout_count"] == 1
        kz = result["keepout_zones"][0]
        assert kz["constraints"]["no_footprints"] is True

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_rotation_passed_to_script(self, mock_run, fp_server):
        mock_run.return_value = {"status": "ok", "rotation_deg": 90}
        fn = _get_tool_fn(fp_server, "get_footprint_dimensions")
        fn(library="Resistor_SMD", footprint_name="R_0603_1608Metric", rotation_deg=90)
        script = mock_run.call_args[0][0]
        assert "90" in script


# -- pre_route_check tests --------------------------------------------------

class TestPreRouteCheck:

    @pytest.fixture
    def keepout_server(self):
        mcp = FastMCP("test-keepout")
        register_pcb_keepout_tools(mcp)
        return mcp

    def test_tool_registered(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "pre_route_check")
        assert fn is not None

    def test_file_not_found(self, keepout_server):
        fn = _get_tool_fn(keepout_server, "pre_route_check")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_route_ready_true(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "route_ready": True,
            "total_footprints": 10,
            "total_pads": 30,
            "min_clearance_mm": 0.2,
            "error_count": 0,
            "warning_count": 0,
            "courtyard_overlaps": [],
            "keepout_violations": [],
            "pad_violations": [],
            "errors": [],
            "warnings": [],
            "summary": "Ready to route: 10 footprints, 30 pads all clear",
        }
        fn = _get_tool_fn(keepout_server, "pre_route_check")
        result = fn(pcb_file)
        assert result["route_ready"] is True
        assert result["error_count"] == 0

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_route_ready_false_with_overlaps(self, mock_run, keepout_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "route_ready": False,
            "total_footprints": 10,
            "total_pads": 30,
            "min_clearance_mm": 0.2,
            "error_count": 1,
            "warning_count": 0,
            "courtyard_overlaps": [
                {"ref_a": "R1", "ref_b": "U1", "overlap_mm2": 5.0},
            ],
            "keepout_violations": [],
            "pad_violations": [],
            "errors": ["Courtyard overlap: R1 and U1 (5.0 mm2)"],
            "warnings": [],
            "summary": "NOT ready to route: 1 courtyard overlap(s)",
        }
        fn = _get_tool_fn(keepout_server, "pre_route_check")
        result = fn(pcb_file)
        assert result["route_ready"] is False
        assert result["error_count"] == 1
        assert len(result["courtyard_overlaps"]) == 1

    @patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
    def test_script_checks_all_three(self, mock_run, keepout_server, pcb_file):
        """Script should contain courtyard, keepout, and pad clearance checks."""
        mock_run.return_value = {
            "status": "ok", "route_ready": True, "total_footprints": 0,
            "total_pads": 0, "min_clearance_mm": 0.2,
            "error_count": 0, "warning_count": 0,
            "courtyard_overlaps": [], "keepout_violations": [],
            "pad_violations": [], "errors": [], "warnings": [],
            "summary": "",
        }
        fn = _get_tool_fn(keepout_server, "pre_route_check")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        # Courtyard check
        assert "CrtYd" in script
        # Keepout check
        assert "extract_keepouts" in script
        # Pad clearance check
        assert "pad.GetPosition()" in script or "fp.Pads()" in script
        # Route ready flag
        assert "route_ready" in script


# -- set_design_rules project file tests ------------------------------------

class TestSetDesignRulesProjectFile:

    @pytest.fixture
    def board_server(self):
        mcp = FastMCP("test-board")
        register_pcb_board_tools(mcp)
        return mcp

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_updates_kicad_pro(self, mock_run, board_server, pcb_with_pro):
        pcb_path, pro_path = pcb_with_pro
        mock_run.return_value = {
            "status": "ok",
            "design_rules": {
                "min_track_width_mm": 0.2,
                "min_clearance_mm": 0.2,
                "min_via_diameter_mm": 0.6,
                "min_via_drill_mm": 0.3,
                "min_hole_to_hole_mm": 0.25,
                "min_through_hole_diameter_mm": 0.15,
                "min_copper_edge_clearance_mm": 0.0,
            },
        }
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn(
            pcb_path,
            min_through_hole_diameter_mm=0.15,
            min_copper_edge_clearance_mm=0.0,
        )
        assert result["project_rules_updated"] is True

        # Verify the .kicad_pro was actually updated
        with open(pro_path) as f:
            pro = json.load(f)
        rules = pro["board"]["design_settings"]["rules"]
        assert rules["min_through_hole_diameter"] == 0.15
        assert rules["min_copper_edge_clearance"] == 0.0

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_no_pro_file_still_works(self, mock_run, board_server, pcb_file):
        """When no .kicad_pro exists, PCB rules are still set."""
        mock_run.return_value = {
            "status": "ok",
            "design_rules": {},
        }
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn(pcb_file)
        assert result["project_rules_updated"] is False

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_default_values(self, mock_run, board_server, pcb_file):
        """Default min_through_hole_diameter should be 0.3mm."""
        mock_run.return_value = {"status": "ok", "design_rules": {}}
        fn = _get_tool_fn(board_server, "set_design_rules")
        fn(pcb_file)
        script = mock_run.call_args[0][0]
        # Script should contain the default via drill value
        assert "0.3" in script  # min_via_drill_mm default

    @patch("kicad_mcp.tools.pcb_board.run_pcbnew_script")
    def test_creates_rules_section_if_missing(self, mock_run, board_server, tmp_path):
        """If .kicad_pro exists but has no rules section, create it."""
        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")  # Empty project file

        mock_run.return_value = {"status": "ok", "design_rules": {}}
        fn = _get_tool_fn(board_server, "set_design_rules")
        result = fn(str(pcb), min_through_hole_diameter_mm=0.15)
        assert result["project_rules_updated"] is True

        with open(str(pro)) as f:
            data = json.load(f)
        assert data["board"]["design_settings"]["rules"]["min_through_hole_diameter"] == 0.15


# -- export_gerbers tests ---------------------------------------------------

class TestExportGerbers:

    @pytest.fixture
    def export_server(self):
        mcp = FastMCP("test-export")
        register_export_tools(mcp)
        return mcp

    def test_tool_registered(self, export_server):
        fn = _get_tool_fn(export_server, "export_gerbers")
        assert fn is not None

    def test_file_not_found(self, export_server):
        fn = _get_tool_fn(export_server, "export_gerbers")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.export.get_kicad_cli_path")
    @patch("kicad_mcp.tools.export.subprocess.run")
    def test_creates_gerbers_and_zip(self, mock_run, mock_cli, export_server, tmp_path):
        """Successful export should produce files and a ZIP."""
        mock_cli.return_value = "/usr/bin/kicad-cli"
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        # Create a fake PCB file and simulate kicad-cli output
        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        gerber_dir = tmp_path / "gerbers"
        gerber_dir.mkdir()

        def fake_run(cmd, **kwargs):
            # Simulate kicad-cli creating output files
            if "gerbers" in cmd:
                (gerber_dir / "test-F_Cu.gbr").write_text("G04*")
                (gerber_dir / "test-B_Cu.gbr").write_text("G04*")
                (gerber_dir / "test-Edge_Cuts.gbr").write_text("G04*")
            elif "drill" in cmd:
                (gerber_dir / "test.drl").write_text("M48")
            return MagicMock(returncode=0, stdout="ok", stderr="")

        mock_run.side_effect = fake_run

        fn = _get_tool_fn(export_server, "export_gerbers")
        result = fn(str(pcb), output_dir=str(gerber_dir))

        assert result["status"] == "ok"
        assert result["gerber_count"] == 3
        assert result["drill_count"] == 1
        assert result["total_files"] == 4
        assert "zip_path" in result
        assert result["zip_path"].endswith("-gerbers.zip")
        # Verify the ZIP is valid and has the right contents
        with zipfile.ZipFile(result["zip_path"]) as zf:
            names = zf.namelist()
            assert len(names) == 4
            assert "test-F_Cu.gbr" in names
            assert "test.drl" in names

    @patch("kicad_mcp.tools.export.get_kicad_cli_path")
    @patch("kicad_mcp.tools.export.subprocess.run")
    def test_no_zip_when_disabled(self, mock_run, mock_cli, export_server, tmp_path):
        """create_zip=False should skip ZIP creation."""
        mock_cli.return_value = "/usr/bin/kicad-cli"

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        gerber_dir = tmp_path / "gerbers"
        gerber_dir.mkdir()

        def fake_run(cmd, **kwargs):
            if "gerbers" in cmd:
                (gerber_dir / "test-F_Cu.gbr").write_text("G04*")
            elif "drill" in cmd:
                (gerber_dir / "test.drl").write_text("M48")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run

        fn = _get_tool_fn(export_server, "export_gerbers")
        result = fn(str(pcb), output_dir=str(gerber_dir), create_zip=False)

        assert result["status"] == "ok"
        assert "zip_path" not in result

    @patch("kicad_mcp.tools.export.get_kicad_cli_path")
    @patch("kicad_mcp.tools.export.subprocess.run")
    def test_default_output_dir(self, mock_run, mock_cli, export_server, tmp_path):
        """Default output_dir should be 'gerbers/' next to the PCB file."""
        mock_cli.return_value = "/usr/bin/kicad-cli"

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        def fake_run(cmd, **kwargs):
            # Extract output dir from command
            out_idx = cmd.index("--output") + 1
            out_dir = cmd[out_idx].rstrip("/")
            os.makedirs(out_dir, exist_ok=True)
            if "gerbers" in cmd:
                open(os.path.join(out_dir, "test-F_Cu.gbr"), "w").write("G04*")
            elif "drill" in cmd:
                open(os.path.join(out_dir, "test.drl"), "w").write("M48")
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_run

        fn = _get_tool_fn(export_server, "export_gerbers")
        result = fn(str(pcb))

        assert result["status"] == "ok"
        assert result["output_dir"] == str(tmp_path / "gerbers")


# -- autoroute preflight tests ----------------------------------------------

class TestAutoroutePreflight:

    @patch("kicad_mcp.tools.pcb_autoroute._run_auto_fix_placement")
    @patch("kicad_mcp.tools.pcb_autoroute._run_pre_route_check")
    @patch("kicad_mcp.tools.pcb_autoroute._run_full_autoroute")
    @patch("kicad_mcp.tools.pcb_autoroute._find_java")
    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar")
    def test_clean_board_skips_fix(self, mock_jar, mock_java, mock_route,
                                    mock_check, mock_fix, mcp_server, pcb_file):
        """When pre-route check is clean, auto_fix should NOT be called."""
        mock_jar.return_value = "/fake/freerouter.jar"
        mock_java.return_value = "/usr/bin/java"
        mock_check.return_value = {
            "status": "ok", "route_ready": True,
            "courtyard_overlaps": 0, "pad_violations": 0,
            "error_count": 0, "errors": [],
        }
        mock_route.return_value = {"status": "ok", "tracks_after": 100, "vias_after": 10}

        fn = _get_tool_fn(mcp_server, "autoroute_pcb")
        result = fn(pcb_file)

        assert result["status"] == "ok"
        mock_fix.assert_not_called()
        assert "preflight" not in result

    @patch("kicad_mcp.tools.pcb_autoroute._run_auto_fix_placement")
    @patch("kicad_mcp.tools.pcb_autoroute._run_pre_route_check")
    @patch("kicad_mcp.tools.pcb_autoroute._run_full_autoroute")
    @patch("kicad_mcp.tools.pcb_autoroute._find_java")
    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar")
    def test_overlaps_trigger_auto_fix(self, mock_jar, mock_java, mock_route,
                                        mock_check, mock_fix, mcp_server, pcb_file):
        """Courtyard overlaps should trigger auto_fix before routing."""
        mock_jar.return_value = "/fake/freerouter.jar"
        mock_java.return_value = "/usr/bin/java"
        # First check: overlaps found
        # Second check (after fix): clean
        mock_check.side_effect = [
            {
                "status": "ok", "route_ready": False,
                "courtyard_overlaps": 2, "pad_violations": 0,
                "error_count": 2, "errors": ["Courtyard overlap: R1 and U1"],
            },
            {
                "status": "ok", "route_ready": True,
                "courtyard_overlaps": 0, "pad_violations": 0,
                "error_count": 0, "errors": [],
            },
        ]
        mock_fix.return_value = {"status": "ok", "components_moved": 1, "moved": ["R1"]}
        mock_route.return_value = {"status": "ok", "tracks_after": 100, "vias_after": 10}

        fn = _get_tool_fn(mcp_server, "autoroute_pcb")
        result = fn(pcb_file)

        assert result["status"] == "ok"
        mock_fix.assert_called_once()
        assert result["preflight"]["auto_fix_applied"] is True
        assert result["preflight"]["route_ready_after_fix"] is True

    @patch("kicad_mcp.tools.pcb_autoroute._run_auto_fix_placement")
    @patch("kicad_mcp.tools.pcb_autoroute._run_pre_route_check")
    @patch("kicad_mcp.tools.pcb_autoroute._run_full_autoroute")
    @patch("kicad_mcp.tools.pcb_autoroute._find_java")
    @patch("kicad_mcp.tools.pcb_autoroute._find_freerouter_jar")
    def test_pad_violations_warn_but_continue(self, mock_jar, mock_java, mock_route,
                                               mock_check, mock_fix, mcp_server, pcb_file):
        """Pad violations (no overlaps) should warn but still route."""
        mock_jar.return_value = "/fake/freerouter.jar"
        mock_java.return_value = "/usr/bin/java"
        mock_check.return_value = {
            "status": "ok", "route_ready": False,
            "courtyard_overlaps": 0, "pad_violations": 3,
            "error_count": 3, "errors": ["Pad clearance: R1:1 and R2:2"],
        }
        mock_route.return_value = {"status": "ok", "tracks_after": 100, "vias_after": 10}

        fn = _get_tool_fn(mcp_server, "autoroute_pcb")
        result = fn(pcb_file)

        assert result["status"] == "ok"
        mock_fix.assert_not_called()  # No overlaps, so no fix attempted
        assert result["preflight"]["pad_violations"] == 3


# -- Pipeline tests ----------------------------------------------------------

class TestBuildPcbFromSchematic:
    """Tests for the build_pcb_from_schematic pipeline tool."""

    def test_missing_project_file(self, mcp_server):
        """Non-existent project path returns error."""
        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn("/nonexistent/path.kicad_pro")
        assert "error" in result

    def test_missing_schematic(self, mcp_server, tmp_path):
        """Project exists but schematic missing returns error."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn(str(pro))
        assert "error" in result
        assert "Schematic not found" in result["error"]

    @patch("kicad_mcp.tools.pcb_pipeline._step_export_gerbers")
    @patch("kicad_mcp.tools.pcb_pipeline._step_add_zones_and_fill")
    @patch("kicad_mcp.tools.pcb_pipeline._step_autoroute")
    @patch("kicad_mcp.tools.pcb_pipeline._step_optimize_placement")
    @patch("kicad_mcp.tools.pcb_pipeline._step_inject_nets_and_assign_pads")
    @patch("kicad_mcp.tools.pcb_pipeline._step_place_footprints")
    @patch("kicad_mcp.tools.pcb_pipeline._step_create_pcb_and_outline")
    @patch("kicad_mcp.tools.pcb_pipeline._step_extract_netlist")
    def test_full_pipeline_happy_path(
        self, mock_netlist, mock_create, mock_place, mock_nets,
        mock_optimize, mock_route, mock_zones, mock_gerbers,
        mcp_server, tmp_path,
    ):
        """Full pipeline with all steps succeeding."""
        # Set up project files
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        mock_netlist.return_value = {
            "status": "ok",
            "components": {
                "R1": {"reference": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603_1608Metric"},
                "C1": {"reference": "C1", "value": "100nF", "footprint": "Capacitor_SMD:C_0603_1608Metric"},
            },
            "components_without_footprint": [],
            "nets": {"GND": [{"component": "R1", "pin": "2"}, {"component": "C1", "pin": "2"}],
                     "SIG": [{"component": "R1", "pin": "1"}, {"component": "C1", "pin": "1"}]},
            "component_count": 2,
            "net_count": 2,
            "skipped_count": 0,
        }
        mock_create.return_value = {"status": "ok", "width_mm": 30, "height_mm": 20, "auto_sized": True}
        mock_place.return_value = {"status": "ok", "placed_count": 2, "placed": [], "errors": []}
        mock_nets.return_value = {"status": "ok", "pads_assigned": 4, "assignment_errors": [],
                                  "nets_created": 2, "total_nets": 2}
        mock_optimize.return_value = {"status": "ok", "components_moved": 2}
        mock_route.return_value = {"status": "ok", "tracks_after": 20, "vias_after": 2, "best_incomplete": 0}
        mock_zones.return_value = {"status": "ok", "zones_added": 2}

        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn(str(pro))

        assert result["status"] == "ok"
        assert result["component_count"] == 2
        assert result["net_count"] == 2
        assert result["tracks"] == 20
        assert result["incomplete_nets"] == 0
        assert "export_gerbers" not in result["steps"]

    @patch("kicad_mcp.tools.pcb_pipeline._step_export_gerbers")
    @patch("kicad_mcp.tools.pcb_pipeline._step_add_zones_and_fill")
    @patch("kicad_mcp.tools.pcb_pipeline._step_autoroute")
    @patch("kicad_mcp.tools.pcb_pipeline._step_optimize_placement")
    @patch("kicad_mcp.tools.pcb_pipeline._step_inject_nets_and_assign_pads")
    @patch("kicad_mcp.tools.pcb_pipeline._step_place_footprints")
    @patch("kicad_mcp.tools.pcb_pipeline._step_create_pcb_and_outline")
    @patch("kicad_mcp.tools.pcb_pipeline._step_extract_netlist")
    def test_explicit_board_size(
        self, mock_netlist, mock_create, mock_place, mock_nets,
        mock_optimize, mock_route, mock_zones, mock_gerbers,
        mcp_server, tmp_path,
    ):
        """Explicit board dimensions are passed through to create step."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        mock_netlist.return_value = {
            "status": "ok",
            "components": {"R1": {"reference": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603"}},
            "components_without_footprint": [],
            "nets": {"SIG": [{"component": "R1", "pin": "1"}]},
            "component_count": 1, "net_count": 1, "skipped_count": 0,
        }
        mock_create.return_value = {"status": "ok", "width_mm": 22, "height_mm": 69, "auto_sized": False}
        mock_place.return_value = {"status": "ok", "placed_count": 1, "placed": [], "errors": []}
        mock_nets.return_value = {"status": "ok", "pads_assigned": 1, "nets_created": 1, "total_nets": 1}
        mock_optimize.return_value = {"status": "ok", "components_moved": 1}
        mock_route.return_value = {"status": "ok", "tracks_after": 5, "vias_after": 0, "best_incomplete": 0}
        mock_zones.return_value = {"status": "ok", "zones_added": 2}

        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn(str(pro), board_width_mm=22, board_height_mm=69)

        assert result["status"] == "ok"
        # Verify explicit dimensions were passed
        mock_create.assert_called_once_with(
            str(tmp_path / "test.kicad_pcb"), 22, 69,
            mock_netlist.return_value["components"],
        )

    @patch("kicad_mcp.tools.pcb_pipeline._step_add_zones_and_fill")
    @patch("kicad_mcp.tools.pcb_pipeline._step_autoroute")
    @patch("kicad_mcp.tools.pcb_pipeline._step_optimize_placement")
    @patch("kicad_mcp.tools.pcb_pipeline._step_inject_nets_and_assign_pads")
    @patch("kicad_mcp.tools.pcb_pipeline._step_place_footprints")
    @patch("kicad_mcp.tools.pcb_pipeline._step_create_pcb_and_outline")
    @patch("kicad_mcp.tools.pcb_pipeline._step_extract_netlist")
    def test_autoroute_failure_stops_pipeline(
        self, mock_netlist, mock_create, mock_place, mock_nets,
        mock_optimize, mock_route, mock_zones,
        mcp_server, tmp_path,
    ):
        """Autoroute error stops pipeline — no zones step."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        mock_netlist.return_value = {
            "status": "ok",
            "components": {"R1": {"reference": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603"}},
            "components_without_footprint": [],
            "nets": {"SIG": [{"component": "R1", "pin": "1"}]},
            "component_count": 1, "net_count": 1, "skipped_count": 0,
        }
        mock_create.return_value = {"status": "ok", "width_mm": 30, "height_mm": 20, "auto_sized": True}
        mock_place.return_value = {"status": "ok", "placed_count": 1, "placed": [], "errors": []}
        mock_nets.return_value = {"status": "ok", "pads_assigned": 1, "nets_created": 1, "total_nets": 1}
        mock_optimize.return_value = {"status": "ok", "components_moved": 1}
        mock_route.return_value = {"error": "FreeRouter JAR not found"}

        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn(str(pro))

        assert "status" not in result or result.get("status") != "ok"
        assert "autoroute" in result["steps"]
        assert "error" in result["steps"]["autoroute"]
        mock_zones.assert_not_called()

    @patch("kicad_mcp.tools.pcb_pipeline._step_extract_netlist")
    def test_missing_footprints_warns(self, mock_netlist, mcp_server, tmp_path):
        """Components without footprints are skipped with warning, pipeline stops if none remain."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        mock_netlist.return_value = {
            "status": "ok",
            "components": {},  # All components lacked footprints
            "components_without_footprint": ["U1", "R1"],
            "nets": {},
            "component_count": 0, "net_count": 0, "skipped_count": 2,
        }

        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn(str(pro))

        assert "error" in result
        assert "No components with footprints" in result["error"]
        assert any("2 component(s) skipped" in w for w in result.get("warnings", []))

    @patch("kicad_mcp.tools.pcb_pipeline._step_export_gerbers")
    @patch("kicad_mcp.tools.pcb_pipeline._step_add_zones_and_fill")
    @patch("kicad_mcp.tools.pcb_pipeline._step_autoroute")
    @patch("kicad_mcp.tools.pcb_pipeline._step_optimize_placement")
    @patch("kicad_mcp.tools.pcb_pipeline._step_inject_nets_and_assign_pads")
    @patch("kicad_mcp.tools.pcb_pipeline._step_place_footprints")
    @patch("kicad_mcp.tools.pcb_pipeline._step_create_pcb_and_outline")
    @patch("kicad_mcp.tools.pcb_pipeline._step_extract_netlist")
    def test_gerber_export_when_requested(
        self, mock_netlist, mock_create, mock_place, mock_nets,
        mock_optimize, mock_route, mock_zones, mock_gerbers,
        mcp_server, tmp_path,
    ):
        """Gerber export runs only when export_gerbers=True."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)")

        mock_netlist.return_value = {
            "status": "ok",
            "components": {"R1": {"reference": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0603"}},
            "components_without_footprint": [],
            "nets": {"SIG": [{"component": "R1", "pin": "1"}]},
            "component_count": 1, "net_count": 1, "skipped_count": 0,
        }
        mock_create.return_value = {"status": "ok", "width_mm": 30, "height_mm": 20, "auto_sized": True}
        mock_place.return_value = {"status": "ok", "placed_count": 1, "placed": [], "errors": []}
        mock_nets.return_value = {"status": "ok", "pads_assigned": 1, "nets_created": 1, "total_nets": 1}
        mock_optimize.return_value = {"status": "ok", "components_moved": 1}
        mock_route.return_value = {"status": "ok", "tracks_after": 5, "vias_after": 0, "best_incomplete": 0}
        mock_zones.return_value = {"status": "ok", "zones_added": 2}
        mock_gerbers.return_value = {"status": "ok", "zip_path": "/tmp/test-gerbers.zip", "total_files": 12}

        fn = _get_tool_fn(mcp_server, "build_pcb_from_schematic")
        result = fn(str(pro), export_gerbers=True)

        assert result["status"] == "ok"
        assert result["gerber_zip"] == "/tmp/test-gerbers.zip"
        mock_gerbers.assert_called_once()
