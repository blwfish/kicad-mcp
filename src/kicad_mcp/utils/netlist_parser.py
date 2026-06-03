"""
KiCad schematic netlist extraction utilities.
"""
import logging
import os
import re
from collections import defaultdict
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# Single source of truth for power-net detection — used in analyze_netlist
# and importable by other modules that need the same classification.
_POWER_NET_PREFIXES = ("VCC", "VDD", "VSS", "VBUS", "GND")


def is_power_net(net_name: str) -> bool:
    """Heuristic classifier: does this net name designate a power/ground rail?

    Matches KiCad power-symbol conventions: VCC/VDD/VSS/VBUS/GND prefixes, and
    SIGNED-VOLTAGE names like ``+3V3``/``+5V``/``-12V`` — a sign followed by a
    DIGIT. Active-low / signed SIGNAL nets like ``-RESET`` or ``+CS`` (a sign
    followed by a letter) are NOT power. The bare ``+``/``-`` prefixes used to
    misclassify those as rails, which dropped them from the signal graph.
    """
    upper = net_name.upper()
    return (upper.startswith(_POWER_NET_PREFIXES)
            or (len(upper) >= 2 and upper[0] in "+-" and upper[1].isdigit()))


# Injectable source for embedded pcbnew scripts that need the same classifier.
# Mirrors is_power_net above (the prefix tuple is interpolated; the body is
# byte-identical). test_power_net_helper_matches_python pins the agreement.
POWER_NET_HELPER = f"""
_POWER_NET_PREFIXES = {_POWER_NET_PREFIXES!r}

def is_power_net(net_name):
    upper = net_name.upper()
    return (upper.startswith(_POWER_NET_PREFIXES)
            or (len(upper) >= 2 and upper[0] in "+-" and upper[1].isdigit()))
"""


# KiCad S-expression quoted-string unescape: \" -> ", \\ -> \, \n -> newline.
_SEXPR_ESCAPES = {'"': '"', '\\': '\\', 'n': '\n', 't': '\t', 'r': '\r'}


