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

    def test_at_least_15_tools_registered(self, mcp_server):
        """Floor — the consolidated surface targets 15 tools."""
        tools = asyncio.run(mcp_server.list_tools())
        assert len(tools) >= 15, (
            f"Expected at least 15 tools, got {len(tools)}"
        )

    def test_current_tool_count(self, mcp_server):
        """Snapshot test: update when tools are intentionally added or removed.

        Tool-consolidation complete (see docs/SPEC_Tool_Consolidation.md).
        Final surface: 10 routers + 5 standalones = 15 tools.
          Phase 1: library + analyze + export routers (10 → 3)
          Phase 2: project + drc + autoroute routers (12 → 3)
          Phase 3: audit router replaces 9 keepout tools → 73 total
          Phase 4: pcb router replaces 27 individual pcb_* tools → 45 total
          Phase 5: schematic router replaces 32 schematic tools + find_component_connections → 13 total
          Phase 6: no-op stubs deleted; final count confirmed at 13.
          Post-consolidation: analyze_placement_telemetry added → 14 total.
          Post-consolidation: lcsc component-intelligence router added → 15 total.
        """
        tools = asyncio.run(mcp_server.list_tools())
        assert len(tools) == 15, (
            f"Expected 15 tools, got {len(tools)}. "
            "Update this test if tools were intentionally added or removed."
        )


class TestExpectedToolsExist:
    """Verify that specific important tools are registered."""

    EXPECTED_TOOLS = [
        # Phase 5 router (schematic — replaces 32 schematic tools + find_component_connections)
        "schematic",
        # Phase 4 router (pcb — replaces all individual pcb_* tools)
        "pcb",
        # PCB panelize tools
        "panelize_pcb",
        # Phase 3 router (audit)
        "audit",
        # PCB planning tools
        "estimate_board_size",
        "suggest_placement",
        # Phase 1 routers (library + analyze + export)
        "library",
        "analyze",
        "export",
        # Phase 2 routers (project + drc + autoroute)
        "project",
        "drc",
        "autoroute",
        # Pipeline tools
        "build_pcb_from_schematic",
        # Telemetry analysis (post-consolidation standalone)
        "analyze_placement_telemetry",
        # LCSC component intelligence router
        "lcsc",
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
