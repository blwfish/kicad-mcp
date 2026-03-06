"""PCB net tools: add, assign, bulk assign, list, net classes, and sync from schematic."""

import json as _json
import logging
import os
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def register_pcb_net_tools(mcp: FastMCP) -> None:
    """Register PCB net tools."""

    @mcp.tool()
    def add_net(
        pcb_path: str,
        net_name: str,
    ) -> Dict[str, Any]:
        """Add a named net to the PCB.

        Uses direct file editing because pcbnew's Save() prunes nets
        that have no pads, tracks, or zones referencing them.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            net_name: Name for the net (e.g., "+3V3", "GND", "SDA").
        """
        import re as _re

        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        with open(pcb_path, "r") as f:
            content = f.read()

        # Check if net already exists
        escaped_name = _re.escape(net_name)
        if _re.search(rf'\(net\s+\d+\s+"{escaped_name}"\)', content):
            # Find the existing net code
            match = _re.search(rf'\(net\s+(\d+)\s+"{escaped_name}"\)', content)
            net_code = int(match.group(1)) if match else -1
            return {
                "status": "ok",
                "net": net_name,
                "net_code": net_code,
                "note": "Net already exists",
            }

        # Find the highest existing net code
        net_codes = [int(m.group(1)) for m in _re.finditer(r'\(net\s+(\d+)\s+"', content)]
        next_code = max(net_codes) + 1 if net_codes else 1

        # Insert the new net definition after the last existing net line
        # Net definitions appear as: (net 0 "")  (net 1 "VCC")  etc.
        last_net_match = None
        for m in _re.finditer(r'\(net\s+\d+\s+"[^"]*"\)', content):
            last_net_match = m

        if last_net_match:
            insert_pos = last_net_match.end()
            new_net_line = f'\n\t(net {next_code} "{net_name}")'
            content = content[:insert_pos] + new_net_line + content[insert_pos:]
        else:
            return {"error": "Could not find net definitions in PCB file"}

        with open(pcb_path, "w") as f:
            f.write(content)

        return {
            "status": "ok",
            "net": net_name,
            "net_code": next_code,
        }

    @mcp.tool()
    def assign_pad_net(
        pcb_path: str,
        reference: str,
        pad_number: str,
        net_name: str,
    ) -> Dict[str, Any]:
        """Assign a net to a specific pad on a footprint.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            reference: Component reference (e.g., "R1").
            pad_number: Pad number (e.g., "1", "2").
            net_name: Net name to assign.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

fp = board.FindFootprintByReference({reference!r})
if fp is None:
    print(json.dumps({{"error": f"Footprint {reference!r} not found"}}))
    raise SystemExit(0)

net = board.FindNet({net_name!r})
if net is None or net.GetNetCode() == 0:
    print(json.dumps({{"error": f"Net {net_name!r} not found"}}))
    raise SystemExit(0)

pad_count = 0
for pad in fp.Pads():
    if pad.GetNumber() == {pad_number!r}:
        pad.SetNet(net)
        pad_count += 1

if pad_count == 0:
    print(json.dumps({{"error": f"Pad {pad_number!r} not found on {reference!r}"}}))
    raise SystemExit(0)

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "reference": {reference!r},
    "pad": {pad_number!r},
    "net": {net_name!r},
    "sub_pads": pad_count,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def bulk_assign_pad_nets(
        pcb_path: str,
        assignments: List[Dict[str, str]] = [],
    ) -> Dict[str, Any]:
        """Assign nets to multiple pads in a single operation.

        Each assignment is a dict with "reference", "pad", and "net" keys.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            assignments: List of {"reference": "R1", "pad": "1", "net": "GND"} dicts.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        if not assignments:
            return {"error": "No assignments provided"}

        assignments_repr = repr(assignments)

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

assignments = {assignments_repr}
results = []
errors = []
created_nets = []

