"""Knowledge base — the single source of truth (CLAUDE.md Rule 3) for:

  * MCU board-id  -> symbol/part        (``resolve_mcu``)
  * peripheral type -> symbol + bus + role→pin-name map (``resolve_peripheral``)

Device facts now live in **YAML device cards** under ``devices/`` (loaded by
``cards.py``), not hand-edited Python tables — a new recognized device is a data
file, not a code edit. This module keeps the public ``resolve_*`` API, the typed
shapes, the template-owned constants (passives, AMS1117, USB/CP2102 block,
headers — design knowledge no firmware names), and the back-compat strap
helpers (now thin wrappers over the card data).

A firmware ``#define`` names a *role* (``HX711_SCK_PIN``, role ``SCK``); the
device symbol names the *pin*. These are NOT always equal — the MCP23017 symbol's
I2C-clock pin is named ``SCK``, the HX711 clock ``PD_SCK``. Each card's ``roles``
map captures that firmware-role → symbol-pin translation.

Unknown MCUs / peripherals resolve to ``None`` — the importer turns that into an
explicit gap rather than guessing.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypedDict, cast

from kicad_mcp.utils.firmware.cards import (
    compute_address_straps,
    load_cards,
    recognized_part_names,
)
from kicad_mcp.utils.firmware.parse import board_chip, canonical_type

__all__ = [
    "McuInfo", "PeripheralInfo", "resolve_mcu", "resolve_mcu_by_part",
    "resolve_peripheral", "role_to_pin_name", "compute_address_straps",
    "mcp23017_address_straps", "mpu6050_ad0_strap",
    "is_terminal_card_type",
]


class _McuInfoBase(TypedDict):
    part: str
    chip: str            # build-target chip id from parse.CHIP_TARGET_DEFINES
                         # (esp32/esp32s3/rp2040/avr/...) — the DECLARED identity
                         # resolve_mcu's fuzzy-match guard verifies against
    lib_id: str
    value: str
    footprint: str
    needs_3v3: bool      # board needs an EXTERNAL 3V3 regulator (power_tree LDO).
                         # False when the MCU module regulates on-board (Pico, a 5V
                         # Arduino): the supply rail is then SOURCED from the MCU's
                         # own supply pin.
    supply_pin: str      # pin NAME on the supply rail (ESP32 VDD/3V3 in; Pico 3V3 out;
                         # a 5V Arduino +5V out)
    ground_pin: str      # pin NAME for ground (ESP32: the merged "GND" pin)
    uart_rx_pin: str     # console UART RX pin NAME (<- USB bridge TXD)
    uart_tx_pin: str     # console UART TX pin NAME (-> USB bridge RXD)
    native_usb: bool     # True if the board needs no external USB-serial bridge
                         # (native-USB MCU, or a module with the bridge on-board)


class McuInfo(_McuInfoBase, total=False):
    # Optional per-MCU extras (Python 3.10 has no typing.NotRequired, so use the
    # total=False inheritance pattern). Absent on MCUs that don't have the feature.
    en_pin: str          # chip-enable pin NAME (pulled up); absent on MCUs w/o a strap
    boot_pin: str        # boot/strap pin NAME (pulled up); absent on MCUs w/o a strap
    gpio_pin_prefix: str  # GPIO pin-name token prefix (default "IO"; Pico/Nano "GPIO"/"D")
    supply_rail: str     # the logic rail peripherals tie to (default "+3V3"; a 5V
                         # AVR Arduino is "+5V"). Single source for the glue templates.
    alt_lib_ids: list[str]


# Required fields + optional card extras (Python 3.10 has no typing.NotRequired,
# so use the stdlib total=False inheritance pattern).
class _PeripheralInfoBase(TypedDict):
    lib_id: str
    value: str
    bus: str | None
    footprint: str
    # firmware signal-role (upper-cased) -> symbol pin NAME or NUMBER
    roles: dict[str, str]
    supply_pins: list[str]   # pin NAMES/NUMBERS tied to +3V3
    ground_pins: list[str]   # pin NAMES/NUMBERS tied to GND
    # True if modeled as a breakout-MODULE header (GY-521 / I2C OLED on a
    # carrier) rather than a chip-down IC — build_intent flags the assumption.
    module: bool


class PeripheralInfo(_PeripheralInfoBase, total=False):
    config: dict[str, Any]            # address_strap + static_ties (device_config)
    decoupling: list[dict[str, Any]]  # optional per-IC bypass override
    realize: str                      # "terminal" = always a screw-terminal wire-out
    # Older-KiCad names for the SAME symbol, tried in order when ``lib_id`` (the
    # newest name) is absent from the running library. KiCad 10 renamed whole
    # symbol families (MCP23017_SO -> MCP23017x-x-SO); listing the prior name here
    # lets one card build on both versions. Resolved against the live library, so
    # no KiCad-version detection is needed (see resolve_symbol).
    alt_lib_ids: list[str]
    # For I/O expanders: the GPA/GPB port pins in hardware register order — the
    # data-only validation source for board.yaml expander_terminals (no KiCad at
    # unit-test time). See ExpanderSpec / _validate_expander_terminals.
    port_pins: list[str]


# --- card cache (loaded once from the packaged + override dirs) ---------------

_CACHE: tuple[dict[str, dict[str, Any]], list[dict[str, Any]]] | None = None


def _cards() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    global _CACHE
    if _CACHE is None:
        _CACHE = load_cards()
    return _CACHE


def reload_cards() -> None:
    """Drop the card cache (tests / hot-reload). Next resolve_* reloads."""
    global _CACHE
    _CACHE = None


# --- template-owned constants (NOT cards: design knowledge no firmware names) -
# These are instantiated by templates, never by a firmware #define, so they stay
# Python. Pins verified against the speed-cal v5 / audio-node ground-truth.

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
# ICS-43434 (TDK InvenSense) — in-stock I2S MEMS mic, the successor to the EOL
# INMP441. Pin names VERIFIED against the KiCad Sensor_Audio:ICS-43434 symbol and
# DIFFER from SPH0645 on three pins: bit clock is "SCK" (not BCLK), data is "SD"
# (not DATA), L/R-select is "LR" (not SEL). Keep in sync with the resolution card
# devices/peripherals/ics-43434.yaml.
ICS43434 = {
    "lib_id": "Sensor_Audio:ICS-43434", "value": "ICS-43434",
    "footprint": "Sensor_Audio:InvenSense_ICS-43434-6_3.5x2.65mm",
    "ws": "WS", "bclk": "SCK", "data": "SD", "sel": "LR",
    "vdd": "VDD", "gnd": "GND",
}
# (Module headers — 1x02 speaker / 1x04 I2C+UART — are now synthesized via
# connectors.synthesize_connector, which owns symbol+footprint selection.)

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

# --- back-compat strap constants/helpers -------------------------------------
# The card is now the source of truth for strap pins/base; these mirror the card
# data for the few call sites/tests that reference them by name. A parity test
# (test_device_cards) pins that they match the cards, so they can't drift.
MCP23017_RESET_PIN = "~{RESET}"
MPU6050_AD0_PIN = "5"


def mcp23017_address_straps(address: int) -> list[tuple[str, str]] | None:
    """[(addr_pin, rail)] for an MCP23017 I2C address (0x20..0x27), else None.
    Thin wrapper over the MCP23017 card's address-strap spec."""
    info = resolve_peripheral("MCP23017")
    if info is None or "config" not in info:
        return None
    return compute_address_straps(info["config"]["address_strap"], address)


