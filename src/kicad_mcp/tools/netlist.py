"""
Netlist extraction and analysis tools for KiCad schematics.
"""
import logging
import os
import re
from typing import Any, Dict

from fastmcp import FastMCP, Context

logger = logging.getLogger(__name__)

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.netlist_parser import analyze_netlist, extract_netlist


def register_netlist_tools(mcp: FastMCP) -> None:
    """Register netlist-related tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    async def extract_schematic_netlist(
        schematic_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Extract netlist information from a KiCad schematic.

        This tool parses a KiCad schematic file and extracts comprehensive
        netlist information including components, connections, and labels.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with netlist information
        """
        logger.debug(f"Extracting netlist from schematic: {schematic_path}")

        if not os.path.exists(schematic_path):
            logger.warning(f"Schematic file not found: {schematic_path}")
            if ctx:
                ctx.info(f"Schematic file not found: {schematic_path}")
            return {
                "success": False,
                "error": f"Schematic file not found: {schematic_path}",
            }

        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(f"Loading schematic file: {os.path.basename(schematic_path)}")

        try:
            if ctx:
                await ctx.report_progress(20, 100)
                ctx.info("Parsing schematic structure...")

            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                logger.warning(f"Error extracting netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(f"Error extracting netlist: {netlist_data['error']}")
                return {"success": False, "error": netlist_data["error"]}

            if ctx:
                await ctx.report_progress(60, 100)
                ctx.info(
                    f"Extracted {netlist_data['component_count']} components "
                    f"and {netlist_data['net_count']} nets"
                )

            if ctx:
                await ctx.report_progress(70, 100)
                ctx.info("Analyzing netlist data...")

            analysis_results = analyze_netlist(netlist_data)

            if ctx:
                await ctx.report_progress(90, 100)

            result = {
                "success": True,
                "schematic_path": schematic_path,
                "component_count": netlist_data["component_count"],
                "net_count": netlist_data["net_count"],
                "components": netlist_data["components"],
                "nets": netlist_data["nets"],
                "analysis": analysis_results,
            }

            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info("Netlist extraction complete")

            return result

        except Exception as e:
            logger.warning(f"Error extracting netlist: {e}")
            if ctx:
                ctx.info(f"Error extracting netlist: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def extract_project_netlist(
        project_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Extract netlist from a KiCad project's schematic.

        This tool finds the schematic associated with a KiCad project
        and extracts its netlist information.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with netlist information
        """
        logger.debug(f"Extracting netlist for project: {project_path}")

        if not os.path.exists(project_path):
            logger.warning(f"Project not found: {project_path}")
            if ctx:
                ctx.info(f"Project not found: {project_path}")
            return {
                "success": False,
                "error": f"Project not found: {project_path}",
            }

        if ctx:
            await ctx.report_progress(10, 100)

        try:
            files = get_project_files(project_path)

            if "schematic" not in files:
                logger.warning("Schematic file not found in project")
                if ctx:
                    ctx.info("Schematic file not found in project")
                return {
                    "success": False,
                    "error": "Schematic file not found in project",
                }

            schematic_path = files["schematic"]
            logger.debug(f"Found schematic file: {schematic_path}")
            if ctx:
                ctx.info(
                    f"Found schematic file: {os.path.basename(schematic_path)}"
                )

            if ctx:
                await ctx.report_progress(20, 100)

            result = await extract_schematic_netlist(schematic_path, ctx)

            if "success" in result and result["success"]:
                result["project_path"] = project_path

            return result

        except Exception as e:
            logger.warning(f"Error extracting project netlist: {e}")
            if ctx:
                ctx.info(f"Error extracting project netlist: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def analyze_schematic_connections(
        schematic_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Analyze connections in a KiCad schematic.

        This tool provides detailed analysis of component connections,
        including power nets, signal paths, and potential issues.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with connection analysis
        """
        logger.debug(f"Analyzing connections in schematic: {schematic_path}")

        if not os.path.exists(schematic_path):
            logger.warning(f"Schematic file not found: {schematic_path}")
            if ctx:
                ctx.info(f"Schematic file not found: {schematic_path}")
            return {
                "success": False,
                "error": f"Schematic file not found: {schematic_path}",
            }

        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(
                f"Extracting netlist from: {os.path.basename(schematic_path)}"
            )

        try:
            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                logger.warning(f"Error extracting netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(f"Error extracting netlist: {netlist_data['error']}")
                return {"success": False, "error": netlist_data["error"]}

            if ctx:
                await ctx.report_progress(40, 100)
                ctx.info("Performing connection analysis...")

            analysis: Dict[str, Any] = {
                "component_count": netlist_data["component_count"],
                "net_count": netlist_data["net_count"],
                "component_types": {},
                "power_nets": [],
                "signal_nets": [],
                "potential_issues": [],
            }

            components = netlist_data.get("components", {})
            for ref in components:
                comp_type_match = re.match(r"^([A-Za-z_]+)", ref)
                if comp_type_match:
                    comp_type = comp_type_match.group(1)
                    if comp_type not in analysis["component_types"]:
                        analysis["component_types"][comp_type] = 0
                    analysis["component_types"][comp_type] += 1

            if ctx:
                await ctx.report_progress(60, 100)

            # Use the canonical is_power_net classifier — the previous
            # inline 6-prefix list (VCC/VDD/GND/+5V/+3V3/+12V) diverged
            # from netlist_parser's _POWER_NET_PREFIXES (which adds
            # VSS/VBUS/-) and silently misclassified those rails as
            # signal nets, then re-misclassified them as floating.
            from kicad_mcp.utils.netlist_parser import is_power_net

            nets = netlist_data.get("nets", {})
            for net_name, pins in nets.items():
                entry = {"name": net_name, "pin_count": len(pins)}
                if is_power_net(net_name):
                    analysis["power_nets"].append(entry)
                else:
                    analysis["signal_nets"].append(entry)

            if ctx:
                await ctx.report_progress(80, 100)

            # Check for floating nets (signal nets with 0 or 1 pin).
            for net_name, pins in nets.items():
                if len(pins) <= 1 and not is_power_net(net_name):
                    analysis["potential_issues"].append({
                        "type": "floating_net",
                        "net": net_name,
                        "description": (
                            f"Net '{net_name}' appears to be floating "
                            f"(only has {len(pins)} connection)"
                        ),
                    })

            if ctx:
                await ctx.report_progress(90, 100)

            result = {
                "success": True,
                "schematic_path": schematic_path,
                "analysis": analysis,
            }

            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info("Connection analysis complete")

            return result

        except Exception as e:
            logger.warning(f"Error analyzing connections: {e}")
            if ctx:
                ctx.info(f"Error analyzing connections: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def find_component_connections(
        project_path: str,
        component_ref: str,
        ctx: Context | None,
    ) -> Dict[str, Any]:
        """Find all connections for a specific component in a KiCad project.

        This tool extracts information about how a specific component
        is connected to other components in the schematic.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            component_ref: Component reference (e.g., "R1", "U3")
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with component connection information
        """
        logger.debug(
            "Finding connections for component %s in project: %s",
            component_ref, project_path,
        )

        if not os.path.exists(project_path):
            logger.warning(f"Project not found: {project_path}")
            if ctx:
                ctx.info(f"Project not found: {project_path}")
            return {
                "success": False,
                "error": f"Project not found: {project_path}",
            }

        if ctx:
            await ctx.report_progress(10, 100)

        try:
            files = get_project_files(project_path)

            if "schematic" not in files:
                logger.warning("Schematic file not found in project")
                if ctx:
                    ctx.info("Schematic file not found in project")
                return {
                    "success": False,
                    "error": "Schematic file not found in project",
                }

            schematic_path = files["schematic"]
            logger.debug(f"Found schematic file: {schematic_path}")
            if ctx:
                ctx.info(
                    f"Found schematic file: {os.path.basename(schematic_path)}"
                )

            if ctx:
                await ctx.report_progress(30, 100)
                ctx.info(
                    f"Extracting netlist to find connections for {component_ref}..."
                )

            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                logger.warning(f"Failed to extract netlist: {netlist_data['error']}")
                if ctx:
                    ctx.info(
                        f"Failed to extract netlist: {netlist_data['error']}"
                    )
                return {"success": False, "error": netlist_data["error"]}

            components = netlist_data.get("components", {})
            if component_ref not in components:
                logger.warning(f"Component {component_ref} not found in schematic")
                if ctx:
                    ctx.info(
                        f"Component {component_ref} not found in schematic"
                    )
                return {
                    "success": False,
                    "error": f"Component {component_ref} not found in schematic",
                    "available_components": list(components.keys()),
                }

            component_info = components[component_ref]

            if ctx:
                await ctx.report_progress(50, 100)
                ctx.info("Finding connections...")

            nets = netlist_data.get("nets", {})
            connections = []
            connected_nets = []

            for net_name, pins in nets.items():
                component_pins = []
                for pin in pins:
                    if pin.get("component") == component_ref:
                        component_pins.append(pin)

                if component_pins:
                    net_connections = []

                    for pin in component_pins:
                        pin_num = pin.get("pin", "Unknown")
                        connected_components = []

                        for other_pin in pins:
                            other_comp = other_pin.get("component")
                            if other_comp and other_comp != component_ref:
                                connected_components.append({
                                    "component": other_comp,
                                    "pin": other_pin.get("pin", "Unknown"),
                                })

                        net_connections.append({
                            "pin": pin_num,
                            "net": net_name,
                            "connected_to": connected_components,
                        })

                    connections.extend(net_connections)
                    connected_nets.append(net_name)

            if ctx:
                await ctx.report_progress(70, 100)
                ctx.info("Analyzing connections...")

            # Categorize connections by pin function.
            # Substring matching ("IN" in name) misfires badly on common
            # pin names: VIN, DRAIN, SHDN, MAIN all contain "IN". Tokenize
            # the pin name and match exact tokens with word boundaries.
            pin_functions: dict[str, dict] = {}
            skipped_pins_missing_num = 0  # surfaced in result regardless of pins-present branch

            _POWER_TOKENS = {"VCC", "VDD", "VEE", "VSS", "GND", "PWR", "POWER", "VBUS"}
            _IO_TOKENS = {"IO", "I/O", "GPIO"}
            _INPUT_TOKENS = {"IN", "INPUT"}
            _OUTPUT_TOKENS = {"OUT", "OUTPUT"}

            def _name_tokens(name: str):
                # Split on non-alphanumeric (and underscore); upper-case;
                # produces tokens like ["VIN"] not ["V", "IN"], so VIN
                # doesn't false-match as INPUT.
                return {t.upper() for t in re.split(r"[^A-Za-z0-9/]+", name) if t}

            if "pins" in component_info:
                for pin in component_info["pins"]:
                    pin_num = pin.get("num")
                    if not pin_num:
                        # Pins lacking a "num" attribute would otherwise
                        # collapse into a single pin_functions[None] entry
                        # (each new None-num pin overwriting the previous).
                        skipped_pins_missing_num += 1
                        continue
                    pin_name = pin.get("name", "")
                    tokens = _name_tokens(pin_name)

                    if tokens & _POWER_TOKENS:
                        pin_type = "power"
                    elif tokens & _IO_TOKENS:
                        pin_type = "io"
                    elif tokens & _INPUT_TOKENS:
                        pin_type = "input"
                    elif tokens & _OUTPUT_TOKENS:
                        pin_type = "output"
                    else:
                        pin_type = "unknown"

                    pin_functions[pin_num] = {
                        "name": pin_name,
                        "type": pin_type,
                    }

            result = {
                "success": True,
                "project_path": project_path,
                "schematic_path": schematic_path,
                "component": component_ref,
                "component_info": component_info,
                "connections": connections,
                "connected_nets": connected_nets,
                "pin_functions": pin_functions,
                "pins_skipped_missing_num": skipped_pins_missing_num,
                "total_connections": len(connections),
            }

            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info(
                    f"Found {len(connections)} connections for "
                    f"component {component_ref}"
                )

            return result

        except Exception as e:
            logger.warning(f"Error finding component connections: {e}")
            if ctx:
                ctx.info(f"Error finding component connections: {e}")
            return {"success": False, "error": str(e)}
