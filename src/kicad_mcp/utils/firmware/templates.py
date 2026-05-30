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


def _header(alloc: RefAllocator, hdr: tuple[str, str], value: str) -> Peripheral:
    return Peripheral(ref=alloc.next("J"), type="HDR", lib_id=hdr[0], value=value,
                      footprint=hdr[1], origin="template")


def i2s_output_amps(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each I2S-output bus -> a stereo MAX98357A pair (L/R) with shared BCLK/LRC,
    per-channel DIN via 1kΩ isolators, SD_MODE channel straps (L→GND, R→+3V3),
    decoupling, and a 2-pin speaker header per channel. GAIN is left at the 9 dB
    Hi-Z default (the GAIN GPIO stays a flagged orphan)."""
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
        lrc_g = bus.signals.get("LRC") or bus.signals.get("WS")
        din_g = bus.signals.get("DIN")
        if bclk_g is None or lrc_g is None or din_g is None:
            continue
        b_net, l_net, d_net = f"I2S{idx}_BCLK", f"I2S{idx}_LRC", f"I2S{idx}_DIN"
        # MCU drives the shared clocks + data bus.
        ex.joins += [(b_net, Endpoint(ref=mcu, gpio=bclk_g)),
                     (l_net, Endpoint(ref=mcu, gpio=lrc_g)),
                     (d_net, Endpoint(ref=mcu, gpio=din_g))]
        for side, sd_rail in (("L", "GND"), ("R", "+3V3")):
            amp = Peripheral(ref=alloc.next("U"), type="MAX98357A", lib_id=M["lib_id"],
                             value="MAX98357A", footprint=M["footprint"], origin="template")
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
            # speaker header
            spk = _header(alloc, K.HDR_1X2, f"SPK{idx}{side}")
            ex.components.append(spk)
            ex.new_nets += [
                _passive_net(f"SPK{idx}{side}_P", Endpoint(ref=amp.ref, pin=M["outp"]),
                             Endpoint(ref=spk.ref, pin="1")),
                _passive_net(f"SPK{idx}{side}_N", Endpoint(ref=amp.ref, pin=M["outn"]),
                             Endpoint(ref=spk.ref, pin="2")),
            ]
        # decoupling for the amp-pair supply
        cb, cbu = _cap(alloc, "100nF", K.FP_C_BYPASS), _cap(alloc, "10uF", K.FP_C_BULK)
        ex.components += [cb, cbu]
        for c in (cb, cbu):
            ex.power += [("+3V3", Endpoint(ref=c.ref, pin="1")),
                         ("GND", Endpoint(ref=c.ref, pin="2"))]
        idx += 1
    return ex


def i2s_mic(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each I2S-input bus -> an SPH0645 MEMS mic (SEL→GND, VDD bypass)."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mcu = intent.mcu.ref
    S = K.SPH0645
    for bus in intent.buses:
        if bus.type != "I2S_IN":
            continue
        bclk_g = bus.signals.get("BCLK")
        ws_g = bus.signals.get("WS") or bus.signals.get("LRC")
        sd_g = bus.signals.get("SD") or bus.signals.get("DOUT")
        if bclk_g is None or ws_g is None or sd_g is None:
            continue
        mic = Peripheral(ref=alloc.next("MK"), type="SPH0645", lib_id=S["lib_id"],
                         value="SPH0645LM4H", footprint=S["footprint"], origin="template")
        c = _cap(alloc, "100nF", K.FP_C_BYPASS)
        ex.components += [mic, c]
        ex.power += [("+3V3", Endpoint(ref=mic.ref, pin=S["vdd"])),
                     ("GND", Endpoint(ref=mic.ref, pin=S["gnd"])),
                     ("GND", Endpoint(ref=mic.ref, pin=S["sel"])),  # SEL low = left
                     ("+3V3", Endpoint(ref=c.ref, pin="1")),
                     ("GND", Endpoint(ref=c.ref, pin="2"))]
        ex.new_nets += [
            _passive_net("MIC_BCLK", Endpoint(ref=mcu, gpio=bclk_g), Endpoint(ref=mic.ref, pin=S["bclk"])),
            _passive_net("MIC_WS", Endpoint(ref=mcu, gpio=ws_g), Endpoint(ref=mic.ref, pin=S["ws"])),
            _passive_net("MIC_SD", Endpoint(ref=mcu, gpio=sd_g), Endpoint(ref=mic.ref, pin=S["data"])),
        ]
    return ex


def i2c_device_header(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each I2C bus with no recognized IC -> a 4-pin module header
    (GND/VCC/SCL/SDA) + 4.7k pull-ups. Covers the OLED and any unknown I2C
    module."""
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
        j = _header(alloc, K.HDR_1X4, f"I2C{n}")
        r_sda, r_scl = _res(alloc, "4.7k", K.FP_R_0603), _res(alloc, "4.7k", K.FP_R_0603)
        ex.components += [j, r_sda, r_scl]
        ex.power += [("GND", Endpoint(ref=j.ref, pin="1")),
                     ("+3V3", Endpoint(ref=j.ref, pin="2")),
                     ("+3V3", Endpoint(ref=r_sda.ref, pin="2")),
                     ("+3V3", Endpoint(ref=r_scl.ref, pin="2"))]
        scl_net, sda_net = f"I2C{n}_SCL", f"I2C{n}_SDA"
        ex.new_nets += [
            _passive_net(scl_net, Endpoint(ref=mcu, gpio=scl_g),
                         Endpoint(ref=j.ref, pin="3"), Endpoint(ref=r_scl.ref, pin="1")),
            _passive_net(sda_net, Endpoint(ref=mcu, gpio=sda_g),
                         Endpoint(ref=j.ref, pin="4"), Endpoint(ref=r_sda.ref, pin="1")),
        ]
        n += 1
    return ex


def uart_device_header(intent: DesignIntent, alloc: RefAllocator) -> Expansion:
    """Each UART bus -> a 4-pin module header (GND/VCC/TX/RX) + bypass.
    Header TX -> MCU RX, header RX <- MCU TX (crossover)."""
    ex = Expansion()
    if intent.mcu is None:
        return ex
    mcu = intent.mcu.ref
    n = 0
    for bus in intent.buses:
        if bus.type != "UART":
            continue
        rx_g = bus.signals.get("RX") or bus.signals.get("RXD")
        tx_g = bus.signals.get("TX") or bus.signals.get("TXD")
        if rx_g is None or tx_g is None:
            continue
        j = _header(alloc, K.HDR_1X4, f"UART{n}")
        c = _cap(alloc, "100nF", K.FP_C_BYPASS)
        ex.components += [j, c]
        ex.power += [("GND", Endpoint(ref=j.ref, pin="1")),
                     ("+3V3", Endpoint(ref=j.ref, pin="2")),
                     ("+3V3", Endpoint(ref=c.ref, pin="1")),
                     ("GND", Endpoint(ref=c.ref, pin="2"))]
        ex.new_nets += [
            _passive_net(f"UART{n}_RX", Endpoint(ref=mcu, gpio=rx_g), Endpoint(ref=j.ref, pin="3")),
            _passive_net(f"UART{n}_TX", Endpoint(ref=mcu, gpio=tx_g), Endpoint(ref=j.ref, pin="4")),
        ]
        n += 1
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
