"""Peripheral-block templates: expand recognized components into their mandatory
support circuitry (power tree, decoupling, I2C pull-ups, MCU straps, MCP23017
address strapping). Firmware never states any of this — the templates encode the
*design knowledge* every board needs.

Each template is a pure function ``(intent, alloc) -> Expansion``. ``expand_intent``
runs the registry, applies the expansions, and marks resolved gaps. ALL pin
facts come from ``knowledge.py`` (single source of truth — Rule 3); no pin
literals live here.

Expansion channels keep templates declarative:
  * ``components`` — new parts (origin="template")
  * ``power``      — (rail_name, Endpoint) contributions, merged into one net/rail
  * ``joins``      — (existing_net_name, Endpoint) appended to that net
  * ``new_nets``   — standalone new nets (e.g. the MCU EN pull-up net)
  * ``resolved``   — gap kinds this template fills
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from kicad_mcp.utils.firmware import knowledge as K
from kicad_mcp.utils.firmware.intent import (
    DesignIntent,
    Endpoint,
    Gap,
    Net,
    Peripheral,
)


@dataclass
class Expansion:
    components: list[Peripheral] = field(default_factory=list)
    power: list[tuple[str, Endpoint]] = field(default_factory=list)
    joins: list[tuple[str, Endpoint]] = field(default_factory=list)
    new_nets: list[Net] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)


class RefAllocator:
    """Hand out fresh refs (C3, R4, U5 …) continuing past the intent's existing
    refs so templates never collide with imported components."""

    def __init__(self, intent: DesignIntent) -> None:
        self._next: dict[str, int] = {}
        existing = list(intent.peripherals) + ([intent.mcu] if intent.mcu else [])
        for comp in existing:
            m = re.match(r"([A-Za-z#]+)(\d+)$", comp.ref)
            if m:
                pre, num = m.group(1), int(m.group(2))
                self._next[pre] = max(self._next.get(pre, 0), num)

    def next(self, prefix: str) -> str:
        self._next[prefix] = self._next.get(prefix, 0) + 1
        return f"{prefix}{self._next[prefix]}"


def _cap(alloc: RefAllocator, value: str, footprint: str) -> Peripheral:
    return Peripheral(ref=alloc.next("C"), type="C", lib_id=K.LIB_C, value=value,
                      footprint=footprint, origin="template")


def _res(alloc: RefAllocator, value: str, footprint: str) -> Peripheral:
    return Peripheral(ref=alloc.next("R"), type="R", lib_id=K.LIB_R, value=value,
                      footprint=footprint, origin="template")


def _ic_power_pins(intent: DesignIntent) -> list[tuple[str, list[str], list[str]]]:
    """(ref, supply_pin_names, ground_pin_names) for every placed IC."""
    out: list[tuple[str, list[str], list[str]]] = []
    if intent.mcu is not None:
        mi = K.resolve_mcu_by_part(intent.mcu.part)
        if mi is not None:
            out.append((intent.mcu.ref, [mi["supply_pin"]], [mi["ground_pin"]]))
    for p in intent.peripherals:
        info = K.resolve_peripheral(p.type)
        if info is not None:
            out.append((p.ref, list(info["supply_pins"]), list(info["ground_pins"])))
    return out


# --- templates ----------------------------------------------------------------

def power_tree(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """AMS1117 5V→3V3 + in/out caps; tie every IC supply→+3V3, ground→GND.
    Fires only for a 3V3 MCU. The regulator choice (AMS1117) is a design default
    firmware never states — flagged in the resolved gap detail."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mi = K.resolve_mcu_by_part(intent.mcu.part)
    if mi is None or not mi["needs_3v3"]:
        return ex

    ldo = Peripheral(ref=alloc.next("U"), type="AMS1117", lib_id=K.AMS1117["lib_id"],
                     value=K.AMS1117["value"], footprint=K.AMS1117["footprint"],
                     origin="template")
    c_in = _cap(alloc, "10uF", K.FP_C_BULK)
    c_out = _cap(alloc, "10uF", K.FP_C_BULK)
    c_byp = _cap(alloc, "100nF", K.FP_C_BYPASS)
    ex.components += [ldo, c_in, c_out, c_byp]

    vi, vo, gnd = K.AMS1117["vin_pin"], K.AMS1117["vout_pin"], K.AMS1117["gnd_pin"]
    # +5V (input rail; source — USB/connector — is out of scope, stays a gap)
    ex.power += [("+5V", Endpoint(ref=ldo.ref, pin=vi)),
                 ("+5V", Endpoint(ref=c_in.ref, pin="1"))]
    # +3V3 (regulated): LDO out, output caps, and EVERY IC supply pin
    ex.power += [("+3V3", Endpoint(ref=ldo.ref, pin=vo)),
                 ("+3V3", Endpoint(ref=c_out.ref, pin="1")),
                 ("+3V3", Endpoint(ref=c_byp.ref, pin="1"))]
    # GND: LDO gnd, all cap returns, and EVERY IC ground pin
    ex.power += [("GND", Endpoint(ref=ldo.ref, pin=gnd)),
                 ("GND", Endpoint(ref=c_in.ref, pin="2")),
                 ("GND", Endpoint(ref=c_out.ref, pin="2")),
                 ("GND", Endpoint(ref=c_byp.ref, pin="2"))]
    for ref, supplies, grounds in _ic_power_pins(intent):
        for s in supplies:
            ex.power.append(("+3V3", Endpoint(ref=ref, pin=s)))
        for g in grounds:
            ex.power.append(("GND", Endpoint(ref=ref, pin=g)))
    ex.resolved.append("power_tree")
    return ex