def mpu6050_ad0_strap(address: int) -> tuple[str, str] | None:
    """(ad0_pin, rail) for an MPU-6050 I2C address (0x68/0x69), else None. Thin
    wrapper over the MPU6050 card; unpacks the single-bit list back to a tuple."""
    info = resolve_peripheral("MPU6050")
    if info is None or "config" not in info:
        return None
    straps = compute_address_straps(info["config"]["address_strap"], address)
    return straps[0] if straps else None


# --- resolution --------------------------------------------------------------

def resolve_mcu(board_id: str | None) -> McuInfo | None:
    """Map a platformio ``board =`` id to an MCU card. Exact ``board_match`` /
    ``part`` first, then the card with the LONGEST ``board_match`` substring in
    the id (deterministic; no hand-ordered precedence — removes the Rule-3 seam
    where ``esp32-s3`` had to precede ``esp32``)."""
    if not board_id:
        return None
    key = board_id.strip().lower()
    _, mcus = _cards()
    best: dict[str, Any] | None = None
    best_len = -1
    for card in mcus:
        matches = [str(s).lower() for s in card["board_match"]] + [str(card["part"]).lower()]
        if key in matches:
            return cast(McuInfo, card)
        for sub in matches:
            if sub and sub in key and len(sub) > best_len:
                best, best_len = card, len(sub)
            elif sub and sub in key and len(sub) == best_len and best is not None:
                # deterministic tie-break by part, for stability
                if str(card["part"]) < str(best["part"]):
                    best = card
    if best is None:
        return None
    # Guard the FUZZY substring match (an exact board_match/part above is trusted):
    # the board id's classified chip must equal the card's DECLARED ``chip`` —
    # identity from data, not a second text-match. (The old guard re-ran the
    # substring classifier on the card's part string, so when the board-side
    # classifier over-matched — "pico" inside tinypico — both sides agreed on the
    # wrong answer.) board_chip None (unclassifiable) fails closed: the front end
    # emits an mcu_unknown gap rather than the wrong pinout. NOTE the guard
    # verifies the CHIP axis only — board_match must still pick the right MODULE
    # (a Feather RP2040 is rp2040 silicon but not a Pico; it must not match).
    if board_chip(board_id) != best.get("chip"):
        return None
    return cast(McuInfo, best)