def _unescape_sexpr(s: str) -> str:
    """Reverse the S-expression quoted-string escape sequences."""
    if "\\" not in s:
        return s
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(_SEXPR_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


class SchematicParser:
    """Parser for KiCad schematic files to extract netlist information."""

    def __init__(self, schematic_path: str):
        """Initialize the schematic parser.

        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
        """
        self.schematic_path = schematic_path
        self.content = ""
        self.components: list[dict] = []
        self.labels: list[dict] = []
        self.wires: list[dict] = []
        self.junctions: list[dict] = []
        self.no_connects: list[dict] = []
        self.power_symbols: list[dict] = []
        self.hierarchical_labels: list[dict] = []
        self.global_labels: list[dict] = []

        # Netlist information
        self.nets: dict[str, list] = defaultdict(list)
        self.component_pins: dict[tuple, str] = {}

        # Component information
        self.component_info: dict[str, dict] = {}

        # Load the file
        self._load_schematic()

    def _load_schematic(self) -> None:
        """Load the schematic file content."""
        if not os.path.exists(self.schematic_path):
            print(f"Schematic file not found: {self.schematic_path}")
            raise FileNotFoundError(f"Schematic file not found: {self.schematic_path}")

        try:
            with open(self.schematic_path, "r") as f:
                self.content = f.read()
                print(f"Successfully loaded schematic: {self.schematic_path}")
        except Exception as e:
            print(f"Error reading schematic file: {e}")
            raise

    def parse(self) -> Dict[str, Any]:
        """Parse the schematic to extract netlist information.

        Returns:
            Dictionary with parsed netlist information
        """
        print("Starting schematic parsing")

        self._extract_components()
        self._extract_wires()
        self._extract_junctions()
        self._extract_labels()
        self._extract_power_symbols()
        self._extract_no_connects()
        self._build_netlist()

        result = {
            "parser_path": "regex",
            "components": self.component_info,
            "nets": dict(self.nets),
            "labels": self.labels,
            "wires": self.wires,
            "junctions": self.junctions,
            "power_symbols": self.power_symbols,
            "component_count": len(self.component_info),
            "net_count": len(self.nets),
        }

        print(
            f"Schematic parsing complete: found {len(self.component_info)} components "
            f"and {len(self.nets)} nets"
        )
        return result

    def _extract_s_expressions(self, pattern: str) -> List[str]:
        """Extract all matching S-expressions from the schematic content.

        Args:
            pattern: Regex pattern to match the start of S-expressions

        Returns:
            List of matching S-expressions
        """
        matches = []
        positions = []

        for match in re.finditer(pattern, self.content):
            positions.append(match.start())

        for pos in positions:
            current_pos = pos
            depth = 0
            s_exp = ""

            while current_pos < len(self.content):
                char = self.content[current_pos]
                s_exp += char

                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        break

                current_pos += 1

            # Skip s-expressions that never closed (unbalanced parens or
            # truncated file) — keeping them would mean s_exp == rest of
            # file and silently produce spurious downstream matches.
            if depth != 0:
                logger.warning(
                    "Unbalanced parens at position %d (depth=%d at EOF); skipping s-expression",
                    pos, depth,
                )
                continue

            matches.append(s_exp)

        return matches

    def _extract_components(self) -> None:
        """Extract component information from schematic."""
        print("Extracting components")

        symbols = self._extract_s_expressions(r"\(symbol\s+")

        skipped_no_reference = 0
        for symbol in symbols:
            component = self._parse_component(symbol)
            if not component:
                continue
            ref = component.get("reference")
            if not ref:
                # Library-definition symbol blocks lack (at ...) and
                # Reference property; the old code clobbered them all under
                # the single "Unknown" key (last write wins). Skip + count
                # so callers can detect the silent loss.
                skipped_no_reference += 1
                continue
            self.components.append(component)
            self.component_info[ref] = component

        if skipped_no_reference:
            logger.info(
                "Skipped %d symbol blocks with no Reference property "
                "(likely library-definition blocks inside (lib_symbols ...))",
                skipped_no_reference,
            )
        print(f"Extracted {len(self.components)} components")

    def _parse_component(self, symbol_expr: str) -> Dict[str, Any]:
        """Parse a component from a symbol S-expression.

        Args:
            symbol_expr: Symbol S-expression

        Returns:
            Component information dictionary
        """
        component: dict[str, Any] = {}

        # KiCad S-expression quoted strings allow escaped quotes (\") and
        # escaped backslashes (\\). The prior [^"]+ pattern truncated values
        # silently at the first \" — e.g. a Value containing inch marks would
        # land in the netlist with the trailing portion dropped. Pattern is
        # "((?:[^"\\]|\\.)*)" — any non-quote-non-backslash OR a backslash
        # followed by anything.
        _QSTR = r'"((?:[^"\\]|\\.)*)"'

        lib_id_match = re.search(r'\(lib_id\s+' + _QSTR + r'\)', symbol_expr)
        if lib_id_match:
            component["lib_id"] = _unescape_sexpr(lib_id_match.group(1))

        property_matches = re.finditer(
            r'\(property\s+' + _QSTR + r'\s+' + _QSTR, symbol_expr
        )
        for match in property_matches:
            prop_name = _unescape_sexpr(match.group(1))
            prop_value = _unescape_sexpr(match.group(2))

            if prop_name == "Reference":
                component["reference"] = prop_value
            elif prop_name == "Value":
                component["value"] = prop_value
            elif prop_name == "Footprint":
                component["footprint"] = prop_value
            else:
                if "properties" not in component:
                    component["properties"] = {}
                component["properties"][prop_name] = prop_value

        pos_match = re.search(
            r"\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)", symbol_expr
        )
        if pos_match:
            component["position"] = {
                "x": float(pos_match.group(1)),
                "y": float(pos_match.group(2)),
                "angle": float(
                    pos_match.group(3).strip() if pos_match.group(3) else 0
                ),
            }

        pins = []
        pin_matches = re.finditer(
            r'\(pin\s+\(num\s+"([^"]+)"\)\s+\(name\s+"([^"]+)"\)', symbol_expr
        )
        for match in pin_matches:
            pin_num = match.group(1)
            pin_name = match.group(2)
            pins.append({"num": pin_num, "name": pin_name})

        if pins:
            component["pins"] = pins

        return component

    def _extract_wires(self) -> None:
        """Extract wire information from schematic."""
        print("Extracting wires")

        wires = self._extract_s_expressions(r"\(wire\s+")

        for wire in wires:
            pts_match = re.search(
                r"\(pts\s+\(xy\s+([\d\.-]+)\s+([\d\.-]+)\)\s+"
                r"\(xy\s+([\d\.-]+)\s+([\d\.-]+)\)\)",
                wire,
            )
            if pts_match:
                self.wires.append({
                    "start": {
                        "x": float(pts_match.group(1)),
                        "y": float(pts_match.group(2)),
                    },
                    "end": {
                        "x": float(pts_match.group(3)),
                        "y": float(pts_match.group(4)),
                    },
                })

        print(f"Extracted {len(self.wires)} wires")

    def _extract_junctions(self) -> None:
        """Extract junction information from schematic."""
        print("Extracting junctions")

        junctions = self._extract_s_expressions(r"\(junction\s+")

        for junction in junctions:
            xy_match = re.search(
                r"\(junction\s+\(xy\s+([\d\.-]+)\s+([\d\.-]+)\)\)", junction
            )
            if xy_match:
                self.junctions.append({
                    "x": float(xy_match.group(1)),
                    "y": float(xy_match.group(2)),
                })

        print(f"Extracted {len(self.junctions)} junctions")

    def _extract_labels(self) -> None:
        """Extract label information from schematic."""
        print("Extracting labels")

        # Local labels
        local_labels = self._extract_s_expressions(r"\(label\s+")
        for label in local_labels:
            label_match = re.search(
                r'\(label\s+"([^"]+)"\s+\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)',
                label,
            )
            if label_match:
                self.labels.append({
                    "type": "local",
                    "text": _unescape_sexpr(label_match.group(1)),
                    "position": {
                        "x": float(label_match.group(2)),
                        "y": float(label_match.group(3)),
                        "angle": float(
                            label_match.group(4).strip()
                            if label_match.group(4)
                            else 0
                        ),
                    },
                })

        # Global labels
        global_labels = self._extract_s_expressions(r"\(global_label\s+")
        for label in global_labels:
            label_match = re.search(
                r'\(global_label\s+"([^"]+)"\s+\(shape\s+([^\s\)]+)\)\s+'
                r"\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)",
                label,
            )
            if label_match:
                self.global_labels.append({
                    "type": "global",
                    "text": _unescape_sexpr(label_match.group(1)),
                    "shape": label_match.group(2),
                    "position": {
                        "x": float(label_match.group(3)),
                        "y": float(label_match.group(4)),
                        "angle": float(
                            label_match.group(5).strip()
                            if label_match.group(5)
                            else 0
                        ),
                    },
                })

        # Hierarchical labels
        hierarchical_labels = self._extract_s_expressions(r"\(hierarchical_label\s+")
        for label in hierarchical_labels:
            label_match = re.search(
                r'\(hierarchical_label\s+"([^"]+)"\s+\(shape\s+([^\s\)]+)\)\s+'
                r"\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)",
                label,
            )
            if label_match:
                self.hierarchical_labels.append({
                    "type": "hierarchical",
                    "text": _unescape_sexpr(label_match.group(1)),
                    "shape": label_match.group(2),
                    "position": {
                        "x": float(label_match.group(3)),
                        "y": float(label_match.group(4)),
                        "angle": float(
                            label_match.group(5).strip()
                            if label_match.group(5)
                            else 0
                        ),
                    },
                })

        print(
            f"Extracted {len(self.labels)} local labels, "
            f"{len(self.global_labels)} global labels, "
            f"and {len(self.hierarchical_labels)} hierarchical labels"
        )

    def _extract_power_symbols(self) -> None:
        """Extract power symbol information from schematic."""
        print("Extracting power symbols")

        power_symbols = self._extract_s_expressions(r'\(symbol\s+\(lib_id\s+"power:')

        for symbol in power_symbols:
            type_match = re.search(r'\(lib_id\s+"power:([^"]+)"\)', symbol)
            pos_match = re.search(
                r"\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)", symbol
            )

            if type_match and pos_match:
                self.power_symbols.append({
                    "type": type_match.group(1),
                    "position": {
                        "x": float(pos_match.group(1)),
                        "y": float(pos_match.group(2)),
                        "angle": float(
                            pos_match.group(3).strip() if pos_match.group(3) else 0
                        ),
                    },
                })

        print(f"Extracted {len(self.power_symbols)} power symbols")

    def _extract_no_connects(self) -> None:
        """Extract no-connect information from schematic."""
        print("Extracting no-connects")

        no_connects = self._extract_s_expressions(r"\(no_connect\s+")

        for no_connect in no_connects:
            xy_match = re.search(
                r"\(no_connect\s+\(at\s+([\d\.-]+)\s+([\d\.-]+)\)", no_connect
            )
            if xy_match:
                self.no_connects.append({
                    "x": float(xy_match.group(1)),
                    "y": float(xy_match.group(2)),
                })

        print(f"Extracted {len(self.no_connects)} no-connects")

    def _build_netlist(self) -> None:
        """Build the netlist from extracted components and connections."""
        print("Building netlist from schematic data")

        # Process global labels as nets
        for label in self.global_labels:
            net_name = label["text"]
            self.nets[net_name] = []

        # Process power symbols as nets
        for power in self.power_symbols:
            net_name = power["type"]
            if net_name not in self.nets:
                self.nets[net_name] = []

        print("Note: Full netlist building requires complex connectivity tracing")
        print(f"Found {len(self.nets)} potential nets from labels and power symbols")


def extract_netlist_via_cli(schematic_path: str) -> Dict[str, Any] | None:
    """Extract netlist using kicad-cli for authoritative connectivity.

    Uses ``kicad-cli sch export netlist --format kicadxml`` which correctly
    resolves all label types (local, global, hierarchical) and power symbols.
    Returns None if kicad-cli is not available so callers can fall back to
    the regex-based parser.

    Args:
        schematic_path: Path to the KiCad schematic file (.kicad_sch)

    Returns:
        Dictionary with netlist information, or None if kicad-cli unavailable
    """
    import subprocess
    import tempfile
    import xml.etree.ElementTree as ET

    from kicad_mcp.utils.kicad_cli import find_kicad_cli

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        return None

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".xml", delete=False
    ) as tmp:
        netlist_path = tmp.name

    try:
        result = subprocess.run(
            [kicad_cli, "sch", "export", "netlist",
             "--format", "kicadxml",
             "--output", netlist_path,
             schematic_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"kicad-cli netlist export failed: {result.stderr.strip()}")
            return None

        with open(netlist_path, encoding="utf-8") as fh:
            xml_text = fh.read()
        return _parse_kicadxml(xml_text)
    except (ET.ParseError, subprocess.TimeoutExpired, OSError) as e:
        # ParseError = malformed XML; TimeoutExpired = kicad-cli hung;
        # OSError = kicad-cli vanished or netlist tmpfile missing. Anything
        # else (e.g. AttributeError in our own code) propagates.
        logger.warning("kicad-cli netlist export/parse failed (%s): %s",
                       type(e).__name__, e)
        return None
    finally:
        if os.path.exists(netlist_path):
            os.unlink(netlist_path)


def _parse_kicadxml(xml_text: str) -> Dict[str, Any]:
    """Parse a kicad-cli ``--format kicadxml`` netlist string into the netlist
    dict. Pure — no subprocess, no file I/O — so every field's survival is
    unit-testable without KiCad (the field-extraction seam, review finding
    h-test-netlist). Raises ``xml.etree.ElementTree.ParseError`` on malformed
    XML; extract_netlist_via_cli catches that and treats it as 'no result'.
    """
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)

    # Extract components from <components>
    component_info: Dict[str, Dict] = {}
    malformed_components_skipped = 0
    comps_element = root.find("components")
    if comps_element is not None:
        for comp_elem in comps_element.findall("comp"):
            ref = comp_elem.get("ref", "")
            if not ref:
                malformed_components_skipped += 1
                continue
            info: Dict[str, Any] = {"reference": ref}

            # DNP attribute (KiCad 7+)
            dnp = comp_elem.get("dnp")
            if dnp is not None:
                info["dnp"] = dnp.lower() in ("1", "true", "yes")

            val_elem = comp_elem.find("value")
            if val_elem is not None and val_elem.text:
                info["value"] = val_elem.text

            fp_elem = comp_elem.find("footprint")
            if fp_elem is not None and fp_elem.text:
                info["footprint"] = fp_elem.text

            ds_elem = comp_elem.find("datasheet")
            if ds_elem is not None and ds_elem.text:
                info["datasheet"] = ds_elem.text

            desc_elem = comp_elem.find("description")
            if desc_elem is not None and desc_elem.text:
                info["description"] = desc_elem.text

            libsrc = comp_elem.find("libsource")
            if libsrc is not None:
                lib = libsrc.get("lib", "")
                part = libsrc.get("part", "")
                if lib and part:
                    info["lib_id"] = f"{lib}:{part}"
                # Description may also appear as libsource attr
                if "description" not in info:
                    desc = libsrc.get("description", "")
                    if desc:
                        info["description"] = desc

            # Collect all extra properties into info["properties"]
            props: Dict[str, str] = {}

            # <fields><field name="...">value</field></fields>
            fields_elem = comp_elem.find("fields")
            if fields_elem is not None:
                for field in fields_elem.findall("field"):
                    name = field.get("name", "")
                    if name and field.text:
                        props[name] = field.text

            # KiCad 7+ inline <property name="..." value="..."/>
            for prop_elem in comp_elem.findall("property"):
                name = prop_elem.get("name", "")
                value = prop_elem.get("value", "")
                if name:
                    props[name] = value

            if props:
                info["properties"] = props

            component_info[ref] = info

    # Extract nets from <nets>
    nets: Dict[str, List] = {}
    malformed_nets_skipped = 0
    nets_element = root.find("nets")
    if nets_element is not None:
        for net_elem in nets_element.findall("net"):
            net_name = net_elem.get("name", "")
            if not net_name:
                malformed_nets_skipped += 1
                continue
            # Strip leading "/" from local label net names
            clean_name = net_name.lstrip("/")
            # Skip auto-generated unconnected nets
            if clean_name.startswith("unconnected-("):
                continue
            pins = []
            for node in net_elem.findall("node"):
                node_ref = node.get("ref", "")
                pin = node.get("pin", "")
                if node_ref and pin:
                    entry: Dict[str, str] = {
                        "component": node_ref,
                        "pin": pin,
                    }
                    # Capture all remaining node attributes (pinfunction,
                    # pintype/electrical-type, alt names, voltage class, etc.)
                    for attr, val in node.attrib.items():
                        if attr not in ("ref", "pin") and val:
                            entry[attr] = val
                    pins.append(entry)
            nets[clean_name] = pins

    return {
        "parser_path": "cli",
        "components": component_info,
        "nets": nets,
        "component_count": len(component_info),
        "net_count": len(nets),
        "malformed_components_skipped": malformed_components_skipped,
        "malformed_nets_skipped": malformed_nets_skipped,
        # Geometric fields are regex-only — kicad-cli's netlist export
        # exposes connectivity, not canvas positions. Return None
        # sentinels so callers see a uniform shape across paths and can
        # check parser_path to know whether geometry is available.
        "labels": None,
        "wires": None,
        "junctions": None,
        "power_symbols": None,
        "incomplete": False,
        "incomplete_reason": None,
    }


