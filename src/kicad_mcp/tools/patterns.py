"""
Circuit pattern recognition tools for KiCad schematics.
"""
import os
from typing import Any, Dict

from fastmcp import FastMCP, Context

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.netlist_parser import extract_netlist
from kicad_mcp.utils.pattern_recognition import (
    identify_amplifiers,
    identify_digital_interfaces,
    identify_filters,
    identify_microcontrollers,
    identify_oscillators,
    identify_power_supplies,
    identify_sensor_interfaces,
)


def register_pattern_tools(mcp: FastMCP) -> None:
    """Register circuit pattern recognition tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    async def identify_circuit_patterns(
        schematic_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Identify common circuit patterns in a KiCad schematic.

        This tool analyzes a schematic to recognize common circuit blocks such as:
        - Power supply circuits (linear regulators, switching converters)
        - Amplifier circuits (op-amps, transistor amplifiers)
        - Filter circuits (RC, LC, active filters)
        - Digital interfaces (I2C, SPI, UART)
        - Microcontroller circuits
        - And more

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with identified circuit patterns
        """
        if not os.path.exists(schematic_path):
            if ctx:
                ctx.info(f"Schematic file not found: {schematic_path}")
            return {
                "success": False,
                "error": f"Schematic file not found: {schematic_path}",
            }

        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(
                f"Loading schematic file: {os.path.basename(schematic_path)}"
            )

        try:
            if ctx:
                await ctx.report_progress(20, 100)
                ctx.info("Parsing schematic structure...")

            netlist_data = extract_netlist(schematic_path)

            if "error" in netlist_data:
                if ctx:
                    ctx.info(
                        f"Error extracting netlist: {netlist_data['error']}"
                    )
                return {"success": False, "error": netlist_data["error"]}

            if ctx:
                await ctx.report_progress(30, 100)
                ctx.info("Analyzing components and connections...")

            components = netlist_data.get("components", {})
            nets = netlist_data.get("nets", {})

            if ctx:
                await ctx.report_progress(50, 100)
                ctx.info("Identifying circuit patterns...")

            identified_patterns: Dict[str, list] = {
                "power_supply_circuits": [],
                "amplifier_circuits": [],
                "filter_circuits": [],
                "oscillator_circuits": [],
                "digital_interface_circuits": [],
                "microcontroller_circuits": [],
                "sensor_interface_circuits": [],
                "other_patterns": [],
            }

            if ctx:
                await ctx.report_progress(60, 100)
            identified_patterns["power_supply_circuits"] = identify_power_supplies(
                components, nets
            )

            if ctx:
                await ctx.report_progress(70, 100)
            identified_patterns["amplifier_circuits"] = identify_amplifiers(
                components, nets
            )

            if ctx:
                await ctx.report_progress(75, 100)
            identified_patterns["filter_circuits"] = identify_filters(
                components, nets
            )

            if ctx:
                await ctx.report_progress(80, 100)
            identified_patterns["oscillator_circuits"] = identify_oscillators(
                components, nets
            )

            if ctx:
                await ctx.report_progress(85, 100)
            identified_patterns["digital_interface_circuits"] = (
                identify_digital_interfaces(components, nets)
            )

            if ctx:
                await ctx.report_progress(90, 100)
            identified_patterns["microcontroller_circuits"] = (
                identify_microcontrollers(components)
            )

            if ctx:
                await ctx.report_progress(95, 100)
            identified_patterns["sensor_interface_circuits"] = (
                identify_sensor_interfaces(components, nets)
            )

            result = {
                "success": True,
                "schematic_path": schematic_path,
                "component_count": netlist_data["component_count"],
                "identified_patterns": identified_patterns,
            }

            total_patterns = sum(
                len(patterns) for patterns in identified_patterns.values()
            )
            result["total_patterns_found"] = total_patterns

            if ctx:
                await ctx.report_progress(100, 100)
                ctx.info(
                    f"Pattern recognition complete. "
                    f"Found {total_patterns} circuit patterns."
                )

            return result

        except Exception as e:
            if ctx:
                ctx.info(f"Error identifying circuit patterns: {e}")
            return {"success": False, "error": str(e)}

    @mcp.tool()
    async def analyze_project_circuit_patterns(
        project_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Identify circuit patterns in a KiCad project's schematic.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with identified circuit patterns
        """
        if not os.path.exists(project_path):
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
                if ctx:
                    ctx.info("Schematic file not found in project")
                return {
                    "success": False,
                    "error": "Schematic file not found in project",
                }

            schematic_path = files["schematic"]
            if ctx:
                ctx.info(
                    f"Found schematic file: {os.path.basename(schematic_path)}"
                )

            result = await identify_circuit_patterns(schematic_path, ctx)

            if "success" in result and result["success"]:
                result["project_path"] = project_path

            return result

        except Exception as e:
            if ctx:
                ctx.info(f"Error analyzing project circuit patterns: {e}")
            return {"success": False, "error": str(e)}
