"""Seed knowledge base — the single source of truth (CLAUDE.md Rule 3) for:

  * MCU board-id  -> symbol/part        (``resolve_mcu``)
  * peripheral type -> symbol + bus + role→pin-name map (``resolve_peripheral``)

Deliberately small and explicit. A firmware ``#define`` names a *role*
(``HX711_SCK_PIN``, role ``SCK``); the device symbol names the *pin*. These are
NOT always equal — e.g. the MCP23017 symbol's I2C-clock pin is named ``SCK`` and
its data pin ``SDA``; the HX711 clock pin is named ``PD_SCK``. The ``roles`` map
below captures that firmware-role → symbol-pin-name translation per peripheral,
so no caller assumes identity. (Pin *names* verified against the symbols used in
the speed-cal v5 netlist.)

Unknown MCUs / peripherals resolve to ``None`` — the importer turns that into an
explicit gap rather than guessing.
"""
from __future__ import annotations

from typing import Optional, TypedDict


class McuInfo(TypedDict):
    part: str
    lib_id: str
    value: str
    footprint: str
    needs_3v3: bool      # board needs a 3V3 supply (power_tree template fires)
    supply_pin: str      # pin NAME for the 3V3 supply
    ground_pin: str      # pin NAME for ground (ESP32: the merged "GND" pin)
    en_pin: str          # chip-enable pin NAME (needs pull-up)
    boot_pin: str        # boot/strap pin NAME (needs pull-up)
    uart_rx_pin: str     # console UART RX pin NAME (<- USB bridge TXD)
    uart_tx_pin: str     # console UART TX pin NAME (-> USB bridge RXD)
    native_usb: bool     # True if the MCU has native USB (no CP2102 bridge needed)


class PeripheralInfo(TypedDict):
    lib_id: str
    value: str
    bus: Optional[str]
    footprint: str
    # firmware signal-role (upper-cased) -> symbol pin NAME
    roles: dict[str, str]
    supply_pins: list[str]   # pin NAMES tied to +3V3
    ground_pins: list[str]   # pin NAMES tied to GND
    # True if this entry models the device as its breakout-MODULE header
    # (e.g. a GY-521 / I2C OLED plugged onto a carrier) rather than a chip-down
    # IC. Firmware names the device; "module vs chip-down" is a design choice we
    # make explicit (build_intent flags it), same spirit as the AMS1117 default.
    module: bool


# ESP32-WROOM-32E facts. GND is the symbol's MERGED pin (physical pads
# 1/15/38/39 collapsed) — resolve it by NAME, never by number.
_ESP32: McuInfo = {
    "part": "ESP32-WROOM-32E", "lib_id": "RF_Module:ESP32-WROOM-32E",
    "value": "ESP32-WROOM-32E", "footprint": "RF_Module:ESP32-WROOM-32E",
    "needs_3v3": True, "supply_pin": "VDD", "ground_pin": "GND",
    "en_pin": "EN", "boot_pin": "IO0",
    "uart_rx_pin": "RXD0/IO3", "uart_tx_pin": "TXD0/IO1",
    "native_usb": False,   # WROOM-32 needs a CP2102 USB-UART bridge
}

# ESP32-S3-WROOM-1. The KiCad symbol names GPIOs as IO{n} (same convention as
# WROOM-32), so the IO{n} mapper works; UART0 pins are named RXD0/TXD0.
_ESP32S3: McuInfo = {
    "part": "ESP32-S3-WROOM-1", "lib_id": "RF_Module:ESP32-S3-WROOM-1",
    "value": "ESP32-S3-WROOM-1", "footprint": "RF_Module:ESP32-S3-WROOM-1",
    "needs_3v3": True, "supply_pin": "3V3", "ground_pin": "GND",
    "en_pin": "EN", "boot_pin": "IO0",
    "uart_rx_pin": "RXD0", "uart_tx_pin": "TXD0",
    "native_usb": True,    # S3 has native USB — no CP2102 bridge
}

# --- audio peripherals + headers (verified pins; audio-node ground-truth FPs) -
MAX98357A: dict[str, str] = {
    "lib_id": "Audio:MAX98357A", "value": "MAX98357A",
    "footprint": "Package_DFN_QFN:HVQFN-16-1EP_3x3mm_P0.5mm_EP1.5x1.5mm",
    "din": "DIN", "bclk": "BCLK", "lrclk": "LRCLK", "sd_mode": "~{SD_MODE}",
    "outp": "OUTP", "outn": "OUTN", "gain": "GAIN_SLOT",
}
MAX98357A_VDD_PINS = ["7", "8"]
MAX98357A_GND_PINS = ["3", "11", "15", "17"]   # GND pins + EP pad 17
SPH0645 = {
    "lib_id": "Sensor_Audio:SPH0645LM4H", "value": "SPH0645LM4H",
    "footprint": "Sensor_Audio:Knowles_SPH0645LM4H-6_3.5x2.65mm",
    "ws": "WS", "bclk": "BCLK", "data": "DATA", "sel": "SEL",
    "vdd": "VDD", "gnd": "GND",
}
# Generic module headers (lib_id, footprint). Pin numbers are "1".."N".
HDR_1X2 = ("Connector_Generic:Conn_01x02",
           "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical")