def extract_netlist(schematic_path: str) -> Dict[str, Any]:
    """Extract netlist information from a KiCad schematic file.

    Tries kicad-cli first (authoritative, handles all label types).
    Falls back to regex-based parser if kicad-cli is not available.

    Args:
        schematic_path: Path to the KiCad schematic file (.kicad_sch)

    Returns:
        Dictionary with netlist information
    """
    # Try kicad-cli first — it handles local labels correctly
    cli_result = extract_netlist_via_cli(schematic_path)
    if cli_result is not None:
        return cli_result

    # Fallback to regex parser (CI environments without KiCad).
    # WARNING: this parser reads only the top-level .kicad_sch file and
    # cannot follow hierarchical sheet references — components and nets in
    # sub-schematics will be missing from the result.
    logger.warning(
        "kicad-cli not available; falling back to regex parser for %s. "
        "Hierarchical sub-schematics will NOT be included — results may be incomplete.",
        schematic_path,
    )
    try:
        parser = SchematicParser(schematic_path)
        result = parser.parse()
        result["incomplete"] = True
        result["incomplete_reason"] = (
            "regex fallback: hierarchical sub-schematics not resolved"
        )
        return result
    except Exception as e:
        logger.warning("Error extracting netlist from %s: %s", schematic_path, e, exc_info=True)
        # Uniform shape even on error — callers can check 'error' key but
        # don't have to special-case missing keys.
        return {
            "parser_path": "error",
            "error": str(e),
            "components": {},
            "nets": {},
            "component_count": 0,
            "net_count": 0,
            "labels": None,
            "wires": None,
            "junctions": None,
            "power_symbols": None,
            "incomplete": True,
            "incomplete_reason": f"parse error: {e}",
        }


