"""Schematic domain router — all schematic editing operations.

See docs/SPEC_Tool_Consolidation.md §1.
"""

import logging
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP, Context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Op imports — schematic.py exposes module-level state and helpers;
# netlist.py exposes _op_find_component_connections.
# ---------------------------------------------------------------------------

import kicad_mcp.tools.schematic_impl as _sch_mod
from kicad_mcp.tools.schematic_impl import (
    _require_schematic,
    _snap,
    _kicad_pin_position,
    _pin_wire_offset,
    _find_component_for_pin,
    _parse_unit_pin_mapping,
)


async def _op_find_component_connections(
    project_path: str,
    component_ref: str,
    ctx: "Context | None",
) -> Dict[str, Any]:
    """Delegate to netlist.py's async implementation."""
    from kicad_mcp.tools.netlist import _op_find_component_connections as _impl
    return await _impl(project_path, component_ref, ctx)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_schematic_router(mcp: FastMCP) -> None:
    """Register the schematic domain router."""

    # Lazy import — allows server startup without kicad-sch-api installed.
    try:
        import kicad_sch_api as ksa  # noqa: F401
    except ImportError:
        logger.warning(
            "kicad-sch-api not installed — schematic router will not be available. "
            "Install with: pip install kicad-sch-api"
        )
        return

    @mcp.tool()
    async def schematic(
        operation: str,
        *,
        # ── File lifecycle ────────────────────────────────────────────────
        name: Optional[str] = None,
        schematic_path: Optional[str] = None,
        new_path: Optional[str] = None,
        suffix: str = ".backup",
        # ── Components ────────────────────────────────────────────────────
        lib_id: Optional[str] = None,
        reference: Optional[str] = None,
        value: Optional[str] = None,
        position: Optional[List[float]] = None,
        footprint: Optional[str] = None,
        properties: Optional[str] = None,
        units: Optional[List[int]] = None,
        unit_spacing: float = 15.0,
        criteria: Optional[Dict[str, Any]] = None,
        updates: Optional[Dict[str, Any]] = None,
        x1: Optional[float] = None,
        y1: Optional[float] = None,
        x2: Optional[float] = None,
        y2: Optional[float] = None,
        # ── Component pins ────────────────────────────────────────────────
        pin_number: Optional[str] = None,
        project_path: Optional[str] = None,
        component_ref: Optional[str] = None,
        # ── Wires ─────────────────────────────────────────────────────────
        start_pos: Optional[List[float]] = None,
        end_pos: Optional[List[float]] = None,
        wire_uuid: Optional[str] = None,
        comp1_ref: Optional[str] = None,
        pin1: Optional[str] = None,
        comp2_ref: Optional[str] = None,
        pin2: Optional[str] = None,
        # ── Junctions ─────────────────────────────────────────────────────
        diameter: float = 0.0,
        # ── Labels ────────────────────────────────────────────────────────
        text: Optional[str] = None,
        rotation: float = 0.0,
        size: float = 1.27,
        label_uuid: Optional[str] = None,
        new_text: Optional[str] = None,
        offset: float = 0.0,
        shape: str = "input",
        net_name: Optional[str] = None,
        # ── Sheets ────────────────────────────────────────────────────────
        filename: Optional[str] = None,
        sheet_size: Optional[List[float]] = None,
        sheet_uuid: Optional[str] = None,
        pin_type: Optional[str] = None,
        edge: Optional[str] = None,
        position_along_edge: float = 0.0,
        # ── find_component_connections async ctx ──────────────────────────
        ctx: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Schematic domain router — file lifecycle, components, wires, labels, sheets.

        Operations:

        # File lifecycle
          create(name) -> {status, name}
              Create a new KiCad schematic.
          load(schematic_path) -> {status, file_path, components}
              Load an existing .kicad_sch file.
          save(schematic_path?) -> {status, file_path}
              Save current schematic; omit schematic_path to save in place.
          validate() -> {status, issues, errors, warnings, details}
              Validate schematic for ERC issues.
          info() -> {status, components, wires, junctions, modified, ...}
              Summary of current schematic state. (was: get_schematic_info)
          clone(new_name?) -> {status, name, components}
              Clone the current schematic (does NOT load the clone).
          backup(suffix=".backup") -> {status, backup_path}
              Create a timestamped backup of the current schematic file.
          check_pin_collisions() -> {status, collision_count, collisions, summary}
              Detect pin-position collisions that cause silent net merges.

        # Components — CRUD
          add_component(lib_id, reference, value, position, footprint?, properties?)
              -> {status, reference, lib_id, value, position}
          add_multi_unit_component(lib_id, reference, value, position,
                                   units?, footprint?, unit_spacing=15)
              -> {status, reference, total_units, units}   for multi-unit symbols
          remove_component(reference) -> {status, reference}
          move_component(reference, position) -> {status, reference, old_position, new_position}
          list_components() -> {status, count, components}
          filter_components(lib_id?, value?, reference?, footprint?) -> {status, count, components}
          components_in_area(x1, y1, x2, y2) -> {status, count, components}
          bulk_update_components(criteria, updates) -> {status, updated}

        # Component pins (read-only queries)
          get_component_pin_position(reference, pin_number) -> {status, reference, pin_number, x, y}
          list_component_pins(reference) -> {status, reference, count, pins}
          find_component_connections(project_path, component_ref) -> {success, connections, ...}
              Netlist-based query; reads .kicad_pro + extracts schematic netlist.

        # Wires (verb-overlap pair — pick by what you have)
          add_wire(start_pos, end_pos) -> {status, wire_uuid, start, end}
              Add wire by absolute [x, y] coordinates.
          add_wire_between_pins(comp1_ref, pin1, comp2_ref, pin2)
              -> {status, pins, segments, segment_count}
              Add wire by component+pin refs — preferred when you have refs.
          remove_wire(wire_uuid) -> {status, wire_uuid}
          add_junction(position, diameter=0) -> {status, junction_uuid, position}

        # Labels (verb-overlap family — pick by intent)
          add_label(text, position, rotation=0, size=1.27)
              -> {status, label_uuid, text, position}
              Net label placed at absolute coordinates.
          add_label_to_pin(reference, pin_number, text, offset=0)
              -> {status, label_uuid, text, reference, pin_number, position}
              Net label attached directly to a pin — preferred when labeling a pin.
          add_hierarchical_label(text, position, shape="input", rotation=0, size=1.27)
              -> {status, label_uuid, text, shape, position}
              For hierarchical sheet pins, not ordinary net labels.
          connect_pins_with_labels(comp1_ref, pin1, comp2_ref, pin2, net_name)
              -> {status, net_name, labels_created, label_uuids}
              High-level: places matching labels on two pins to connect them via a net.
          remove_label(label_uuid) -> {status, label_uuid}
          edit_label(label_uuid, new_text?, position?, rotation?, size?)
              -> {status, label_uuid, text, position, rotation, size}

        # Text annotations
          add_text(text, position, rotation=0, size=1.27)
              -> {status, text_uuid, text, position}
          add_text_box(text, position, sheet_size, rotation=0, font_size=1.27)
              -> {status, textbox_uuid, text, position, size}

        # Hierarchical sheets
          add_sheet(name, filename, position, sheet_size) -> {status, sheet_uuid, name, filename, position, size}
          add_sheet_pin(sheet_uuid, name, pin_type, edge, position_along_edge)
              -> {status, pin_uuid, name, pin_type, edge, position_along_edge, sheet_uuid}
              edge: right|bottom|left|top; position_along_edge: mm from reference corner
        """
        import kicad_sch_api as ksa

        # ── File lifecycle ─────────────────────────────────────────────────

        if operation == "create":
            _sch_mod._current_schematic = ksa.create_schematic(name or "untitled")
            logger.info("Created new schematic: %s", name or "untitled")
            return {"status": "ok", "name": name or "untitled"}

        if operation == "load":
            if schematic_path is None:
                return {"error": "operation='load' requires 'schematic_path'"}
            _sch_mod._current_schematic = ksa.load_schematic(schematic_path)
            comp_count = len(list(_sch_mod._current_schematic.components))
            logger.info("Loaded schematic: %s (%d components)", schematic_path, comp_count)
            return {"status": "ok", "file_path": schematic_path, "components": comp_count}

        if operation == "save":
            sch = _require_schematic()
            if schematic_path:
                sch.save(schematic_path)
            else:
                sch.save()
            dest = schematic_path or str(getattr(sch, "file_path", "current file"))
            logger.info("Saved schematic to: %s", dest)
            return {"status": "ok", "file_path": dest}

        if operation == "validate":
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
                "details": details[:20],
                "details_truncated": len(details) > 20,
            }

        if operation == "info":
            sch = _require_schematic()
            try:
                summary = sch.get_summary()
            except Exception as e:
                logger.warning("sch.get_summary() failed: %s", e)
                summary = {}
            info: Dict[str, Any] = {
                "status": "ok",
                "components": len(list(sch.components)),
                "wires": len(sch.wires) if hasattr(sch, "wires") else 0,
                "junctions": len(sch.junctions) if hasattr(sch, "junctions") else 0,
                "modified": getattr(sch, "modified", False),
            }
            for k, v in summary.items():
                if k not in info:
                    info[k] = v
            return info

        if operation == "clone":
            sch = _require_schematic()
            cloned = sch.clone(name)
            comp_count = len(list(cloned.components))
            return {"status": "ok", "name": name or "Clone", "components": comp_count}

        if operation == "backup":
            sch = _require_schematic()
            backup_path = sch.backup(suffix)
            return {"status": "ok", "backup_path": str(backup_path)}

        if operation == "check_pin_collisions":
            sch = _require_schematic()
            position_map: Dict[tuple, list] = {}
            for comp in sch.components:
                ref = comp.reference
                for pin in comp.pins:
                    pin_pos = _kicad_pin_position(comp, pin.number)
                    if pin_pos is None:
                        continue
                    pos_key = (round(pin_pos.x, 2), round(pin_pos.y, 2))
                    position_map.setdefault(pos_key, []).append({
                        "reference": ref,
                        "pin_number": pin.number,
                        "pin_name": pin.name,
                    })
            collisions = []
            for collision_pos, pins in position_map.items():
                if len(pins) < 2:
                    continue
                refs = {p["reference"] for p in pins}
                if len(refs) < 2:
                    continue
                collisions.append({
                    "position": [collision_pos[0], collision_pos[1]],
                    "pins": pins,
                    "component_count": len(refs),
                    "message": (
                        f"{len(pins)} pins from {len(refs)} components collide at "
                        f"({collision_pos[0]}, {collision_pos[1]}): "
                        + ", ".join(f"{p['reference']}:{p['pin_number']}" for p in pins)
                    ),
                })
            if collisions:
                summary_msg = (
                    f"{len(collisions)} pin collision(s) found — these will cause "
                    f"silent net merges. Move components to separate the pins."
                )
            else:
                total_pins = sum(len(v) for v in position_map.values())
                summary_msg = f"No pin collisions among {total_pins} pins"
            return {
                "status": "ok",
                "collision_count": len(collisions),
                "collisions": collisions,
                "summary": summary_msg,
            }

        # ── Components — CRUD ──────────────────────────────────────────────

        if operation == "add_component":
            if lib_id is None:
                return {"error": "operation='add_component' requires 'lib_id'"}
            if reference is None:
                return {"error": "operation='add_component' requires 'reference'"}
            if value is None:
                return {"error": "operation='add_component' requires 'value'"}
            if position is None:
                return {"error": "operation='add_component' requires 'position'"}
            if len(position) != 2:
                return {"error": "position must be [x, y]"}
            sch = _require_schematic()
            pos = list(_snap(position[0], position[1]))
            comp = sch.components.add(
                lib_id=lib_id,
                reference=reference,
                value=value,
                position=tuple(pos),
                footprint=footprint,
            )
            if properties:
                for prop in properties.split(","):
                    if "=" in prop:
                        key, val = prop.strip().split("=", 1)
                        comp.set_property(key.strip(), val.strip())
            logger.info("Added component %s (%s) = %s at %s", reference, lib_id, value, pos)
            return {
                "status": "ok",
                "reference": reference,
                "lib_id": lib_id,
                "value": value,
                "position": pos,
            }

        if operation == "add_multi_unit_component":
            if lib_id is None:
                return {"error": "operation='add_multi_unit_component' requires 'lib_id'"}
            if reference is None:
                return {"error": "operation='add_multi_unit_component' requires 'reference'"}
            if value is None:
                return {"error": "operation='add_multi_unit_component' requires 'value'"}
            if position is None:
                return {"error": "operation='add_multi_unit_component' requires 'position'"}
            if len(position) != 2:
                return {"error": "position must be [x, y]"}
            sch = _require_schematic()
            from kicad_sch_api.library.cache import get_symbol_cache
            cache = get_symbol_cache()
            symbol_def = cache.get_symbol(lib_id)
            if symbol_def is None:
                return {"error": f"Symbol '{lib_id}' not found in KiCad libraries"}
            unit_pins = _parse_unit_pin_mapping(symbol_def)
            if not unit_pins:
                # Single-unit symbol — fall through to normal add
                pos = list(_snap(position[0], position[1]))
                comp = sch.components.add(
                    lib_id=lib_id, reference=reference, value=value,
                    position=tuple(pos), footprint=footprint,
                )
                return {"status": "ok", "reference": reference, "lib_id": lib_id,
                        "value": value, "position": pos}
            units_to_place = sorted(units) if units is not None else sorted(unit_pins.keys())
            invalid = [u for u in units_to_place if u not in unit_pins]
            if invalid:
                return {
                    "error": f"Units {invalid} not found in symbol. Available: {sorted(unit_pins.keys())}",
                }
            if len(units_to_place) == len(unit_pins) and units is None:
                result = sch.components.add(
                    lib_id=lib_id, reference=reference, value=value,
                    position=tuple(position), footprint=footprint,
                    add_all_units=True, unit_spacing=unit_spacing,
                )
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
                placed_units = []
                for i, unit_num in enumerate(units_to_place):
                    unit_x = position[0]
                    unit_y = position[1] + i * unit_spacing
                    comp = sch.components.add(
                        lib_id=lib_id, reference=reference, value=value,
                        position=(unit_x, unit_y), footprint=footprint,
                        unit=unit_num,
                    )
                    placed_units.append({
                        "unit": unit_num,
                        "pins": unit_pins[unit_num],
                        "position": [round(comp.position.x, 3), round(comp.position.y, 3)],
                    })
            logger.info(
                "Added multi-unit component %s (%s) with %d units: %s",
                reference, lib_id, len(placed_units), [u["unit"] for u in placed_units],
            )
            return {
                "status": "ok",
                "reference": reference,
                "lib_id": lib_id,
                "value": value,
                "total_units": len(placed_units),
                "units": placed_units,
            }

        if operation == "remove_component":
            if reference is None:
                return {"error": "operation='remove_component' requires 'reference'"}
            sch = _require_schematic()
            removed = sch.components.remove(reference)
            if removed:
                return {"status": "ok", "reference": reference}
            return {"error": f"Component {reference} not found"}

        if operation == "move_component":
            if reference is None:
                return {"error": "operation='move_component' requires 'reference'"}
            if position is None:
                return {"error": "operation='move_component' requires 'position'"}
            if len(position) != 2:
                return {"error": "position must be [x, y]"}
            sch = _require_schematic()
            matches = list(sch.components.filter(reference=reference))
            if not matches:
                return {"error": f"Component {reference!r} not found"}
            comp = matches[0]
            old_pos = [comp.position.x, comp.position.y] if comp.position else None
            new_pos = list(_snap(position[0], position[1]))
            comp.position = tuple(new_pos)
            return {
                "status": "ok",
                "reference": reference,
                "old_position": old_pos,
                "new_position": new_pos,
            }

        if operation == "list_components":
            sch = _require_schematic()
            components = []
            for comp in sch.components:
                entry: Dict[str, Any] = {
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

        if operation == "filter_components":
            sch = _require_schematic()
            # kicad-sch-api filter() only recognizes: lib_id, value, value_pattern,
            # reference_pattern, footprint, in_area, has_property.  Passing a key
            # it doesn't recognize is silently dropped — the public-facing
            # `reference` arg here is mapped to `reference_pattern` (regex) on the
            # way in.  The docstring above already describes it as a "pattern".
            filter_criteria: Dict[str, str] = {}
            if lib_id:
                filter_criteria["lib_id"] = lib_id
            if value:
                filter_criteria["value"] = value
            if reference:
                filter_criteria["reference_pattern"] = reference
            if footprint:
                filter_criteria["footprint"] = footprint
            if not filter_criteria:
                return {"error": "At least one filter criterion required"}
            filtered = sch.components.filter(**filter_criteria)
            results = []
            for comp in filtered:
                results.append({
                    "reference": comp.reference,
                    "lib_id": comp.lib_id,
                    "value": comp.value,
                    "position": [comp.position.x, comp.position.y] if comp.position else None,
                })
            return {"status": "ok", "count": len(results), "components": results}

        if operation == "components_in_area":
            if x1 is None or y1 is None or x2 is None or y2 is None:
                return {"error": "operation='components_in_area' requires 'x1', 'y1', 'x2', 'y2'"}
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

        if operation == "bulk_update_components":
            if criteria is None:
                return {"error": "operation='bulk_update_components' requires 'criteria'"}
            if updates is None:
                return {"error": "operation='bulk_update_components' requires 'updates'"}
            sch = _require_schematic()
            updated_count = sch.components.bulk_update(criteria=criteria, updates=updates)
            return {"status": "ok", "updated": updated_count}

        # ── Component pins ─────────────────────────────────────────────────

        if operation == "get_component_pin_position":
            if reference is None:
                return {"error": "operation='get_component_pin_position' requires 'reference'"}
            if pin_number is None:
                return {"error": "operation='get_component_pin_position' requires 'pin_number'"}
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

        if operation == "list_component_pins":
            if reference is None:
                return {"error": "operation='list_component_pins' requires 'reference'"}
            sch = _require_schematic()
            all_comps = [c for c in sch.components if c.reference == reference]
            if not all_comps:
                return {"error": f"Component {reference} not found"}
            pins_data = []
            seen_pins: set = set()
            if len(all_comps) == 1:
                comp = all_comps[0]
                for pin in comp.pins:
                    pin_pos = _kicad_pin_position(comp, pin.number)
                    pin_entry: Dict[str, Any] = {"number": pin.number, "name": pin.name}
                    if pin_pos:
                        pin_entry["x"] = round(pin_pos.x, 3)
                        pin_entry["y"] = round(pin_pos.y, 3)
                    pins_data.append(pin_entry)
            else:
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
                        if pins_in_unit and pin.number not in pins_in_unit:
                            continue
                        seen_pins.add(pin.number)
                        pin_pos = _kicad_pin_position(comp, pin.number)
                        entry = {"number": pin.number, "name": pin.name, "unit": unit}
                        if pin_pos:
                            entry["x"] = round(pin_pos.x, 3)
                            entry["y"] = round(pin_pos.y, 3)
                        pins_data.append(entry)
            return {"status": "ok", "reference": reference, "count": len(pins_data), "pins": pins_data}

        if operation == "find_component_connections":
            if project_path is None:
                return {"error": "operation='find_component_connections' requires 'project_path'"}
            if component_ref is None:
                return {"error": "operation='find_component_connections' requires 'component_ref'"}
            return await _op_find_component_connections(project_path, component_ref, ctx)

        # ── Wires ──────────────────────────────────────────────────────────

        if operation == "add_wire":
            if start_pos is None:
                return {"error": "operation='add_wire' requires 'start_pos'"}
            if end_pos is None:
                return {"error": "operation='add_wire' requires 'end_pos'"}
            if len(start_pos) != 2 or len(end_pos) != 2:
                return {"error": "Positions must be [x, y] coordinates"}
            sch = _require_schematic()
            sp = list(_snap(start_pos[0], start_pos[1]))
            ep = list(_snap(end_pos[0], end_pos[1]))
            wire_uuid = sch.add_wire(start=tuple(sp), end=tuple(ep))
            return {"status": "ok", "wire_uuid": wire_uuid, "start": sp, "end": ep}

        if operation == "add_wire_between_pins":
            if comp1_ref is None:
                return {"error": "operation='add_wire_between_pins' requires 'comp1_ref'"}
            if pin1 is None:
                return {"error": "operation='add_wire_between_pins' requires 'pin1'"}
            if comp2_ref is None:
                return {"error": "operation='add_wire_between_pins' requires 'comp2_ref'"}
            if pin2 is None:
                return {"error": "operation='add_wire_between_pins' requires 'pin2'"}
            sch = _require_schematic()
            segments: list = []

            def _wire(x1f: float, y1f: float, x2f: float, y2f: float):
                if abs(x1f - x2f) < 1e-6 and abs(y1f - y2f) < 1e-6:
                    return None
                uuid: str = sch.add_wire(start=(x1f, y1f), end=(x2f, y2f))
                segments.append({"start": [x1f, y1f], "end": [x2f, y2f], "uuid": uuid})
                return uuid

            results = []
            stub_ends = []
            for ref, pnum in [(comp1_ref, pin1), (comp2_ref, pin2)]:
                comp = _find_component_for_pin(sch, ref, pnum)
                if comp is None:
                    return {"error": f"Component {ref!r} not found"}
                tip = _kicad_pin_position(comp, pnum)
                if tip is None:
                    return {"error": f"Pin {pnum!r} not found on {ref!r}"}
                dx, dy = _pin_wire_offset(comp, pnum, 2.54)
                sx, sy = _snap(tip.x + dx, tip.y + dy)
                tx, ty = _snap(tip.x, tip.y)
                _wire(tx, ty, sx, sy)
                stub_ends.append((sx, sy))
                results.append({"ref": ref, "pin": pnum, "tip": [tx, ty], "stub_end": [sx, sy]})

            (wx1, wy1), (wx2, wy2) = stub_ends
            if abs(wx1 - wx2) < 1e-6 or abs(wy1 - wy2) < 1e-6:
                _wire(wx1, wy1, wx2, wy2)
            else:
                elbow_x, elbow_y = _snap(wx2, wy1)
                _wire(wx1, wy1, elbow_x, elbow_y)
                _wire(elbow_x, elbow_y, wx2, wy2)
            return {
                "status": "ok",
                "pins": results,
                "segments": segments,
                "segment_count": len(segments),
            }

        if operation == "remove_wire":
            if wire_uuid is None:
                return {"error": "operation='remove_wire' requires 'wire_uuid'"}
            sch = _require_schematic()
            removed = sch.remove_wire(wire_uuid)
            if removed:
                return {"status": "ok", "wire_uuid": wire_uuid}
            return {"error": f"Wire {wire_uuid} not found"}

        if operation == "add_junction":
            if position is None:
                return {"error": "operation='add_junction' requires 'position'"}
            if len(position) != 2:
                return {"error": "Position must be [x, y] coordinates"}
            sch = _require_schematic()
            junction_uuid = sch.junctions.add(position=tuple(position), diameter=diameter)
            return {"status": "ok", "junction_uuid": junction_uuid, "position": position}

        # ── Labels ─────────────────────────────────────────────────────────

        if operation == "add_label":
            if text is None:
                return {"error": "operation='add_label' requires 'text'"}
            if position is None:
                return {"error": "operation='add_label' requires 'position'"}
            if len(position) != 2:
                return {"error": "Position must be [x, y] coordinates"}
            sch = _require_schematic()
            pos = list(_snap(position[0], position[1]))
            label_uuid = sch.add_label(text=text, position=tuple(pos), rotation=rotation, size=size)
            return {"status": "ok", "label_uuid": label_uuid, "text": text, "position": pos}

        if operation == "add_label_to_pin":
            if reference is None:
                return {"error": "operation='add_label_to_pin' requires 'reference'"}
            if pin_number is None:
                return {"error": "operation='add_label_to_pin' requires 'pin_number'"}
            if text is None:
                return {"error": "operation='add_label_to_pin' requires 'text'"}
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

        if operation == "add_hierarchical_label":
            if text is None:
                return {"error": "operation='add_hierarchical_label' requires 'text'"}
            if position is None:
                return {"error": "operation='add_hierarchical_label' requires 'position'"}
            if len(position) != 2:
                return {"error": "Position must be [x, y] coordinates"}
            sch = _require_schematic()
            from kicad_sch_api.core.types import HierarchicalLabelShape
            shape_map = {
                "input": HierarchicalLabelShape.INPUT,
                "output": HierarchicalLabelShape.OUTPUT,
                "bidirectional": HierarchicalLabelShape.BIDIRECTIONAL,
                "tristate": HierarchicalLabelShape.TRISTATE,
                "passive": HierarchicalLabelShape.PASSIVE,
                "unspecified": HierarchicalLabelShape.UNSPECIFIED,
            }
            shape_key = shape.lower()
            if shape_key not in shape_map:
                return {"error": f"Unknown shape {shape!r}. Valid: {sorted(shape_map)}"}
            label_uuid = sch.add_hierarchical_label(
                text=text, position=tuple(position),
                shape=shape_map[shape_key], rotation=rotation, size=size,
            )
            return {
                "status": "ok",
                "label_uuid": label_uuid,
                "text": text,
                "shape": shape,
                "position": position,
            }

        if operation == "connect_pins_with_labels":
            if comp1_ref is None:
                return {"error": "operation='connect_pins_with_labels' requires 'comp1_ref'"}
            if pin1 is None:
                return {"error": "operation='connect_pins_with_labels' requires 'pin1'"}
            if comp2_ref is None:
                return {"error": "operation='connect_pins_with_labels' requires 'comp2_ref'"}
            if pin2 is None:
                return {"error": "operation='connect_pins_with_labels' requires 'pin2'"}
            if net_name is None:
                return {"error": "operation='connect_pins_with_labels' requires 'net_name'"}
            sch = _require_schematic()
            label_uuids = []
            for ref, pnum in [(comp1_ref, pin1), (comp2_ref, pin2)]:
                comp = _find_component_for_pin(sch, ref, pnum)
                if comp is None:
                    return {"error": f"Component {ref} not found"}
                pin_pos = _kicad_pin_position(comp, pnum)
                if pin_pos is None:
                    return {"error": f"Pin {pnum} not found on {ref}"}
                dx, dy = _pin_wire_offset(comp, pnum, 2.54)
                label_x = pin_pos.x + dx
                label_y = pin_pos.y + dy
                sch.add_wire(start=(pin_pos.x, pin_pos.y), end=(label_x, label_y))
                uuid = sch.add_label(net_name, (label_x, label_y))
                label_uuids.append(uuid)
            return {
                "status": "ok",
                "net_name": net_name,
                "labels_created": len(label_uuids),
                "label_uuids": label_uuids,
            }

        if operation == "remove_label":
            if label_uuid is None:
                return {"error": "operation='remove_label' requires 'label_uuid'"}
            sch = _require_schematic()
            removed = sch.remove_label(label_uuid)
            if removed:
                return {"status": "ok", "label_uuid": label_uuid}
            return {"error": f"Label {label_uuid} not found"}

        if operation == "edit_label":
            if label_uuid is None:
                return {"error": "operation='edit_label' requires 'label_uuid'"}
            sch = _require_schematic()
            if all(v is None for v in (new_text, position, rotation if rotation != 0.0 else None, size if size != 1.27 else None)):
                return {"error": "No modifications specified. Provide at least one of: new_text, position, rotation, size"}
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
            # rotation/size carry 0.0/1.27 defaults; apply ONLY when the caller
            # passed a non-default — matching the modification check above. Without
            # this guard a text-only edit silently reset rotation to 0 and size to
            # 1.27 (h-edit-label). Setting them to *exactly* the defaults is not
            # expressible — a known sentinel limitation (l-edit-label).
            if rotation != 0.0:
                label.rotation = rotation
            if size != 1.27:
                label.size = size
            return {
                "status": "ok",
                "label_uuid": label_uuid,
                "text": label.text,
                "position": [label.position.x, label.position.y],
                "rotation": label.rotation,
                "size": label.size,
            }

        # ── Text annotations ───────────────────────────────────────────────

        if operation == "add_text":
            if text is None:
                return {"error": "operation='add_text' requires 'text'"}
            if position is None:
                return {"error": "operation='add_text' requires 'position'"}
            if len(position) != 2:
                return {"error": "Position must be [x, y] coordinates"}
            sch = _require_schematic()
            text_uuid = sch.add_text(text, tuple(position), rotation, size)
            return {"status": "ok", "text_uuid": text_uuid, "text": text, "position": position}

        if operation == "add_text_box":
            if text is None:
                return {"error": "operation='add_text_box' requires 'text'"}
            if position is None:
                return {"error": "operation='add_text_box' requires 'position'"}
            if sheet_size is None:
                return {"error": "operation='add_text_box' requires 'sheet_size'"}
            if len(position) != 2 or len(sheet_size) != 2:
                return {"error": "Position and size must be [x, y] and [width, height]"}
            sch = _require_schematic()
            textbox_uuid = sch.add_text_box(text, tuple(position), tuple(sheet_size), rotation, size)
            return {
                "status": "ok",
                "textbox_uuid": textbox_uuid,
                "text": text,
                "position": position,
                "size": sheet_size,
            }

        # ── Hierarchical sheets ────────────────────────────────────────────

        if operation == "add_sheet":
            if name is None:
                return {"error": "operation='add_sheet' requires 'name'"}
            if filename is None:
                return {"error": "operation='add_sheet' requires 'filename'"}
            if position is None:
                return {"error": "operation='add_sheet' requires 'position'"}
            if sheet_size is None:
                return {"error": "operation='add_sheet' requires 'sheet_size'"}
            if len(position) != 2 or len(sheet_size) != 2:
                return {"error": "Position and size must be [x, y] and [width, height]"}
            sch = _require_schematic()
            sheet_uuid = sch.add_sheet(name, filename, tuple(position), tuple(sheet_size))
            return {
                "status": "ok",
                "sheet_uuid": sheet_uuid,
                "name": name,
                "filename": filename,
                "position": position,
                "size": sheet_size,
            }

        if operation == "add_sheet_pin":
            if sheet_uuid is None:
                return {"error": "operation='add_sheet_pin' requires 'sheet_uuid'"}
            if name is None:
                return {"error": "operation='add_sheet_pin' requires 'name'"}
            if pin_type is None:
                return {"error": "operation='add_sheet_pin' requires 'pin_type'"}
            if edge is None:
                return {"error": "operation='add_sheet_pin' requires 'edge'"}
            sch = _require_schematic()

            # Reject unknown pin_type explicitly — silent default-substitution by
            # the underlying library would hide caller typos (mirrors the
            # add_hierarchical_label guard pattern).
            valid_pin_types = {"input", "output", "bidirectional", "tri_state", "passive"}
            pin_type_key = pin_type.lower()
            if pin_type_key not in valid_pin_types:
                return {
                    "error": f"Unknown pin_type {pin_type!r}. Valid: {sorted(valid_pin_types)}"
                }

            valid_edges = {"right", "bottom", "left", "top"}
            edge_key = edge.lower()
            if edge_key not in valid_edges:
                return {
                    "error": f"Unknown edge {edge!r}. Valid: {sorted(valid_edges)}"
                }

            pin_uuid = sch.add_sheet_pin(
                sheet_uuid, name, pin_type_key, edge_key, position_along_edge
            )
            return {
                "status": "ok",
                "pin_uuid": pin_uuid,
                "name": name,
                "pin_type": pin_type_key,
                "edge": edge_key,
                "position_along_edge": position_along_edge,
                "sheet_uuid": sheet_uuid,
            }

        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: create|load|save|validate|info|clone|backup|check_pin_collisions|"
                f"add_component|add_multi_unit_component|remove_component|move_component|"
                f"list_components|filter_components|components_in_area|bulk_update_components|"
                f"get_component_pin_position|list_component_pins|find_component_connections|"
                f"add_wire|add_wire_between_pins|remove_wire|add_junction|"
                f"add_label|add_label_to_pin|add_hierarchical_label|connect_pins_with_labels|"
                f"remove_label|edit_label|"
                f"add_text|add_text_box|"
                f"add_sheet|add_sheet_pin"
            )
        }
