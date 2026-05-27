"""PCB net ops: add, assign, bulk assign, list, net classes, and sync from schematic.

These are module-level _op_* helpers consumed by the pcb router (pcb.py).
"""

import json as _json
import logging
import os
from typing import Any, Dict, List, Optional

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def _op_add_net(pcb_path: str, net_name: str) -> Dict[str, Any]:
    """Add a named net to the PCB (direct file editing)."""
    import re as _re

    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}
    if not net_name:
        return {"error": "net_name must not be empty"}
    if '"' in net_name or "\\" in net_name or "\n" in net_name or "\r" in net_name:
        return {"error": f"net_name contains characters that require S-expression escaping: {net_name!r}"}

    with open(pcb_path, "r") as f:
        content = f.read()

    escaped_name = _re.escape(net_name)
    match = _re.search(rf'\(net\s+(\d+)\s+"{escaped_name}"\)', content)
    if match:
        return {
            "status": "ok",
            "net": net_name,
            "net_code": int(match.group(1)),
            "note": "Net already exists",
        }

    net_codes = [int(m.group(1)) for m in _re.finditer(r'\(net\s+(\d+)\s+"', content)]
    next_code = max(net_codes) + 1 if net_codes else 1

    last_net_match = None
    for m in _re.finditer(r'\(net\s+\d+\s+"(?:[^"\\]|\\.)*"\)', content):
        last_net_match = m

    if last_net_match:
        insert_pos = last_net_match.end()
    else:
        fp_match = _re.search(r'\n\t\(footprint\b', content)
        if fp_match:
            insert_pos = fp_match.start()
        else:
            insert_pos = content.rfind('\n)')
            if insert_pos == -1:
                return {"error": "Could not find insertion point in PCB file"}
    new_net_line = f'\n\t(net {next_code} "{net_name}")'
    content = content[:insert_pos] + new_net_line + content[insert_pos:]

    with open(pcb_path, "w") as f:
        f.write(content)

    return {
        "status": "ok",
        "net": net_name,
        "net_code": next_code,
    }


def _op_assign_pad_net(
    pcb_path: str,
    reference: str,
    pad_number: str,
    net_name: str,
) -> Dict[str, Any]:
    """Assign a net to a specific pad on a footprint."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]
reference = params["reference"]
pad_number = params["pad_number"]
net_name = params["net_name"]

board = pcbnew.LoadBoard(pcb_path)

fp = board.FindFootprintByReference(reference)
if fp is None:
    print(json.dumps({"error": f"Footprint {reference!r} not found"}))
    raise SystemExit(0)

net = board.FindNet(net_name)
if net is None or net.GetNetCode() == 0:
    print(json.dumps({"error": f"Net {net_name!r} not found"}))
    raise SystemExit(0)

pad_count = 0
for pad in fp.Pads():
    if pad.GetNumber() == pad_number:
        pad.SetNet(net)
        pad_count += 1

if pad_count == 0:
    print(json.dumps({"error": f"Pad {pad_number!r} not found on {reference!r}"}))
    raise SystemExit(0)

board.Save(pcb_path)

print(json.dumps({
    "status": "ok",
    "reference": reference,
    "pad": pad_number,
    "net": net_name,
    "sub_pads": pad_count,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "reference": reference,
        "pad_number": pad_number,
        "net_name": net_name,
    })


def _op_bulk_assign_pad_nets(
    pcb_path: str,
    assignments: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Assign nets to multiple pads in a single operation."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    if not assignments:
        return {"error": "No assignments provided"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

assignments = params["assignments"]
results = []
errors = []
created_nets = []

for a in assignments:
    ref = a["reference"]
    pad_num = a["pad"]
    net_name = a["net"]

    fp = board.FindFootprintByReference(ref)
    if fp is None:
        errors.append(f"Footprint {ref} not found")
        continue

    net = board.FindNet(net_name)
    if net is None or net.GetNetCode() == 0:
        net_info = pcbnew.NETINFO_ITEM(board, net_name)
        board.Add(net_info)
        net = board.FindNet(net_name)
        if net is None or net.GetNetCode() == 0:
            errors.append(f"Net {net_name} not found and could not be created")
            continue
        created_nets.append(net_name)

    pad_count = 0
    for pad in fp.Pads():
        if pad.GetNumber() == pad_num:
            pad.SetNet(net)
            pad_count += 1
    if pad_count > 0:
        results.append({"reference": ref, "pad": pad_num, "net": net_name, "sub_pads": pad_count})
    else:
        errors.append(f"Pad {pad_num} not found on {ref}")

board.Save(pcb_path)

if not errors:
    out_status = "ok"
elif results:
    out_status = "partial"
else:
    out_status = "error"

print(json.dumps({
    "status": out_status,
    "assigned": len(results),
    "error_count": len(errors),
    "nets_created": created_nets,
    "errors": errors,
    "results": results,
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "assignments": assignments,
    })


def _op_rename_net(pcb_path: str, old_name: str, new_name: str) -> Dict[str, Any]:
    """Rename a net across the entire PCB."""
    import re as _re

    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}
    for name, label in [(old_name, "old_name"), (new_name, "new_name")]:
        if not name:
            return {"error": f"{label} must not be empty"}
        if '"' in name or "\\" in name or "\n" in name or "\r" in name:
            return {"error": f"{label} contains characters that require S-expression escaping: {name!r}"}

    with open(pcb_path, "r") as f:
        content = f.read()

    match = _re.search(r'\(net\s+(\d+)\s+"' + _re.escape(old_name) + r'"\)', content)
    if not match:
        return {"error": f"Net '{old_name}' not found in {pcb_path}"}

    if old_name == new_name:
        return {"status": "ok", "old_name": old_name, "new_name": new_name, "replacements": 0}

    if _re.search(r'\(net\s+\d+\s+"' + _re.escape(new_name) + r'"\)', content):
        return {"error": f"Net '{new_name}' already exists — merge not supported"}

    net_code = match.group(1)
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


def _op_list_nets(pcb_path: str) -> Dict[str, Any]:
    """List all nets in the PCB."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)

nets = []
for code, net in board.GetNetsByNetcode().items():
    if code > 0:
        nets.append({
            "code": code,
            "name": net.GetNetname(),
        })

print(json.dumps({
    "status": "ok",
    "net_count": len(nets),
    "nets": nets,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _op_set_net_class(
    pcb_path: str,
    class_name: str,
    nets: List[str],
    track_width_mm: float = 0.25,
    clearance_mm: float = 0.2,
    via_diameter_mm: float = 0.6,
    via_drill_mm: float = 0.3,
) -> Dict[str, Any]:
    """Create or update a net class and assign nets to it."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    stem = os.path.splitext(pcb_path)[0]
    pro_path = stem + ".kicad_pro"
    if not os.path.exists(pro_path):
        return {"error": f"Project file not found: {pro_path}"}

    with open(pro_path, "r") as f:
        project = _json.load(f)

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
