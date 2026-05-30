"""Tests for the peripheral-block templates + expansion pass.

The MCP23017 address→strap bit mapping is the highest silent-wrong risk, so it
gets at/below/above boundary coverage (CLAUDE.md threshold rule)."""
from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.firmware import knowledge as K
from kicad_mcp.utils.firmware.intent import (
    build_intent,
    from_dict,
    to_dict,
)
from kicad_mcp.utils.firmware.parse import parse_defines, partition
from kicad_mcp.utils.firmware.templates import (
    RefAllocator,
    decoupling,
    expand_intent,
    i2c_pullups,
    mcp23017_config,
    mcu_straps,
    power_tree,
    usb_programming,
)

SPEEDCAL_CONFIG = Path(
    "/Volumes/Files/claude/mr-esp32/speed-cal/firmware/include/config.h"
)

_SAMPLE = """\
#define I2C_SDA 21
#define I2C_SCL 22
#define MCP23017_ADDR 0x27
#define MCP23017_INT_PIN 13
#define HX711_DOUT_PIN 16
#define HX711_SCK_PIN 17
"""


def _intent():
    return build_intent(partition(parse_defines(_SAMPLE)),
                        firmware_path="c.h", board_id="esp32dev")


# --- the silent-wrong hotspot: address strapping -----------------------------

def test_address_strap_all_low():
    # 0x20 = base -> A2,A1,A0 all to GND
    assert K.mcp23017_address_straps(0x20) == [("A0", "GND"), ("A1", "GND"), ("A2", "GND")]

def test_address_strap_all_high():
    # 0x27 = base + 0b111 -> all to +3V3
    assert K.mcp23017_address_straps(0x27) == [("A0", "+3V3"), ("A1", "+3V3"), ("A2", "+3V3")]

def test_address_strap_mixed():
    # 0x24 = base + 0b100 -> A2 high, A1/A0 low
    assert K.mcp23017_address_straps(0x24) == [("A0", "GND"), ("A1", "GND"), ("A2", "+3V3")]

def test_address_strap_lsb_only():
    # 0x21 = base + 0b001 -> A0 high only
    assert K.mcp23017_address_straps(0x21) == [("A0", "+3V3"), ("A1", "GND"), ("A2", "GND")]

@pytest.mark.parametrize("addr", [0x1F, 0x28, 0x00, 0x50])
def test_address_strap_out_of_range_is_none(addr):
    assert K.mcp23017_address_straps(addr) is None


# --- ref allocation -----------------------------------------------------------

def test_ref_allocator_continues_past_existing():
    alloc = RefAllocator(_intent())   # has U1(mcu), U2/U3 peripherals
    assert alloc.next("U") == "U4"
    assert alloc.next("C") == "C1"
    assert alloc.next("R") == "R1"
    assert alloc.next("C") == "C2"


# --- individual templates -----------------------------------------------------

def test_power_tree_instantiates_ldo_and_caps():
    ex = power_tree(_intent(), RefAllocator(_intent()))
    types = [c.type for c in ex.components]
    assert "AMS1117" in types and types.count("C") == 3
    rails = {rail for rail, _ in ex.power}
    assert rails == {"+5V", "+3V3", "GND"}
    # every IC supply lands on +3V3 (MCU VDD + HX711 + MCP)
    plus3 = {(ep.ref, ep.pin) for rail, ep in ex.power if rail == "+3V3"}
    assert ("U1", "VDD") in plus3              # ESP32 supply by NAME
    assert "power_tree" in ex.resolved

def test_decoupling_one_cap_per_ic():
    ex = decoupling(_intent(), RefAllocator(_intent()))
    # MCU + 2 peripherals = 3 ICs -> 3 caps
    assert len([c for c in ex.components if c.type == "C"]) == 3
    assert "decoupling" in ex.resolved

