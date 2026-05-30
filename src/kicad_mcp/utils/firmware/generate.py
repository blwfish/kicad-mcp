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

# Power-rail name -> KiCad power symbol lib_id (verified to resolve).
_RAIL_LIB = {
    "+3V3": "power:+3V3", "+5V": "power:+5V",
    "GND": "power:GND", "VBUS": "power:VBUS",
}


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
    to_place: list[tuple[str, Optional[str], str, Optional[str]]] = [
        (intent.mcu.ref, intent.mcu.lib_id, intent.mcu.part, intent.mcu.footprint)
    ]
    to_place += [
        (p.ref, p.lib_id, p.value or p.type, p.footprint) for p in intent.peripherals
    ]

    placed: dict[str, Any] = {}
    symbols: dict[str, Any] = {}
    component_errors: list[dict[str, Any]] = []
    for i, (ref, lib_id, value, footprint) in enumerate(to_place):
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
            comp = sch.components.add(lib_id=lib_id, reference=ref, value=value,
                                      position=pos, footprint=footprint)
        except Exception as e:  # noqa: BLE001 - record + continue, don't abort the build
            component_errors.append({"ref": ref, "error": f"add failed: {e}"})
            continue
        placed[ref] = comp
        symbols[ref] = sym

    type_by_ref = {p.ref: p.type for p in intent.peripherals}

    def _name_or_number(sym: Any, token: Any) -> Any:
        """Resolve a pin token to its NUMBER. A token is either a pin NAME
        (MCP23017 ``SDA``) or already a literal pin NUMBER (a header pin ``4``,
        a USB-C/QFN pad ``A4``/``29``). Name lookup first; else validate the
        token against the symbol's real numbers (alphanumeric, so not a digit
        check)."""
        num = pin_number_by_name(sym, token)
        if num is not None:
            return num
        valid = {str(p.number) for p in (getattr(sym, "pins", None) or ())}
        return str(token) if str(token) in valid else None

    def resolve_pin(ref: str, gpio: Any, role: Any, pin: Any) -> Any:
        sym = symbols.get(ref)
        if sym is None:
            return None
        if pin is not None:                        # direct pin NAME or number
            return _name_or_number(sym, pin)
        if gpio is not None:                       # MCU side
            return gpio_to_pin_number(sym, int(gpio))
        ptype = type_by_ref.get(ref)               # peripheral side
        # The roles map may point at a pin NAME (MCP23017 ``SDA``) or a pin
        # NUMBER (a module-header device, ``SDA`` -> ``4``) — handle both.
        pin_name = role_to_pin_name(ptype, role) if ptype else None
        if pin_name is not None:
            num = _name_or_number(sym, pin_name)
            if num is not None:
                return num
        if role:                                   # last resort: role == pin name
            return _name_or_number(sym, role)
        return None

    def wire_pin(comp: Any, pin_num: str, net_name: str) -> bool:
        """Add a wire stub + net label at a pin (the connectivity primitive)."""
        pin_pos = _kicad_pin_position(comp, pin_num)
        if pin_pos is None:
            return False
        dx, dy = _pin_wire_offset(comp, pin_num, 2.54)
        lx, ly = pin_pos.x + dx, pin_pos.y + dy
        sch.add_wire(start=(pin_pos.x, pin_pos.y), end=(lx, ly))
        sch.add_label(net_name, (lx, ly))
        return True

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
            pin_num = resolve_pin(ep.ref, ep.gpio, ep.role, ep.pin)
            if pin_num is None:
                unresolved.append({"net": net.name, "ref": ep.ref, "gpio": ep.gpio,
                                   "role": ep.role, "pin": ep.pin,
                                   "reason": "pin unresolved"})
                continue
            if wire_pin(placed[ep.ref], pin_num, net.name):
                endpoints_wired += 1
            else:
                unresolved.append({"net": net.name, "ref": ep.ref, "pin": pin_num,
                                   "reason": "pin position unavailable"})
        nets_by_kind[net.kind] = nets_by_kind.get(net.kind, 0) + 1

    # --- idiomatic power-rail markers (best-effort) ---
    # Rail connectivity is already established by the rail-named labels above;
    # these add KiCad's conventional power symbol + PWR_FLAG per rail so the net
    # is recognized as a power rail (and ERC has a driver). Failure never aborts.
    # Markers go in a clear band BELOW all placed components. Position is
    # irrelevant to connectivity (labels join by name), but a marker dropped
    # onto a component pin would merge that pin's net into the rail — so keep
    # them well clear of the grid.
    rows = (len(placed) + _COLS - 1) // _COLS
    marker_y = 25.4 + rows * _PITCH_MM + _PITCH_MM
    rail_markers = 0
    pwr_idx = 0
    for rail in sorted({n.name for n in intent.nets if n.kind == "power"}):
        rail_lib = _RAIL_LIB.get(rail)
        if rail_lib is None:
            continue
        for mlib in (rail_lib, "power:PWR_FLAG"):
            if cache.get_symbol(mlib) is None:
                continue
            try:
                mcomp = sch.components.add(lib_id=mlib, reference=f"#PWR{pwr_idx:02d}",
                                           value=rail, position=(25.4 + pwr_idx * 20.32, marker_y))
            except Exception:  # noqa: BLE001 - markers are best-effort
                continue
            pwr_idx += 1
            if wire_pin(mcomp, "1", rail):
                rail_markers += 1

    sch.save(schematic_path)
    return {
        "status": "ok",
        "schematic_path": schematic_path,
        "components_placed": len(placed),
        "component_errors": component_errors,
        "nets_total": len(intent.nets),
        "nets_by_kind": nets_by_kind,
        "endpoints_wired": endpoints_wired,
        "rail_markers": rail_markers,
        "unresolved_endpoints": unresolved,
        "gaps": len(intent.gaps),
    }
