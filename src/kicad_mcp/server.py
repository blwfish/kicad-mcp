"""KiCad MCP server — entry point and tool registration."""

import logging
from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def create_server() -> FastMCP:
    """Create and configure the KiCad MCP server."""
    mcp = FastMCP("KiCad")

    # PCB domain router (phase 4: replaces 27 individual pcb_* tools)
    from kicad_mcp.tools.pcb import register_pcb_tools
    from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools
    from kicad_mcp.tools.pcb_autoroute import register_pcb_autoroute_tools
    from kicad_mcp.tools.pcb_panelize import register_pcb_panelize_tools
    from kicad_mcp.tools.pcb_planning import register_pcb_planning_tools
    from kicad_mcp.tools.pcb_drc_fix import register_pcb_drc_fix_tools
    from kicad_mcp.tools.pcb_pipeline import register_pipeline_tools

    register_pcb_tools(mcp)
    register_pcb_keepout_tools(mcp)
    register_pcb_autoroute_tools(mcp)
    register_pcb_panelize_tools(mcp)
    register_pcb_planning_tools(mcp)
    register_pcb_drc_fix_tools(mcp)
    register_pipeline_tools(mcp)

    # Domain routers (phase 1: library + analyze + export)
    from kicad_mcp.tools.library import register_library_tools
    from kicad_mcp.tools.analyze import register_analyze_tools
    from kicad_mcp.tools.export import register_export_tools

    register_library_tools(mcp)
    register_analyze_tools(mcp)
    register_export_tools(mcp)

    # Domain routers (phase 2: project + drc + autoroute)
    from kicad_mcp.tools.project import register_project_tools
    from kicad_mcp.tools.drc import register_drc_tools

    register_project_tools(mcp)
    register_drc_tools(mcp)

    # Domain routers (phase 3: audit)
    # Note: register_pcb_keepout_tools is imported above and already called.

    # Schematic router (phase 5: replaces 32 individual schematic tools + find_component_connections)
    from kicad_mcp.tools.schematic_router import register_schematic_router

    register_schematic_router(mcp)

    logger.info("KiCad MCP server initialized with all tool modules")
    return mcp


def main() -> None:
    """Start the KiCad MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logger.info("Starting KiCad MCP server...")
    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
