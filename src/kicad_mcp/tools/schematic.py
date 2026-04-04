"""Schematic tools — wrapping kicad-sch-api for schematic manipulation.

All tools operate on a single in-memory schematic at a time.  ``load_schematic``
or ``create_schematic`` must be called first; subsequent tools operate on that
loaded instance until a different schematic is loaded.

The underlying library is kicad-sch-api (PyPI), which provides lossless
round-trip parsing of .kicad_sch files.
"""
# TODO: Migrate !r script interpolation to JSON params (see pcb_board.py for pattern)

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
    def add_multi_unit_component(
        lib_id: str,
        reference: str,
        value: str,
        position: list[float],
        units: list[int] | None = None,
        footprint: str | None = None,
        unit_spacing: float = 15.0,
    ) -> dict:
        """Add a multi-unit component, placing each unit as a separate symbol block.

        For ICs like LM393 (dual comparator) or 7400 (quad NAND), each unit
        gets its own symbol block in the schematic sharing the same reference.
        Power units are included automatically.

        Args:
            lib_id: Library ID (e.g., Comparator:LM393).
            reference: Component reference (e.g., U2). Shared by all units.
            value: Component value (e.g., LM393).
            position: [x, y] for unit 1. Subsequent units placed below.
            units: Which units to place (default: all with pins). E.g., [1, 3] for LM393.
            footprint: Component footprint (e.g., Package_SO:SOIC-8_3.9x4.9mm_P1.27mm).
            unit_spacing: Vertical spacing between units in mm (default 15).
        """
        from kicad_sch_api.library.cache import get_symbol_cache

        sch = _require_schematic()
        if len(position) != 2:
            return {"error": "position must be [x, y]"}

        # Look up symbol definition to build unit→pins mapping for the response
        cache = get_symbol_cache()
        symbol_def = cache.get_symbol(lib_id)
        if symbol_def is None:
            return {"error": f"Symbol '{lib_id}' not found in KiCad libraries"}

        unit_pins = _parse_unit_pin_mapping(symbol_def)
        if not unit_pins:
            # Single-unit symbol — fall through to normal add
            return add_component(lib_id, reference, value, position, footprint)

        # Determine which units to place
        if units is not None:
            units_to_place = sorted(units)
        else:
            # Place all units that have pins
            units_to_place = sorted(unit_pins.keys())

        # Validate requested units
        invalid = [u for u in units_to_place if u not in unit_pins]
        if invalid:
            return {
                "error": f"Units {invalid} not found in symbol. Available: {sorted(unit_pins.keys())}",
            }

        if len(units_to_place) == len(unit_pins) and units is None:
            # Place ALL units — use kicad-sch-api's built-in add_all_units
            result = sch.components.add(
                lib_id=lib_id,
                reference=reference,
                value=value,
                position=tuple(position),
                footprint=footprint,
                add_all_units=True,
                unit_spacing=unit_spacing,
            )
            # Result is a MultiUnitComponentGroup
            placed_units = []
            for comp in result:
                unit = getattr(comp, "unit", None)
                if unit is None:
                    unit = getattr(getattr(comp, "_data", None), "unit", 1)
                placed_units.append({
                    "unit": unit,
                    "pins": unit_pins.get(unit, []),
                    "position": [round(comp.position.x, 3), round(comp.position.y, 3)],
                })
        else:
            # Place selected units individually
            placed_units = []
            for i, unit_num in enumerate(units_to_place):
                unit_x = position[0]
                unit_y = position[1] + i * unit_spacing
                comp = sch.components.add(
                    lib_id=lib_id,
                    reference=reference,
                    value=value,
                    position=(unit_x, unit_y),
                    footprint=footprint,
                    unit=unit_num,
                )
                placed_units.append({
                    "unit": unit_num,
                    "pins": unit_pins[unit_num],
                    "position": [round(comp.position.x, 3), round(comp.position.y, 3)],
                })

        logger.info(
            "Added multi-unit component %s (%s) with %d units: %s",
            reference, lib_id, len(placed_units),
            [u["unit"] for u in placed_units],
        )
        return {
            "status": "ok",
            "reference": reference,
            "lib_id": lib_id,
            "value": value,
            "total_units": len(placed_units),
            "units": placed_units,
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
            from kicad_mcp.utils.library_index import get_library_index

            index = get_library_index()

            if index.symbols_stale():
                index.rebuild_symbols()

            results = index.search_symbols(query, library=library, limit=limit)
            return {"status": "ok", "count": len(results), "results": results}
        except Exception as e:
            logger.error("Component search failed: %s", e)
            return {"error": f"Component search failed: {e}"}

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
        comp = _find_component_for_pin(sch, reference, pin_number)
        if comp is None:
            return {"error": f"Component {reference} not found"}

        pin_pos = _kicad_pin_position(comp, pin_number)
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

        For multi-unit components, lists pins across all placed units with
        correct positions relative to each unit's placement.

        Args:
            reference: Component reference (e.g., R1).
        """
        sch = _require_schematic()

        # Collect all component instances with this reference
        all_comps = [c for c in sch.components if c.reference == reference]
        if not all_comps:
            return {"error": f"Component {reference} not found"}

        pins_data = []
        seen_pins: set[str] = set()

        if len(all_comps) == 1:
            # Single-unit: list all pins from the one component
            comp = all_comps[0]
            for pin in comp.pins:
                pin_pos = _kicad_pin_position(comp, pin.number)
                entry: dict[str, Any] = {
                    "number": pin.number,
                    "name": pin.name,
                }
                if pin_pos:
                    entry["x"] = round(pin_pos.x, 3)
                    entry["y"] = round(pin_pos.y, 3)
                pins_data.append(entry)
        else:
            # Multi-unit: find correct unit for each pin
            from kicad_sch_api.library.cache import get_symbol_cache

            cache = get_symbol_cache()
            symbol_def = cache.get_symbol(all_comps[0].lib_id)
            unit_pins = _parse_unit_pin_mapping(symbol_def) if symbol_def else {}

            for comp in all_comps:
                unit = getattr(comp, "unit", None)
                if unit is None:
                    unit = getattr(getattr(comp, "_data", None), "unit", 1) or 1
                pins_in_unit = set(unit_pins.get(unit, []))
                for pin in comp.pins:
                    if pin.number in seen_pins:
                        continue
                    # Only compute position for pins belonging to this unit
                    if pins_in_unit and pin.number not in pins_in_unit:
                        continue
                    seen_pins.add(pin.number)
                    pin_pos = _kicad_pin_position(comp, pin.number)
                    entry = {
                        "number": pin.number,
                        "name": pin.name,
                        "unit": unit,
                    }
                    if pin_pos:
                        entry["x"] = round(pin_pos.x, 3)
                        entry["y"] = round(pin_pos.y, 3)
                    pins_data.append(entry)

        return {"status": "ok", "reference": reference, "count": len(pins_data), "pins": pins_data}

    # ------------------------------------------------------------------
    # Helpers: correct pin position & wire stub direction
    # ------------------------------------------------------------------
    #
    # KiCad symbol libraries use a Y-up coordinate system (math
    # convention) while the schematic editor uses Y-down (screen
    # convention).  When a symbol is placed in the schematic, KiCad
    # negates the Y coordinate of every point before applying the
    # component rotation and adding the component position.
    #
    # kicad-sch-api's ``get_pin_position()`` does NOT negate Y, which
    # causes wire endpoints to land on the wrong pins.  The helpers
    # below apply the correct transformation.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Multi-unit symbol helpers
    # ------------------------------------------------------------------

    def _parse_unit_pin_mapping(symbol_def):
        """Parse raw KiCad symbol data to build {unit_num: [pin_numbers]} mapping.

        Multi-unit symbols (LM393, 7400, etc.) have sub-symbols named like
        ``LM393_1_1`` (unit 1, style 1), ``LM393_3_1`` (unit 3, style 1).
        Style ``_2`` variants are DeMorgan representations and are skipped.

        The raw data uses ``sexpdata.Symbol`` objects where the tag name is
        accessed via ``.value()`` (a method call, not a property).
        """
        import sexpdata as _sexpdata

        def _tag(elem) -> str:
            """Extract tag string from a sexpdata.Symbol or plain string."""
            if isinstance(elem, _sexpdata.Symbol):
                return elem.value()
            return str(elem).strip('"')

        unit_pins: dict[int, list[str]] = {}
        raw = symbol_def.raw_kicad_data
        if not isinstance(raw, list):
            return unit_pins
        for item in raw:
            if not isinstance(item, list) or len(item) < 2:
                continue
            if _tag(item[0]) != "symbol":
                continue
            name = item[1] if isinstance(item[1], str) else str(item[1]).strip('"')
            parts = name.split("_")
            if len(parts) < 3:
                continue
            try:
                unit_num = int(parts[-2])
                style = int(parts[-1])
                if style != 1:  # Skip DeMorgan variants
                    continue
            except ValueError:
                continue
            pins: list[str] = []
            for sub in item:
                if not isinstance(sub, list) or len(sub) < 2:
                    continue
                if _tag(sub[0]) != "pin":
                    continue
                for s in sub:
                    if isinstance(s, list) and len(s) >= 2:
                        if _tag(s[0]) == "number":
                            pn = s[1] if isinstance(s[1], str) else str(s[1]).strip('"')
                            pins.append(pn)
            if pins:
                unit_pins[unit_num] = pins
        return unit_pins

    def _find_component_for_pin(sch, reference: str, pin_number: str):
        """Find the component instance (unit) that owns the given pin.

        For single-unit symbols, this is equivalent to ``sch.components.get(reference)``.
        For multi-unit symbols (multiple components sharing the same reference),
        it finds the correct unit by looking up which sub-symbol owns the pin.
        """
        from kicad_sch_api.library.cache import get_symbol_cache

        # Collect all component instances with this reference
        all_comps = [c for c in sch.components if c.reference == reference]
        if len(all_comps) <= 1:
            return sch.components.get(reference)

        # Build unit→pins mapping from symbol library
        cache = get_symbol_cache()
        symbol_def = cache.get_symbol(all_comps[0].lib_id)
        if symbol_def is None:
            return all_comps[0]
        unit_pins = _parse_unit_pin_mapping(symbol_def)

        # Find which unit owns this pin
        for comp in all_comps:
            # Component wrapper doesn't expose .unit — access via ._data
            unit = getattr(comp, "unit", None)
            if unit is None:
                unit = getattr(getattr(comp, "_data", None), "unit", 1) or 1
            if pin_number in unit_pins.get(unit, []):
                return comp

        # Fallback: return first component (it has all pins in its pin list)
        return all_comps[0]

    def _kicad_pin_position(comp, pin_number: str):
        """Return the absolute pin-tip position as KiCad computes it.

        KiCad symbol libraries use Y-up while the schematic uses Y-down.
        KiCad's internal transform is: rotate first, then negate Y,
        then add component position.

        Returns:
            ``Point(x, y)`` in schematic coordinates, or *None*.
        """
        import math
        from kicad_sch_api.core.types import Point

        pin = comp.get_pin(pin_number)
        if pin is None:
            return None

        # Step 1: Rotate pin position by component angle (still in Y-up space)
        angle_rad = math.radians(comp.rotation)
        cos_a = math.cos(angle_rad)
        sin_a = math.sin(angle_rad)

        rx = pin.position.x * cos_a - pin.position.y * sin_a
        ry = pin.position.x * sin_a + pin.position.y * cos_a

        # Step 2: Negate Y to convert to schematic Y-down, then translate
        return Point(
            round(comp.position.x + rx, 3),
            round(comp.position.y - ry, 3),   # negate AFTER rotation
        )

    def _pin_wire_offset(comp, pin_number: str, distance: float = 2.54):
        """Compute (dx, dy) for a wire stub extending *away* from the symbol body.

        In the symbol's Y-up coordinate system, the pin angle points
        FROM tip TOWARD body.  After rotating by comp.rotation and
        negating Y, the "away from body" direction in schematic
        coordinates (Y-down) is::

            away_deg = (-pin.rotation - comp.rotation + 180) % 360

        Returns:
            (dx, dy) tuple in schematic coordinates.
        """
        import math

        pin = comp.get_pin(pin_number)
        if pin is None:
            return (distance, 0.0)  # Fallback: rightward

        away_deg = (-pin.rotation - comp.rotation + 180) % 360
        away_rad = math.radians(away_deg)
        dx = distance * math.cos(away_rad)
        dy = distance * math.sin(away_rad)

        # Snap near-zero values to 0 to keep coordinates on the 1.27mm grid
        if abs(dx) < 0.01:
            dx = 0.0
        if abs(dy) < 0.01:
            dy = 0.0

        return (round(dx, 3), round(dy, 3))

    @mcp.tool()
    def add_label_to_pin(
        reference: str,
        pin_number: str,
        text: str,
        offset: float = 0.0,
    ) -> dict:
        """Add label directly to component pin.

        Places a net label at the pin's absolute position and creates a
        wire stub connecting the label to the pin. The wire ensures
        KiCad's netlist exporter recognizes the connection.

        Args:
            reference: Component reference (e.g., R1).
            pin_number: Pin number (e.g., 1, 2).
            text: Label text.
            offset: Offset distance from pin (default: 0).
        """
        sch = _require_schematic()
        comp = _find_component_for_pin(sch, reference, pin_number)
        if comp is None:
            return {"error": f"Component {reference} not found"}

        pin_pos = _kicad_pin_position(comp, pin_number)
        if pin_pos is None:
            return {"error": f"Pin {pin_number} not found on {reference}"}

        effective_offset = offset if offset != 0 else 2.54
        dx, dy = _pin_wire_offset(comp, pin_number, effective_offset)
        label_x = pin_pos.x + dx
        label_y = pin_pos.y + dy

        # Wire stub from pin to label — required for netlist connectivity
        sch.add_wire(start=(pin_pos.x, pin_pos.y), end=(label_x, label_y))
        label_uuid = sch.add_label(text, (label_x, label_y))
        return {
            "status": "ok",
            "label_uuid": label_uuid,
            "text": text,
            "reference": reference,
            "pin_number": pin_number,
            "position": [round(label_x, 3), round(label_y, 3)],
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
        connected. Each label is connected to its pin via a wire stub
        to ensure KiCad's netlist exporter recognizes the connection.

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
            comp = _find_component_for_pin(sch, ref, pin)
            if comp is None:
                return {"error": f"Component {ref} not found"}
            pin_pos = _kicad_pin_position(comp, pin)
            if pin_pos is None:
                return {"error": f"Pin {pin} not found on {ref}"}
            dx, dy = _pin_wire_offset(comp, pin, 2.54)
            label_x = pin_pos.x + dx
            label_y = pin_pos.y + dy
            # Wire stub from pin to label — required for netlist connectivity
            sch.add_wire(start=(pin_pos.x, pin_pos.y), end=(label_x, label_y))
            uuid = sch.add_label(net_name, (label_x, label_y))
            label_uuids.append(uuid)

        return {
            "status": "ok",
            "net_name": net_name,
            "labels_created": len(label_uuids),
            "label_uuids": label_uuids,
        }

    # ------------------------------------------------------------------
    # Pin collision detection
    # ------------------------------------------------------------------

    @mcp.tool()
    def check_pin_collisions() -> dict:
        """Detect schematic pin position collisions that cause silent net merges.

        Scans all placed component pins and flags any two pins from different
        components that occupy the same schematic coordinates.  When two pins
        collide, KiCad silently merges their nets — this is the root cause of
        GND/+3V3 net loss bugs.

        Run this after placing all components and before saving/exporting the
        schematic.  Any collisions should be fixed by moving one component.

        Returns:
            A dict with ``collisions`` (list of pin pairs at the same position)
            and ``collision_count``.
        """
        sch = _require_schematic()

        # Build a map: (rounded_x, rounded_y) -> list of (reference, pin_number, pin_name)
        position_map: dict[tuple[float, float], list[dict[str, Any]]] = {}

        for comp in sch.components:
            ref = comp.reference
            for pin in comp.pins:
                pin_pos = _kicad_pin_position(comp, pin.number)
                if pin_pos is None:
                    continue
                key = (round(pin_pos.x, 2), round(pin_pos.y, 2))
                position_map.setdefault(key, []).append({
                    "reference": ref,
                    "pin_number": pin.number,
                    "pin_name": pin.name,
                })

        collisions = []
        for pos, pins in position_map.items():
            if len(pins) < 2:
                continue
            # Only report if pins are from DIFFERENT components
            refs = {p["reference"] for p in pins}
            if len(refs) < 2:
                continue
            collisions.append({
                "position": [pos[0], pos[1]],
                "pins": pins,
                "component_count": len(refs),
                "message": (
                    f"{len(pins)} pins from {len(refs)} components collide at "
                    f"({pos[0]}, {pos[1]}): "
                    + ", ".join(f"{p['reference']}:{p['pin_number']}" for p in pins)
                ),
            })

        if collisions:
            summary = (
                f"{len(collisions)} pin collision(s) found — these will cause "
                f"silent net merges. Move components to separate the pins."
            )
        else:
            total_pins = sum(len(v) for v in position_map.values())
            summary = f"No pin collisions among {total_pins} pins"

        return {
            "status": "ok",
            "collision_count": len(collisions),
            "collisions": collisions,
            "summary": summary,
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

    @mcp.tool()
    def edit_label(
        label_uuid: str,
        new_text: str | None = None,
        position: list[float] | None = None,
        rotation: float | None = None,
        size: float | None = None,
    ) -> dict:
        """Edit an existing label's text, position, rotation, or size in place.

        Avoids the correctness risk of delete-and-re-add by mutating the
        label object directly.  Use the label_uuid returned by add_label or
        list the schematic info to find existing UUIDs.

        Args:
            label_uuid: UUID of the label to edit.
            new_text: Replacement net label text. None to keep current.
            position: New [x, y] position. None to keep current.
            rotation: New rotation in degrees. None to keep current.
            size: New font size. None to keep current.
        """
        sch = _require_schematic()

        if all(v is None for v in (new_text, position, rotation, size)):
            return {"error": "No modifications specified"}

        # Find the label by UUID across both regular and hierarchical collections
        label = None
        for lbl in sch.labels:
            if lbl.uuid == label_uuid:
                label = lbl
                break
        if label is None:
            for lbl in sch.hierarchical_labels:
                if lbl.uuid == label_uuid:
                    label = lbl
                    break
        if label is None:
            return {"error": f"Label with UUID {label_uuid!r} not found"}

        if new_text is not None:
            label.text = new_text
        if position is not None:
            if len(position) != 2:
                return {"error": "position must be [x, y]"}
            label.position = tuple(position)
        if rotation is not None:
            label.rotation = rotation
        if size is not None:
            label.size = size

        return {
            "status": "ok",
            "label_uuid": label_uuid,
            "text": label.text,
            "position": [label.position.x, label.position.y],
            "rotation": label.rotation,
            "size": label.size,
        }

    @mcp.tool()
    def move_component(
        reference: str,
        position: list[float],
    ) -> dict:
        """Move a schematic component to a new position.

        Repositions the component without removing or re-adding it, so all
        existing wire connections by net name are preserved.

        Args:
            reference: Component reference (e.g., "R1", "U3").
            position: New [x, y] coordinates in schematic units.
        """
        sch = _require_schematic()

        if len(position) != 2:
            return {"error": "position must be [x, y]"}

        matches = list(sch.components.filter(reference=reference))
        if not matches:
            return {"error": f"Component {reference!r} not found"}

        comp = matches[0]
        old_pos = [comp.position.x, comp.position.y] if comp.position else None
        comp.position = tuple(position)

        return {
            "status": "ok",
            "reference": reference,
            "old_position": old_pos,
            "new_position": position,
        }

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
