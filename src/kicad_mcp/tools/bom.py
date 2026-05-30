"""BOM operation implementations.

Tool surface lives on the `analyze` router (operation="bom") and the
`export` router (operation="bom_csv"). This module contains the impls
and the parser/analyzer helpers they share.
"""
import csv
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import pandas as pd
except ImportError:
    pd = None

from fastmcp import Context

from kicad_mcp.utils.file_utils import get_project_files


async def _op_analyze_bom(
    project_path: str,
    ctx: Context | None,
    column_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Analyze a KiCad project's Bill of Materials."""
    logger.debug("Analyzing BOM for project: %s", project_path)

    if not os.path.exists(project_path):
        logger.warning("Project not found: %s", project_path)
        if ctx:
            await ctx.info(f"Project not found: {project_path}")
        return {"success": False, "error": f"Project not found: {project_path}"}

    if ctx:
        await ctx.report_progress(10, 100)
        await ctx.info(
            f"Looking for BOM files related to {os.path.basename(project_path)}"
        )

    files = get_project_files(project_path)

    bom_files = {}
    for file_type, file_path in files.items():
        if "bom" in file_type.lower() or file_path.lower().endswith(".csv"):
            bom_files[file_type] = file_path
            logger.debug("Found potential BOM file: %s", file_path)

    if not bom_files:
        logger.warning("No BOM files found for project")
        if ctx:
            await ctx.info("No BOM files found for project")
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
    file_error_count = 0
    file_parse_skipped = 0

    for file_type, file_path in bom_files.items():
        try:
            if ctx:
                await ctx.info(f"Analyzing {os.path.basename(file_path)}")

            bom_data, format_info = _parse_bom_file(file_path)

            if not bom_data:
                logger.warning("Failed to parse BOM file: %s", file_path)
                file_parse_skipped += 1
                continue

            analysis = _analyze_bom_data(bom_data, format_info, column_map=column_map)

            results["bom_files"][file_type] = {
                "path": file_path,
                "format": format_info,
                "analysis": analysis,
            }

            total_unique_components += analysis["unique_component_count"]
            total_components += analysis["total_component_count"]

            logger.info("Successfully analyzed BOM file: %s", file_path)

        except (OSError, ValueError, KeyError) as e:
            # OSError = file/IO; ValueError = parse/CSV; KeyError = schema.
            # Anything else (AttributeError, ImportError) propagates as a
            # programming bug. file_error_count is surfaced below so a
            # caller scanning only "results" doesn't miss partial failure.
            logger.error("Error analyzing BOM file %s: %s", file_path, e)
            file_error_count += 1
            results["bom_files"][file_type] = {
                "path": file_path,
                "error": f"{type(e).__name__}: {e}",
            }

    # Surface partial-failure counts so callers don't have to scan the
    # bom_files dict for "error" keys.
    if file_error_count or file_parse_skipped:
        results["file_error_count"] = file_error_count
        results["file_parse_skipped"] = file_parse_skipped

    if ctx:
        await ctx.report_progress(70, 100)

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
        await ctx.info(f"BOM analysis complete: found {total_components} components")

    return results


async def _op_export_bom_csv(
    project_path: str, ctx: Context | None
) -> Dict[str, Any]:
    """Export a CSV BOM from the project's schematic."""
    logger.debug("Exporting BOM for project: %s", project_path)

    if not os.path.exists(project_path):
        logger.warning("Project not found: %s", project_path)
        if ctx:
            await ctx.info(f"Project not found: {project_path}")
        return {"success": False, "error": f"Project not found: {project_path}"}

    if ctx:
        await ctx.report_progress(10, 100)

    files = get_project_files(project_path)

    if "schematic" not in files:
        logger.warning("Schematic file not found in project")
        if ctx:
            await ctx.info("Schematic file not found in project")
        return {"success": False, "error": "Schematic file not found"}

    schematic_file = files["schematic"]
    project_dir = os.path.dirname(project_path)
    project_name = os.path.basename(project_path)[:-10]

    if ctx:
        await ctx.report_progress(20, 100)
        await ctx.info(f"Found schematic file: {os.path.basename(schematic_file)}")

    try:
        if ctx:
            await ctx.info("Attempting to export BOM using command-line tools...")
        export_result = await _export_bom_with_cli(
            schematic_file, project_dir, project_name, ctx
        )
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.error("Error exporting BOM with CLI: %s", e)
        if ctx:
            await ctx.info(f"Error using command-line tools: {e}")
        export_result = {"success": False, "error": str(e)}

    if ctx:
        await ctx.report_progress(100, 100)

    if export_result.get("success", False):
        if ctx:
            await ctx.info(
                f"BOM exported successfully to "
                f"{export_result.get('output_file', 'unknown location')}"
            )
    else:
        if ctx:
            await ctx.info(
                f"Failed to export BOM: "
                f"{export_result.get('error', 'Unknown error')}"
            )

    return export_result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bom_file(
    file_path: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Parse a BOM file and detect its format."""
    logger.debug("Parsing BOM file: %s", file_path)

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
            elif isinstance(data, dict) and ("components" in data or "parts" in data):
                raw = data.get("components", data.get("parts"))
                if isinstance(raw, dict):
                    components = list(raw.values())   # {refdes: row} mapping
                elif isinstance(raw, list):
                    components = raw
                else:
                    format_info["unrecognized_json_keys"] = sorted(map(str, data.keys()))
                    logger.warning("JSON BOM %s: 'components'/'parts' is not a list or mapping", file_path)
            elif isinstance(data, dict):
                # Populated dict with no recognized container — surface the keys
                # rather than silently returning an empty BOM (otherwise a
                # populated-but-unrecognized BOM is indistinguishable from empty).
                format_info["unrecognized_json_keys"] = sorted(map(str, data.keys()))
                logger.warning("JSON BOM %s: no 'components'/'parts' container; "
                               "top-level keys=%s", file_path, format_info["unrecognized_json_keys"])

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
            except (OSError, csv.Error, UnicodeDecodeError, ValueError) as e:
                logger.warning("Failed to parse unknown file format %s: %s", file_path, e)
                return [], {"detected_format": "unsupported", "error": str(e)}

    except (OSError, csv.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError, KeyError) as e:
        logger.warning("Error parsing BOM file %s: %s", file_path, e, exc_info=True)
        return [], {"error": str(e)}

    if not components:
        logger.warning("No components found in BOM file: %s", file_path)
    else:
        logger.debug("Successfully parsed %d components from %s", len(components), file_path)

        if components:
            format_info["sample_fields"] = list(components[0].keys())

    return components, format_info


# Canonical field → list of column-name candidates (lowercased). Heuristic
# detection probes these in order; first match wins. Caller can override
# any field via the column_map parameter (see _analyze_bom_data docstring).
_BOM_FIELD_PROBES: Dict[str, List[str]] = {
    "reference":    ["reference", "designator", "references", "designators", "refdes", "ref"],
    "value":        ["value", "component", "comp", "part", "component value", "comp value"],
    "quantity":     ["quantity", "qty", "count", "amount"],
    "footprint":    ["footprint", "package", "pattern", "pcb footprint"],
    "cost":         ["cost", "price", "unit price", "unit cost", "cost each"],
    "category":     ["category", "type", "group", "component type", "lib"],
    # Supplier / part-info fields — important for JLCPCB / Octopart workflows.
    # Were silently dropped from earlier versions of this analysis.
    "mpn":          ["mpn", "manufacturer part number", "manufacturer_part_number",
                     "mfr part #", "mfr part number", "mpn1", "part number"],
    "manufacturer": ["manufacturer", "mfr", "manufacturer name", "vendor", "mfg"],
    "lcsc":         ["lcsc", "lcsc part", "lcsc#", "lcsc part #", "lcsc_part_number",
                     "jlcpcb part #", "jlcpcb part number"],
    "datasheet":    ["datasheet", "data sheet", "documentation", "datasheet url"],
    "description":  ["description", "desc", "long description", "component description"],
}


def _analyze_bom_data(
    components: List[Dict[str, Any]],
    format_info: Dict[str, Any],
    column_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Analyze component data from a BOM file."""
    import re

    logger.debug("Analyzing %d components", len(components))

    results: Dict[str, Any] = {
        "unique_component_count": 0,
        "total_component_count": 0,
        "categories": {},
        "has_cost_data": False,
        "detected_fields": {},
        "all_columns": [],
        "stage_errors": {},
    }

    if not components:
        return results

    if pd is None:
        # Fallback without pandas: basic counting
        results["unique_component_count"] = len(components)
        results["total_component_count"] = len(components)
        results["stage_errors"]["pandas"] = "pandas not installed; counts only"
        logger.warning("pandas not installed — returning basic BOM counts only")
        return results

    # --- DataFrame construction --------------------------------------------
    try:
        df = pd.DataFrame(components)
        df.columns = [str(col).strip().lower() for col in df.columns]
    except (ValueError, TypeError) as e:
        # pd.DataFrame can raise on inconsistent dict shapes; if construction
        # fails we genuinely cannot proceed, so surface as analysis_error.
        results["analysis_error"] = f"DataFrame construction failed: {type(e).__name__}: {e}"
        return results

    results["all_columns"] = list(df.columns)

    # --- Field detection (heuristic + caller overrides) --------------------
    overrides = {k.lower(): v.lower() for k, v in (column_map or {}).items()}
    detected: Dict[str, Optional[str]] = {}
    for field, candidates in _BOM_FIELD_PROBES.items():
        chosen: Optional[str] = None
        if field in overrides:
            requested = overrides[field]
            if requested in df.columns:
                chosen = requested
            else:
                results["stage_errors"][f"column_map.{field}"] = (
                    f"requested column {requested!r} not in BOM (have: {list(df.columns)})"
                )
        if chosen is None:
            for cand in candidates:
                if cand in df.columns:
                    chosen = cand
                    break
        detected[field] = chosen
    results["detected_fields"] = {k: v for k, v in detected.items() if v is not None}

    ref_col       = detected["reference"]
    value_col     = detected["value"]
    quantity_col  = detected["quantity"]
    footprint_col = detected["footprint"]
    cost_col      = detected["cost"]
    category_col  = detected["category"]

    # --- Counts -----------------------------------------------------------
    try:
        if quantity_col:
            df[quantity_col] = pd.to_numeric(
                df[quantity_col], errors="coerce"
            ).fillna(1)
            results["total_component_count"] = int(df[quantity_col].sum())
        else:
            results["total_component_count"] = len(df)
        results["unique_component_count"] = len(df)
    except (ValueError, TypeError, KeyError) as e:
        # Quantity parse failure → we still know unique count. Don't fall
        # back to total = len(df) silently; signal the partial result.
        results["unique_component_count"] = len(df)
        results["total_component_count"] = None
        results["stage_errors"]["counts"] = f"{type(e).__name__}: {e}"

    # --- Categories ---------------------------------------------------------
    try:
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
    except (KeyError, ValueError, IndexError) as e:
        results["stage_errors"]["categories"] = f"{type(e).__name__}: {e}"

    # --- Cost ---------------------------------------------------------------
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
                    elif "€" in cost_str:
                        results["currency"] = "EUR"
                        break
                    elif "£" in cost_str:
                        results["currency"] = "GBP"
                        break

                if "currency" not in results:
                    results["currency"] = "USD"
        except (KeyError, ValueError, TypeError) as e:
            results["stage_errors"]["cost"] = f"{type(e).__name__}: {e}"
            logger.warning("Failed to parse cost data: %s", e, exc_info=True)

    # --- Most-common values -------------------------------------------------
    if ref_col and value_col:
        try:
            value_counts = df[value_col].value_counts()
            most_common = value_counts.head(5).to_dict()
            results["most_common_values"] = {
                str(k): int(v) for k, v in most_common.items()
            }
            # Surface total distinct count so callers can tell "exactly 5
            # unique values" from "top 5 of 500".
            results["distinct_value_count"] = int(value_counts.size)
            results["most_common_values_truncated"] = value_counts.size > 5
        except (KeyError, ValueError) as e:
            results["stage_errors"]["most_common_values"] = f"{type(e).__name__}: {e}"

    if not results["stage_errors"]:
        del results["stage_errors"]

    return results


async def _export_bom_with_cli(
    schematic_file: str,
    output_dir: str,
    project_name: str,
    ctx: Context | None,
) -> Dict[str, Any]:
    """Export a BOM using KiCad command-line tools."""
    import platform

    system = platform.system()
    logger.debug("Exporting BOM using CLI tools on %s", system)
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
        logger.debug("Running command: %s", " ".join(cmd))
        if ctx:
            await ctx.report_progress(60, 100)

        process = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

        if process.returncode != 0:
            logger.warning("BOM export command failed with code %d", process.returncode)
            logger.warning("Error output: %s", process.stderr)

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
        logger.warning("BOM export command timed out after 30 seconds")
        return {
            "success": False,
            "error": "BOM export command timed out after 30 seconds",
            "schematic_file": schematic_file,
        }

    except (OSError, subprocess.SubprocessError, ValueError) as e:
        logger.error("Error exporting BOM: %s", e)
        return {
            "success": False,
            "error": f"Error exporting BOM: {e}",
            "schematic_file": schematic_file,
        }
