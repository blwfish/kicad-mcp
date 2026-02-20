"""KiCad MCP server — entry point and tool registration."""

import logging
from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def create_server() -> FastMCP:
    """Create and configure the KiCad MCP server."""
    mcp = FastMCP("KiCad")

    # PCB tools (split from former monolithic pcb_tools.py)
    from kicad_mcp.tools.pcb_board import register_pcb_board_tools
    from kicad_mcp.tools.pcb_footprints import register_pcb_footprint_tools
    from kicad_mcp.tools.pcb_nets import register_pcb_net_tools
    from kicad_mcp.tools.pcb_routing import register_pcb_routing_tools
    from kicad_mcp.tools.pcb_zones import register_pcb_zone_tools
    from kicad_mcp.tools.pcb_silkscreen import register_pcb_silkscreen_tools
    from kicad_mcp.tools.pcb_keepout import register_pcb_keepout_tools
    from kicad_mcp.tools.pcb_autoroute import register_pcb_autoroute_tools

    register_pcb_board_tools(mcp)
    register_pcb_footprint_tools(mcp)
    register_pcb_net_tools(mcp)
    register_pcb_routing_tools(mcp)
    register_pcb_zone_tools(mcp)
    register_pcb_silkscreen_tools(mcp)
    register_pcb_keepout_tools(mcp)
    register_pcb_autoroute_tools(mcp)

    # Upstream tools (project, export, DRC, BOM, netlist, patterns)
    from kicad_mcp.tools.project import register_project_tools
    from kicad_mcp.tools.export import register_export_tools
    from kicad_mcp.tools.drc import register_drc_tools
    from kicad_mcp.tools.bom import register_bom_tools
    from kicad_mcp.tools.netlist import register_netlist_tools
    from kicad_mcp.tools.patterns import register_pattern_tools

    register_project_tools(mcp)
    register_export_tools(mcp)
    register_drc_tools(mcp)
    register_bom_tools(mcp)
    register_netlist_tools(mcp)
    register_pattern_tools(mcp)

    # Schematic tools (wrapping kicad-sch-api)
    from kicad_mcp.tools.schematic import register_schematic_tools

    register_schematic_tools(mcp)

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
