"""Tests for the peripheral-block templates + expansion pass.

The MCP23017 address→strap bit mapping is the highest silent-wrong risk, so it
gets at/below/above boundary coverage (CLAUDE.md threshold rule)."""
from __future__ import annotations

from collections import Counter
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
    i2c_device_header,
    i2c_pullups,
    i2s_mic,
    i2s_output_amps,
    mcp23017_config,
    mcu_straps,
    mpu6050_config,
    power_tree,
    uart_device_header,
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


# --- MPU-6050 AD0 strap: the same silent-wrong hotspot, single-bit version ----

def test_mpu_ad0_low_is_gnd():
    assert K.mpu6050_ad0_strap(0x68) == ("5", "GND")     # AD0 low -> base addr

def test_mpu_ad0_high_is_3v3():
    assert K.mpu6050_ad0_strap(0x69) == ("5", "+3V3")    # AD0 high -> base+1

@pytest.mark.parametrize("addr", [0x67, 0x6A, 0x00, 0x70])
def test_mpu_ad0_out_of_range_is_none(addr):
    # exactly one below, one above, and far-off addresses are NOT strappable
    assert K.mpu6050_ad0_strap(addr) is None


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


# --- MPU-6050 AD0 strapping (dual same-type devices on the bus) ---------------

_HUB = """\
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
#define MPU6050_ADDR 0x68
#define MPU6050_ADDR_2 0x69
#define OLED_ADDR 0x3C
#define BUZZER_PIN 25
"""


def _hub():
    return build_intent(partition(parse_defines(_HUB)),
                        firmware_path="c.h", board_id="esp32dev")


def test_mpu6050_config_straps_each_instance_by_address():
    i = _hub()
    ex = mpu6050_config(i, RefAllocator(i))
    assert not ex.components                             # direct ties, no parts
    # the 0x68 MPU's AD0 -> GND, the 0x69 MPU's AD0 -> +3V3 (per-instance)
    mpus = {p.ref: p.address for p in i.peripherals if p.type == "MPU6050"}
    ref_68 = next(r for r, a in mpus.items() if a == 0x68)
    ref_69 = next(r for r, a in mpus.items() if a == 0x69)
    straps = {(ep.ref, ep.pin, rail) for rail, ep in ex.power}
    assert (ref_68, K.MPU6050_AD0_PIN, "GND") in straps
    assert (ref_69, K.MPU6050_AD0_PIN, "+3V3") in straps

def test_mpu6050_config_invalid_address_flagged_not_strapped():
    # an MPU at an un-strappable address must be flagged, never silently tied.
    i = build_intent(partition(parse_defines(
        "#define I2C_SDA_PIN 21\n#define I2C_SCL_PIN 22\n#define MPU6050_ADDR 0x55\n")),
        firmware_path="c.h", board_id="esp32dev")
    expand_intent(i)
    assert any(g.kind == "invalid_address" and "MPU" in g.detail for g in i.gaps)

def test_hub_expand_routes_bus_and_pullups():
    # full expand on the hub: power tree resolved, I2C pull-ups joined, AD0 straps
    # present, board grows well past the bare import.
    i = _hub()
    n_before = len(i.peripherals)
    expand_intent(i)
    assert len(i.peripherals) > n_before
    resolved = {g.kind for g in i.gaps if g.resolved}
    assert {"power_tree", "decoupling", "pullups"} <= resolved


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


# --- bus-driven audio templates (axis 5) -------------------------------------

_AUDIO = """\
#define CMCA_I2S_BCLK 15
#define CMCA_I2S_LRC 16
#define CMCA_I2S_DIN 17
#define CMCA_I2S2_BCLK 35
#define CMCA_I2S2_LRC 36
#define CMCA_I2S2_DIN 37
#define CMCA_OLED_SDA 47
#define CMCA_OLED_SCL 48
#define CMCA_OLED_ADDR 0x3C
#define CMCA_PRESENCE_RX_PIN 18
#define CMCA_PRESENCE_TX_PIN 21
#define CMCA_MIC_BCLK 8
#define CMCA_MIC_WS 9
#define CMCA_MIC_SD 10
"""