def analyze_netlist(netlist_data: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze netlist data to provide insights.

    Args:
        netlist_data: Dictionary with netlist information

    Returns:
        Dictionary with analysis results
    """
    # Recompute counts from the actual collections rather than reading
    # netlist_data["component_count"]/"net_count" with a 0 default. A
    # default of 0 would silently misreport counts when a caller passes
    # a dict missing those keys (legitimate use — callers may construct
    # their own netlist_data) while still populating components/nets.
    components_dict = netlist_data.get("components", {})
    nets_dict = netlist_data.get("nets", {})

    results: dict[str, Any] = {
        "component_count": len(components_dict),
        "net_count": len(nets_dict),
        "component_types": defaultdict(int),
        "power_nets": [],
    }

    # Propagate the incomplete flag from the parser so callers know that
    # total_pin_connections from the regex fallback will be 0 (the parser
    # records nets-by-label but does not trace connectivity).
    if netlist_data.get("incomplete"):
        results["incomplete"] = True
        results["incomplete_reason"] = netlist_data.get("incomplete_reason", "")

    for ref in components_dict:
        comp_type = re.match(r"^([A-Za-z_]+)", ref)
        if comp_type:
            results["component_types"][comp_type.group(1)] += 1

    for net_name in nets_dict:
        if is_power_net(net_name):
            results["power_nets"].append(net_name)

    results["total_pin_connections"] = sum(len(pins) for pins in nets_dict.values())

    return results
