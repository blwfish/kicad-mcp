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
from kicad_mcp.utils.firmware.connectors import (
    ConnectorPosition,
    synthesize_connector,
)
from kicad_mcp.utils.firmware.intent import (
    Bus,
    ConnectorLegend,
    DesignIntent,
    Endpoint,
    Gap,
    Net,
    Peripheral,
    Placement,
    is_remote,
)
from kicad_mcp.utils.firmware.power_names import RAILS as _RAILS


def _inject_terminal_loci(intent: DesignIntent) -> None:
    """For every carded peripheral whose card declares ``realize: terminal``,
    inject a synthetic ``Placement(locus="remote")`` into ``intent.placements``
    (if no explicit user directive already exists for that ref).  Also stamps
    ``p.locus = "remote"`` on the ``Peripheral`` record.

    This MUST run before any template that inspects ``is_remote`` / ``_peripheral_is_remote``
    (i.e. before power_tree, device_config, etc.) so that glue suppression and
    placement exclusion work correctly for terminal-only devices — they are
    treated exactly like an explicitly-remote carded peripheral from this point.

    The honesty contract: the card itself IS the explicit declaration that this
    part is always a wire-out (G3 satisfied).  No board.yaml directive is
    required (and no ``placement_unsupported`` gap is raised) because the locus
    is injected here, not validated by _apply_placement."""
    for p in intent.peripherals:
        if p.ref in intent.placements:
            continue   # user override takes precedence — honor it
        if K.is_terminal_card_type(p.type):
            pl = Placement(locus="remote", device=p.value or p.type)
            intent.placements[p.ref] = pl
            p.locus = "remote"


def _first(signals: dict[str, int], *roles: str) -> Optional[int]:
    """First present role's GPIO, else None — preserving a GPIO of 0.
    ``signals.get(a) or signals.get(b)`` wrongly falls through when a maps to
    GPIO 0 (a valid ESP32 pin), silently dropping that subcircuit."""
    for r in roles:
        if r in signals:
            return signals[r]
    return None


@dataclass
class Expansion:
    components: list[Peripheral] = field(default_factory=list)
    power: list[tuple[str, Endpoint]] = field(default_factory=list)
    joins: list[tuple[str, Endpoint]] = field(default_factory=list)
    new_nets: list[Net] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)   # new gaps a template surfaces
    legends: list[ConnectorLegend] = field(default_factory=list)  # connector silk legends
    # Auto-emitted PCB placement hints keyed by connector ref. On-board module
    # headers get {"edge": "none"} so the autoplacer keeps them interior (vs.
    # field-wiring screw terminals, which get no hint -> the edge heuristic).
    placement_hints: dict[str, dict] = field(default_factory=dict)


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


def _emit_connector(
    ex: Expansion,
    alloc: RefAllocator,
    positions: list[ConnectorPosition],
    *,
    device: str,
    placement: Optional[Placement] = None,
    connector_type: str = "pin_header",
    value: Optional[str] = None,
) -> tuple[Peripheral, dict[str, Endpoint]]:
    """Synthesize a connector for ``positions`` (via the ONE §4 helper) and fold
    the common parts into ``ex``: the Peripheral into ``components``, every
    power-rail position onto ``power`` (so the rail-merge dedups it), and the silk
    legend into ``legends``. Returns ``(connector, {net_name: terminal_endpoint})``
    for the SIGNAL positions, so the caller wires each into a fresh net together
    with its near-side (MCU/amp) endpoint — avoiding the join-before-new_nets
    ordering hazard in ``expand_intent``. ``placement`` overrides the connector
    type/footprint from board.yaml."""
    ctype = placement.connector if placement and placement.connector else connector_type
    footprint = placement.footprint if placement else None
    conn, joins, legend = synthesize_connector(
        positions, alloc=alloc.next, device=device, connector_type=ctype,
        footprint=footprint, value=value,
    )
    ex.components.append(conn)
    ex.legends.append(legend)
    # On-board module headers (pin_header) belong interior, near their IC — not
    # at a board edge. Field-wiring terminals (screw_terminal / pluggable) get
    # NO hint, so the autoplacer's edge heuristic places them.
    if ctype == "pin_header":
        ex.placement_hints[conn.ref] = {"edge": "none"}
    signal_eps: dict[str, Endpoint] = {}
    for net_name, ep in joins:
        if net_name in _RAILS:
            ex.power.append((net_name, ep))   # rail tap (power delivered over wire)
        else:
            signal_eps[net_name] = ep
    return conn, signal_eps


def _peripheral_is_remote(intent: DesignIntent, p: Peripheral) -> bool:
    """A carded peripheral declared fully ``remote`` in board.yaml is not placed,
    so it gets no on-board support glue (its bypass/straps live at the device).
    Power is still delivered over the wire — via the terminal's +3V3/GND taps.

    Delegates to ``intent.is_remote`` (placements = the SINGLE source) so this
    glue-suppression check can never disagree with ``generate``'s placement-
    exclusion check (which also reads placements). ``Peripheral.locus`` is only a
    serialized display projection — never an independent behavioral source — so a
    hand-edited doc cannot land in the split state where a remote device is placed
    yet has its glue suppressed."""
    return is_remote(intent, p.ref)


