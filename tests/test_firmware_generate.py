"""Tests for pin resolution (CI-safe, fake symbols) + schematic generation
(gated on a real KiCad symbol library being present)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_mcp.utils.firmware.mcu_pinmap import gpio_to_pin_number, pin_number_by_name


# --- fake symbol for pin-map unit tests (no KiCad needed) --------------------

@dataclass
class _Pin:
    name: str
    number: str

@dataclass
class _Sym:
    pins: list


_ESP_LIKE = _Sym(pins=[
    _Pin("IO21", "33"), _Pin("IO22", "36"), _Pin("IO16", "27"),
    _Pin("RXD0/IO3", "34"), _Pin("TXD0/IO1", "35"), _Pin("IO0", "25"),
    _Pin("SENSOR_VP", "5"),    # input-only, no IO token
])


@pytest.mark.parametrize("gpio,expected", [
    (21, "33"), (22, "36"), (16, "27"),
    (3, "34"),   # RXD0/IO3
    (1, "35"),   # TXD0/IO1
    (0, "25"),
])
def test_gpio_to_pin_number(gpio, expected):
    assert gpio_to_pin_number(_ESP_LIKE, gpio) == expected

def test_gpio_token_does_not_partial_match():
    # gpio 2 must NOT match "IO21"/"IO22"; gpio 3 must NOT match nonexistent IO35.
    assert gpio_to_pin_number(_ESP_LIKE, 2) is None
    assert gpio_to_pin_number(_ESP_LIKE, 5) is None  # SENSOR_VP has no IO token

def test_pin_number_by_name():
    sym = _Sym(pins=[_Pin("SDA", "13"), _Pin("SCK", "12"), _Pin("INTA", "20")])
    assert pin_number_by_name(sym, "SDA") == "13"
    assert pin_number_by_name(sym, "INTA") == "20"
    assert pin_number_by_name(sym, "MISSING") is None
    assert pin_number_by_name(sym, None) is None

def test_pin_map_handles_none_symbol():
    assert gpio_to_pin_number(None, 5) is None
    assert pin_number_by_name(None, "SDA") is None


# --- generation integration (needs real KiCad symbol libraries) --------------

SPEEDCAL_CONFIG = Path(
    "/Volumes/Files/claude/mr-esp32/speed-cal/firmware/include/config.h"
)


def _kicad_symbols_available() -> bool:
    try:
        from kicad_sch_api.library.cache import get_symbol_cache
        return get_symbol_cache().get_symbol("RF_Module:ESP32-WROOM-32E") is not None
    except Exception:
        return False


@pytest.mark.skipif(not _kicad_symbols_available(),
                    reason="KiCad symbol libraries not available")
@pytest.mark.skipif(not SPEEDCAL_CONFIG.exists(), reason="speed-cal config.h not present")
def test_generate_speedcal_end_to_end(tmp_path):
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
    from kicad_mcp.utils.firmware.parse import parse_defines, partition
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

    parsed = partition(parse_defines(SPEEDCAL_CONFIG.read_text()))
    intent = build_intent(parsed, firmware_path=str(SPEEDCAL_CONFIG),
                          board_id=find_board_id(str(SPEEDCAL_CONFIG)))
    out = tmp_path / "gen.kicad_sch"
    res = generate_schematic(intent, str(out))

    assert res["status"] == "ok"
    # PIEZO and TRACK are terminal-only cards (realize: terminal) — intrinsically
    # off-board, so generate excludes them from schematic placement even on this
    # raw (non-expanded) intent. Only the on-board ICs are placed; their terminal
    # signals are not flagged unresolved (a screw terminal carries them at expand).
    assert res["components_placed"] == 3        # ESP32 + HX711 + MCP23017
    assert not res["component_errors"] and not res["unresolved_endpoints"]

    nl = extract_netlist_via_cli(str(out))
    assert nl is not None

    def members(name):
        return {f"{x['component']}.{x['pin']}" for x in nl["nets"].get(name, [])}

    # bus + peripheral nets join the correct pins (matches v5 connectivity)
    assert members("I2C_SDA") == {"U1.33", "U3.13"}      # ESP32 IO21 ↔ MCP SDA
    assert members("I2C_SCL") == {"U1.36", "U3.12"}      # ESP32 IO22 ↔ MCP SCL(=SCK pin)
    assert members("HX711_DOUT") == {"U1.27", "U2.12"}
    assert members("HX711_SCK") == {"U1.28", "U2.11"}    # firmware SCK -> PD_SCK pin
    assert members("MCP23017_INT") == {"U1.16", "U3.20"} # -> INTA pin
    # orphan net: MCU pin only, far end open
    assert members("I2S_SCK") == {"U1.30"}
