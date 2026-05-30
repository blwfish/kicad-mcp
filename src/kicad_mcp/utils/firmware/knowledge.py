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


class PeripheralInfo(TypedDict):
    lib_id: str
    value: str
    bus: Optional[str]
    # firmware signal-role (upper-cased) -> symbol pin NAME
    roles: dict[str, str]


# board-id substrings (from platformio.ini ``board =``) -> MCU.
_MCUS: dict[str, McuInfo] = {
    "esp32dev": {"part": "ESP32-WROOM-32E", "lib_id": "RF_Module:ESP32-WROOM-32E",
                 "value": "ESP32-WROOM-32E"},
    "esp32": {"part": "ESP32-WROOM-32E", "lib_id": "RF_Module:ESP32-WROOM-32E",
              "value": "ESP32-WROOM-32E"},
}

_PERIPHERALS: dict[str, PeripheralInfo] = {
    "MCP23017": {
        "lib_id": "Interface_Expansion:MCP23017x-x-SO",
        "value": "MCP23017", "bus": "I2C",
        # NB: I2C clock pin is named "SCK" on this symbol, not "SCL".
        "roles": {"SDA": "SDA", "SCL": "SCK", "INT": "INTA", "INTA": "INTA"},
    },
    "HX711": {
        "lib_id": "Analog_ADC:HX711",
        "value": "HX711", "bus": None,
        "roles": {"DOUT": "DOUT", "SCK": "PD_SCK", "PD_SCK": "PD_SCK"},
    },
}


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