for a in assignments:
    ref = a["reference"]
    pad_num = a["pad"]
    net_name = a["net"]

    fp = board.FindFootprintByReference(ref)
    if fp is None:
        errors.append(f"Footprint {{ref}} not found")
        continue

    net = board.FindNet(net_name)
    if net is None or net.GetNetCode() == 0:
        # Auto-create missing nets — they persist because pads reference them
        net_info = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net_info)
        net = board.FindNet(net_name)
        if net is None or net.GetNetCode() == 0:
            errors.append(f"Net {{net_name}} not found and could not be created")
            continue
        created_nets.append(net_name)

    pad_count = 0
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            pad.SetNet(net)
            pad_count += 1
    if pad_count > 0:
        results.append({{"reference": ref, "pad": pad_num, "net": net_name, "sub_pads": pad_count}})
    else:
        errors.append(f"Pad {{pad_num}} not found on {{ref}}")

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "assigned": len(results),
    "nets_created": created_nets,
    "errors": errors,
    "results": results,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def rename_net(
        pcb_path: str,
        old_name: str,
        new_name: str,
    ) -> Dict[str, Any]:
        """Rename a net across the entire PCB.

        Updates the net definition and all pad references that embed the net
        name.  Track and via references store only the net code and need no
        change.  Uses direct file editing so the rename survives pcbnew's
        Save() pruning rules.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            old_name: Current net name.
            new_name: Replacement net name.
        """
        import re as _re

        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        with open(pcb_path, "r") as f:
            content = f.read()

        # Verify old net exists and get its code
        match = _re.search(r'\(net\s+(\d+)\s+"' + _re.escape(old_name) + r'"\)', content)
        if not match:
            return {"error": f"Net '{old_name}' not found in {pcb_path}"}

        if old_name == new_name:
            return {"status": "ok", "old_name": old_name, "new_name": new_name, "replacements": 0}

        # Check new name doesn't already exist
        if _re.search(r'\(net\s+\d+\s+"' + _re.escape(new_name) + r'"\)', content):
            return {"error": f"Net '{new_name}' already exists — merge not supported"}

        net_code = match.group(1)
        # Replace all occurrences of (net N "old_name") — covers both the
        # top-level definition and pad-level references.
        pattern = r'\(net\s+' + net_code + r'\s+"' + _re.escape(old_name) + r'"\)'
        new_content, count = _re.subn(pattern, f'(net {net_code} "{new_name}")', content)

        with open(pcb_path, "w") as f:
            f.write(new_content)

        return {
            "status": "ok",
            "old_name": old_name,
            "new_name": new_name,
            "net_code": int(net_code),
            "replacements": count,
        }

    @mcp.tool()
    def list_pcb_nets(pcb_path: str) -> Dict[str, Any]:
        """List all nets in the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

nets = []
for code, net in board.GetNetsByNetcode().items():
    if code > 0:
        nets.append({{
            "code": code,
            "name": net.GetNetname(),
        }})

print(json.dumps({{
    "status": "ok",
    "net_count": len(nets),
    "nets": nets,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def update_pcb_from_schematic(
        project_path: str,
    ) -> Dict[str, Any]:
        """Update PCB nets and pad assignments from the schematic (KiCad F8 equivalent).

        Exports the netlist from the schematic using kicad-cli, then creates
        all nets in the PCB and assigns them to the correct pads. This replaces
        the manual process of defining nets and assigning pads one by one.

        Requires that:
        1. The schematic (.kicad_sch) exists in the project directory
        2. The PCB (.kicad_pcb) exists in the project directory
        3. Footprints in the PCB have matching references to the schematic

        Args:
            project_path: Path to the KiCad project file (.kicad_pro).
        """
        if not os.path.exists(project_path):
            return {"error": f"Project file not found: {project_path}"}

        project_dir = os.path.dirname(project_path)
        project_name = os.path.splitext(os.path.basename(project_path))[0]

        # Find schematic and PCB files
        sch_path = os.path.join(project_dir, project_name + ".kicad_sch")
        pcb_path = os.path.join(project_dir, project_name + ".kicad_pcb")

        if not os.path.exists(sch_path):
            return {"error": f"Schematic file not found: {sch_path}"}
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        # Extract netlist from schematic via kicad-cli
        from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

        netlist_data = extract_netlist_via_cli(sch_path)
        if netlist_data is None:
            return {"error": "kicad-cli not available for netlist export"}

        # Convert from extract_netlist_via_cli format to net_definitions + pad_assignments
        nets = netlist_data.get("nets", {})
        net_definitions = list(nets.keys())
        pad_assignments = []
        for net_name, pins in nets.items():
            for pin_info in pins:
                pad_assignments.append({
                    "reference": pin_info["component"],
                    "pad": pin_info["pin"],
                    "net": net_name,
                    "pinfunction": pin_info.get("pinfunction", ""),
                })

        if not net_definitions:
            return {"error": "No nets found in schematic netlist"}

        # Sanity-check: detect obvious power/ground cross-wiring
        power_ground_warnings = []
        for a in pad_assignments:
            func = (a.get("pinfunction") or "").upper()
            net = a["net"].upper()
            if func in ("GND", "VSS") and any(
                p in net for p in ("+3V3", "+5V", "VCC", "VDD", "3V3", "5V")
            ):
                power_ground_warnings.append(
                    f"{a['reference']} pin {a['pad']} ({func}) → power net {a['net']}"
                )
            elif func in ("VDD", "VCC", "3V3", "5V") and "GND" in net:
                power_ground_warnings.append(
                    f"{a['reference']} pin {a['pad']} ({func}) → ground net {a['net']}"
                )

        # Detect suspiciously large nets (likely schematic wiring errors)
        LARGE_NET_THRESHOLD = 30
        for net_name, pins in nets.items():
            if len(pins) > LARGE_NET_THRESHOLD:
                # Collect distinct pin functions on this net
                funcs = set()
                for p in pins:
                    f = (p.get("pinfunction") or "").upper()
                    if f:
                        funcs.add(f)
                # If a net has both power and ground pin functions, it's shorted
                has_gnd = any(f in ("GND", "VSS", "GNDA") for f in funcs)
                has_pwr = any(
                    f in ("VDD", "VCC", "VIN", "3V3", "5V", "VBUS") for f in funcs
                )
                severity = "ERROR: power/ground short" if (has_gnd and has_pwr) else "WARNING"
                power_ground_warnings.append(
                    f"{severity}: net '{net_name}' has {len(pins)} nodes "
                    f"(functions: {', '.join(sorted(funcs)) or 'none'}). "
                    f"Check schematic for unintended connections."
                )

        # Detect missing expected power/ground nets
        EXPECTED_POWER_NETS = {"GND", "+3V3", "+5V", "VCC", "VDD"}
        # Check if any GND-function pin exists in the design
        all_funcs = set()
        for a in pad_assignments:
            f = (a.get("pinfunction") or "").upper()
            if f:
                all_funcs.add(f)
        net_names_upper = {n.upper() for n in net_definitions}
        if any(f in ("GND", "VSS") for f in all_funcs):
            if "GND" not in net_names_upper:
                power_ground_warnings.append(
                    "WARNING: design has GND-function pins but no GND net. "
                    "GND may be absorbed into another net due to schematic wiring error."
                )

        # Step 3: Inject nets into PCB file via direct editing
        # (pcbnew's Save() prunes unused nets, so we must inject them
        # into the file first, then use pcbnew for pad assignments)
        import re as _re

        with open(pcb_path, "r") as f:
            pcb_content = f.read()

        # Find existing nets
        existing_nets = {}
        for m in _re.finditer(r'\(net\s+(\d+)\s+"([^"]*)"\)', pcb_content):
            existing_nets[m.group(2)] = int(m.group(1))

        # Determine next available net code
        max_code = max(existing_nets.values()) if existing_nets else 0

        nets_created = []
        nets_existing = []
        skipped_unconnected = []

        for net_name in net_definitions:
            if net_name in existing_nets:
                nets_existing.append(net_name)
            else:
                max_code += 1
                existing_nets[net_name] = max_code
                nets_created.append(net_name)

        # Insert new net definitions into the file
        if nets_created:
            # Find the last (net ...) line to insert after
            last_net_match = None
            for m in _re.finditer(r'\(net\s+\d+\s+"[^"]*"\)', pcb_content):
                last_net_match = m

            if last_net_match:
                insert_pos = last_net_match.end()
                new_lines = ""
                for net_name in nets_created:
                    code = existing_nets[net_name]
                    new_lines += f'\n\t(net {code} "{net_name}")'
                pcb_content = (
                    pcb_content[:insert_pos] + new_lines + pcb_content[insert_pos:]
                )

                with open(pcb_path, "w") as f:
                    f.write(pcb_content)

        # Step 4: Assign nets to pads via pcbnew
        assignments_repr = repr(pad_assignments)

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

assignments = {assignments_repr}
assigned = []
assign_errors = []

for a in assignments:
    ref = a["reference"]
    pad_num = a["pad"]
    net_name = a["net"]

    fp = board.FindFootprintByReference(ref)
    if fp is None:
        assign_errors.append(f"Footprint {{ref}} not found in PCB")
        continue

    net = board.FindNet(net_name)
    if net is None or net.GetNetCode() == 0:
        assign_errors.append(f"Net {{net_name}} not found")
        continue

    pad_count = 0
    pinfunc = a.get("pinfunction", "")
    old_net = ""
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            if pad_count == 0:
                old_net = pad.GetNetname()
            pad.SetNet(net)
            pad_count += 1
    if pad_count > 0:
        assigned.append({{
            "reference": ref,
            "pad": pad_num,
            "net": net_name,
            "pinfunction": pinfunc,
            "old_net": old_net,
            "sub_pads": pad_count,
        }})
    else:
        assign_errors.append(f"Pad {{pad_num}} not found on {{ref}}")

board.Save({pcb_path!r})

# Verify nets
final_nets = []
for code, net in board.GetNetsByNetcode().items():
    if code > 0:
        final_nets.append(net.GetNetname())

# Build a mapping summary for quick human verification
# Format: "NET_NAME: REF:pad(function), REF:pad(function), ..."
net_map = {{}}
for a in assigned:
    net = a["net"]
    entry = f"{{a['reference']}}:{{a['pad']}}"
    if a.get("pinfunction"):
        entry += f"({{a['pinfunction']}})"
    net_map.setdefault(net, []).append(entry)
mapping_summary = {{net: ", ".join(entries) for net, entries in sorted(net_map.items())}}

print(json.dumps({{
    "status": "ok",
    "total_nets_in_pcb": len(final_nets),
    "pads_assigned": len(assigned),
    "pad_assignments": assigned,
    "assignment_errors": assign_errors,
    "mapping_summary": mapping_summary,
}}))
"""
        pcbnew_result = run_pcbnew_script(script, timeout=60.0)

        # Merge file-editing results with pcbnew results
        pcbnew_result["schematic"] = sch_path
        pcbnew_result["pcb"] = pcb_path
        pcbnew_result["nets_created"] = nets_created
        pcbnew_result["nets_existing"] = nets_existing
        pcbnew_result["unconnected_nets_skipped"] = skipped_unconnected
        if power_ground_warnings:
            pcbnew_result["power_ground_warnings"] = power_ground_warnings

        return pcbnew_result

    @mcp.tool()
    def set_net_class(
        pcb_path: str,
        class_name: str,
        nets: List[str],
        track_width_mm: float = 0.25,
        clearance_mm: float = 0.2,
        via_diameter_mm: float = 0.6,
        via_drill_mm: float = 0.3,
    ) -> Dict[str, Any]:
        """Create or update a net class and assign nets to it.

        Net classes define per-net routing rules (trace width, clearance, via
        size).  FreeRouter reads these from the Specctra DSN export and routes
        each net at the correct width.  Call this BEFORE autoroute_pcb so
        FreeRouter sees the classes.

        Common usage: create a "Power" class with wider traces for GND/VCC,
        and leave signal nets on the Default class.

        Net class definitions live in the KiCad project file (.kicad_pro),
        which must exist alongside the PCB file.

        Args:
            pcb_path: Path to the .kicad_pcb file.  The .kicad_pro file is
                derived from this path (same directory, same stem).
            class_name: Name for the net class (e.g., "Power", "HighSpeed").
            nets: List of net names to assign to this class.
            track_width_mm: Trace width in mm (default 0.25).
            clearance_mm: Clearance to other nets in mm (default 0.2).
            via_diameter_mm: Via pad diameter in mm (default 0.6).
            via_drill_mm: Via drill diameter in mm (default 0.3).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        # Derive project file path
        stem = os.path.splitext(pcb_path)[0]
        pro_path = stem + ".kicad_pro"
        if not os.path.exists(pro_path):
            return {"error": f"Project file not found: {pro_path}"}

        with open(pro_path, "r") as f:
            project = _json.load(f)

        # Ensure net_settings structure exists
        if "net_settings" not in project:
            project["net_settings"] = {
                "classes": [_default_net_class()],
                "meta": {"version": 4},
                "net_colors": None,
                "netclass_assignments": None,
                "netclass_patterns": [],
            }

        ns = project["net_settings"]
        classes = ns.get("classes", [])

        # Find or create the class
        existing = None
        for cls in classes:
            if cls.get("name") == class_name:
                existing = cls
                break

        if existing:
            existing["track_width"] = track_width_mm
            existing["clearance"] = clearance_mm
            existing["via_diameter"] = via_diameter_mm
            existing["via_drill"] = via_drill_mm
            action = "updated"
        else:
            new_class = _default_net_class()
            new_class["name"] = class_name
            new_class["track_width"] = track_width_mm
            new_class["clearance"] = clearance_mm
            new_class["via_diameter"] = via_diameter_mm
            new_class["via_drill"] = via_drill_mm
            classes.append(new_class)
            ns["classes"] = classes
            action = "created"

        # Assign nets to this class
        assignments = ns.get("netclass_assignments") or {}
        assigned_count = 0
        for net_name in nets:
            assignments[net_name] = class_name
            assigned_count += 1
        ns["netclass_assignments"] = assignments

        with open(pro_path, "w") as f:
            _json.dump(project, f, indent=2)
            f.write("\n")

        return {
            "status": "ok",
            "class_name": class_name,
            "action": action,
            "track_width_mm": track_width_mm,
            "clearance_mm": clearance_mm,
            "via_diameter_mm": via_diameter_mm,
            "via_drill_mm": via_drill_mm,
            "nets_assigned": assigned_count,
            "nets": nets,
            "project_file": pro_path,
        }


def _default_net_class() -> Dict[str, Any]:
    """Return a default net class template matching KiCad 9's format."""
    return {
        "bus_width": 12,
        "clearance": 0.2,
        "diff_pair_gap": 0.25,
        "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2,
        "line_style": 0,
        "microvia_diameter": 0.3,
        "microvia_drill": 0.1,
        "name": "Default",
        "pcb_color": "rgba(0, 0, 0, 0.000)",
        "priority": 2147483647,
        "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.2,
        "via_diameter": 0.6,
        "via_drill": 0.3,
        "wire_width": 6,
    }
