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

For part-resolution it also exposes ``recognized_part_names()`` (raw part name →
canonical lookup key) feeding the corpus scanner, and accepts two optional
peripheral-card fields: ``aliases`` (alternate raw names) and ``serves`` (the
directional bus a part resolves onto, ``_SERVES_BUS_TYPES``).

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

# Valid values for the optional ``realize`` field on a peripheral card.
# ``terminal`` = the device is ALWAYS realized as a screw terminal (no chip
# symbol is placed); the card IS the wire-out declaration and no board.yaml
# ``locus: remote`` directive is needed.  Absence = default chip-down behavior.
_REALIZE_VALUES = frozenset({"terminal"})

# What a card may declare it ``serves`` — the bus a part can be RESOLVED onto.
# Deliberately FINER than ``_BUSES``: a bus is directional at resolution time
# (``Bus.type`` is ``I2S_IN`` / ``I2S_OUT``), so a mic card serves ``I2S_IN`` and
# an amp serves ``I2S_OUT``. Validating ``serves`` against ``_BUSES`` (which only
# has the coarse ``"I2S"``) would let a card claim a bus it can never match.
_SERVES_BUS_TYPES = frozenset({"I2C", "SPI", "I2S_IN", "I2S_OUT", "UART"})


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

# "bus" must be PRESENT (it is read below), though its value may be null for a
# non-bus peripheral — so a card omitting it gets a clean error, not a KeyError.
_PERIPHERAL_REQUIRED = ("type", "lib_id", "value", "footprint", "roles",
                        "supply_pins", "ground_pins", "module", "bus")
# en_pin / boot_pin are NOT required: an MCU module without a chip-enable or boot
# strap (the RP2040/Pico) simply omits them and mcu_straps places no pull-ups.
# They stay in MCU_PIN_FIELDS so, WHEN present, their values are still validated
# against the real symbol.
_MCU_REQUIRED = ("part", "lib_id", "value", "footprint", "board_match",
                 "needs_3v3", "supply_pin", "ground_pin",
                 "uart_rx_pin", "uart_tx_pin", "native_usb")


# --- which card fields reference symbol PIN names (single source of truth) -----
# The card-pin integration gate validates every value of the PIN fields against
# the real KiCad symbol; the completeness meta-gate (tests/test_device_cards)
# asserts every field present on a card is classified here as pin-bearing or not.
# Together they mean a NEW pin-bearing field cannot silently escape the symbol
# gate — the port_pins lesson (a pin field was added but left out of the
# collector for a release). CLAUDE.md Rule 3 (one source) + data-capture rule.
# Limit (be honest): the meta-gate enforces that every field is *classified*, not
# that the classification is *correct* — a pin field misfiled into NONPIN would
# still escape. That residual is the one conscious call a human makes per field.
PERIPHERAL_PIN_FIELDS = ("roles", "supply_pins", "ground_pins", "port_pins")
PERIPHERAL_NONPIN_FIELDS = ("type", "lib_id", "alt_lib_ids", "value", "footprint",
                            "bus", "module", "aliases", "serves", "realize", "decoupling")
# ``config`` is a nested container; its pin-bearing sub-keys (address_strap ->
# pin_bits, static_ties[] -> pin) are read below. A NEW config sub-key must be
# classified too (extend the collector + this set), enforced by the meta-gate.
CONFIG_SUBKEYS = ("address_strap", "static_ties")
MCU_PIN_FIELDS = ("supply_pin", "ground_pin", "en_pin", "boot_pin",
                  "uart_rx_pin", "uart_tx_pin")
MCU_NONPIN_FIELDS = ("part", "lib_id", "alt_lib_ids", "value", "footprint",
                     "board_match", "needs_3v3", "native_usb", "gpio_pin_prefix")


def peripheral_pin_refs(card: dict[str, Any]) -> list[str]:
    """Every symbol-pin name a peripheral card references — driven by
    ``PERIPHERAL_PIN_FIELDS`` plus the ``config`` straps/ties. Falsy values are
    skipped (an absent/optional pin is not a phantom symbol reference). Consumed
    by the card-pin gate (validate vs the real symbol) and the completeness
    meta-gate, so the two never drift."""
    refs: list[str] = []
    for f in PERIPHERAL_PIN_FIELDS:
        v = card.get(f)
        if isinstance(v, dict):
            refs += [str(x) for x in v.values()]      # roles: role -> pin
        elif isinstance(v, list):
            refs += [str(x) for x in v]               # supply/ground/port pin lists
    cfg = card.get("config") or {}
    strap = cfg.get("address_strap")
    if isinstance(strap, dict):
        refs += [str(x) for x in strap.get("pin_bits", []) or []]
    refs += [str(t["pin"]) for t in cfg.get("static_ties", []) or []
             if isinstance(t, dict) and t.get("pin") is not None]
    return [r for r in refs if r]


def mcu_pin_refs(card: dict[str, Any]) -> list[str]:
    """Every symbol-pin name an MCU card references (driven by ``MCU_PIN_FIELDS``).
    Coerce-then-filter, SYMMETRIC with ``peripheral_pin_refs``: an absent or empty
    field is skipped, but a falsy-but-valid pin like ``0`` is kept (filtering the
    raw value would drop int ``0`` while keeping ``"0"`` — a silent asymmetry)."""
    return [r for r in (str(card[f]) for f in MCU_PIN_FIELDS if f in card) if r]


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
    _validate_aliases(card.get("aliases"), where, errs)
    _validate_alt_lib_ids(card.get("alt_lib_ids"), where, errs)
    _validate_port_pins(card.get("port_pins"), where, errs)
    _validate_serves(card.get("serves"), where, errs)
    _validate_config(card.get("config"), where, errs)
    _validate_realize(card.get("realize"), where, errs)
    return errs


