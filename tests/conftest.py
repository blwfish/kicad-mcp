"""Shared test fixtures for kicad-mcp tests."""

import asyncio
import json

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


@pytest.fixture
def pcb_file_with_project(tmp_path) -> dict:
    """Create a .kicad_pcb file alongside a .kicad_pro project file.

    Returns a dict with 'pcb_path' and 'pro_path' keys.
    """
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        '  (net 2 "VCC")\n'
        ")\n"
    )
    pro = tmp_path / "test.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": "test.kicad_pro"}}, indent=2))
    return {"pcb_path": str(pcb), "pro_path": str(pro), "tmp_path": tmp_path}


@pytest.fixture
def tmp_project_dir(tmp_path) -> dict:
    """Create a minimal KiCad project directory with all standard files.

    Returns a dict with paths to all project files.
    """
    name = "testproject"
    pro = tmp_path / f"{name}.kicad_pro"
    pcb = tmp_path / f"{name}.kicad_pcb"
    sch = tmp_path / f"{name}.kicad_sch"

    pro.write_text(json.dumps({"meta": {"filename": f"{name}.kicad_pro"}}, indent=2))
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        '  (net 0 "")\n'
        ")\n"
    )
    sch.write_text(
        '(kicad_sch (version 20230121) (generator "test")\n'
        "  (lib_symbols)\n"
        ")\n"
    )

    return {
        "project_path": str(pro),
        "pcb_path": str(pcb),
        "sch_path": str(sch),
        "dir": str(tmp_path),
        "name": name,
    }


def get_tool_fn(mcp_server: FastMCP, tool_name: str):
    """Extract a tool's underlying function from a FastMCP 3.0 server.

    In FastMCP 3.0, tools are retrieved via ``asyncio.run(mcp.get_tool(name))``
    and the raw callable lives on ``tool.fn``.
    """
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not registered on server")
    return tool.fn
