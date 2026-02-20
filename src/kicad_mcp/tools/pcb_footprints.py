"""PCB footprint tools: place, move, list, search, and get pad positions."""

import logging
import os
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

logger = logging.getLogger(__name__)


def register_pcb_footprint_tools(mcp: FastMCP) -> None:
    """Register PCB footprint tools."""

    @mcp.tool()
    def place_footprint(
        pcb_path: str,
        library: str,
        footprint_name: str,
        reference: str,
        value: str,
        x_mm: float,
        y_mm: float,
        rotation_deg: float = 0.0,
        layer: str = "F.Cu",
    ) -> Dict[str, Any]:
        """Place a footprint on the PCB from a KiCad library.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            library: Footprint library name (e.g., "Resistor_THT").
            footprint_name: Footprint name within the library.
            reference: Component reference (e.g., "R1").
            value: Component value (e.g., "330").
            x_mm: X position in millimeters.
            y_mm: Y position in millimeters.
            rotation_deg: Rotation angle in degrees (default 0).
            layer: PCB layer, "F.Cu" or "B.Cu" (default "F.Cu").
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json, os, glob

board = pcbnew.LoadBoard({pcb_path!r})

# Find the footprint library path
lib_name = {library!r}
fp_name = {footprint_name!r}

# Search standard KiCad footprint library locations
lib_search_paths = [
    "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
    os.path.expanduser("~/Documents/KiCad/footprints"),
    "/usr/share/kicad/footprints",
]

lib_path = None
for search_path in lib_search_paths:
    candidate = os.path.join(search_path, lib_name + ".pretty")
    if os.path.isdir(candidate):
        lib_path = candidate
        break

if not lib_path:
    print(json.dumps({{"error": f"Library '{{lib_name}}' not found"}}))
    raise SystemExit(0)

fp = pcbnew.FootprintLoad(lib_path, fp_name)
if fp is None:
    print(json.dumps({{"error": f"Footprint '{{fp_name}}' not found in '{{lib_name}}'"}}))
    raise SystemExit(0)

fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))
fp.SetReference({reference!r})
fp.SetValue({value!r})

if {rotation_deg} != 0:
    fp.SetOrientationDegrees({rotation_deg})

if {layer!r} == "B.Cu":
    fp.Flip(fp.GetPosition(), False)

board.Add(fp)
board.Save({pcb_path!r})

print(json.dumps({{
    "status": "ok",
    "placed": {{
        "reference": {reference!r},
        "footprint": f"{{lib_name}}:{{fp_name}}",
        "x_mm": {x_mm},
        "y_mm": {y_mm},
        "rotation": {rotation_deg},
        "layer": {layer!r},
    }},
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def move_footprint(
        pcb_path: str,
        reference: str,
        x_mm: float,
        y_mm: float,
        rotation_deg: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Move a footprint to a new position on the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            reference: Component reference (e.g., "R1").
            x_mm: New X position in millimeters.
            y_mm: New Y position in millimeters.
            rotation_deg: New rotation in degrees (None to keep current).
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        rot_code = f"fp.SetOrientationDegrees({rotation_deg})" if rotation_deg is not None else ""

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

fp = board.FindFootprintByReference({reference!r})
if fp is None:
    print(json.dumps({{"error": f"Footprint {reference!r} not found"}}))
    raise SystemExit(0)

fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM({x_mm}), pcbnew.FromMM({y_mm})))
{rot_code}

board.Save({pcb_path!r})

pos = fp.GetPosition()
print(json.dumps({{
    "status": "ok",
    "reference": {reference!r},
    "x_mm": round(pcbnew.ToMM(pos.x), 3),
    "y_mm": round(pcbnew.ToMM(pos.y), 3),
    "rotation": fp.GetOrientationDegrees(),
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def list_pcb_footprints(pcb_path: str) -> Dict[str, Any]:
        """List all footprints currently placed on the PCB.

        Args:
            pcb_path: Path to the .kicad_pcb file.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        script = f"""
import pcbnew, json

board = pcbnew.LoadBoard({pcb_path!r})

fp_list = []
for fp in board.GetFootprints():
    pos = fp.GetPosition()
    pads = []
    for pad in fp.Pads():
        pad_pos = pad.GetPosition()
        pads.append({{
            "number": pad.GetNumber(),
            "x_mm": round(pcbnew.ToMM(pad_pos.x), 3),
            "y_mm": round(pcbnew.ToMM(pad_pos.y), 3),
            "net": pad.GetNetname(),
        }})
    fp_list.append({{
        "reference": fp.GetReference(),
        "value": fp.GetValue(),
        "footprint": fp.GetFPID().GetUniStringLibItemName(),
        "x_mm": round(pcbnew.ToMM(pos.x), 3),
        "y_mm": round(pcbnew.ToMM(pos.y), 3),
        "rotation": fp.GetOrientationDegrees(),
        "layer": board.GetLayerName(fp.GetLayer()),
        "pads": pads,
    }})

print(json.dumps({{
    "status": "ok",
    "footprint_count": len(fp_list),
    "footprints": fp_list,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def get_pad_positions(
        pcb_path: str,
        reference: str,
    ) -> Dict[str, Any]:
        """Get all pad positions for a footprint.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            reference: Component reference (e.g., "R1").
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

pads = []
for pad in fp.Pads():
    pos = pad.GetPosition()
    pads.append({{
        "number": pad.GetNumber(),
        "x_mm": round(pcbnew.ToMM(pos.x), 3),
        "y_mm": round(pcbnew.ToMM(pos.y), 3),
        "net": pad.GetNetname(),
        "shape": str(pad.GetShape()),
    }})

print(json.dumps({{
    "status": "ok",
    "reference": {reference!r},
    "pad_count": len(pads),
    "pads": pads,
}}))
"""
        return run_pcbnew_script(script)

    @mcp.tool()
    def search_footprints(
        query: str,
        library: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Search for footprints in KiCad footprint libraries.

        Searches a SQLite FTS5 index built from all installed KiCad footprint
        libraries. The index auto-rebuilds when library files change (e.g. after
        a KiCad upgrade). Returns footprint names suitable for use with
        place_footprint.

        Args:
            query: Search terms (e.g., "SOT-23", "0603 resistor", "QFP 48").
            library: Optional library name to restrict search (e.g., "Resistor_SMD").
            limit: Maximum number of results (default 20).
        """
        try:
            from kicad_mcp.utils.library_index import get_library_index

            index = get_library_index()

            if index.footprints_stale():
                count = index.rebuild_footprints()
                logger.info("Footprint index rebuilt: %d entries", count)

            results = index.search_footprints(query, library=library, limit=limit)

            return {
                "status": "ok",
                "count": len(results),
                "results": results,
            }
        except Exception as e:
            logger.error("Footprint search failed: %s", e)
            return {"error": f"Footprint search failed: {e}"}
