"""Generate a partial ``.kicad_sch`` from a DesignIntent.

Places the MCU + recognized peripherals on a naive grid and wires each net by
label-at-pin (same net name on every resolved endpoint → KiCad connectivity),
the same mechanism the schematic-build tools use. Endpoints that don't resolve
to a real symbol pin are recorded (not silently skipped). Orphan nets get only
their MCU pin labeled — the far end is left open, matching the intent's gap.
"""
from __future__ import annotations

from typing import Any, Optional

from kicad_mcp.utils.firmware.intent import DesignIntent
from kicad_mcp.utils.firmware.knowledge import role_to_pin_name
from kicad_mcp.utils.firmware.mcu_pinmap import gpio_to_pin_number, pin_number_by_name

_PITCH_MM = 63.5
_COLS = 4


def generate_schematic(intent: DesignIntent, schematic_path: str) -> dict[str, Any]:
    import kicad_sch_api as ksa
    from kicad_sch_api.library.cache import get_symbol_cache

    from kicad_mcp.tools.schematic_impl import _kicad_pin_position, _pin_wire_offset

    if intent.mcu is None:
        return {
            "status": "error", "code": "no_mcu",
            "message": "Design intent has no MCU (unresolved board); cannot generate.",
        }

    cache = get_symbol_cache()
    sch = ksa.create_schematic("firmware_gen")

    # --- place components (MCU first, then peripherals) ---
    to_place: list[tuple[str, Optional[str], str]] = [
        (intent.mcu.ref, intent.mcu.lib_id, intent.mcu.part)
    ]
    to_place += [(p.ref, p.lib_id, p.value or p.type) for p in intent.peripherals]

    placed: dict[str, Any] = {}
    symbols: dict[str, Any] = {}
    component_errors: list[dict[str, Any]] = []
    for i, (ref, lib_id, value) in enumerate(to_place):
        if not lib_id:
            component_errors.append({"ref": ref, "error": "no lib_id in intent"})
            continue
        sym = cache.get_symbol(lib_id)
        if sym is None:
            component_errors.append({"ref": ref, "error": f"symbol {lib_id!r} not found"})
            continue
        col, row = i % _COLS, i // _COLS
        pos = (25.4 + col * _PITCH_MM, 25.4 + row * _PITCH_MM)
        try:
            comp = sch.components.add(lib_id=lib_id, reference=ref, value=value, position=pos)
        except Exception as e:  # noqa: BLE001 - record + continue, don't abort the build
            component_errors.append({"ref": ref, "error": f"add failed: {e}"})
            continue
        placed[ref] = comp
        symbols[ref] = sym

    type_by_ref = {p.ref: p.type for p in intent.peripherals}

    def resolve_pin(ref: str, gpio: Any, role: Any) -> Any:
        sym = symbols.get(ref)
        if sym is None:
            return None
        if gpio is not None:                       # MCU side
            return gpio_to_pin_number(sym, int(gpio))
        ptype = type_by_ref.get(ref)               # peripheral side
        pin_name = role_to_pin_name(ptype, role) if ptype else None
        num = pin_number_by_name(sym, pin_name) if pin_name else None
        if num is None and role:                   # last resort: role == pin name
            num = pin_number_by_name(sym, role)
        return num

    # --- wire nets ---
    nets_by_kind: dict[str, int] = {}
    endpoints_wired = 0
    unresolved: list[dict[str, Any]] = []
    for net in intent.nets:
        for ep in net.endpoints:
            if ep.ref not in placed:
                unresolved.append({"net": net.name, "ref": ep.ref,
                                   "reason": "component not placed"})
                continue
            pin_num = resolve_pin(ep.ref, ep.gpio, ep.role)
            if pin_num is None:
                unresolved.append({"net": net.name, "ref": ep.ref,
                                   "gpio": ep.gpio, "role": ep.role,
                                   "reason": "pin unresolved"})
                continue
            comp = placed[ep.ref]
            pin_pos = _kicad_pin_position(comp, pin_num)
            if pin_pos is None:
                unresolved.append({"net": net.name, "ref": ep.ref, "pin": pin_num,
                                   "reason": "pin position unavailable"})
                continue
            dx, dy = _pin_wire_offset(comp, pin_num, 2.54)
            lx, ly = pin_pos.x + dx, pin_pos.y + dy
            sch.add_wire(start=(pin_pos.x, pin_pos.y), end=(lx, ly))
            sch.add_label(net.name, (lx, ly))
            endpoints_wired += 1
        nets_by_kind[net.kind] = nets_by_kind.get(net.kind, 0) + 1

    sch.save(schematic_path)
    return {
        "status": "ok",
        "schematic_path": schematic_path,
        "components_placed": len(placed),
        "component_errors": component_errors,
        "nets_total": len(intent.nets),
        "nets_by_kind": nets_by_kind,
        "endpoints_wired": endpoints_wired,
        "unresolved_endpoints": unresolved,
        "gaps": len(intent.gaps),
    }
