"""Generate a partial ``.kicad_sch`` from a DesignIntent.

Places the MCU + recognized peripherals on a naive grid and wires each net by
label-at-pin (same net name on every resolved endpoint → KiCad connectivity),
the same mechanism the schematic-build tools use. Endpoints that don't resolve
to a real symbol pin are recorded (not silently skipped). Orphan nets get only
their MCU pin labeled — the far end is left open, matching the intent's gap.

Three disposition buckets in the result keep an ``ok`` status honest about what
was NOT wired and why: ``component_errors`` (a chip couldn't be placed) and
``unresolved_endpoints`` (a wire that should have been made failed) are FAILURES
and gate the status off ``ok``; ``deferred_endpoints`` are off-board endpoints
(remote/terminal) intentionally not wired here — realized at the expand step or
field-wired through a connector — and do NOT affect status (deferral isn't loss).
"""
from __future__ import annotations

from typing import Any

from kicad_mcp.utils.firmware.intent import DesignIntent, is_remote
from kicad_mcp.utils.firmware.knowledge import (
    is_terminal_card_type,
    resolve_mcu_by_part,
    resolve_symbol,
    role_to_pin_name,
)
from kicad_mcp.utils.firmware.mcu_pinmap import (
    gpio_to_pin_number,
    resolve_pin_token,
)

_PITCH_MM = 63.5
_COLS = 4


