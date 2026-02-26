"""PCB net tools: add, assign, bulk assign, list, and sync from schematic."""

import logging
import os
from typing import Any, Dict, List

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

pad_found = False
for pad in fp.Pads():
    if pad.GetNumber() == {pad_number!r}:
        pad.SetNet(net)
        pad_found = True
        break

if not pad_found:
    print(json.dumps({{"error": f"Pad {pad_number!r} not found on {reference!r}"}}))
    raise SystemExit(0)

board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "reference": {reference!r},
    "pad": {pad_number!r},
    "net": {net_name!r},
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

    pad_found = False
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            pad.SetNet(net)
            pad_found = True
            results.append({{"reference": ref, "pad": pad_num, "net": net_name}})
            break

    if not pad_found:
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

    pad_found = False
    pinfunc = a.get("pinfunction", "")
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            old_net = pad.GetNetname()
            pad.SetNet(net)
            assigned.append({{
                "reference": ref,
                "pad": pad_num,
                "net": net_name,
                "pinfunction": pinfunc,
                "old_net": old_net,
            }})
            pad_found = True
            break

    if not pad_found:
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