def test_i2c_pullups_join_bus_and_pull_to_3v3():
    ex = i2c_pullups(_intent(), RefAllocator(_intent()))
    assert len(ex.components) == 2                       # SDA + SCL pull-ups
    joined = {name for name, _ in ex.joins}
    assert joined == {"I2C_SDA", "I2C_SCL"}             # onto the real I2C lines
    assert all(rail == "+3V3" for rail, _ in ex.power)
    assert "pullups" in ex.resolved

def test_mcu_straps_en_and_boot():
    ex = mcu_straps(_intent(), RefAllocator(_intent()))
    names = {n.name for n in ex.new_nets}
    assert names == {"MCU_EN", "MCU_BOOT"}
    # each strap net ties the MCU pin (by name) to a resistor
    en = next(n for n in ex.new_nets if n.name == "MCU_EN")
    assert any(e.ref == "U1" and e.pin == "EN" for e in en.endpoints)

def test_mcp23017_config_straps_for_0x27():
    ex = mcp23017_config(_intent(), RefAllocator(_intent()))
    assert not ex.components                             # direct ties, no parts
    # all three address pins on +3V3 (0x27) + RESET high
    plus3_pins = {ep.pin for rail, ep in ex.power if rail == "+3V3"}
    assert {"A0", "A1", "A2", K.MCP23017_RESET_PIN} <= plus3_pins


# --- CP2102 USB programming block (highest-landmine template) -----------------

def test_usb_programming_parts():
    ex = usb_programming(_intent(), RefAllocator(_intent()))
    types = [c.type for c in ex.components]
    assert types.count("CP2102") == 1 and types.count("USB_C") == 1
    assert types.count("SW") == 2 and types.count("R") == 2 and types.count("C") == 3
    assert all(c.footprint for c in ex.components)       # PCB-ready

def test_usb_cp2102_vdd_isolated_not_3v3():
    # LANDMINE: CP2102 VDD is the chip's LDO OUTPUT — must NOT join +3V3.
    ex = usb_programming(_intent(), RefAllocator(_intent()))
    cp = next(c for c in ex.components if c.type == "CP2102")
    assert not any(ep.ref == cp.ref for rail, ep in ex.power if rail == "+3V3")
    vdd = next(n for n in ex.new_nets if n.name == "CP2102_VDD")
    assert any(e.ref == cp.ref and e.pin == "VDD" for e in vdd.endpoints)

def test_usb_cp2102_dual_ground_pins():
    # LANDMINE: GND is on pin 3 AND the EP pad pin 29 (both named "GND").
    ex = usb_programming(_intent(), RefAllocator(_intent()))
    cp = next(c for c in ex.components if c.type == "CP2102")
    gnd = {(ep.ref, ep.pin) for rail, ep in ex.power if rail == "GND"}
    assert (cp.ref, "3") in gnd and (cp.ref, "29") in gnd

def test_usb_vbus_sources_5v():
    ex = usb_programming(_intent(), RefAllocator(_intent()))
    usb = next(c for c in ex.components if c.type == "USB_C")
    p5 = {(ep.ref, ep.pin) for rail, ep in ex.power if rail == "+5V"}
    assert all((usb.ref, vb) in p5 for vb in ("A4", "A9", "B4", "B9"))

def test_usb_uart_crossover():
    ex = usb_programming(_intent(), RefAllocator(_intent()))
    cp = next(c for c in ex.components if c.type == "CP2102")
    rx = next(n for n in ex.new_nets if n.name == "UART_RX")
    tx = next(n for n in ex.new_nets if n.name == "UART_TX")
    assert any(e.ref == cp.ref and e.pin == "TXD" for e in rx.endpoints)
    assert any(e.ref == "U1" and e.pin == "RXD0/IO3" for e in rx.endpoints)
    assert any(e.ref == cp.ref and e.pin == "RXD" for e in tx.endpoints)
    assert any(e.ref == "U1" and e.pin == "TXD0/IO1" for e in tx.endpoints)

def test_usb_joins_en_and_boot():
    ex = usb_programming(_intent(), RefAllocator(_intent()))
    assert {name for name, _ in ex.joins} == {"MCU_EN", "MCU_BOOT"}


