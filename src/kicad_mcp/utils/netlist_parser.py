"""
KiCad schematic netlist extraction utilities.
"""
import os
import re
from collections import defaultdict
from typing import Any, Dict, List


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

            matches.append(s_exp)

        return matches

    def _extract_components(self) -> None:
        """Extract component information from schematic."""
        print("Extracting components")

        symbols = self._extract_s_expressions(r"\(symbol\s+")

        for symbol in symbols:
            component = self._parse_component(symbol)
            if component:
                self.components.append(component)
                ref = component.get("reference", "Unknown")
                self.component_info[ref] = component

        print(f"Extracted {len(self.components)} components")

    def _parse_component(self, symbol_expr: str) -> Dict[str, Any]:
        """Parse a component from a symbol S-expression.

        Args:
            symbol_expr: Symbol S-expression

        Returns:
            Component information dictionary
        """
        component: dict[str, Any] = {}

        lib_id_match = re.search(r'\(lib_id\s+"([^"]+)"\)', symbol_expr)
        if lib_id_match:
            component["lib_id"] = lib_id_match.group(1)

        property_matches = re.finditer(
            r'\(property\s+"([^"]+)"\s+"([^"]+)"', symbol_expr
        )
        for match in property_matches:
            prop_name = match.group(1)
            prop_value = match.group(2)

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
                    "text": label_match.group(1),
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
                    "text": label_match.group(1),
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
                    "text": label_match.group(1),
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


def extract_netlist(schematic_path: str) -> Dict[str, Any]:
    """Extract netlist information from a KiCad schematic file.

    Args:
        schematic_path: Path to the KiCad schematic file (.kicad_sch)

    Returns:
        Dictionary with netlist information
    """
    try:
        parser = SchematicParser(schematic_path)
        return parser.parse()
    except Exception as e:
        print(f"Error extracting netlist: {e}")
        return {
            "error": str(e),
            "components": {},
            "nets": {},
            "component_count": 0,
            "net_count": 0,
        }


def analyze_netlist(netlist_data: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze netlist data to provide insights.

    Args:
        netlist_data: Dictionary with netlist information

    Returns:
        Dictionary with analysis results
    """
    results: dict[str, Any] = {
        "component_count": netlist_data.get("component_count", 0),
        "net_count": netlist_data.get("net_count", 0),
        "component_types": defaultdict(int),
        "power_nets": [],
    }

    for ref in netlist_data.get("components", {}):
        comp_type = re.match(r"^([A-Za-z_]+)", ref)
        if comp_type:
            results["component_types"][comp_type.group(1)] += 1

    for net_name in netlist_data.get("nets", {}):
        if any(
            net_name.startswith(prefix)
            for prefix in ["VCC", "VDD", "GND", "+5V", "+3V3", "+12V"]
        ):
            results["power_nets"].append(net_name)

    total_pins = sum(
        len(pins) for pins in netlist_data.get("nets", {}).values()
    )
    results["total_pin_connections"] = total_pins

    return results
