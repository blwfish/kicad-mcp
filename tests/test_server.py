"""Basic sanity tests for the KiCad MCP server."""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.server import create_server


class TestCreateServer:
    """Verify that create_server() returns a properly configured FastMCP instance."""

    def test_returns_fastmcp_instance(self):
        server = create_server()
        assert isinstance(server, FastMCP)

    def test_server_name(self):
        server = create_server()
        assert server.name == "KiCad"

    def test_at_least_60_tools_registered(self, mcp_server):
        tools = asyncio.run(mcp_server.list_tools())
        assert len(tools) >= 60, (
            f"Expected at least 60 tools, got {len(tools)}"
        )

    def test_current_tool_count(self, mcp_server):
        """Snapshot test: currently 78 tools. Update if tools are added/removed."""
        tools = asyncio.run(mcp_server.list_tools())
        assert len(tools) == 97, (
            f"Expected 97 tools, got {len(tools)}. "
            "Update this test if tools were intentionally added or removed."
        )


class TestExpectedToolsExist:
    """Verify that specific important tools are registered."""

    EXPECTED_TOOLS = [
        # PCB board tools
        "create_pcb",
        "load_pcb",
        "add_board_outline",
        # PCB footprint tools
        "place_footprint",
        "move_footprint",
        "list_pcb_footprints",
        "get_pad_positions",
        "get_footprint_dimensions",
        "search_footprints",
        # PCB net tools
        "add_net",
        "assign_pad_net",
        "bulk_assign_pad_nets",
        "list_pcb_nets",
        # PCB routing tools
        "add_trace",
        "add_via",
        "clear_routing",
        "set_design_rules",
        # PCB zone tools
        "add_copper_zone",
        "fill_zones",
        # PCB silkscreen tools
        "list_silkscreen_items",
        "update_silkscreen_item",
        "check_silkscreen_overlaps",
        "auto_fix_silkscreen",
        "add_text_to_pcb",
        # PCB autoroute tools
        "autoroute_pcb",
        "autoroute_pcb_async",
        "poll_autoroute",
        "cancel_autoroute",
        "list_autoroute_jobs",
        # PCB panelize tools
        "panelize_pcb",
        # PCB keepout tools
        "get_keepout_zones",
        "get_board_constraints",
        "validate_placement",
        "audit_pcb_placement",
        "audit_footprint_overlaps",
        "audit_all",
        "pre_route_check",
        "auto_fix_placement",
        # PCB DRC fix tools
        "drc_autofix",
        # PCB planning tools
        "estimate_board_size",
        "suggest_placement",
        # Project tools
        "list_projects",
        "get_project_structure",
        "open_project",
        "validate_project",
        # Export / DRC / BOM / netlist / patterns
        "export_gerbers",
        "generate_pcb_thumbnail",
        "run_drc_check",
        "analyze_bom",
        "export_bom_csv",
        "extract_schematic_netlist",
        "extract_project_netlist",
        "identify_circuit_patterns",
        "analyze_project_circuit_patterns",
        # Schematic tools
        "create_schematic",
        "load_schematic",
        "save_schematic",
        "add_component",
        "remove_component",
        "list_components",
        "search_components",
        "add_wire",
        "remove_wire",
        "add_label",
        "remove_label",
        "add_junction",
        "get_component_pin_position",
        "list_component_pins",
        "add_label_to_pin",
        "connect_pins_with_labels",
        "check_pin_collisions",
        "validate_schematic",
        "get_schematic_info",
    ]

    @pytest.fixture(autouse=True)
    def _load_tool_names(self, mcp_server):
        tools = asyncio.run(mcp_server.list_tools())
        self.tool_names = {t.name for t in tools}

    @pytest.mark.parametrize("tool_name", EXPECTED_TOOLS)
    def test_tool_exists(self, tool_name):
        assert tool_name in self.tool_names, (
            f"Expected tool {tool_name!r} not found among registered tools"
        )
