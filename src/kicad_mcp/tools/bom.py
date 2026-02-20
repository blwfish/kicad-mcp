"""
Bill of Materials (BOM) processing tools for KiCad projects.
"""
import csv
import json
import os
import subprocess
from typing import Any, Dict, List, Tuple

try:
    import pandas as pd
except ImportError:
    pd = None

from fastmcp import FastMCP, Context

from kicad_mcp.utils.file_utils import get_project_files


def register_bom_tools(mcp: FastMCP) -> None:
    """Register BOM-related tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    async def analyze_bom(
        project_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Analyze a KiCad project's Bill of Materials.

        This tool will look for BOM files related to a KiCad project and provide
        analysis including component counts, categories, and cost estimates if available.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with BOM analysis results
        """
        print(f"Analyzing BOM for project: {project_path}")

        if not os.path.exists(project_path):
            print(f"Project not found: {project_path}")
            if ctx:
                ctx.info(f"Project not found: {project_path}")
            return {"success": False, "error": f"Project not found: {project_path}"}

        if ctx:
            await ctx.report_progress(10, 100)
            ctx.info(
                f"Looking for BOM files related to {os.path.basename(project_path)}"
            )

        files = get_project_files(project_path)

        # Look for BOM files
        bom_files = {}
        for file_type, file_path in files.items():
            if "bom" in file_type.lower() or file_path.lower().endswith(".csv"):
                bom_files[file_type] = file_path
                print(f"Found potential BOM file: {file_path}")

        if not bom_files:
            print("No BOM files found for project")
            if ctx:
                ctx.info("No BOM files found for project")
            return {
                "success": False,
                "error": "No BOM files found. Export a BOM from KiCad first.",
                "project_path": project_path,
            }

        if ctx:
            await ctx.report_progress(30, 100)

        results: Dict[str, Any] = {
            "success": True,
            "project_path": project_path,
            "bom_files": {},
            "component_summary": {},
        }

        total_unique_components = 0
        total_components = 0

        for file_type, file_path in bom_files.items():
            try:
                if ctx:
                    ctx.info(f"Analyzing {os.path.basename(file_path)}")

                bom_data, format_info = _parse_bom_file(file_path)

                if not bom_data or len(bom_data) == 0:
                    print(f"Failed to parse BOM file: {file_path}")
                    continue

                analysis = _analyze_bom_data(bom_data, format_info)

                results["bom_files"][file_type] = {
                    "path": file_path,
                    "format": format_info,
                    "analysis": analysis,
                }

                total_unique_components += analysis["unique_component_count"]
                total_components += analysis["total_component_count"]

                print(f"Successfully analyzed BOM file: {file_path}")

            except Exception as e:
                print(f"Error analyzing BOM file {file_path}: {e}")
                results["bom_files"][file_type] = {
                    "path": file_path,
                    "error": str(e),
                }

        if ctx:
            await ctx.report_progress(70, 100)

        # Generate overall component summary
        if total_components > 0:
            results["component_summary"] = {
                "total_unique_components": total_unique_components,
                "total_components": total_components,
            }

            all_categories: dict[str, int] = {}
            for file_type, file_info in results["bom_files"].items():
                if "analysis" in file_info and "categories" in file_info["analysis"]:
                    for category, count in file_info["analysis"]["categories"].items():
                        if category not in all_categories:
                            all_categories[category] = 0
                        all_categories[category] += count

            results["component_summary"]["categories"] = all_categories

            total_cost = 0.0
            cost_available = False
            for file_type, file_info in results["bom_files"].items():
                if "analysis" in file_info and "total_cost" in file_info["analysis"]:
                    if file_info["analysis"]["total_cost"] > 0:
                        total_cost += file_info["analysis"]["total_cost"]
                        cost_available = True

            if cost_available:
                results["component_summary"]["total_cost"] = round(total_cost, 2)
                currency = next(
                    (
                        file_info["analysis"].get("currency", "USD")
                        for file_type, file_info in results["bom_files"].items()
                        if "analysis" in file_info
                        and "currency" in file_info["analysis"]
                    ),
                    "USD",
                )
                results["component_summary"]["currency"] = currency

        if ctx:
            await ctx.report_progress(100, 100)
            ctx.info(f"BOM analysis complete: found {total_components} components")

        return results

    @mcp.tool()
    async def export_bom_csv(
        project_path: str, ctx: Context | None
    ) -> Dict[str, Any]:
        """Export a Bill of Materials for a KiCad project.

        This tool attempts to generate a CSV BOM file for a KiCad project.
        It requires KiCad to be installed with the appropriate command-line tools.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: MCP context for progress reporting

        Returns:
            Dictionary with export results
        """
        print(f"Exporting BOM for project: {project_path}")

        if not os.path.exists(project_path):
            print(f"Project not found: {project_path}")
            if ctx:
                ctx.info(f"Project not found: {project_path}")
            return {"success": False, "error": f"Project not found: {project_path}"}

        if ctx:
            await ctx.report_progress(10, 100)

        files = get_project_files(project_path)

        if "schematic" not in files:
            print("Schematic file not found in project")
            if ctx:
                ctx.info("Schematic file not found in project")
            return {"success": False, "error": "Schematic file not found"}

        schematic_file = files["schematic"]
        project_dir = os.path.dirname(project_path)
        project_name = os.path.basename(project_path)[:-10]

        if ctx:
            await ctx.report_progress(20, 100)
            ctx.info(f"Found schematic file: {os.path.basename(schematic_file)}")

        # Try CLI export
        try:
            if ctx:
                ctx.info("Attempting to export BOM using command-line tools...")
            export_result = await _export_bom_with_cli(
                schematic_file, project_dir, project_name, ctx
            )
        except Exception as e:
            print(f"Error exporting BOM with CLI: {e}")
            if ctx:
                ctx.info(f"Error using command-line tools: {e}")
            export_result = {"success": False, "error": str(e)}

        if ctx:
            await ctx.report_progress(100, 100)

        if export_result.get("success", False):
            if ctx:
                ctx.info(
                    f"BOM exported successfully to "
                    f"{export_result.get('output_file', 'unknown location')}"
                )
        else:
            if ctx:
                ctx.info(
                    f"Failed to export BOM: "
                    f"{export_result.get('error', 'Unknown error')}"
                )

        return export_result