HDR_1X4 = ("Connector_Generic:Conn_01x04",
           "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
HDR_1X5 = ("Connector_Generic:Conn_01x05",
           "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical")

# board-id substrings (from platformio.ini ``board =``) -> MCU. MORE-SPECIFIC
# keys FIRST: resolve_mcu does substring matching, so `esp32-s3` must precede the
# generic `esp32` or an S3 board would mis-resolve to WROOM-32.
_MCUS: dict[str, McuInfo] = {
    "esp32-s3-devkitc-1": _ESP32S3, "esp32-s3": _ESP32S3, "esp32s3": _ESP32S3,
    "esp32dev": _ESP32, "esp32": _ESP32,
}

_PERIPHERALS: dict[str, PeripheralInfo] = {
    "MCP23017": {
        "lib_id": "Interface_Expansion:MCP23017x-x-SO",
        "value": "MCP23017", "bus": "I2C",
        "footprint": "Package_SO:SOIC-28W_7.5x17.9mm_P1.27mm",
        # NB: I2C clock pin is named "SCK" on this symbol, not "SCL".
        "roles": {"SDA": "SDA", "SCL": "SCK", "INT": "INTA", "INTA": "INTA"},
        "supply_pins": ["V_{DD}"], "ground_pins": ["V_{SS}"], "module": False,
    },
    "HX711": {
        "lib_id": "Analog_ADC:HX711",
        "value": "HX711", "bus": None,
        "footprint": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
        "roles": {"DOUT": "DOUT", "SCK": "PD_SCK", "PD_SCK": "PD_SCK"},
        # HX711 has no separate DGND — analog + digital ground share AGND.
        "supply_pins": ["VSUP", "AVDD", "DVDD"], "ground_pins": ["AGND"],
        "module": False,
    },
    # --- I2C breakout modules (carrier-board representation) ------------------
    # A GY-521 (MPU-6050) module exposes a header; we model the connection, not
    # the bare QFN (whose charge-pump/REGOUT support firmware can't inform).
    # Header pin convention (matches i2c_device_header): 1=GND 2=VCC 3=SCL 4=SDA,
    # plus 5=AD0 for I2C address selection.
    "MPU6050": {
        "lib_id": HDR_1X5[0], "value": "GY-521 (MPU-6050)", "bus": "I2C",
        "footprint": HDR_1X5[1],
        "roles": {"SDA": "4", "SCL": "3"},
        "supply_pins": ["2"], "ground_pins": ["1"], "module": True,
    },
    # I2C OLED module (SSD1306/SSD1315). 4-pin module: 1=GND 2=VCC 3=SCL 4=SDA.
    "OLED": {
        "lib_id": HDR_1X4[0], "value": "OLED (SSD1306)", "bus": "I2C",
        "footprint": HDR_1X4[1],
        "roles": {"SDA": "4", "SCL": "3"},
        "supply_pins": ["2"], "ground_pins": ["1"], "module": True,
    },
}

# MPU-6050 I2C address strapping: a single AD0 pin selects the LSB of the
# address — AD0=low -> base 0x68, AD0=high -> 0x69. (GY-521 header pin 5 = AD0.)
# Single source of truth for the address->AD0-rail map (boundary-tested).
MPU6050_AD0_PIN = "5"
MPU6050_ADDR_BASE = 0x68


def mpu6050_ad0_strap(address: int) -> Optional[tuple[str, str]]:
    """Return ``(ad0_pin_name, rail)`` for an MPU-6050 I2C address, or None if
    the address is outside the strappable pair 0x68/0x69. AD0 ties to "GND"
    (0x68) or "+3V3" (0x69)."""
    if address == MPU6050_ADDR_BASE:
        return (MPU6050_AD0_PIN, "GND")
    if address == MPU6050_ADDR_BASE + 1:
        return (MPU6050_AD0_PIN, "+3V3")
    return None

# --- verified footprints (speed-cal v5 BOM) for template-placed passives ---
LIB_C = "Device:C"
LIB_R = "Device:R"
FP_C_BULK = "Capacitor_SMD:C_0805_2012Metric"     # 10µF
FP_C_BYPASS = "Capacitor_SMD:C_0603_1608Metric"   # 100nF
FP_R_0603 = "Resistor_SMD:R_0603_1608Metric"

# AMS1117-3.3 LDO (instantiated by the power_tree template; firmware never names
# it). Tab = pin 2 (VO) — a copper-pour note for the PCB step, not a separate net.
AMS1117 = {
    "lib_id": "Regulator_Linear:AMS1117-3.3", "value": "AMS1117-3.3",
    "footprint": "Package_TO_SOT_SMD:SOT-223-3_TabPin2",
    "vin_pin": "VI", "vout_pin": "VO", "gnd_pin": "GND",
}

# --- USB-C + CP2102 programming block (verified pins; v5 footprints) ---------
USB_C_LIB = "Connector:USB_C_Receptacle_USB2.0_16P"
USB_C_FP = "Connector_USB:USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal"
USB_C_VBUS_PINS = ["A4", "A9", "B4", "B9"]   # all VBUS pins -> +5V
USB_C_GND_PINS = ["A1", "A12", "B1", "B12"]  # all GND pins
USB_C_CC1, USB_C_CC2 = "A5", "B5"            # 5.1k pulldown each (sink role)
USB_C_DP, USB_C_DM = "A6", "A7"              # USB data (A-side)

CP2102_LIB = "Interface_USB:CP2102N-Axx-xQFN28"
CP2102_FP = "Package_DFN_QFN:QFN-28-1EP_5x5mm_P0.5mm_EP3.35x3.35mm"
# Pin NAMES (unique) used by the template:
CP2102_VREGIN, CP2102_VBUS = "VREGIN", "VBUS"  # both <- +5V (USB-powered)
CP2102_VDD_OUT = "VDD"                          # LDO OUTPUT — bypass only, NOT +3V3!
CP2102_DP, CP2102_DM = "D+", "D-"
CP2102_TXD, CP2102_RXD = "TXD", "RXD"           # TXD->MCU RX, RXD<-MCU TX
CP2102_DTR, CP2102_RST = "~{DTR}", "~{RST}"
# GND is on TWO pins both named "GND" (pin 3 + EP pad pin 29) — name lookup
# returns only one, so wire these by NUMBER.
CP2102_GND_PINS = ["3", "29"]

SW_PUSH_LIB = "Switch:SW_Push"
SW_PUSH_FP = "Button_Switch_SMD:SW_SPST_B3S-1000"

# 5.1k = USB-C CC sink resistors.
FP_R_0603_5K1 = FP_R_0603

# MCP23017 I2C address strapping. Base 0x20; bits A2 A1 A0 select 0x20..0x27.
MCP23017_ADDRESS_BASE = 0x20
MCP23017_ADDRESS_PINS = ("A0", "A1", "A2")   # index = bit position
MCP23017_RESET_PIN = "~{RESET}"


def mcp23017_address_straps(address: int) -> Optional[list[tuple[str, str]]]:
    """Return [(addr_pin_name, rail)] for an MCP23017 I2C address, or None if the
    address is outside the strappable range 0x20..0x27. Each address pin ties to
    "+3V3" (bit set) or "GND" (bit clear). This is the single source of truth for
    the address→strap mapping (the silent-wrong hotspot — boundary-tested)."""
    offset = address - MCP23017_ADDRESS_BASE
    if offset < 0 or offset > 0b111:
        return None
    out: list[tuple[str, str]] = []
    for bit, pin in enumerate(MCP23017_ADDRESS_PINS):
        rail = "+3V3" if (offset >> bit) & 1 else "GND"
        out.append((pin, rail))
    return out


def resolve_mcu(board_id: Optional[str]) -> Optional[McuInfo]:
    """Map a platformio ``board =`` id to an MCU. Exact match first, then
    substring (so ``esp32dev`` and ``esp32-s3-...`` both resolve)."""
    if not board_id:
        return None
    key = board_id.strip().lower()
    if key in _MCUS:
        return _MCUS[key]
    for sub, info in _MCUS.items():
        if sub in key:
            return info
    return None


def resolve_mcu_by_part(part: Optional[str]) -> Optional[McuInfo]:
    """Look up MCU facts by part string (templates have ``intent.mcu.part``)."""
    if not part:
        return None
    for info in _MCUS.values():
        if info["part"] == part:
            return info
    return None


def resolve_peripheral(type_name: Optional[str]) -> Optional[PeripheralInfo]:
    """Map a peripheral type (e.g. ``MCP23017``, derived from ``MCP23017_ADDR``
    or a pin macro's name prefix) to its symbol + role map."""
    if not type_name:
        return None
    return _PERIPHERALS.get(type_name.strip().upper())


def role_to_pin_name(type_name: str, role: Optional[str]) -> Optional[str]:
    """Translate a firmware signal-role to the device symbol's pin NAME, or
    None if unknown (caller flags it)."""
    info = resolve_peripheral(type_name)
    if info is None or role is None:
        return None
    return info["roles"].get(role.upper())
