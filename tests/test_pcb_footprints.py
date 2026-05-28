"""
Tests for PCB footprint tools: place, move, list, pad positions, dimensions, search.

Unit tests mock run_pcbnew_script to test tool logic without requiring
KiCad's Python 3.9 / pcbnew bindings.
"""

import asyncio
from unittest.mock import patch, MagicMock

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb import register_pcb_tools
from kicad_mcp.tools.library import register_library_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def pcb_server():
    mcp = FastMCP("test-pcb")
    register_pcb_tools(mcp)
    return mcp


@pytest.fixture
def library_server():
    mcp = FastMCP("test-library")
    register_library_tools(mcp)
    return mcp


@pytest.fixture
def library_server():
    mcp = FastMCP("test-library")
    register_library_tools(mcp)
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


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- place_footprint tests ---------------------------------------------------

class TestPlaceFootprint:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("place_footprint",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    library="Resistor_SMD", footprint_name="R_0805",
                    reference="R1", value="10k", x_mm=100, y_mm=80)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_returns_placement_info(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "placed": {
                "reference": "R1",
                "footprint": "Resistor_SMD:R_0805",
                "x_mm": 100.0,
                "y_mm": 80.0,
                "rotation": 0,
                "layer": "F.Cu",
            },
            "bounding_box": {
                "x_min_mm": 98.5, "y_min_mm": 79.5,
                "x_max_mm": 101.5, "y_max_mm": 80.5,
                "width_mm": 3.0, "height_mm": 1.0,
            },
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("place_footprint",
                    pcb_path=pcb_file,
                    library="Resistor_SMD", footprint_name="R_0805",
                    reference="R1", value="10k", x_mm=100, y_mm=80)
        assert result["status"] == "ok"
        assert result["placed"]["reference"] == "R1"
        assert "bounding_box" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_passes_all_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "placed": {}, "bounding_box": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("place_footprint",
           pcb_path=pcb_file,
           library="LED_SMD", footprint_name="LED_0805",
           reference="D1", value="RED",
           x_mm=50, y_mm=60,
           rotation_deg=90, layer="B.Cu")
        params = mock_run.call_args[1]["params"]
        assert params["library"] == "LED_SMD"
        assert params["footprint_name"] == "LED_0805"
        assert params["reference"] == "D1"
        assert params["value"] == "RED"
        assert params["x_mm"] == 50
        assert params["y_mm"] == 60
        assert params["rotation_deg"] == 90
        assert params["layer"] == "B.Cu"

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_placement_warnings(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "placed": {"reference": "U1", "footprint": "RF:ESP32",
                       "x_mm": 130, "y_mm": 76, "rotation": 0, "layer": "F.Cu"},
            "bounding_box": {},
            "placement_warnings": ["Overlaps keepout from U1 (blocks tracks, vias)"],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("place_footprint",
                    pcb_path=pcb_file,
                    library="RF_Module", footprint_name="ESP32",
                    reference="U1", value="ESP32",
                    x_mm=130, y_mm=76)
        assert "placement_warnings" in result
        assert len(result["placement_warnings"]) == 1

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_keepout_check_included_by_default(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "placed": {}, "bounding_box": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("place_footprint",
           pcb_path=pcb_file, library="R", footprint_name="R_0805",
           reference="R1", value="1k", x_mm=100, y_mm=80)
        script = mock_run.call_args[0][0]
        assert "extract_keepouts" in script

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_keepout_check_disabled(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "placed": {}, "bounding_box": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("place_footprint",
           pcb_path=pcb_file, library="R", footprint_name="R_0805",
           reference="R1", value="1k", x_mm=100, y_mm=80,
           check_keepouts=False)
        script = mock_run.call_args[0][0]
        assert "extract_keepouts" not in script


# -- move_footprint tests ----------------------------------------------------

class TestMoveFootprint:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("move_footprint",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    reference="R1", x_mm=100, y_mm=80)
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_moves_footprint(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "reference": "R1",
            "x_mm": 120.0,
            "y_mm": 90.0,
            "rotation": 0,
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("move_footprint",
                    pcb_path=pcb_file, reference="R1", x_mm=120, y_mm=90)
        assert result["status"] == "ok"
        assert result["x_mm"] == 120.0

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_move_with_rotation(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "reference": "R1",
                                  "x_mm": 100, "y_mm": 80, "rotation": 45}
        fn = _get_pcb_fn(pcb_server)
        fn("move_footprint",
           pcb_path=pcb_file, reference="R1", x_mm=100, y_mm=80,
           rotation_deg=45)
        params = mock_run.call_args[1]["params"]
        assert params["rotation_deg"] == 45

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_move_without_rotation(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "reference": "R1",
                                  "x_mm": 100, "y_mm": 80, "rotation": 0}
        fn = _get_pcb_fn(pcb_server)
        fn("move_footprint",
           pcb_path=pcb_file, reference="R1", x_mm=100, y_mm=80)
        params = mock_run.call_args[1]["params"]
        assert params["rotation_deg"] is None


# -- list_footprints tests ---------------------------------------------------

class TestListFootprints:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("list_footprints", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_lists_footprints(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "footprint_count": 2,
            "footprints": [
                {"reference": "R1", "value": "10k", "footprint": "R_0805",
                 "x_mm": 100, "y_mm": 80, "rotation": 0, "layer": "F.Cu",
                 "pads": [{"number": "1", "x_mm": 99, "y_mm": 80, "net": "GND"}]},
                {"reference": "C1", "value": "100nF", "footprint": "C_0805",
                 "x_mm": 105, "y_mm": 80, "rotation": 0, "layer": "F.Cu",
                 "pads": []},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("list_footprints", pcb_path=pcb_file)
        assert result["footprint_count"] == 2
        assert result["footprints"][0]["reference"] == "R1"


# -- get_pad_positions tests -------------------------------------------------

class TestGetPadPositions:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("get_pad_positions",
                    pcb_path="/nonexistent/board.kicad_pcb", reference="R1")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_returns_pads(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "reference": "U1",
            "pad_count": 4,
            "pads": [
                {"number": "1", "x_mm": 100, "y_mm": 80, "net": "VCC", "shape": "Rect"},
                {"number": "2", "x_mm": 100, "y_mm": 81.27, "net": "GND", "shape": "Oval"},
                {"number": "3", "x_mm": 102.54, "y_mm": 81.27, "net": "SDA", "shape": "Oval"},
                {"number": "4", "x_mm": 102.54, "y_mm": 80, "net": "SCL", "shape": "Oval"},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("get_pad_positions", pcb_path=pcb_file, reference="U1")
        assert result["pad_count"] == 4
        assert result["pads"][0]["net"] == "VCC"


# -- get_footprint_dimensions tests ------------------------------------------

class TestGetFootprintDimensions:

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_returns_dimensions(self, mock_run, pcb_server):
        mock_run.return_value = {
            "status": "ok",
            "library": "Resistor_SMD",
            "footprint": "R_0805_2012Metric",
            "rotation_deg": 0,
            "pad_count": 2,
            "body_bbox": {"x_min_mm": -1.5, "y_min_mm": -0.65,
                         "x_max_mm": 1.5, "y_max_mm": 0.65,
                         "width_mm": 3.0, "height_mm": 1.3},
            "pad_span": {"x_min_mm": -1.1, "y_min_mm": -0.5,
                        "x_max_mm": 1.1, "y_max_mm": 0.5,
                        "width_mm": 2.2, "height_mm": 1.0},
            "courtyard": {"x_min_mm": -1.7, "y_min_mm": -0.85,
                         "x_max_mm": 1.7, "y_max_mm": 0.85,
                         "width_mm": 3.4, "height_mm": 1.7},
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("get_footprint_dimensions",
                    library="Resistor_SMD", footprint_name="R_0805_2012Metric")
        assert result["status"] == "ok"
        assert result["pad_count"] == 2
        assert "body_bbox" in result
        assert "courtyard" in result

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_with_rotation(self, mock_run, pcb_server):
        mock_run.return_value = {"status": "ok", "library": "R", "footprint": "R_0805",
                                  "rotation_deg": 90, "pad_count": 2,
                                  "body_bbox": {}, "pad_span": {}}
        fn = _get_pcb_fn(pcb_server)
        fn("get_footprint_dimensions",
           library="R", footprint_name="R_0805", rotation_deg=90)
        params = mock_run.call_args[1]["params"]
        assert params["rotation_deg"] == 90

    @patch("kicad_mcp.tools.pcb_footprints.run_pcbnew_script")
    def test_with_keepout_zones(self, mock_run, pcb_server):
        mock_run.return_value = {
            "status": "ok",
            "library": "RF_Module",
            "footprint": "ESP32-WROOM-32E",
            "rotation_deg": 0,
            "pad_count": 39,
            "body_bbox": {},
            "pad_span": {},
            "keepout_zones": [
                {"bounding_box": {"x_min_mm": -9, "y_min_mm": -25,
                                  "x_max_mm": 9, "y_max_mm": -18,
                                  "width_mm": 18, "height_mm": 7},
                 "constraints": {"no_tracks": True, "no_vias": True,
                                "no_pads": True, "no_copper_pour": True,
                                "no_footprints": True}},
            ],
            "keepout_count": 1,
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("get_footprint_dimensions",
                    library="RF_Module", footprint_name="ESP32-WROOM-32E")
        assert result["keepout_count"] == 1


# -- library router → search operation (was: search tool) ------------------

class TestSearch:

    @patch("kicad_mcp.utils.library_index.get_library_index")
    def test_returns_footprint_results(self, mock_get_index, library_server):
        mock_index = MagicMock()
        mock_index.footprints_stale.return_value = False
        mock_index.search_footprints.return_value = [
            {"library": "Resistor_SMD", "name": "R_0805_2012Metric",
             "description": "0805 resistor"},
        ]
        mock_get_index.return_value = mock_index

        fn = _get_tool_fn(library_server, "library")
        result = fn(operation="search", query="0805 resistor")
        assert result["status"] == "ok"
        assert result["count"] == 1

    @patch("kicad_mcp.utils.library_index.get_library_index")
    def test_rebuilds_stale_footprint_index(self, mock_get_index, library_server):
        mock_index = MagicMock()
        mock_index.footprints_stale.return_value = True
        mock_index.rebuild_footprints.return_value = 500
        mock_index.search_footprints.return_value = []
        mock_get_index.return_value = mock_index

        fn = _get_tool_fn(library_server, "library")
        fn(operation="search", query="something")
        mock_index.rebuild_footprints.assert_called_once()

    @patch("kicad_mcp.utils.library_index.get_library_index")
    def test_with_library_filter(self, mock_get_index, library_server):
        mock_index = MagicMock()
        mock_index.footprints_stale.return_value = False
        mock_index.search_footprints.return_value = []
        mock_get_index.return_value = mock_index

        fn = _get_tool_fn(library_server, "library")
        fn(operation="search", query="0805", library="Resistor_SMD", limit=5)
        mock_index.search_footprints.assert_called_once_with(
            "0805", library="Resistor_SMD", limit=5
        )

    @patch("kicad_mcp.utils.library_index.get_library_index")
    def test_handles_exception(self, mock_get_index, library_server):
        mock_get_index.side_effect = RuntimeError("DB locked")
        fn = _get_tool_fn(library_server, "library")
        result = fn(operation="search", query="anything")
        assert "error" in result

    @patch("kicad_mcp.utils.library_index.get_library_index")
    def test_symbol_type_uses_symbol_search(self, mock_get_index, library_server):
        mock_index = MagicMock()
        mock_index.symbols_stale.return_value = False
        mock_index.search_symbols.return_value = [
            {"lib_id": "Device:R", "name": "R", "description": "Resistor"},
        ]
        mock_get_index.return_value = mock_index

        fn = _get_tool_fn(library_server, "library")
        result = fn(operation="search", query="resistor", type="symbol")
        assert result["status"] == "ok"
        assert result["count"] == 1
        mock_index.search_symbols.assert_called_once_with(
            "resistor", library=None, limit=20
        )
        mock_index.search_footprints.assert_not_called()

    def test_rejects_invalid_type(self, library_server):
        fn = _get_tool_fn(library_server, "library")
        result = fn(operation="search", query="anything", type="garbage")
        assert "error" in result
        assert "footprint" in result["error"] and "symbol" in result["error"]

    def test_missing_query(self, library_server):
        fn = _get_tool_fn(library_server, "library")
        result = fn(operation="search")
        assert "error" in result
        assert "query" in result["error"]

    def test_unknown_operation(self, library_server):
        fn = _get_tool_fn(library_server, "library")
        result = fn(operation="bogus")
        assert "error" in result
        assert "unknown operation" in result["error"]