def decoupling(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """A 100nF bypass cap between +3V3 and GND for each IC."""
    ex = Expansion()
    for ref, supplies, grounds in _ic_power_pins(intent):
        if not supplies or not grounds:
            continue
        c = _cap(alloc, "100nF", K.FP_C_BYPASS)
        ex.components.append(c)
        ex.power.append(("+3V3", Endpoint(ref=c.ref, pin="1")))
        ex.power.append(("GND", Endpoint(ref=c.ref, pin="2")))
    if ex.components:
        ex.resolved.append("decoupling")
    return ex


def i2c_pullups(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """4.7k pull-ups to +3V3 on SDA and SCL, once per I2C bus with a device.
    Joins the existing bus nets (so the resistor sits on the real I2C line)."""
    ex = Expansion()
    seen_bus = False
    for net in intent.nets:
        if net.kind != "bus" or net.bus != "I2C":
            continue
        # which line? the peripheral endpoint's role is SDA or SCL.
        roles = {e.role for e in net.endpoints if e.role}
        if not (roles & {"SDA", "SCL"}):
            continue
        seen_bus = True
        r = _res(alloc, "4.7k", K.FP_R_0603)
        ex.components.append(r)
        ex.joins.append((net.name, Endpoint(ref=r.ref, pin="1")))   # onto the I2C line
        ex.power.append(("+3V3", Endpoint(ref=r.ref, pin="2")))
    if seen_bus:
        ex.resolved.append("pullups")
    return ex


def mcu_straps(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """ESP32 EN and IO0 (boot) each pulled up to +3V3 via 10k."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mi = K.resolve_mcu_by_part(intent.mcu.part)
    if mi is None:
        return ex
    for net_name, pin_name in (("MCU_EN", mi["en_pin"]), ("MCU_BOOT", mi["boot_pin"])):
        r = _res(alloc, "10k", K.FP_R_0603)
        ex.components.append(r)
        ex.new_nets.append(Net(
            name=net_name, kind="passive", confidence="high", origin="template",
            endpoints=[Endpoint(ref=intent.mcu.ref, pin=pin_name),
                       Endpoint(ref=r.ref, pin="1")],
        ))
        ex.power.append(("+3V3", Endpoint(ref=r.ref, pin="2")))
    ex.resolved.append("pullups")
    return ex


def mcp23017_config(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Strap each MCP23017's A0/A1/A2 per its I2C address, and tie RESET high.
    Direct ties (no resistors) — correct for a fixed address. The bit→rail map is
    knowledge.mcp23017_address_straps (the boundary-tested single source)."""
    ex = Expansion()
    for p in intent.peripherals:
        if p.type.upper() != "MCP23017" or p.address is None:
            continue
        straps = K.mcp23017_address_straps(p.address)
        if straps is None:
            ex.resolved.append("__invalid_address")  # surfaced as a gap below
            continue
        for addr_pin, rail in straps:
            ex.power.append((rail, Endpoint(ref=p.ref, pin=addr_pin)))
        # RESET is active-low — hold high.
        ex.power.append(("+3V3", Endpoint(ref=p.ref, pin=K.MCP23017_RESET_PIN)))
    return ex


_REGISTRY: list[tuple[str, Callable[[DesignIntent, RefAllocator], Expansion]]] = [
    ("power_tree", power_tree),
    ("decoupling", decoupling),
    ("i2c_pullups", i2c_pullups),
    ("mcu_straps", mcu_straps),
    ("mcp23017_config", mcp23017_config),
]


# --- expansion pass -----------------------------------------------------------

def expand_intent(intent: DesignIntent) -> DesignIntent:
    """Run all templates over ``intent`` (mutated in place and returned)."""
    alloc = RefAllocator(intent)
    rail_endpoints: dict[str, list[Endpoint]] = {}
    nets_by_name = {n.name: n for n in intent.nets}
    gaps_by_kind: dict[str, Gap] = {}
    for g in intent.gaps:
        gaps_by_kind.setdefault(g.kind, g)

    for tname, fn in _REGISTRY:
        ex = fn(intent, alloc)
        added_refs = [c.ref for c in ex.components]
        intent.peripherals.extend(ex.components)

        for rail, ep in ex.power:
            rail_endpoints.setdefault(rail, []).append(ep)

        for net_name, ep in ex.joins:
            tgt = nets_by_name.get(net_name)
            if tgt is not None:
                tgt.endpoints.append(ep)
            else:  # no such net — materialize a passive one
                n = Net(name=net_name, kind="passive", confidence="high",
                        origin="template", endpoints=[ep])
                intent.nets.append(n)
                nets_by_name[net_name] = n

        for n in ex.new_nets:
            intent.nets.append(n)
            nets_by_name[n.name] = n

        for kind in ex.resolved:
            if kind == "__invalid_address":
                intent.gaps.append(Gap("invalid_address",
                                       "MCP23017 I2C address outside 0x20–0x27; not strapped."))
                continue
            gap = gaps_by_kind.get(kind)
            if gap is not None and not gap.resolved:
                gap.resolved = True
                gap.resolved_by = tname
                gap.resolved_components = added_refs

    # Merge accumulated rail contributions into one power net per rail.
    for rail, eps in rail_endpoints.items():
        existing = nets_by_name.get(rail)
        deduped: list[Endpoint] = []
        seen: set[tuple[str, Optional[str]]] = set()
        pool = (existing.endpoints if existing else []) + eps
        for ep in pool:
            key = (ep.ref, ep.pin)
            if key not in seen:
                seen.add(key)
                deduped.append(ep)
        if existing is not None:
            existing.kind = "power"
            existing.endpoints = deduped
        else:
            n = Net(name=rail, kind="power", confidence="high", origin="template",
                    endpoints=deduped)
            intent.nets.append(n)
            nets_by_name[rail] = n
    return intent