def _validate_aliases(aliases: Any, where: str, errs: list[str]) -> None:
    """``aliases`` is optional; when present it must be a list of non-empty
    strings (alternate part names the firmware might spell, e.g. SPH0645LM4H for
    SPH0645). Each becomes a raw-name regex target in part resolution."""
    if aliases is None:
        return
    if not isinstance(aliases, list) or not all(
        isinstance(a, str) and a.strip() for a in aliases
    ):
        errs.append(f"{where}: aliases must be a list of non-empty strings")


def _validate_alt_lib_ids(alts: Any, where: str, errs: list[str]) -> None:
    """``alt_lib_ids`` is optional; when present it must be a list of well-formed
    ``Library:Symbol`` ids — older-KiCad names for the SAME symbol the card's
    ``lib_id`` names on the newest version (resolve_symbol tries them in order).
    Distinct from ``aliases`` (alternate firmware part-NAME spellings); each entry
    here is a lib_id, so it is validated by the same ``valid_lib_id`` rule (Rule 3)."""
    if alts is None:
        return
    if not isinstance(alts, list) or not all(valid_lib_id(a) for a in alts):
        errs.append(f"{where}: alt_lib_ids must be a list of 'Library:Symbol' ids")


def _validate_port_pins(pins: Any, where: str, errs: list[str]) -> None:
    """``port_pins`` is optional (I/O expanders only); when present it must be a
    non-empty list of non-empty pin-name strings — the GPA/GPB port pins that
    board.yaml ``expander_terminals`` may tap. Integration tier checks they exist
    on the real symbol; here we only pin the shape."""
    if pins is None:
        return
    if not isinstance(pins, list) or not pins or not all(
        isinstance(p, str) and p.strip() for p in pins
    ):
        errs.append(f"{where}: port_pins must be a non-empty list of pin-name strings")


def _validate_serves(serves: Any, where: str, errs: list[str]) -> None:
    """``serves`` is optional; when present it names the DIRECTIONAL bus this
    part can be resolved onto (``_SERVES_BUS_TYPES``), matched against
    ``Bus.type`` during part resolution."""
    if serves is None:
        return
    if serves not in _SERVES_BUS_TYPES:
        errs.append(
            f"{where}: serves {serves!r} not in {sorted(_SERVES_BUS_TYPES)}"
        )


def _validate_realize(realize: Any, where: str, errs: list[str]) -> None:
    """``realize`` is optional; when present it declares how this peripheral is
    always realized, overriding the default chip-down path.  Only ``"terminal"``
    is a valid value (v1) — the device is ALWAYS a screw-terminal wire-out and
    no board.yaml ``locus: remote`` directive is required."""
    if realize is None:
        return
    if realize not in _REALIZE_VALUES:
        errs.append(
            f"{where}: realize {realize!r} not in {sorted(_REALIZE_VALUES)}"
        )


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


def recognized_part_names(
    peripherals: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """Map every RAW part name a card answers to → its canonical lookup key.

    The part-name extractor must match firmware text against the *raw* names
    (``"INMP441"``, ``"ICS-43434"``, ``"SPH0645LM4H"``) — building the search
    regex from canonical names would strip the hyphen in ``ICS-43434`` and never
    match the source (blocker **I1**). So this returns ``raw -> canonical``: the
    extractor compiles ``\\bRAW\\b`` patterns from the keys; the resolver then
    looks the matched name's value up in the canonical-keyed ``peripherals`` dict.

    Covers each card's ``type`` plus every ``aliases`` entry. Raw names collide
    to the same canonical key by construction (``ICS-43434`` and ``ICS43434`` →
    ``ICS43434``), so a firmware spelling either way resolves to one card.
    """
    out: dict[str, str] = {}
    for card in peripherals.values():
        canonical = canonical_type(str(card["type"]))
        for raw in (str(card["type"]), *(card.get("aliases") or [])):
            prior = out.get(raw)
            if prior is not None and prior != canonical:
                raise CardError(
                    f"part name {raw!r} is claimed by two cards ({prior!r} and "
                    f"{canonical!r}); a type/alias must map to exactly one card "
                    "(load order is filesystem-dependent — silent last-write-wins "
                    "would pick a card non-deterministically)"
                )
            out[raw] = canonical
    return out


def terminal_card_types(
    peripherals: dict[str, dict[str, Any]]
) -> frozenset[str]:
    """Return the set of canonical type keys whose cards declare ``realize:
    terminal`` — devices that are ALWAYS realized as screw terminals (no
    chip-down placement, no board.yaml ``locus: remote`` required).

    Consumed by ``templates.py:_inject_terminal_loci`` to auto-assign a remote
    placement before any template runs, so power_tree / device_config skip
    their glue for these devices (same path as an explicitly-remote carded
    peripheral)."""
    return frozenset(
        key for key, card in peripherals.items()
        if card.get("realize") == "terminal"
    )


def part_serves_map(
    peripherals: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """Map canonical part key → the directional bus type its card declares it
    ``serves`` (cards without ``serves`` are omitted). Feeds the bus→part
    resolver (C4): a recognized part is a candidate for a bus iff its ``serves``
    matches the bus's ``type``. ``peripherals`` is already canonical-keyed."""
    return {key: card["serves"]
            for key, card in peripherals.items() if card.get("serves")}


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