def _ic_power_pins(intent: DesignIntent) -> list[tuple[str, list[str], list[str]]]:
    """(ref, supply_pin_names, ground_pin_names) for every placed IC. Remote
    peripherals are excluded — they are not on the board (glue suppression)."""
    out: list[tuple[str, list[str], list[str]]] = []
    if intent.mcu is not None:
        mi = K.resolve_mcu_by_part(intent.mcu.part)
        if mi is not None:
            out.append((intent.mcu.ref, [mi["supply_pin"]], [mi["ground_pin"]]))
    for p in intent.peripherals:
        if _peripheral_is_remote(intent, p):
            continue
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


def device_config(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Data-driven device configuration — strap each placed peripheral's address
    pins + apply its static ties, ALL read from the device card's ``config``
    (CLAUDE.md Rule 3: the card is the single source). Replaces the former
    per-device ``mcp23017_config`` / ``mpu6050_config`` templates: a new
    addressable device needs only a ``config.address_strap`` stanza in its card,
    no new template. Direct ties (no resistors) — correct for a fixed address.
    An out-of-range address is flagged as a gap, never silently strapped."""
    ex = Expansion()
    for p in intent.peripherals:
        if _peripheral_is_remote(intent, p):
            continue   # remote device configures itself off-board (glue suppression)
        info = K.resolve_peripheral(p.type)
        if info is None or "config" not in info:
            continue
        cfg = info["config"]
        strap = cfg.get("address_strap")
        if strap is not None and p.address is not None:
            straps = K.compute_address_straps(strap, p.address)
            if straps is None:
                ex.gaps.append(Gap(
                    "invalid_address",
                    f"{p.type} I2C address {hex(p.address)} is outside its "
                    f"strappable range; address pins not strapped.",
                ))
            else:
                for addr_pin, rail in straps:
                    ex.power.append((rail, Endpoint(ref=p.ref, pin=addr_pin)))
        # Fixed ties (e.g. MCP23017 RESET held high) — address-independent.
        for tie in cfg.get("static_ties", []) or []:
            ex.power.append((tie["rail"], Endpoint(ref=p.ref, pin=tie["pin"])))
    return ex


def _passive_net(name: str, *endpoints: Endpoint) -> Net:
    return Net(name=name, kind="passive", confidence="high", origin="template",
               endpoints=list(endpoints))


def usb_programming(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """USB-C + CP2102 programming block — makes the board flashable. Powers it
    from USB (VBUS→+5V, giving the +5V rail its source), and gives the MCU a
    UART console + auto-reset + boot/reset buttons. Fires for an ESP32.

    Landmines (verified): CP2102 VDD (pin 6) is the chip's LDO OUTPUT — it gets a
    bypass cap on its OWN net, never tied to +3V3. GND is on TWO pins (3 + EP 29)
    — wired by number. Auto-reset is the v5 scheme: a 100nF couples DTR→EN, with
    manual RESET/BOOT buttons (no transistors)."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mi = K.resolve_mcu_by_part(intent.mcu.part)
    if mi is None or mi["native_usb"]:
        return ex  # native-USB MCUs (ESP32-S3/C3) need no CP2102 bridge
    mcu = intent.mcu.ref

    cp = Peripheral(ref=alloc.next("U"), type="CP2102", lib_id=K.CP2102_LIB,
                    value="CP2102N", footprint=K.CP2102_FP, origin="template")
    usb = Peripheral(ref=alloc.next("J"), type="USB_C", lib_id=K.USB_C_LIB,
                     value="USB-C", footprint=K.USB_C_FP, origin="template")
    r_cc1 = _res(alloc, "5.1k", K.FP_R_0603_5K1)
    r_cc2 = _res(alloc, "5.1k", K.FP_R_0603_5K1)
    sw_rst = Peripheral(ref=alloc.next("SW"), type="SW", lib_id=K.SW_PUSH_LIB,
                        value="RESET", footprint=K.SW_PUSH_FP, origin="template")
    sw_boot = Peripheral(ref=alloc.next("SW"), type="SW", lib_id=K.SW_PUSH_LIB,
                         value="BOOT", footprint=K.SW_PUSH_FP, origin="template")
    c_vreg = _cap(alloc, "10uF", K.FP_C_BULK)
    c_vdd = _cap(alloc, "100nF", K.FP_C_BYPASS)
    c_dtr = _cap(alloc, "100nF", K.FP_C_BYPASS)
    ex.components += [cp, usb, r_cc1, r_cc2, sw_rst, sw_boot, c_vreg, c_vdd, c_dtr]

    # Power: USB VBUS -> +5V (the rail's source); CP2102 VREGIN/VBUS <- +5V.
    for vb in K.USB_C_VBUS_PINS:
        ex.power.append(("+5V", Endpoint(ref=usb.ref, pin=vb)))
    ex.power += [("+5V", Endpoint(ref=cp.ref, pin=K.CP2102_VREGIN)),
                 ("+5V", Endpoint(ref=cp.ref, pin=K.CP2102_VBUS)),
                 ("+5V", Endpoint(ref=c_vreg.ref, pin="1"))]
    # Ground: USB GND, CP2102 GND (pin 3 + EP 29 by NUMBER), cap returns,
    # CC resistor returns, switch returns.
    for g in K.USB_C_GND_PINS:
        ex.power.append(("GND", Endpoint(ref=usb.ref, pin=g)))
    for g in K.CP2102_GND_PINS:
        ex.power.append(("GND", Endpoint(ref=cp.ref, pin=g)))
    ex.power += [("GND", Endpoint(ref=c_vreg.ref, pin="2")),
                 ("GND", Endpoint(ref=c_vdd.ref, pin="2")),
                 ("GND", Endpoint(ref=r_cc1.ref, pin="2")),
                 ("GND", Endpoint(ref=r_cc2.ref, pin="2")),
                 ("GND", Endpoint(ref=sw_rst.ref, pin="2")),
                 ("GND", Endpoint(ref=sw_boot.ref, pin="2"))]

    ex.new_nets += [
        _passive_net("CC1", Endpoint(ref=usb.ref, pin=K.USB_C_CC1), Endpoint(ref=r_cc1.ref, pin="1")),
        _passive_net("CC2", Endpoint(ref=usb.ref, pin=K.USB_C_CC2), Endpoint(ref=r_cc2.ref, pin="1")),
        _passive_net("USB_DP", Endpoint(ref=usb.ref, pin=K.USB_C_DP), Endpoint(ref=cp.ref, pin=K.CP2102_DP)),
        _passive_net("USB_DM", Endpoint(ref=usb.ref, pin=K.USB_C_DM), Endpoint(ref=cp.ref, pin=K.CP2102_DM)),
        # CP2102 VDD is its LDO OUTPUT — bypass only, on its own net.
        _passive_net("CP2102_VDD", Endpoint(ref=cp.ref, pin=K.CP2102_VDD_OUT), Endpoint(ref=c_vdd.ref, pin="1")),
        # UART console crossover: CP2102 TXD -> MCU RX, CP2102 RXD <- MCU TX.
        _passive_net("UART_RX", Endpoint(ref=cp.ref, pin=K.CP2102_TXD), Endpoint(ref=mcu, pin=mi["uart_rx_pin"])),
        _passive_net("UART_TX", Endpoint(ref=cp.ref, pin=K.CP2102_RXD), Endpoint(ref=mcu, pin=mi["uart_tx_pin"])),
        # Auto-reset coupling: DTR --100nF--> EN.
        _passive_net("DTR", Endpoint(ref=cp.ref, pin=K.CP2102_DTR), Endpoint(ref=c_dtr.ref, pin="1")),
    ]
    # Join the EN / BOOT nets that mcu_straps created (pull-up resistors).
    ex.joins += [
        ("MCU_EN", Endpoint(ref=c_dtr.ref, pin="2")),     # DTR coupling cap
        ("MCU_EN", Endpoint(ref=sw_rst.ref, pin="1")),    # RESET button
        ("MCU_BOOT", Endpoint(ref=sw_boot.ref, pin="1")),  # BOOT button
    ]
    return ex


def _decide_part(bus: Bus, template_part: str, ex: Expansion) -> tuple[bool, str]:
    """Part-resolution decision (C6) for a bus whose on-board template builds
    ``template_part``. Returns ``(place, origin)`` and stamps the bus + emits the
    honest gap. The three cases — the whole point of part resolution:

      * ``part_provenance == "ambiguous"`` — firmware named MORE THAN ONE
        candidate part → hold the bus unrealized. import_firmware already disclosed
        it with a ``part_ambiguous`` gap, so DO NOT assume a default here (the old
        code fell into the ``resolved_part is None`` branch and emitted a
        contradictory ``assumed_part`` gap — "firmware names no specific part" —
        then placed the default despite the named candidates).
      * ``resolved_part is None`` — firmware named no part for this bus → ASSUME
        ``template_part`` (place it, origin ``template``, ``part_is_assumption``),
        disclosed by an ``assumed_part`` gap.
      * ``resolved_part == template_part`` — firmware named exactly this part →
        place it as ``imported`` (provenance already ``corpus``/``user``).
      * ``resolved_part != template_part`` — firmware declares a DIFFERENT part
        this template can't build → DO NOT substitute ``template_part``; leave the
        bus unrealized and disclose a ``part_unavailable`` gap. (This is the
        SPH0645-for-INMP441 bug, refused.)
    """
    rp = bus.resolved_part
    if bus.part_provenance == "ambiguous":
        # >1 candidate named: held unrealized, never assumed. The part_ambiguous
        # gap from import_firmware is the disclosure; emitting another here would
        # duplicate it. The user disambiguates via board.yaml bus_part_overrides.
        return False, ""
    if rp is None:
        bus.resolved_part = template_part
        bus.part_provenance = "assumed"
        bus.part_is_assumption = True
        ex.gaps.append(Gap(
            "assumed_part",
            f"Bus {bus.name!r} ({bus.type}): firmware names no specific part; "
            f"assumed {template_part}. Set a board.yaml bus_part_overrides entry "
            f"if the real part differs."))
        return True, "template"
    if rp == template_part:
        return True, "imported"
    ex.gaps.append(Gap(
        "part_unavailable",
        f"Bus {bus.name!r} ({bus.type}): firmware declares {rp}, but no template "
        f"or symbol realizes it yet — NOT substituting {template_part}. The bus is "
        f"left unrealized until {rp} ships (add its device card + symbol)."))
    return False, ""


def i2s_output_amps(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each I2S-output bus -> a stereo MAX98357A pair (L/R) with shared BCLK/LRC,
    per-channel DIN via 1kΩ isolators, SD_MODE channel straps (L→GND, R→+3V3),
    decoupling, and a 2-pin speaker connector per channel (the unified §4 helper,
    so it carries a silk legend). The amps stay on-board; the speaker leads always
    cross out to a connector — a pin header by default, or a screw terminal when
    the bus is declared ``on_board_with_remote_io`` in board.yaml. GAIN is left at
    the 9 dB Hi-Z default (the GAIN GPIO stays a flagged orphan)."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mcu = intent.mcu.ref
    M = K.MAX98357A
    idx = 0
    for bus in intent.buses:
        if bus.type != "I2S_OUT":
            continue
        bclk_g = bus.signals.get("BCLK")
        lrc_g = _first(bus.signals, "LRC", "WS")
        din_g = bus.signals.get("DIN")
        if bclk_g is None or lrc_g is None or din_g is None:
            continue
        place, amp_origin = _decide_part(bus, "MAX98357A", ex)
        if not place:
            continue   # firmware declares a different amp we can't realize (gapped)
        pl = intent.placements.get(bus.name)
        spk_type = ("screw_terminal"
                    if pl is not None and pl.locus == "on_board_with_remote_io"
                    else "pin_header")
        b_net, l_net, d_net = f"I2S{idx}_BCLK", f"I2S{idx}_LRC", f"I2S{idx}_DIN"
        # MCU drives the shared clocks + data bus.
        ex.joins += [(b_net, Endpoint(ref=mcu, gpio=bclk_g)),
                     (l_net, Endpoint(ref=mcu, gpio=lrc_g)),
                     (d_net, Endpoint(ref=mcu, gpio=din_g))]
        for side, sd_rail in (("L", "GND"), ("R", "+3V3")):
            amp = Peripheral(ref=alloc.next("U"), type="MAX98357A", lib_id=M["lib_id"],
                             value="MAX98357A", footprint=M["footprint"], origin=amp_origin)
            ex.components.append(amp)
            for p in K.MAX98357A_VDD_PINS:
                ex.power.append(("+3V3", Endpoint(ref=amp.ref, pin=p)))
            for p in K.MAX98357A_GND_PINS:
                ex.power.append(("GND", Endpoint(ref=amp.ref, pin=p)))
            ex.power.append((sd_rail, Endpoint(ref=amp.ref, pin=M["sd_mode"])))
            ex.joins += [(b_net, Endpoint(ref=amp.ref, pin=M["bclk"])),
                         (l_net, Endpoint(ref=amp.ref, pin=M["lrclk"]))]
            # DIN via a 1k series isolator (MCU DIN bus -> R -> this amp's DIN).
            r = _res(alloc, "1k", K.FP_R_0603)
            ex.components.append(r)
            ex.joins.append((d_net, Endpoint(ref=r.ref, pin="1")))
            ex.new_nets.append(_passive_net(f"I2S{idx}{side}_DIN",
                Endpoint(ref=r.ref, pin="2"), Endpoint(ref=amp.ref, pin=M["din"])))
            # speaker connector (unified §4 helper -> carries a silk legend).
            p_net, n_net = f"SPK{idx}{side}_P", f"SPK{idx}{side}_N"
            # Honor a board.yaml connector/footprint override only on the realized
            # remote_io path (passing placement when locus is on_board would let
            # its default connector="screw_terminal" override the pin-header default).
            spk_pl = pl if pl is not None and pl.locus == "on_board_with_remote_io" else None
            _spk, te = _emit_connector(
                ex, alloc,
                [ConnectorPosition(p_net, f"{side}+"),
                 ConnectorPosition(n_net, f"{side}-")],
                device=(pl.device if pl and pl.device else "Speaker"),
                placement=spk_pl, connector_type=spk_type, value=f"SPK{idx}{side}",
            )
            ex.new_nets += [
                _passive_net(p_net, Endpoint(ref=amp.ref, pin=M["outp"]), te[p_net]),
                _passive_net(n_net, Endpoint(ref=amp.ref, pin=M["outn"]), te[n_net]),
            ]
        # decoupling for the amp-pair supply
        cb, cbu = _cap(alloc, "100nF", K.FP_C_BYPASS), _cap(alloc, "10uF", K.FP_C_BULK)
        ex.components += [cb, cbu]
        for c in (cb, cbu):
            ex.power += [("+3V3", Endpoint(ref=c.ref, pin="1")),
                         ("GND", Endpoint(ref=c.ref, pin="2"))]
        idx += 1
    return ex


# Buildable I2S-input mics, keyed by the canonical resolved-part string the bus
# resolver produces (canonical_type: hyphen-stripped, upper — so "ICS-43434" → key
# "ICS43434"). A firmware-named mic in this registry is placed as that exact part;
# an EOL/unrecognized mic with no realization (e.g. INMP441 — no KiCad symbol) is
# deliberately absent, so _decide_part refuses to substitute the default rather
# than silently swapping parts.
_MIC_REALIZATIONS = {"SPH0645": K.SPH0645, "ICS43434": K.ICS43434}
_DEFAULT_MIC = "SPH0645"


def i2s_mic(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each I2S-input bus -> a MEMS mic (SEL/LR→GND, VDD bypass): the specific part
    the firmware names when it's one we can realize (SPH0645 or ICS-43434), else the
    SPH0645 default. A firmware-named mic with no realization (e.g. EOL INMP441) is
    refused, not substituted. UNLESS the bus is declared ``remote`` in board.yaml —
    then the mic is field-wired and the bus crosses to a screw terminal instead of an
    on-board chip (no chip, no bypass: the device's support glue lives at the device)."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mcu = intent.mcu.ref
    mic_idx = 0
    for bus in intent.buses:
        if bus.type != "I2S_IN":
            continue
        bclk_g = bus.signals.get("BCLK")
        ws_g = _first(bus.signals, "WS", "LRC")
        sd_g = _first(bus.signals, "SD", "DOUT")
        if bclk_g is None or ws_g is None or sd_g is None:
            continue
        # Net-name prefix: unindexed "MIC" for the first I2S-input bus (the common
        # single-mic board), then "MIC1", "MIC2", … for any additional buses. With
        # >1 I2S_IN bus, a shared "MIC_BCLK"/"MIC_WS"/"MIC_SD" would collide and
        # expand_intent's nets_by_name map would silently overwrite the first mic's
        # nets, orphaning its MCU-side endpoints. Mirrors i2s_output_amps' I2S{idx}.
        prefix = "MIC" if mic_idx == 0 else f"MIC{mic_idx}"
        mic_idx += 1
        pl = intent.placements.get(bus.name)
        if pl is not None and pl.locus == "remote":
            _emit_remote_mic(ex, alloc, mcu, bclk_g, ws_g, sd_g, pl, prefix)
            continue
        # Build the firmware-named mic if we can realize it; else fall back to the
        # default part so _decide_part either assumes it (rp is None) or refuses it
        # (rp names a different, unrealizable part — the INMP441 case).
        build_part = bus.resolved_part if bus.resolved_part in _MIC_REALIZATIONS else _DEFAULT_MIC
        S = _MIC_REALIZATIONS[build_part]
        place, mic_origin = _decide_part(bus, build_part, ex)
        if not place:
            continue   # firmware declares a different mic we can't realize (gapped)
        mic = Peripheral(ref=alloc.next("MK"), type=build_part, lib_id=S["lib_id"],
                         value=S["value"], footprint=S["footprint"], origin=mic_origin)
        c = _cap(alloc, "100nF", K.FP_C_BYPASS)
        ex.components += [mic, c]
        ex.power += [("+3V3", Endpoint(ref=mic.ref, pin=S["vdd"])),
                     ("GND", Endpoint(ref=mic.ref, pin=S["gnd"])),
                     ("GND", Endpoint(ref=mic.ref, pin=S["sel"])),  # SEL low = left
                     ("+3V3", Endpoint(ref=c.ref, pin="1")),
                     ("GND", Endpoint(ref=c.ref, pin="2"))]
        ex.new_nets += [
            _passive_net(f"{prefix}_BCLK", Endpoint(ref=mcu, gpio=bclk_g), Endpoint(ref=mic.ref, pin=S["bclk"])),
            _passive_net(f"{prefix}_WS", Endpoint(ref=mcu, gpio=ws_g), Endpoint(ref=mic.ref, pin=S["ws"])),
            _passive_net(f"{prefix}_SD", Endpoint(ref=mcu, gpio=sd_g), Endpoint(ref=mic.ref, pin=S["data"])),
        ]
    return ex


def _emit_remote_mic(
    ex: Expansion, alloc: RefAllocator, mcu: str,
    bclk_g: int, ws_g: int, sd_g: int, pl: Placement, prefix: str = "MIC",
) -> None:
    """Field-wired I2S mic: a screw terminal carries the three I2S signals plus a
    +3V3/GND tap (power delivered over the wire). ``prefix`` namespaces the I2S
    nets per bus ("MIC", "MIC1", …) so multiple remote mics don't collide."""
    device = pl.device or "I2S mic"
    bclk_net, ws_net, sd_net = f"{prefix}_BCLK", f"{prefix}_WS", f"{prefix}_SD"
    positions = [
        ConnectorPosition(bclk_net, "BCLK"),
        ConnectorPosition(ws_net, "WS"),
        ConnectorPosition(sd_net, "SD"),
        ConnectorPosition("+3V3", "+3V3"),
        ConnectorPosition("GND", "GND"),
    ]
    conn, te = _emit_connector(ex, alloc, positions, device=device, placement=pl,
                               connector_type="screw_terminal")
    ex.new_nets += [
        _passive_net(bclk_net, Endpoint(ref=mcu, gpio=bclk_g), te[bclk_net]),
        _passive_net(ws_net, Endpoint(ref=mcu, gpio=ws_g), te[ws_net]),
        _passive_net(sd_net, Endpoint(ref=mcu, gpio=sd_g), te[sd_net]),
    ]
    ex.gaps.append(Gap(
        "remote_device",
        f"{device} (I2S mic): remote — field-wired to {conn.ref} "
        f"(5-pos screw terminal); sourced off-board.",
        resolved=True, resolved_by="placement", resolved_components=[conn.ref],
    ))


def i2c_device_header(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each I2C bus with no recognized IC -> a 4-pin module header
    (GND/+3V3/SCL/SDA) + 4.7k pull-ups. Covers the OLED and any unknown I2C
    module. Routed through the §4 ``_emit_connector`` helper so the header carries
    a per-pad silk legend like every other synthesized connector — the module
    header was the last connector class still bypassing the legend path."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mcu = intent.mcu.ref
    n = 0
    for bus in intent.buses:
        if bus.type != "I2C":
            continue
        sda_g, scl_g = bus.signals.get("SDA"), bus.signals.get("SCL")
        if sda_g is None or scl_g is None:
            continue
        scl_net, sda_net = f"I2C{n}_SCL", f"I2C{n}_SDA"
        # Positions in pad order 1..4 (GND, +3V3, SCL, SDA) — same pinout the bare
        # header used. The rails route to ``ex.power`` as taps; the signal pads come
        # back in ``sig`` for the passive nets below. ``_emit_connector`` also adds
        # the connector to ``ex.components``, stamps the interior ``edge:none`` hint,
        # and appends the silk legend. OLED / I2C breakout → interior, near its bus.
        positions = [
            ConnectorPosition("GND", "GND"),
            ConnectorPosition("+3V3", "+3V3"),
            ConnectorPosition(scl_net, "SCL"),
            ConnectorPosition(sda_net, "SDA"),
        ]
        j, sig = _emit_connector(ex, alloc, positions, device=f"I2C{n}",
                                 connector_type="pin_header")
        r_sda, r_scl = _res(alloc, "4.7k", K.FP_R_0603), _res(alloc, "4.7k", K.FP_R_0603)
        ex.components += [r_sda, r_scl]
        ex.power += [("+3V3", Endpoint(ref=r_sda.ref, pin="2")),
                     ("+3V3", Endpoint(ref=r_scl.ref, pin="2"))]
        ex.new_nets += [
            _passive_net(scl_net, Endpoint(ref=mcu, gpio=scl_g),
                         sig[scl_net], Endpoint(ref=r_scl.ref, pin="1")),
            _passive_net(sda_net, Endpoint(ref=mcu, gpio=sda_g),
                         sig[sda_net], Endpoint(ref=r_sda.ref, pin="1")),
        ]
        n += 1
    return ex


def uart_device_header(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each UART bus -> a 4-pin module header (GND/VCC/TX/RX) + bypass.
    Header TX -> MCU RX, header RX <- MCU TX (crossover). A bus declared
    ``remote`` in board.yaml crosses to a screw terminal instead (field-wired
    sensor, e.g. an LD2410 on flying leads)."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mcu = intent.mcu.ref
    n = 0
    for bus in intent.buses:
        if bus.type != "UART":
            continue
        rx_g = _first(bus.signals, "RX", "RXD")
        tx_g = _first(bus.signals, "TX", "TXD")
        if rx_g is None or tx_g is None:
            continue
        pl = intent.placements.get(bus.name)
        if pl is not None and pl.locus == "remote":
            _emit_remote_uart(ex, alloc, mcu, n, rx_g, tx_g, pl)
            n += 1
            continue
        rx_net, tx_net = f"UART{n}_RX", f"UART{n}_TX"
        # Positions in pad order 1..4 (GND, +3V3, RX, TX) — same pinout the bare
        # header used. ``_emit_connector`` adds the connector, taps the rails to
        # power, stamps the interior ``edge:none`` hint, and emits the silk legend
        # (matches i2c_device_header and _emit_remote_uart's RX/TX labels).
        positions = [
            ConnectorPosition("GND", "GND"),
            ConnectorPosition("+3V3", "+3V3"),
            ConnectorPosition(rx_net, "RX"),
            ConnectorPosition(tx_net, "TX"),
        ]
        j, sig = _emit_connector(ex, alloc, positions, device=f"UART{n}",
                                 connector_type="pin_header")
        c = _cap(alloc, "100nF", K.FP_C_BYPASS)
        ex.components += [c]
        ex.power += [("+3V3", Endpoint(ref=c.ref, pin="1")),
                     ("GND", Endpoint(ref=c.ref, pin="2"))]
        ex.new_nets += [
            _passive_net(rx_net, Endpoint(ref=mcu, gpio=rx_g), sig[rx_net]),
            _passive_net(tx_net, Endpoint(ref=mcu, gpio=tx_g), sig[tx_net]),
        ]
        n += 1
    return ex


def _emit_remote_uart(
    ex: Expansion, alloc: RefAllocator, mcu: str, n: int,
    rx_g: int, tx_g: int, pl: Placement,
) -> None:
    """Field-wired UART sensor: a screw terminal carries RX/TX + a +3V3/GND tap."""
    device = pl.device or "UART device"
    rx_net, tx_net = f"UART{n}_RX", f"UART{n}_TX"
    positions = [
        ConnectorPosition(rx_net, "RX"),
        ConnectorPosition(tx_net, "TX"),
        ConnectorPosition("+3V3", "+3V3"),
        ConnectorPosition("GND", "GND"),
    ]
    conn, te = _emit_connector(ex, alloc, positions, device=device, placement=pl,
                               connector_type="screw_terminal")
    ex.new_nets += [
        _passive_net(rx_net, Endpoint(ref=mcu, gpio=rx_g), te[rx_net]),
        _passive_net(tx_net, Endpoint(ref=mcu, gpio=tx_g), te[tx_net]),
    ]
    ex.gaps.append(Gap(
        "remote_device",
        f"{device} (UART): remote — field-wired to {conn.ref} "
        f"(4-pos screw terminal); sourced off-board.",
        resolved=True, resolved_by="placement", resolved_components=[conn.ref],
    ))


def _short_label(net_name: str, role: Optional[str]) -> str:
    """Silk label for a crossing signal: its firmware role if known, else the net
    name with a leading bus prefix trimmed (``I2C0_SDA`` -> ``SDA``)."""
    if role:
        return role
    if "_" in net_name:
        return net_name.rsplit("_", 1)[-1]
    return net_name


def remote_peripherals(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """A CARDED peripheral declared ``remote`` in board.yaml (keyed by its ref) is
    field-wired: every signal net it touches crosses to a screw terminal, plus a
    +3V3/GND tap. The peripheral stays in the intent (for the BOM) but is excluded
    from placement by the generator, so its own pin endpoints drop out and only
    the terminal is wired. Its support glue is already suppressed (``_ic_power_pins``
    / ``device_config`` skip it)."""
    ex = Expansion()
    for p in intent.peripherals:
        pl = intent.placements.get(p.ref)
        if pl is None or pl.locus != "remote":
            continue
        # Signal nets this peripheral touches (rails excluded — power crosses as a
        # tap, not a retarget). One position per net, first-seen label wins.
        seen: dict[str, str] = {}
        for net in intent.nets:
            if net.name in _RAILS:
                continue
            role = next((ep.role for ep in net.endpoints if ep.ref == p.ref), None)
            if any(ep.ref == p.ref for ep in net.endpoints) and net.name not in seen:
                seen[net.name] = _short_label(net.name, role)
        device = pl.device or p.value or p.type
        positions = [ConnectorPosition(nn, lbl) for nn, lbl in seen.items()]
        positions += [ConnectorPosition("+3V3", "+3V3"), ConnectorPosition("GND", "GND")]
        conn, te = _emit_connector(ex, alloc, positions, device=device, placement=pl,
                                   connector_type="screw_terminal")
        # Signal nets already exist (from import) — join the terminal onto them.
        for net_name, ep in te.items():
            ex.joins.append((net_name, ep))
        ex.gaps.append(Gap(
            "remote_device",
            f"{device} ({p.type}): remote — field-wired to {conn.ref} "
            f"({len(positions)}-pos screw terminal); sourced off-board.",
            resolved=True, resolved_by="placement", resolved_components=[conn.ref],
        ))
    return ex


# Max positions for a Phoenix screw terminal (Connector:Screw_Terminal_01x{N});
# beyond this `single` grouping falls back to a pin header. Mirrors
# connectors._PHOENIX_RANGE (2..16).
_SCREW_MAX = 16

_RAIL_FOR_POWER = {"3v3": "+3V3", "5v": "+5V", "none": None}

# The peripherals that SOURCE the +5V rail — power_tree's regulator and the USB
# programming block (the only two sites that emit ("+5V", ...) onto ex.power).
# Used to detect +5V availability: those templates run BEFORE expander_terminals
# and have already been added to intent.peripherals, but the rail NETS aren't
# merged until after the template loop, so checking intent.nets would always
# (wrongly) see no +5V.
_FIVE_V_SOURCES = frozenset({"AMS1117", "USB_C", "CP2102"})


def _emit_expander_terminal(
    ex: Expansion, alloc: RefAllocator, expander_ref: str,
    signals: list[tuple[str, str]], *, rail: Optional[str], device: str,
    value: str, force_header: bool = False,
) -> None:
    """Emit ONE terminal for ``signals`` (each ``(new_net_name, port_pin)``) plus
    the power/GND rail positions, and wire each signal into a NEW net joining the
    expander's port pin to the terminal position. Power rails are delivered as taps
    (folded onto ``ex.power`` by ``_emit_connector``)."""
    positions = [ConnectorPosition(net_name, pin) for net_name, pin in signals]
    if rail is not None:
        positions.append(ConnectorPosition(rail, rail))
    positions.append(ConnectorPosition("GND", "GND"))
    ctype = "pin_header" if (force_header or len(positions) > _SCREW_MAX) else "screw_terminal"
    _conn, sig_eps = _emit_connector(ex, alloc, positions, device=device,
                                     value=value, connector_type=ctype)
    for net_name, pin in signals:
        ex.new_nets.append(Net(
            name=net_name, kind="passive", confidence="high", origin="template",
            endpoints=[Endpoint(ref=expander_ref, pin=pin), sig_eps[net_name]],
        ))


def expander_terminals(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Tap a placed I/O-expander's floating GPA/GPB pins out to labeled screw
    terminal(s), per a board.yaml ``expander_terminals`` declaration. Each tapped
    port pin gets a NEW net ``{prefix}_{i}`` joining the expander pin to a terminal
    position; +power/GND are delivered over the terminal as rail taps. The
    expander's I2C/INT side is untouched (only GPA/GPB are tapped), and the MCP is
    a normally-placed peripheral so ``remote_peripherals`` never sees it.

    Honest-by-construction: only fires for a user declaration, and only when the
    named ref is actually a placed MCP23017 — otherwise a Gap, never a silent
    no-op or a board missing its sensor terminals."""
    ex = Expansion()
    if not intent.expander_terminals:
        return ex
    periph_by_ref = {p.ref: p for p in intent.peripherals}
    existing_net_names = {n.name for n in intent.nets}
    has_5v = any(p.type in _FIVE_V_SOURCES for p in intent.peripherals)

    for ref, spec in intent.expander_terminals.items():
        p = periph_by_ref.get(ref)
        if p is None or p.type != "MCP23017":
            ex.gaps.append(Gap(
                "expander_terminals_unresolved",
                f"board.yaml expander_terminals[{ref!r}] is not a placed MCP23017 "
                f"expander (got {p.type if p else 'no such ref'!r}); no terminals "
                "synthesized."))
            continue
        if not spec.ports:
            ex.gaps.append(Gap(
                "expander_terminals_empty",
                f"expander_terminals[{ref!r}]: no ports declared; nothing to tap."))
            continue

        # Power rail tap is declared topology (not a sensor inference). 5v needs the
        # +5V rail to exist (a regulator/USB block sources it) — else disclose +
        # drop to none, so we never hang a sourceless +5V pin on the terminal.
        rail = _RAIL_FOR_POWER[spec.power]
        if rail == "+5V" and not has_5v:
            ex.gaps.append(Gap(
                "expander_terminals_power",
                f"expander_terminals[{ref!r}]: power: 5v but no +5V rail on this "
                "board; wiring signal + GND only."))
            rail = None

        # One NEW net per tapped pin. Namespace on collision (the expand_intent
        # new_nets append has no guard) and disclose it, never silently duplicate.
        port_nets: list[tuple[str, str]] = []   # (net_name, port_pin)
        for i, pin in enumerate(spec.ports):
            base = f"{spec.net_prefix}_{i}"
            name = base
            if name in existing_net_names:
                name = f"{base}_{ref}"
                ex.gaps.append(Gap(
                    "expander_terminals_net_collision",
                    f"expander_terminals[{ref!r}]: net {base!r} already exists; "
                    f"using {name!r}."))
            existing_net_names.add(name)
            port_nets.append((name, pin))

        device = spec.device
        if spec.group == "per_sensor":
            for net_name, pin in port_nets:
                _emit_expander_terminal(ex, alloc, ref, [(net_name, pin)],
                                        rail=rail, device=device, value=device)
        elif spec.group == "per_bank":
            banks: dict[str, list[tuple[str, str]]] = {}
            for net_name, pin in port_nets:
                banks.setdefault(pin[:3], []).append((net_name, pin))   # GPA / GPB
            for bank in sorted(banks):
                _emit_expander_terminal(ex, alloc, ref, banks[bank], rail=rail,
                                        device=device, value=f"{device} {bank}")
        else:  # single
            total = len(port_nets) + (1 if rail else 0) + 1   # signals + power + GND
            force_header = total > _SCREW_MAX
            if force_header:
                ex.gaps.append(Gap(
                    "expander_terminals_single_overflow",
                    f"expander_terminals[{ref!r}]: 'single' needs {total} positions "
                    f"(> {_SCREW_MAX}-way screw terminal); using a pin header — "
                    "consider group: per_bank."))
            _emit_expander_terminal(ex, alloc, ref, port_nets, rail=rail,
                                    device=device, value=device, force_header=force_header)

        ex.gaps.append(Gap(
            "expander_terminals",
            f"expander_terminals[{ref!r}]: {len(spec.ports)} {device} port(s) tapped "
            f"to terminal(s) (group={spec.group}, power={spec.power}).",
            resolved=True, resolved_by="board.yaml"))
    return ex


_REGISTRY: list[tuple[str, Callable[[DesignIntent, RefAllocator], Expansion]]] = [
    ("power_tree", power_tree),
    ("decoupling", decoupling),
    ("i2c_pullups", i2c_pullups),
    ("mcu_straps", mcu_straps),
    ("usb_programming", usb_programming),
    ("i2s_output_amps", i2s_output_amps),
    ("i2s_mic", i2s_mic),
    ("i2c_device_header", i2c_device_header),
    ("uart_device_header", uart_device_header),
    ("device_config", device_config),
    ("remote_peripherals", remote_peripherals),
    # After remote_peripherals: the MCP is on-board (never remote), and power_tree
    # has already created the rails the +power taps merge into.
    ("expander_terminals", expander_terminals),
]


# --- expansion pass -----------------------------------------------------------

def expand_intent(intent: DesignIntent) -> DesignIntent:
    """Run all templates over ``intent`` (mutated in place and returned)."""
    # Inject synthetic remote placements for terminal-only cards BEFORE any
    # template runs, so is_remote() / _peripheral_is_remote() returns True for
    # them and glue suppression (power_tree, device_config) fires correctly.
    _inject_terminal_loci(intent)
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

        for g in ex.gaps:               # new gaps a template surfaces directly
            intent.gaps.append(g)

        intent.connector_legends.extend(ex.legends)   # synthesized-terminal silk
        # Auto-emitted PCB placement hints; never clobber a user/sidecar hint for
        # the same ref (precedence: board.yaml/param > auto-emit).
        for ref, hint in ex.placement_hints.items():
            intent.placement_hints.setdefault(ref, dict(hint))

        for kind in ex.resolved:
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