# Helper functions for BOM processing


def _parse_bom_file(
    file_path: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse a BOM file and detect its format.

    Args:
        file_path: Path to the BOM file

    Returns:
        Tuple containing:
            - List of component dictionaries
            - Dictionary with format information
    """
    print(f"Parsing BOM file: {file_path}")

    _, ext = os.path.splitext(file_path)
    ext = ext.lower()

    format_info: Dict[str, Any] = {
        "file_type": ext,
        "detected_format": "unknown",
        "header_fields": [],
    }

    components: List[Dict[str, Any]] = []

    try:
        if ext == ".csv":
            with open(file_path, "r", encoding="utf-8-sig") as f:
                sample = "".join([f.readline() for _ in range(10)])
                f.seek(0)

                if "," in sample:
                    delimiter = ","
                elif ";" in sample:
                    delimiter = ";"
                elif "\t" in sample:
                    delimiter = "\t"
                else:
                    delimiter = ","

                format_info["delimiter"] = delimiter

                reader = csv.DictReader(f, delimiter=delimiter)
                format_info["header_fields"] = (
                    reader.fieldnames if reader.fieldnames else []
                )

                header_str = ",".join(format_info["header_fields"]).lower()

                if "reference" in header_str and "value" in header_str:
                    format_info["detected_format"] = "kicad"
                elif "designator" in header_str:
                    format_info["detected_format"] = "altium"
                elif (
                    "part number" in header_str
                    or "manufacturer part" in header_str
                ):
                    format_info["detected_format"] = "generic"

                for row in reader:
                    components.append(dict(row))

        elif ext == ".xml":
            from defusedxml.ElementTree import parse as safe_parse

            tree = safe_parse(file_path)
            root = tree.getroot()

            format_info["detected_format"] = "xml"

            component_elements = root.findall(
                ".//component"
            ) or root.findall(".//Component")

            if component_elements:
                for elem in component_elements:
                    component: dict[str, Any] = {}
                    for attr in elem.attrib:
                        component[attr] = elem.attrib[attr]
                    for child in elem:
                        component[child.tag] = child.text
                    components.append(component)

        elif ext == ".json":
            with open(file_path, "r") as f:
                data = json.load(f)

            format_info["detected_format"] = "json"

            if isinstance(data, list):
                components = data
            elif "components" in data:
                components = data["components"]
            elif "parts" in data:
                components = data["parts"]

        else:
            try:
                with open(file_path, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    format_info["header_fields"] = (
                        reader.fieldnames if reader.fieldnames else []
                    )
                    format_info["detected_format"] = "unknown_csv"

                    for row in reader:
                        components.append(dict(row))
            except Exception:
                print(f"Failed to parse unknown file format: {file_path}")
                return [], {"detected_format": "unsupported"}

    except Exception as e:
        print(f"Error parsing BOM file: {e}")
        return [], {"error": str(e)}

    if not components:
        print(f"No components found in BOM file: {file_path}")
    else:
        print(f"Successfully parsed {len(components)} components from {file_path}")

        if components:
            format_info["sample_fields"] = list(components[0].keys())

    return components, format_info


def _analyze_bom_data(
    components: List[Dict[str, Any]], format_info: Dict[str, Any]
) -> Dict[str, Any]:
    """Analyze component data from a BOM file.

    Args:
        components: List of component dictionaries
        format_info: Dictionary with format information

    Returns:
        Dictionary with analysis results
    """
    import re

    print(f"Analyzing {len(components)} components")

    results: Dict[str, Any] = {
        "unique_component_count": 0,
        "total_component_count": 0,
        "categories": {},
        "has_cost_data": False,
    }

    if not components:
        return results

    if pd is None:
        # Fallback without pandas: basic counting
        results["unique_component_count"] = len(components)
        results["total_component_count"] = len(components)
        print("pandas not installed — returning basic BOM counts only")
        return results

    try:
        df = pd.DataFrame(components)
        df.columns = [str(col).strip().lower() for col in df.columns]

        ref_col = None
        value_col = None
        quantity_col = None
        footprint_col = None
        cost_col = None
        category_col = None

        for possible_col in [
            "reference", "designator", "references", "designators", "refdes", "ref",
        ]:
            if possible_col in df.columns:
                ref_col = possible_col
                break

        for possible_col in [
            "value", "component", "comp", "part", "component value", "comp value",
        ]:
            if possible_col in df.columns:
                value_col = possible_col
                break

        for possible_col in ["quantity", "qty", "count", "amount"]:
            if possible_col in df.columns:
                quantity_col = possible_col
                break

        for possible_col in [
            "footprint", "package", "pattern", "pcb footprint",
        ]:
            if possible_col in df.columns:
                footprint_col = possible_col
                break

        for possible_col in [
            "cost", "price", "unit price", "unit cost", "cost each",
        ]:
            if possible_col in df.columns:
                cost_col = possible_col
                break

        for possible_col in [
            "category", "type", "group", "component type", "lib",
        ]:
            if possible_col in df.columns:
                category_col = possible_col
                break

        if quantity_col:
            df[quantity_col] = pd.to_numeric(
                df[quantity_col], errors="coerce"
            ).fillna(1)
            results["total_component_count"] = int(df[quantity_col].sum())
        else:
            results["total_component_count"] = len(df)

        results["unique_component_count"] = len(df)

        if category_col:
            categories = df[category_col].value_counts().to_dict()
            results["categories"] = {str(k): int(v) for k, v in categories.items()}
        elif footprint_col:
            categories = df[footprint_col].value_counts().to_dict()
            results["categories"] = {str(k): int(v) for k, v in categories.items()}
        elif ref_col:

            def extract_prefix(ref):
                if isinstance(ref, str):
                    match = re.match(r"^([A-Za-z]+)", ref)
                    if match:
                        return match.group(1)
                return "Other"

            if isinstance(df[ref_col].iloc[0], str) and "," in df[ref_col].iloc[0]:
                all_refs = []
                for refs in df[ref_col]:
                    all_refs.extend([r.strip() for r in refs.split(",")])

                categories_dict: dict[str, int] = {}
                for ref in all_refs:
                    prefix = extract_prefix(ref)
                    categories_dict[prefix] = categories_dict.get(prefix, 0) + 1

                results["categories"] = categories_dict
            else:
                categories = (
                    df[ref_col].apply(extract_prefix).value_counts().to_dict()
                )
                results["categories"] = {
                    str(k): int(v) for k, v in categories.items()
                }

        # Map reference prefixes to component types
        category_mapping = {
            "R": "Resistors",
            "C": "Capacitors",
            "L": "Inductors",
            "D": "Diodes",
            "Q": "Transistors",
            "U": "ICs",
            "SW": "Switches",
            "J": "Connectors",
            "K": "Relays",
            "Y": "Crystals/Oscillators",
            "F": "Fuses",
            "T": "Transformers",
        }

        mapped_categories: dict[str, int] = {}
        for cat, count in results["categories"].items():
            if cat in category_mapping:
                mapped_name = category_mapping[cat]
                mapped_categories[mapped_name] = (
                    mapped_categories.get(mapped_name, 0) + count
                )
            else:
                mapped_categories[cat] = count

        results["categories"] = mapped_categories

        if cost_col:
            try:
                df[cost_col] = (
                    df[cost_col]
                    .astype(str)
                    .str.replace("$", "")
                    .str.replace(",", "")
                )
                df[cost_col] = pd.to_numeric(df[cost_col], errors="coerce")

                df_with_cost = df.dropna(subset=[cost_col])

                if not df_with_cost.empty:
                    results["has_cost_data"] = True

                    if quantity_col:
                        total_cost = (
                            df_with_cost[cost_col] * df_with_cost[quantity_col]
                        ).sum()
                    else:
                        total_cost = df_with_cost[cost_col].sum()

                    results["total_cost"] = round(float(total_cost), 2)

                    for _, row in df.iterrows():
                        cost_str = str(row.get(cost_col, ""))
                        if "$" in cost_str:
                            results["currency"] = "USD"
                            break
                        elif "\u20ac" in cost_str:
                            results["currency"] = "EUR"
                            break
                        elif "\u00a3" in cost_str:
                            results["currency"] = "GBP"
                            break

                    if "currency" not in results:
                        results["currency"] = "USD"
            except Exception:
                print("Failed to parse cost data")

        if ref_col and value_col:
            value_counts = df[value_col].value_counts()
            most_common = value_counts.head(5).to_dict()
            results["most_common_values"] = {
                str(k): int(v) for k, v in most_common.items()
            }

    except Exception as e:
        print(f"Error analyzing BOM data: {e}")
        results["unique_component_count"] = len(components)
        results["total_component_count"] = len(components)

    return results


async def _export_bom_with_cli(
    schematic_file: str,
    output_dir: str,
    project_name: str,
    ctx: Context | None,
) -> Dict[str, Any]:
    """Export a BOM using KiCad command-line tools.

    Args:
        schematic_file: Path to the schematic file
        output_dir: Directory to save the BOM
        project_name: Name of the project
        ctx: MCP context for progress reporting

    Returns:
        Dictionary with export results
    """
    import platform

    system = platform.system()
    print(f"Exporting BOM using CLI tools on {system}")
    if ctx:
        await ctx.report_progress(40, 100)

    output_file = os.path.join(output_dir, f"{project_name}_bom.csv")

    if system == "Darwin":
        from kicad_mcp.config import KICAD_APP_PATH

        kicad_cli = os.path.join(KICAD_APP_PATH, "Contents/MacOS/kicad-cli")

        if not os.path.exists(kicad_cli):
            return {
                "success": False,
                "error": f"KiCad CLI tool not found at {kicad_cli}",
                "schematic_file": schematic_file,
            }

        cmd = [
            kicad_cli,
            "sch",
            "export",
            "bom",
            "--output",
            output_file,
            schematic_file,
        ]

    elif system == "Windows":
        from kicad_mcp.config import KICAD_APP_PATH

        kicad_cli = os.path.join(KICAD_APP_PATH, "bin", "kicad-cli.exe")

        if not os.path.exists(kicad_cli):
            return {
                "success": False,
                "error": f"KiCad CLI tool not found at {kicad_cli}",
                "schematic_file": schematic_file,
            }

        cmd = [
            kicad_cli,
            "sch",
            "export",
            "bom",
            "--output",
            output_file,
            schematic_file,
        ]

    elif system == "Linux":
        kicad_cli = "kicad-cli"
        cmd = [
            kicad_cli,
            "sch",
            "export",
            "bom",
            "--output",
            output_file,
            schematic_file,
        ]

    else:
        return {
            "success": False,
            "error": f"Unsupported operating system: {system}",
            "schematic_file": schematic_file,
        }

    try:
        print(f"Running command: {' '.join(cmd)}")
        if ctx:
            await ctx.report_progress(60, 100)

        process = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if process.returncode != 0:
            print(f"BOM export command failed with code {process.returncode}")
            print(f"Error output: {process.stderr}")

            return {
                "success": False,
                "error": f"BOM export command failed: {process.stderr}",
                "schematic_file": schematic_file,
                "command": " ".join(cmd),
            }

        if not os.path.exists(output_file):
            return {
                "success": False,
                "error": "BOM file was not created",
                "schematic_file": schematic_file,
                "output_file": output_file,
            }

        if ctx:
            await ctx.report_progress(80, 100)

        with open(output_file, "r") as f:
            bom_content = f.read(1024)

        if len(bom_content.strip()) == 0:
            return {
                "success": False,
                "error": "Generated BOM file is empty",
                "schematic_file": schematic_file,
                "output_file": output_file,
            }

        return {
            "success": True,
            "schematic_file": schematic_file,
            "output_file": output_file,
            "file_size": os.path.getsize(output_file),
            "message": "BOM exported successfully",
        }

    except subprocess.TimeoutExpired:
        print("BOM export command timed out after 30 seconds")
        return {
            "success": False,
            "error": "BOM export command timed out after 30 seconds",
            "schematic_file": schematic_file,
        }

    except Exception as e:
        print(f"Error exporting BOM: {e}")
        return {
            "success": False,
            "error": f"Error exporting BOM: {e}",
            "schematic_file": schematic_file,
        }
