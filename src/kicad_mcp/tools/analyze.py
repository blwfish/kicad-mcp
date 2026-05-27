"""Analyze router — read-only schematic/project analysis.

See docs/SPEC_Tool_Consolidation.md.
"""
import logging
from typing import Any, Dict, Optional

from fastmcp import FastMCP, Context

from kicad_mcp.tools.bom import _op_analyze_bom
from kicad_mcp.tools.netlist import (
    _op_extract_netlist,
    _op_analyze_schematic_connections,
)
from kicad_mcp.tools.patterns import (
    _op_identify_circuit_patterns,
    _op_analyze_project_circuit_patterns,
)

logger = logging.getLogger(__name__)


def register_analyze_tools(mcp: FastMCP) -> None:
    """Register the analyze domain router."""

    @mcp.tool()
    async def analyze(
        operation: str,
        ctx: Context | None,
        *,
        path: Optional[str] = None,
        schematic_path: Optional[str] = None,
        project_path: Optional[str] = None,
        column_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Read-only analysis of schematics and projects.

        Operations:
          netlist(path)
              -> {success, components, nets, analysis, ...}
              Extract netlist from a .kicad_sch or .kicad_pro file.

          connections(schematic_path)
              -> {success, analysis: {power_nets, signal_nets, potential_issues, ...}}
              Analyze schematic connections, including floating-net detection
              and power/signal net classification.

          circuit_patterns(schematic_path)
              -> {success, identified_patterns: {power_supply_circuits, ...}}
              Identify common circuit blocks (regulators, amplifiers, filters,
              digital interfaces, microcontrollers, etc.) in a schematic.

          project_patterns(project_path)
              -> {success, identified_patterns: {...}}
              Same as circuit_patterns, but resolves the schematic from a
              project file first.

          bom(project_path, column_map=None)
              -> {success, bom_files, component_summary, ...}
              Analyze the project's BOM file(s) — counts, categories, cost,
              supplier metadata. column_map overrides heuristic column-name
              detection per canonical field.
        """
        if operation == "netlist":
            if path is None:
                return {"error": "operation='netlist' requires 'path'"}
            return await _op_extract_netlist(path, ctx)
        if operation == "connections":
            if schematic_path is None:
                return {"error": "operation='connections' requires 'schematic_path'"}
            return await _op_analyze_schematic_connections(schematic_path, ctx)
        if operation == "circuit_patterns":
            if schematic_path is None:
                return {"error": "operation='circuit_patterns' requires 'schematic_path'"}
            return await _op_identify_circuit_patterns(schematic_path, ctx)
        if operation == "project_patterns":
            if project_path is None:
                return {"error": "operation='project_patterns' requires 'project_path'"}
            return await _op_analyze_project_circuit_patterns(project_path, ctx)
        if operation == "bom":
            if project_path is None:
                return {"error": "operation='bom' requires 'project_path'"}
            return await _op_analyze_bom(project_path, ctx, column_map=column_map)
        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: netlist|connections|circuit_patterns|project_patterns|bom"
            )
        }