def resolve_mcu_by_part(part: str | None) -> McuInfo | None:
    """Look up MCU facts by part string (templates have ``intent.mcu.part``)."""
    if not part:
        return None
    _, mcus = _cards()
    for card in mcus:
        if card["part"] == part:
            return cast(McuInfo, card)
    return None


def resolve_peripheral(type_name: str | None) -> PeripheralInfo | None:
    """Map a peripheral type/hint (e.g. ``MCP23017`` from ``MCP23017_ADDR``, or a
    pin-macro prefix like ``PIEZO``/``TRACK``) to its card. The name may be the
    card's canonical ``type`` OR one of its ``aliases`` (e.g. hint ``TRACK`` ->
    SWITCH card, ``BUZZER`` -> PIEZO) — alias resolution uses the same raw->canonical
    registry as the free-text part extractor, so the hint path and the comment-scan
    path can never disagree on what a name resolves to."""
    if not type_name:
        return None
    peris, _ = _cards()
    canon = canonical_type(type_name)
    card = peris.get(canon)
    if card is None:
        alias_to_canon = {
            canonical_type(raw): ckey
            for raw, ckey in recognized_part_names(peris).items()
        }
        ckey = alias_to_canon.get(canon)
        card = peris.get(ckey) if ckey else None
    return cast(PeripheralInfo, card) if card is not None else None


def resolve_symbol(
    cache: Any, lib_id: str | None, alt_lib_ids: Iterable[str] = (),
) -> tuple[str | None, Any]:
    """Resolve a component to a real KiCad symbol, trying the primary ``lib_id``
    then any cross-version alternates in order; return ``(resolved_lib_id, sym)``
    or ``(None, None)``.

    The alternates let a card name a symbol KiCad RENAMED across versions
    (``MCP23017_SO`` in KiCad 9 -> ``MCP23017x-x-SO`` in KiCad 10): the first
    candidate that actually exists in the running library wins. Resolving against
    the live library — not a version string — means no KiCad-version detection is
    needed and a future rename only adds a name to the card.

    Single source of truth for cross-version symbol resolution (CLAUDE.md Rule 3):
    consumed by the generator (placement) and the card integration validator (pin
    existence), so neither re-encodes the candidate rule.
    """
    for candidate in (lib_id, *alt_lib_ids):
        if candidate:
            sym = cache.get_symbol(candidate)
            if sym is not None:
                return candidate, sym
    return None, None


def is_terminal_card_type(type_name: str | None) -> bool:
    """True if the peripheral card for ``type_name`` (canonical type OR alias)
    declares ``realize: terminal`` — meaning it is ALWAYS a screw-terminal
    wire-out and no board.yaml ``locus: remote`` directive is required."""
    info = resolve_peripheral(type_name)
    return info is not None and info.get("realize") == "terminal"


def role_to_pin_name(type_name: str, role: str | None) -> str | None:
    """Translate a firmware signal-role to the device symbol's pin NAME/NUMBER,
    or None if unknown (caller flags it)."""
    info = resolve_peripheral(type_name)
    if info is None or role is None:
        return None
    return info["roles"].get(role.upper())