def _build_status(component_errors: list, unresolved: list, any_placed: bool) -> str:
    """Three-state status for a (partial) schematic build.

    ``ok`` only when nothing was dropped and every endpoint that should have
    wired did. A dropped component or an unresolved endpoint means the saved
    schematic is incomplete: ``partial`` if at least something was placed,
    ``error`` if nothing was. Mirrors pcb_nets' ok/partial/error so callers see
    one consistent contract — and so a version-renamed/typo'd lib_id surfaces
    instead of silently reporting ``ok`` on a board missing a chip.

    Deferred endpoints (off-board/terminal, see ``deferred_endpoints``) are NOT
    failures and are intentionally excluded here — deferral is not loss.
    """
    if not component_errors and not unresolved:
        return "ok"
    return "partial" if any_placed else "error"

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

    # MCU card facts (resolved once): the GPIO pin-name prefix (ESP32 "IO", Pico
    # "GPIO") for gpio resolution below, and any cross-version symbol alternates.
    _mcu_info = resolve_mcu_by_part(intent.mcu.part)
    gpio_prefix = _mcu_info.get("gpio_pin_prefix", "IO") if _mcu_info else "IO"
    mcu_alts = list(_mcu_info.get("alt_lib_ids", []) or []) if _mcu_info else []

    # --- place components (MCU first, then peripherals) ---
    # Tuple: (ref, lib_id, alt_lib_ids, value, footprint). alt_lib_ids carries
    # older-KiCad names for a renamed symbol (from the card, when present).
    to_place: list[tuple[str, str | None, list[str], str, str | None]] = [
        (intent.mcu.ref, intent.mcu.lib_id, mcu_alts, intent.mcu.part, intent.mcu.footprint)
    ]
    # Off-board peripherals are field-wired (a terminal carries their nets), so
    # they are documented in the BOM but never placed as a symbol — their original
    # pin endpoints then drop out of the wiring loop (ref not in ``placed``),
    # leaving only the synthesized terminal wired (see remote_peripherals). A
    # peripheral is off-board if it's declared remote OR its card is
    # ``realize: terminal`` — the latter is intrinsically remote even on a raw
    # (non-expanded) intent, before _inject_terminal_loci has run.
    # ref -> why it's off-board (the reason carried into deferred_endpoints, so an
    # ``ok`` build still says which declared connections it deferred and why).
    offboard_reason: dict[str, str] = {}
    for p in intent.peripherals:
        if is_remote(intent, p.ref):
            offboard_reason[p.ref] = "remote: off-board, field-wired through a connector"
        elif is_terminal_card_type(p.type):
            offboard_reason[p.ref] = "terminal: realized as a screw terminal at the expand step"
    offboard_refs = set(offboard_reason)
    to_place += [
        (p.ref, p.lib_id, list(p.alt_lib_ids), p.value or p.type, p.footprint)
        for p in intent.peripherals if p.ref not in offboard_refs
    ]

    placed: dict[str, Any] = {}
    symbols: dict[str, Any] = {}
    component_errors: list[dict[str, Any]] = []
    for i, (ref, lib_id, alts, value, footprint) in enumerate(to_place):
        if not lib_id:
            component_errors.append({"ref": ref, "error": "no lib_id in intent"})
            continue
        # Try the primary lib_id then any cross-version alternates; use whichever
        # name actually exists in the running library for the placement.
        resolved_lib_id, sym = resolve_symbol(cache, lib_id, alts)
        if sym is None:
            tried = ", ".join(repr(c) for c in (lib_id, *alts))
            component_errors.append({"ref": ref, "error": f"no symbol found (tried {tried})"})
            continue
        assert resolved_lib_id is not None  # sym present => a candidate resolved
        col, row = i % _COLS, i // _COLS
        pos = (25.4 + col * _PITCH_MM, 25.4 + row * _PITCH_MM)
        try:
            comp = sch.components.add(lib_id=resolved_lib_id, reference=ref, value=value,
                                      position=pos, footprint=footprint)
        except Exception as e:  # noqa: BLE001 - record + continue, don't abort the build
            component_errors.append({"ref": ref, "error": f"add failed: {e}"})
            continue
        placed[ref] = comp
        symbols[ref] = sym

    type_by_ref = {p.ref: p.type for p in intent.peripherals}

    # gpio_prefix (resolved above) covers every gpio resolution below — an endpoint
    # carrying a gpio is always MCU-side.
    def resolve_pin(ref: str, gpio: Any, role: Any, pin: Any) -> Any:
        sym = symbols.get(ref)
        if sym is None:
            return None
        if pin is not None:                        # direct pin NAME or number
            return resolve_pin_token(sym, pin)
        if gpio is not None:                       # MCU side
            return gpio_to_pin_number(sym, int(gpio), gpio_prefix)
        ptype = type_by_ref.get(ref)               # peripheral side
        # The roles map may point at a pin NAME (MCP23017 ``SDA``) or a pin
        # NUMBER (a module-header device, ``SDA`` -> ``4``) — handle both.
        pin_name = role_to_pin_name(ptype, role) if ptype else None
        if pin_name is not None:
            num = resolve_pin_token(sym, pin_name)
            if num is not None:
                return num
        if role:                                   # last resort: role == pin name
            return resolve_pin_token(sym, role)
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
    deferred: list[dict[str, Any]] = []
    for net in intent.nets:
        for ep in net.endpoints:
            if ep.ref not in placed:
                # Off-board peripherals are intentionally excluded from placement
                # (a terminal/connector handles their signals). Not a failure —
                # record them in their own bucket so a later ``ok`` is transparent
                # about which declared connections were deferred, rather than the
                # connection silently vanishing from the schematic.
                if ep.ref in offboard_refs:
                    deferred.append({"net": net.name, "ref": ep.ref,
                                     "role": ep.role,
                                     "reason": offboard_reason[ep.ref]})
                    continue
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
        "status": _build_status(component_errors, unresolved, bool(placed)),
        "schematic_path": schematic_path,
        "components_placed": len(placed),
        "component_errors": component_errors,
        "nets_total": len(intent.nets),
        "nets_by_kind": nets_by_kind,
        "endpoints_wired": endpoints_wired,
        "rail_markers": rail_markers,
        "unresolved_endpoints": unresolved,
        "deferred_endpoints": deferred,
        "gaps": len(intent.gaps),
    }
