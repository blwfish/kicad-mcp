"""Device-card loader, validator, and strap math — the data layer behind
``knowledge.py`` (CLAUDE.md Rule 3: one source of truth for device facts).

A *card* is a YAML file describing one MCU or peripheral: its symbol/footprint,
firmware-role → symbol-pin map, supply/ground pins, and optional ``config``
(address straps + static ties). Cards replace the hand-edited Python tables that
used to live in ``knowledge.py`` — a new recognized device becomes a data file,
not a code edit.

This module is **pure data plumbing**: it imports nothing from ``knowledge.py``
(avoids an import cycle) and returns plain dicts. ``knowledge.py`` casts those to
its ``PeripheralInfo`` / ``McuInfo`` TypedDicts and exposes ``resolve_*``.

Two validation tiers (the honesty backstop):
  * **structural** — here, no KiCad needed: required fields, types, strap math.
    Runs in the no-KiCad unit matrix.
  * **pin-existence** — in the integration tier (needs the symbol cache); see
    ``tests/integration/test_device_cards.py``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml

from kicad_mcp.utils.firmware.parse import canonical_type
from kicad_mcp.utils.firmware.power_names import RAILS as _RAILS

# Packaged seed library shipped with the tool (the KiCad-library model — no
# runtime network, ever). Override/extend via KICAD_MCP_DEVICE_DIRS or a
# project-local ./firmware-devices/ dir.
_PACKAGED_DIR = Path(__file__).parent / "devices"
_ENV_DIRS_VAR = "KICAD_MCP_DEVICE_DIRS"
_PROJECT_DIR = Path("firmware-devices")

_BUSES = frozenset({"I2C", "SPI", "I2S", "UART", None})


class CardError(ValueError):
    """A malformed card. Raised loudly at load — never silently skipped."""


# --- strap math: single source of truth (replaces the two hand functions) ----

def compute_address_straps(
    strap: dict[str, Any], address: int
) -> Optional[list[tuple[str, str]]]:
    """Map an I2C address to ``[(pin, rail)]`` for an address-strap spec, or None
    if the address is outside the strappable range.

    ``strap`` = ``{pin_bits: [pin, …], base: int, rail_set, rail_clear}``. Bit
    *i* (LSB-first) ties ``pin_bits[i]`` to ``rail_set`` when set, ``rail_clear``
    when clear. ``offset = address - base`` must be in ``[0, 2**N - 1]``.

    Reproduces both legacy functions exactly: MCP23017 (3 bits, base 0x20) and
    MPU-6050 AD0 (1 bit, base 0x68). Boundary-tested.
    """
    pins = strap["pin_bits"]
    base = strap["base"]
    rail_set = strap.get("rail_set", "+3V3")
    rail_clear = strap.get("rail_clear", "GND")
    offset = address - base
    if offset < 0 or offset > (1 << len(pins)) - 1:
        return None
    return [
        (pin, rail_set if (offset >> i) & 1 else rail_clear)
        for i, pin in enumerate(pins)
    ]


# --- structural validation (no KiCad) ----------------------------------------

_PERIPHERAL_REQUIRED = ("type", "lib_id", "value", "footprint", "roles",
                        "supply_pins", "ground_pins", "module")
_MCU_REQUIRED = ("part", "lib_id", "value", "footprint", "board_match",
                 "needs_3v3", "supply_pin", "ground_pin", "en_pin", "boot_pin",
                 "uart_rx_pin", "uart_tx_pin", "native_usb")


def valid_lib_id(val: Any) -> bool:
    """A well-formed ``Library:Symbol`` — non-empty on BOTH sides of a single
    colon (rejects ``":"``, ``"Lib:"``, ``":Sym"``). Shared by every card/sidecar
    validator (Rule 3)."""
    return isinstance(val, str) and bool(re.match(r"^[^:]+:[^:]+$", val))


def _validate_lib_id(val: Any, where: str, errs: list[str]) -> None:
    if not valid_lib_id(val):
        errs.append(f"{where}: lib_id {val!r} must be 'Library:Symbol'")


def _validate_address_strap(strap: Any, where: str, errs: list[str]) -> None:
    if not isinstance(strap, dict):
        errs.append(f"{where}: address_strap must be a mapping")
        return
    pins = strap.get("pin_bits")
    if not isinstance(pins, list) or not pins or not all(isinstance(p, str) for p in pins):
        errs.append(f"{where}: address_strap.pin_bits must be a non-empty list of pin strings")
    if not isinstance(strap.get("base"), int):
        errs.append(f"{where}: address_strap.base must be an int")
    for k in ("rail_set", "rail_clear"):
        if k in strap and strap[k] not in _RAILS:
            errs.append(f"{where}: address_strap.{k}={strap[k]!r} not a known rail {sorted(r for r in _RAILS)}")


def _validate_config(cfg: Any, where: str, errs: list[str]) -> None:
    if cfg is None:
        return
    if not isinstance(cfg, dict):
        errs.append(f"{where}: config must be a mapping")
        return
    if "address_strap" in cfg:
        _validate_address_strap(cfg["address_strap"], where, errs)
    for tie in cfg.get("static_ties", []) or []:
        if not (isinstance(tie, dict) and isinstance(tie.get("pin"), str)
                and tie.get("rail") in _RAILS):
            errs.append(f"{where}: static_ties entry {tie!r} needs pin:str + rail in {sorted(r for r in _RAILS)}")


def validate_peripheral_card(card: dict[str, Any]) -> list[str]:
    """Return a list of structural errors for a peripheral card (empty = ok)."""
    where = f"peripheral card {card.get('type', '?')!r}"
    errs: list[str] = []
    for f in _PERIPHERAL_REQUIRED:
        if f not in card:
            errs.append(f"{where}: missing required field {f!r}")
    if errs:
        return errs
    _validate_lib_id(card["lib_id"], where, errs)
    if card["bus"] not in _BUSES:
        errs.append(f"{where}: bus {card['bus']!r} not in {sorted(str(b) for b in _BUSES)}")
    if not isinstance(card["roles"], dict):
        errs.append(f"{where}: roles must be a mapping role->pin")
    if not isinstance(card["module"], bool):
        errs.append(f"{where}: module must be a bool")
    _validate_config(card.get("config"), where, errs)
    return errs


def validate_mcu_card(card: dict[str, Any]) -> list[str]:
    """Return a list of structural errors for an MCU card (empty = ok)."""
    where = f"mcu card {card.get('part', '?')!r}"
    errs: list[str] = []
    for f in _MCU_REQUIRED:
        if f not in card:
            errs.append(f"{where}: missing required field {f!r}")
    if errs:
        return errs
    _validate_lib_id(card["lib_id"], where, errs)
    bm = card["board_match"]
    if not isinstance(bm, list) or not bm or not all(isinstance(s, str) for s in bm):
        errs.append(f"{where}: board_match must be a non-empty list of strings")
    if not isinstance(card["native_usb"], bool) or not isinstance(card["needs_3v3"], bool):
        errs.append(f"{where}: needs_3v3 and native_usb must be bools")
    return errs


# --- discovery + load --------------------------------------------------------

def _card_dirs(extra_dirs: Optional[list[str]] = None) -> list[Path]:
    """Discovery order (later overrides earlier on key collision): packaged seed,
    then $KICAD_MCP_DEVICE_DIRS, then project-local ./firmware-devices, then any
    explicit extra_dirs (used by tests)."""
    dirs: list[Path] = [_PACKAGED_DIR]
    env = os.environ.get(_ENV_DIRS_VAR, "")
    dirs += [Path(p) for p in env.split(os.pathsep) if p.strip()]
    if _PROJECT_DIR.is_dir():
        dirs.append(_PROJECT_DIR)
    dirs += [Path(p) for p in (extra_dirs or [])]
    return dirs


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise CardError(f"card {path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise CardError(f"card {path} must contain a single YAML mapping")
    return data


def load_cards(
    extra_dirs: Optional[list[str]] = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Discover, parse, and structurally-validate every card.

    Returns ``(peripherals_by_type_upper, mcus)``. A malformed card raises
    ``CardError`` (loud — never silently dropped; data-capture rule). Later dirs
    override earlier on a colliding ``type`` / ``part`` key, enabling project
    pins without editing the packaged set.
    """
    peripherals: dict[str, dict[str, Any]] = {}
    mcus_by_part: dict[str, dict[str, Any]] = {}
    for d in _card_dirs(extra_dirs):
        if not d.is_dir():
            continue
        for path in sorted(d.rglob("*.yaml")) + sorted(d.rglob("*.yml")):
            card = _load_yaml(path)
            if "part" in card and "board_match" in card:
                errs = validate_mcu_card(card)
                if errs:
                    raise CardError(f"{path}:\n  " + "\n  ".join(errs))
                mcus_by_part[card["part"]] = card
            elif "type" in card:
                errs = validate_peripheral_card(card)
                if errs:
                    raise CardError(f"{path}:\n  " + "\n  ".join(errs))
                peripherals[canonical_type(str(card["type"]))] = card
            else:
                raise CardError(
                    f"card {path} is neither a peripheral (needs 'type') nor an "
                    f"MCU (needs 'part'+'board_match')"
                )
    return peripherals, list(mcus_by_part.values())