def _audio():
    return build_intent(partition(parse_defines(_AUDIO)),
                        firmware_path="a.h", board_id="esp32-s3-devkitc-1")


def test_i2s_output_amps_stereo_pairs():
    i = _audio()
    ex = i2s_output_amps(i, RefAllocator(i))
    amps = [c for c in ex.components if c.type == "MAX98357A"]
    assert len(amps) == 4                       # 2 I2S-out buses x L/R
    spk = [c for c in ex.components if c.value.startswith("SPK")]
    assert len(spk) == 4                        # one speaker header per channel
    # shared BCLK net joins the MCU + both amps of bus 0
    assert len([ep for name, ep in ex.joins if name == "I2S0_BCLK"]) == 3

def test_i2s_amp_sd_mode_straps():
    i = _audio()
    ex = i2s_output_amps(i, RefAllocator(i))
    sd = K.MAX98357A["sd_mode"]
    rails = {rail for rail, ep in ex.power if ep.pin == sd}
    assert rails == {"GND", "+3V3"}             # L->GND, R->+3V3

def test_i2s_mic_instantiated():
    i = _audio()
    ex = i2s_mic(i, RefAllocator(i))
    assert [c.type for c in ex.components].count("SPH0645") == 1
    nets = {n.name for n in ex.new_nets}
    assert {"MIC_BCLK", "MIC_WS", "MIC_SD"} <= nets

def test_i2c_device_header_with_pullups():
    i = _audio()
    ex = i2c_device_header(i, RefAllocator(i))
    assert any(c.value.startswith("I2C") for c in ex.components)   # the header
    assert len([c for c in ex.components if c.value == "4.7k"]) == 2

def test_uart_device_header():
    i = _audio()
    ex = uart_device_header(i, RefAllocator(i))
    assert any(c.value.startswith("UART") for c in ex.components)
    assert {n.name for n in ex.new_nets} == {"UART0_RX", "UART0_TX"}

def test_usb_programming_skipped_for_native_usb():
    # ESP32-S3 has native USB -> no CP2102 bridge.
    assert usb_programming(_audio(), RefAllocator(_audio())).components == []

def test_audio_expand_grows_board():
    i = expand_intent(_audio())
    types = Counter(p.type for p in i.peripherals)
    assert types["MAX98357A"] == 4 and types["SPH0645"] == 1
    assert i.mcu.part == "ESP32-S3-WROOM-1"


# --- KiCad-gated audio E2E ---------------------------------------------------

def _audio_symbols_available() -> bool:
    # Older KiCad (e.g. the no-pcbnew CI matrix) may lack the S3/audio symbols;
    # the full audio E2E is covered by tests/integration on KiCad 9/10.
    try:
        from kicad_sch_api.library.cache import get_symbol_cache
        c = get_symbol_cache()
        return all(c.get_symbol(l) is not None for l in (
            "RF_Module:ESP32-S3-WROOM-1", "Audio:MAX98357A",
            "Sensor_Audio:SPH0645LM4H"))
    except Exception:
        return False


def test_generate_audio_board(tmp_path):
    if not _audio_symbols_available():
        pytest.skip("S3/audio KiCad symbols not available")
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

    intent = expand_intent(_audio())
    out = tmp_path / "audio.kicad_sch"
    res = generate_schematic(intent, str(out))
    assert res["status"] == "ok" and not res["unresolved_endpoints"]

    nl = extract_netlist_via_cli(str(out))
    if nl is None:
        pytest.skip("kicad-cli netlist export unavailable")
    def refs_on(net):
        return {x["component"] for x in nl["nets"].get(net, [])}
    from collections import Counter as _C
    vals = _C(c.get("value") for c in nl["components"].values())
    assert vals["MAX98357A"] == 4 and vals["ESP32-S3-WROOM-1"] == 1
    # I2S bus 0 joins the MCU (U1) and two amps; +3V3 powers the MCU
    assert "U1" in refs_on("I2S0_BCLK") and len(refs_on("I2S0_BCLK")) == 3
    assert "U1" in refs_on("+3V3")


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