# --- expansion pass -----------------------------------------------------------

def test_expand_grows_intent_and_resolves_gaps():
    intent = expand_intent(_intent())
    assert len(intent.peripherals) > 2                  # template parts added
    resolved = {g.kind: g for g in intent.gaps if g.resolved}
    assert {"power_tree", "decoupling", "pullups"} <= set(resolved)
    # provenance recorded
    assert resolved["power_tree"].resolved_by == "power_tree"
    assert resolved["power_tree"].resolved_components

def test_expand_merges_power_rails():
    intent = expand_intent(_intent())
    power_nets = [n for n in intent.nets if n.kind == "power"]
    names = {n.name for n in power_nets}
    assert names == {"+5V", "+3V3", "GND"}              # one net per rail
    plus3 = next(n for n in power_nets if n.name == "+3V3")
    # endpoints deduped (no repeated ref/pin)
    keys = [(e.ref, e.pin) for e in plus3.endpoints]
    assert len(keys) == len(set(keys))

def test_expanded_intent_round_trips(tmp_path):
    intent = expand_intent(_intent())
    assert to_dict(from_dict(to_dict(intent))) == to_dict(intent)

def test_template_components_carry_footprints():
    intent = expand_intent(_intent())
    tmpl = [p for p in intent.peripherals if p.origin == "template"]
    assert tmpl and all(p.footprint for p in tmpl)      # PCB-ready


# --- KiCad-gated full E2E -----------------------------------------------------

def _kicad_available() -> bool:
    try:
        from kicad_sch_api.library.cache import get_symbol_cache
        return get_symbol_cache().get_symbol("RF_Module:ESP32-WROOM-32E") is not None
    except Exception:
        return False


@pytest.mark.skipif(not _kicad_available(), reason="KiCad symbol libraries not available")
@pytest.mark.skipif(not SPEEDCAL_CONFIG.exists(), reason="speed-cal config.h not present")
def test_generate_expanded_speedcal(tmp_path):
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import find_board_id
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

    parsed = partition(parse_defines(SPEEDCAL_CONFIG.read_text()))
    intent = expand_intent(build_intent(parsed, firmware_path=str(SPEEDCAL_CONFIG),
                                        board_id=find_board_id(str(SPEEDCAL_CONFIG))))
    out = tmp_path / "expanded.kicad_sch"
    res = generate_schematic(intent, str(out))
    assert res["status"] == "ok" and not res["unresolved_endpoints"]
    assert res["components_placed"] >= 20               # 3 ICs + power + USB block

    nl = extract_netlist_via_cli(str(out))
    def members(name):
        return {f"{x['component']}.{x['pin']}" for x in nl["nets"].get(name, [])}

    p3, gnd, p5 = members("+3V3"), members("GND"), members("+5V")
    assert "U1.2" in p3                                  # ESP32 VDD on +3V3
    assert {"U1.1", "U1.15", "U1.38", "U1.39"} <= gnd   # all ESP32 GND pads
    # MCP 0x27 address straps + reset on +3V3 (pins 15/16/17/18)
    assert {"U3.15", "U3.16", "U3.17", "U3.18", "U3.9"} <= p3
    # the MCP_INT signal net is NOT polluted into a rail
    assert members("MCP23017_INT") == {"U1.16", "U3.20"}

    # --- CP2102 programming block (U5=CP2102, J1=USB-C — deterministic refs) ---
    assert {"J1.A4", "J1.A9", "J1.B4", "J1.B9", "U4.3"} <= p5  # +5V USB-sourced
    assert "U5.6" not in p3                              # LANDMINE: CP2102 VDD not +3V3
    assert "U5.6" in members("CP2102_VDD")              # ...it's on its own net
    assert {"U5.3", "U5.29"} <= gnd                      # LANDMINE: both CP2102 GND pins
    assert members("UART_RX") == {"U1.34", "U5.26"}     # CP2102 TXD -> ESP32 RX
    assert members("UART_TX") == {"U1.35", "U5.25"}     # CP2102 RXD <- ESP32 TX
