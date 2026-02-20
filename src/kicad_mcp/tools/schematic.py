"""Schematic tools — wrapping kicad-sch-api for schematic manipulation.

All tools operate on a single in-memory schematic at a time.  ``load_schematic``
or ``create_schematic`` must be called first; subsequent tools operate on that
loaded instance until a different schematic is loaded.

The underlying library is kicad-sch-api (PyPI), which provides lossless
round-trip parsing of .kicad_sch files.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level schematic state
# ---------------------------------------------------------------------------

_current_schematic: Any | None = None


def _require_schematic() -> Any:
    """Return the current schematic or raise."""
    if _current_schematic is None:
        raise RuntimeError("No schematic loaded. Call create_schematic or load_schematic first.")
    return _current_schematic


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_schematic_tools(mcp: FastMCP) -> None:
    """Register all schematic MCP tools on *mcp*."""

    # Lazy import so the module can be loaded without kicad-sch-api installed
    # (allows server startup to report a clean error instead of ImportError at import time).
    try:
        import kicad_sch_api as ksa  # noqa: F401 – checked at registration time
    except ImportError:
        logger.warning(
            "kicad-sch-api not installed — schematic tools will not be available. "
            "Install with: pip install kicad-sch-api"
        )
        return

    # ------------------------------------------------------------------
    # Schematic lifecycle
    # ------------------------------------------------------------------

    @mcp.tool()
    def create_schematic(name: str = "untitled") -> dict:
        """Create a new KiCad schematic.

        Args:
            name: Name for the schematic.
        """
        global _current_schematic
        import kicad_sch_api as ksa

        _current_schematic = ksa.create_schematic(name)
        logger.info("Created new schematic: %s", name)
        return {"status": "ok", "name": name}

    @mcp.tool()
    def load_schematic(file_path: str) -> dict:
        """Load an existing KiCad schematic file.

        Args:
            file_path: Path to the .kicad_sch file.
        """
        global _current_schematic
        import kicad_sch_api as ksa

        _current_schematic = ksa.load_schematic(file_path)
        comp_count = len(list(_current_schematic.components))
        logger.info("Loaded schematic: %s (%d components)", file_path, comp_count)
        return {"status": "ok", "file_path": file_path, "components": comp_count}

    @mcp.tool()
    def save_schematic(file_path: str | None = None) -> dict:
        """Save the current schematic to a file.

        Args:
            file_path: Optional path to save to.  If omitted, saves to the
                       original file path.
        """
        sch = _require_schematic()
        if file_path:
            sch.save(file_path)
        else:
            sch.save()
        dest = file_path or str(getattr(sch, "file_path", "current file"))
        logger.info("Saved schematic to: %s", dest)
        return {"status": "ok", "file_path": dest}

    @mcp.tool()
    def get_schematic_info() -> dict:
        """Get information about the current schematic."""
        sch = _require_schematic()
        try:
            summary = sch.get_summary()
        except Exception:
            summary = {}

        info: dict[str, Any] = {
            "status": "ok",
            "components": len(list(sch.components)),
            "wires": len(sch.wires) if hasattr(sch, "wires") else 0,
            "junctions": len(sch.junctions) if hasattr(sch, "junctions") else 0,
            "modified": getattr(sch, "modified", False),
        }
        # Merge any extra summary keys (title, etc.)
        for k, v in summary.items():
            if k not in info:
                info[k] = v
        return info

    @mcp.tool()
    def validate_schematic() -> dict:
        """Validate schematic for errors and issues."""
        sch = _require_schematic()
        issues = sch.validate()
        if not issues:
            return {"status": "ok", "issues": 0, "errors": 0, "warnings": 0, "details": []}

        details = []
        errors = 0
        warnings = 0
        for issue in issues:
            level = issue.level.value if hasattr(issue.level, "value") else str(issue.level)
            if level in ("error", "critical"):
                errors += 1
            elif level == "warning":
                warnings += 1
            details.append({"level": level, "message": str(issue)})

        return {
            "status": "ok",
            "issues": len(issues),
            "errors": errors,
            "warnings": warnings,
            "details": details[:20],  # cap output size
        }

    @mcp.tool()
    def backup_schematic(suffix: str = ".backup") -> dict:
        """Create backup of current schematic file.

        Args:
            suffix: Backup file suffix (default: .backup).
        """
        sch = _require_schematic()
        backup_path = sch.backup(suffix)
        return {"status": "ok", "backup_path": str(backup_path)}

    @mcp.tool()
    def clone_schematic(new_name: str | None = None) -> dict:
        """Create a copy of the current schematic.

        The clone is NOT loaded as the current schematic.

        Args:
            new_name: Name for cloned schematic (optional).
        """
        sch = _require_schematic()
        cloned = sch.clone(new_name)
        comp_count = len(list(cloned.components))
        return {"status": "ok", "name": new_name or "Clone", "components": comp_count}

    # ------------------------------------------------------------------
    # Component management
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_component(
        lib_id: str,
        reference: str,
        value: str,
        position: list[float],
        footprint: str | None = None,
        properties: str | None = None,
    ) -> dict:
        """Add a component to the current schematic.

        Args:
            lib_id: Library ID (e.g., Device:R).
            reference: Component reference (e.g., R1).
            value: Component value (e.g., 10k).
            position: [x, y] coordinates.
            footprint: Component footprint (e.g., Resistor_SMD:R_0603_1608Metric).
            properties: Additional properties as key=value pairs, comma-separated.
        """
        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "position must be [x, y]"}

        comp = sch.components.add(
            lib_id=lib_id,
            reference=reference,
            value=value,
            position=tuple(position),
            footprint=footprint,
        )

        if properties:
            for prop in properties.split(","):
                if "=" in prop:
                    key, val = prop.strip().split("=", 1)
                    comp.set_property(key.strip(), val.strip())

        logger.info("Added component %s (%s) = %s at %s", reference, lib_id, value, position)
        return {
            "status": "ok",
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "position": position,
        }

    @mcp.tool()
    def remove_component(reference: str) -> dict:
        """Remove component from schematic.

        Args:
            reference: Component reference to remove.
        """
        sch = _require_schematic()
        removed = sch.components.remove(reference)
        if removed:
            return {"status": "ok", "reference": reference}
        return {"error": f"Component {reference} not found"}

    @mcp.tool()
    def list_components() -> dict:
        """List all components in the current schematic."""
        sch = _require_schematic()
        components = []
        for comp in sch.components:
            entry: dict[str, Any] = {
                "reference": comp.reference,
                "lib_id": comp.lib_id,
                "value": comp.value,
            }
            if hasattr(comp, "position") and comp.position:
                entry["position"] = [comp.position.x, comp.position.y]
            if hasattr(comp, "footprint") and comp.footprint:
                entry["footprint"] = comp.footprint
            components.append(entry)
        return {"status": "ok", "count": len(components), "components": components}

    @mcp.tool()
    def search_components(query: str, library: str | None = None, limit: int = 20) -> dict:
        """Search for components in KiCad symbol libraries.

        Args:
            query: Search term (e.g., resistor, op amp, 555).
            library: Optional library to search in.
            limit: Maximum number of results.
        """
        try:
            from kicad_sch_api.discovery import get_search_index

            search_index = get_search_index()

            # Rebuild if index is empty or any library file has been modified
            if search_index.is_stale():
                search_index.rebuild_index()

            results = search_index.search(query, library=library, limit=limit)
            if not results:
                return {"status": "ok", "count": 0, "results": []}

            items = []
            for r in results[:limit]:
                items.append({
                    "lib_id": r.get("lib_id", "Unknown"),
                    "name": r.get("name", ""),
                    "library": r.get("library", ""),
                    "description": r.get("description", ""),
                    "keywords": r.get("keywords", ""),
                    "pin_count": r.get("pin_count", 0),
                })
            return {"status": "ok", "count": len(items), "results": items}
        except ImportError:
            return {"error": "Component search functionality not available"}

    @mcp.tool()
    def filter_components(
        lib_id: str | None = None,
        value: str | None = None,
        reference: str | None = None,
        footprint: str | None = None,
    ) -> dict:
        """Filter components by criteria.

        Args:
            lib_id: Filter by library ID (e.g., Device:R).
            value: Filter by component value.
            reference: Filter by reference pattern.
            footprint: Filter by footprint.
        """
        sch = _require_schematic()
        criteria: dict[str, str] = {}
        if lib_id:
            criteria["lib_id"] = lib_id
        if value:
            criteria["value"] = value
        if reference:
            criteria["reference"] = reference
        if footprint:
            criteria["footprint"] = footprint

        if not criteria:
            return {"error": "At least one filter criterion required"}

        filtered = sch.components.filter(**criteria)
        results = []
        for comp in filtered:
            results.append({
                "reference": comp.reference,
                "lib_id": comp.lib_id,
                "value": comp.value,
                "position": [comp.position.x, comp.position.y] if comp.position else None,
            })
        return {"status": "ok", "count": len(results), "components": results}

    @mcp.tool()
    def components_in_area(x1: float, y1: float, x2: float, y2: float) -> dict:
        """Find components in rectangular area.

        Args:
            x1: Left X coordinate.
            y1: Top Y coordinate.
            x2: Right X coordinate.
            y2: Bottom Y coordinate.
        """
        sch = _require_schematic()
        found = sch.components.in_area(x1, y1, x2, y2)
        results = []
        for comp in found:
            results.append({
                "reference": comp.reference,
                "lib_id": comp.lib_id,
                "position": [comp.position.x, comp.position.y] if comp.position else None,
            })
        return {"status": "ok", "count": len(results), "components": results}

    @mcp.tool()
    def bulk_update_components(criteria: dict, updates: dict) -> dict:
        """Update multiple components at once.

        Args:
            criteria: Filter criteria (lib_id, value, etc.).
            updates: Updates to apply (value, footprint, properties).
        """
        sch = _require_schematic()
        updated_count = sch.components.bulk_update(criteria=criteria, updates=updates)
        return {"status": "ok", "updated": updated_count}

    # ------------------------------------------------------------------
    # Pin tools (formerly broken stubs — now working via kicad-sch-api)
    # ------------------------------------------------------------------

    @mcp.tool()
    def get_component_pin_position(reference: str, pin_number: str) -> dict:
        """Get absolute position of a component pin.

        Args:
            reference: Component reference (e.g., R1).
            pin_number: Pin number (e.g., 1, 2).
        """
        sch = _require_schematic()
        comp = sch.components.get(reference)
        if comp is None:
            return {"error": f"Component {reference} not found"}

        pin_pos = comp.get_pin_position(pin_number)
        if pin_pos is None:
            return {"error": f"Pin {pin_number} not found on {reference}"}

        return {
            "status": "ok",
            "reference": reference,
            "pin_number": pin_number,
            "x": round(pin_pos.x, 3),
            "y": round(pin_pos.y, 3),
        }

    @mcp.tool()
    def list_component_pins(reference: str) -> dict:
        """List all pins for a component with positions.

        Args:
            reference: Component reference (e.g., R1).
        """
        sch = _require_schematic()
        comp = sch.components.get(reference)
        if comp is None:
            return {"error": f"Component {reference} not found"}

        pins_data = []
        for pin in comp.pins:
            pin_pos = comp.get_pin_position(pin.number)
            entry: dict[str, Any] = {
                "number": pin.number,
                "name": pin.name,
            }
            if pin_pos:
                entry["x"] = round(pin_pos.x, 3)
                entry["y"] = round(pin_pos.y, 3)
            pins_data.append(entry)

        return {"status": "ok", "reference": reference, "count": len(pins_data), "pins": pins_data}

    @mcp.tool()
    def add_label_to_pin(
        reference: str,
        pin_number: str,
        text: str,
        offset: float = 0.0,
    ) -> dict:
        """Add label directly to component pin.

        Places a net label at the pin's absolute position.

        Args:
            reference: Component reference (e.g., R1).
            pin_number: Pin number (e.g., 1, 2).
            text: Label text.
            offset: Offset distance from pin (default: 0).
        """
        sch = _require_schematic()
        comp = sch.components.get(reference)
        if comp is None:
            return {"error": f"Component {reference} not found"}

        pin_pos = comp.get_pin_position(pin_number)
        if pin_pos is None:
            return {"error": f"Pin {pin_number} not found on {reference}"}

        label_uuid = sch.add_label(text, (pin_pos.x + offset, pin_pos.y))
        return {
            "status": "ok",
            "label_uuid": label_uuid,
            "text": text,
            "reference": reference,
            "pin_number": pin_number,
            "position": [round(pin_pos.x + offset, 3), round(pin_pos.y, 3)],
        }

    @mcp.tool()
    def connect_pins_with_labels(
        comp1_ref: str,
        pin1: str,
        comp2_ref: str,
        pin2: str,
        net_name: str,
    ) -> dict:
        """Connect two component pins using same label.

        Places matching net labels on both pins so KiCad treats them as
        connected.

        Args:
            comp1_ref: First component reference.
            pin1: First component pin number.
            comp2_ref: Second component reference.
            pin2: Second component pin number.
            net_name: Net name for connection.
        """
        sch = _require_schematic()
        label_uuids = []
        for ref, pin in [(comp1_ref, pin1), (comp2_ref, pin2)]:
            comp = sch.components.get(ref)
            if comp is None:
                return {"error": f"Component {ref} not found"}
            pin_pos = comp.get_pin_position(pin)
            if pin_pos is None:
                return {"error": f"Pin {pin} not found on {ref}"}
            uuid = sch.add_label(net_name, (pin_pos.x, pin_pos.y))
            label_uuids.append(uuid)

        return {
            "status": "ok",
            "net_name": net_name,
            "labels_created": len(label_uuids),
            "label_uuids": label_uuids,
        }

    # ------------------------------------------------------------------
    # Wire management
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_wire(start_pos: list[float], end_pos: list[float]) -> dict:
        """Add a wire connection between two points.

        Args:
            start_pos: [x, y] start coordinates.
            end_pos: [x, y] end coordinates.
        """
        sch = _require_schematic()
        if len(start_pos) != 2 or len(end_pos) != 2:
            return {"error": "Positions must be [x, y] coordinates"}

        wire_uuid = sch.add_wire(start=tuple(start_pos), end=tuple(end_pos))
        return {
            "status": "ok",
            "wire_uuid": wire_uuid,
            "start": start_pos,
            "end": end_pos,
        }

    @mcp.tool()
    def remove_wire(wire_uuid: str) -> dict:
        """Remove wire from schematic.

        Args:
            wire_uuid: Wire UUID to remove.
        """
        sch = _require_schematic()
        removed = sch.remove_wire(wire_uuid)
        if removed:
            return {"status": "ok", "wire_uuid": wire_uuid}
        return {"error": f"Wire {wire_uuid} not found"}

    # ------------------------------------------------------------------
    # Label management
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_label(
        text: str,
        position: list[float],
        rotation: float = 0.0,
        size: float = 1.27,
    ) -> dict:
        """Add a text label to the schematic.

        Args:
            text: Label text.
            position: [x, y] coordinates.
            rotation: Text rotation in degrees.
            size: Font size.
        """
        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "Position must be [x, y] coordinates"}

        label_uuid = sch.add_label(
            text=text, position=tuple(position), rotation=rotation, size=size
        )
        return {"status": "ok", "label_uuid": label_uuid, "text": text, "position": position}

    @mcp.tool()
    def add_hierarchical_label(
        text: str,
        position: list[float],
        shape: str = "input",
        rotation: float = 0.0,
        size: float = 1.27,
    ) -> dict:
        """Add a hierarchical label to the schematic.

        Args:
            text: Label text.
            position: [x, y] coordinates.
            shape: Label shape (input, output, bidirectional, tristate, passive, unspecified).
            rotation: Text rotation in degrees.
            size: Font size.
        """
        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "Position must be [x, y] coordinates"}

        from kicad_sch_api.core.types import HierarchicalLabelShape

        shape_map = {
            "input": HierarchicalLabelShape.INPUT,
            "output": HierarchicalLabelShape.OUTPUT,
            "bidirectional": HierarchicalLabelShape.BIDIRECTIONAL,
            "tristate": HierarchicalLabelShape.TRISTATE,
            "passive": HierarchicalLabelShape.PASSIVE,
            "unspecified": HierarchicalLabelShape.UNSPECIFIED,
        }
        shape_enum = shape_map.get(shape.lower(), HierarchicalLabelShape.INPUT)

        label_uuid = sch.add_hierarchical_label(
            text=text, position=tuple(position), shape=shape_enum, rotation=rotation, size=size
        )
        return {
            "status": "ok",
            "label_uuid": label_uuid,
            "text": text,
            "shape": shape,
            "position": position,
        }

    @mcp.tool()
    def remove_label(label_uuid: str) -> dict:
        """Remove label from schematic.

        Args:
            label_uuid: Label UUID to remove.
        """
        sch = _require_schematic()
        removed = sch.remove_label(label_uuid)
        if removed:
            return {"status": "ok", "label_uuid": label_uuid}
        return {"error": f"Label {label_uuid} not found"}

    # ------------------------------------------------------------------
    # Junction management
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_junction(position: list[float], diameter: float = 0.0) -> dict:
        """Add a junction (connection point) to the schematic.

        Args:
            position: [x, y] coordinates.
            diameter: Junction diameter (optional).
        """
        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "Position must be [x, y] coordinates"}

        junction_uuid = sch.junctions.add(position=tuple(position), diameter=diameter)
        return {"status": "ok", "junction_uuid": junction_uuid, "position": position}

    # ------------------------------------------------------------------
    # Text elements
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_text(
        text: str,
        position: list[float],
        rotation: float = 0.0,
        size: float = 1.27,
    ) -> dict:
        """Add text element to schematic.

        Args:
            text: Text content.
            position: [x, y] coordinates.
            rotation: Text rotation in degrees.
            size: Font size.
        """
        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "Position must be [x, y] coordinates"}

        text_uuid = sch.add_text(text, tuple(position), rotation, size)
        return {"status": "ok", "text_uuid": text_uuid, "text": text, "position": position}

    @mcp.tool()
    def add_text_box(
        text: str,
        position: list[float],
        size: list[float],
        rotation: float = 0.0,
        font_size: float = 1.27,
    ) -> dict:
        """Add text box element to schematic.

        Args:
            text: Text content.
            position: [x, y] top-left coordinates.
            size: [width, height] dimensions.
            rotation: Text rotation in degrees.
            font_size: Font size.
        """
        sch = _require_schematic()
        if len(position) != 2 or len(size) != 2:
            return {"error": "Position and size must be [x, y] and [width, height]"}

        textbox_uuid = sch.add_text_box(text, tuple(position), tuple(size), rotation, font_size)
        return {
            "status": "ok",
            "textbox_uuid": textbox_uuid,
            "text": text,
            "position": position,
            "size": size,
        }

    # ------------------------------------------------------------------
    # Hierarchical sheets
    # ------------------------------------------------------------------

    @mcp.tool()
    def add_sheet(
        name: str,
        filename: str,
        position: list[float],
        size: list[float],
    ) -> dict:
        """Add hierarchical sheet to schematic.

        Args:
            name: Sheet name.
            filename: Sheet filename (.kicad_sch).
            position: [x, y] coordinates.
            size: [width, height] dimensions.
        """
        sch = _require_schematic()
        if len(position) != 2 or len(size) != 2:
            return {"error": "Position and size must be [x, y] and [width, height]"}

        sheet_uuid = sch.add_sheet(name, filename, tuple(position), tuple(size))
        return {
            "status": "ok",
            "sheet_uuid": sheet_uuid,
            "name": name,
            "filename": filename,
            "position": position,
            "size": size,
        }

    @mcp.tool()
    def add_sheet_pin(
        sheet_uuid: str,
        name: str,
        pin_type: str,
        position: list[float],
    ) -> dict:
        """Add pin to hierarchical sheet.

        Args:
            sheet_uuid: UUID of sheet to add pin to.
            name: Pin name.
            pin_type: Pin type (input, output, bidirectional).
            position: [x, y] coordinates relative to sheet.
        """
        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "Position must be [x, y] coordinates"}

        pin_uuid = sch.add_sheet_pin(sheet_uuid, name, pin_type, tuple(position))
        return {
            "status": "ok",
            "pin_uuid": pin_uuid,
            "name": name,
            "pin_type": pin_type,
            "sheet_uuid": sheet_uuid,
        }
