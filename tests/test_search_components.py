"""
Tests for the search_components MCP tool.

The underlying LibraryIndex parsing and search are tested in test_library_index.py.
This file only tests the MCP tool wrapper integration.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.schematic import register_schematic_tools
import kicad_mcp.tools.schematic as sch_module


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def sch_server():
    """Create a FastMCP server with only schematic tools registered."""
    mcp = FastMCP("test-search")
    register_schematic_tools(mcp)
    return mcp


@pytest.fixture(autouse=True)
def reset_schematic_state():
    """Reset the module-level schematic state between tests."""
    sch_module._current_schematic = None
    yield
    sch_module._current_schematic = None


def _get_tool_fn(mcp_server, tool_name):
    """Extract a tool function from the FastMCP 3.0 server by name."""
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- search_components MCP tool tests ----------------------------------------

class TestSearchComponentsTool:
    """Tests for the search_components MCP tool wrapper."""

    def test_tool_returns_structured_result(self, sch_server):
        """search_components should return status and results list."""
        fn = _get_tool_fn(sch_server, "search_components")
        # Use a query that's unlikely to match much to keep it fast
        result = fn(query="zzz_no_match_expected", limit=5)
        assert result["status"] == "ok"
        assert "count" in result
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_tool_respects_limit(self, sch_server):
        """Results should not exceed the limit parameter."""
        fn = _get_tool_fn(sch_server, "search_components")
        result = fn(query="R", limit=3)
        assert result["status"] == "ok"
        assert len(result["results"]) <= 3

    def test_tool_result_fields(self, sch_server):
        """Each result should have lib_id, name, description, etc."""
        fn = _get_tool_fn(sch_server, "search_components")
        result = fn(query="resistor", limit=1)
        if result["count"] > 0:
            item = result["results"][0]
            assert "lib_id" in item
            assert "name" in item
            assert "description" in item
