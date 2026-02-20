"""Shared test fixtures for kicad-mcp tests."""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.server import create_server


@pytest.fixture
def mcp_server() -> FastMCP:
    """Create a fully configured KiCad MCP server with all tools registered."""
    return create_server()


@pytest.fixture
def pcb_file(tmp_path) -> str:
    """Create a minimal .kicad_pcb file for path-existence checks."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        ")\n"
    )
    return str(pcb)


def get_tool_fn(mcp_server: FastMCP, tool_name: str):
    """Extract a tool's underlying function from a FastMCP 3.0 server.

    In FastMCP 3.0, tools are retrieved via ``asyncio.run(mcp.get_tool(name))``
    and the raw callable lives on ``tool.fn``.
    """
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not registered on server")
    return tool.fn
