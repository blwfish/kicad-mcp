"""Design-intent doc: the canonical, human/LLM-editable seam between the
firmware importer and the schematic generator.

``build_intent`` turns parsed firmware into a ``DesignIntent`` — high-confidence
*facts* (MCU, peripherals, signal nets) plus an explicit *gap* manifest for
everything firmware can't know (power, passives, connectors, parts). Nets carry
a confidence tier (bus / peripheral / orphan). YAML is the on-disk form.
"""
from __future__ import annotations

import dataclasses
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

from kicad_mcp.utils.firmware.knowledge import resolve_mcu, resolve_peripheral
from kicad_mcp.utils.firmware.parse import (
    ParsedFirmware,
    _strip_pin_suffix,
    address_base,
)

SCHEMA_VERSION = 8  # v8: DesignIntent.expander_terminals (GPA/GPB -> labeled terminals)

# Gap categories firmware is structurally blind to — always emitted so the doc
# is honest about what a human/LLM must still supply.
_ALWAYS_GAPS = [
    ("power_tree", "Firmware declares no power rails or regulator."),
    ("decoupling", "Decoupling/bypass capacitors are not in firmware."),
    ("pullups", "Pull-up/down resistors (I2C, EN, boot) are not in firmware."),
    ("connectors", "Connector parts and pinouts are not in firmware."),
    ("parts", "Part numbers/footprints beyond recognized ICs are not in firmware."),
]


@dataclass
class Endpoint:
    ref: str
    gpio: Optional[int] = None   # set on the MCU side (resolved via IO{n})
    role: Optional[str] = None   # firmware signal-role on the peripheral side
    pin: Optional[str] = None    # direct pin NAME/number — for passives & power
                                 # pins that are neither a GPIO nor a known role


@dataclass
class Net:
    name: str
    kind: str                    # "bus"|"peripheral"|"orphan"|"power"|"passive"
    confidence: str              # "high" | "low"
    endpoints: list[Endpoint] = field(default_factory=list)
    bus: Optional[str] = None
    origin: str = "imported"     # "imported"|"template"|"user" (see merge())


# The net-"kind" universe — single source of truth (CLAUDE.md Rule 3) so tests and
# consumers import these rather than hand-copying the string literals. MODELED =
# the high-confidence connections the importer asserts as firmware-as-spec claims
# (bus/peripheral); orphan/power/passive are gaps or template-supplied.
NET_KINDS = frozenset({"bus", "peripheral", "orphan", "power", "passive"})
MODELED_NET_KINDS = frozenset({"bus", "peripheral"})


@dataclass
class Peripheral:
    ref: str
    type: str
    lib_id: Optional[str] = None
    # Older-KiCad names for the same symbol (KiCad 10 renamed MCP23017_SO ->
    # MCP23017x-x-SO); the generator tries these in order when lib_id is absent
    # from the running library. Carried on the intent (not baked at build time)
    # so resolution happens against the live library at generate time.
    alt_lib_ids: list[str] = field(default_factory=list)
    value: Optional[str] = None
    footprint: Optional[str] = None
    bus: Optional[str] = None
    address: Optional[int] = None
    origin: str = "imported"     # "imported"|"template"|"user"
    # Placement locus — where the device physically lives relative to the board.
    # A board-level decision (firmware cannot know if a mic is reflowed or hung on
    # 18" of wire), so it is set from board.yaml, never firmware-derived.
    #   on_board                 -> place the symbol/footprint (default)
    #   remote                   -> not placed; its nets cross to a terminal
    #   on_board_with_remote_io  -> placed, but external-load nets cross to a terminal
    locus: str = "on_board"


@dataclass
class ConnectorLegend:
    """Per-connector silk-legend metadata, populated by ``synthesize_connector``,
    consumed by ``pcb_pipeline`` (``_step_silkscreen_legends``). The silk legend
    IS the field-wiring documentation for a terminal — not cosmetic."""
    ref: str                 # e.g. "J3" — identifies the footprint in the placed PCB
    positions: list[str]     # positions[i] = short label for pad number (i+1)
    device: str              # e.g. "INMP441" — set as the footprint value/description


@dataclass
class Placement:
    """A board.yaml placement directive — where a device physically lives relative
    to the board. The SINGLE source for the locus decision (CLAUDE.md Rule 3),
    keyed in ``DesignIntent.placements`` by the handle that exists in the intent:
    a **bus stem** (``CMCA_MIC``) for a bus-template device, or a **peripheral
    ref** (``U2``) for a carded device. ``device`` is the human-supplied identity
    — firmware names these only in comments, so it cannot be recovered, only
    declared (honest-by-construction)."""
    locus: str = "on_board"          # on_board | remote | on_board_with_remote_io
    device: Optional[str] = None     # field-wired device identity (legend + BOM)
    connector: str = "screw_terminal"   # screw_terminal | pin_header | pluggable
    footprint: Optional[str] = None  # series-default override for the terminal
    external_io: list[str] = field(default_factory=list)  # roles crossing (remote_io)


@dataclass
class ExpanderSpec:
    """A board.yaml ``expander_terminals`` entry (keyed by expander peripheral ref
    in ``DesignIntent.expander_terminals``): tap an I/O-expander's floating GPA/GPB
    port pins out to labeled screw terminal(s).

    Honest-by-construction — firmware can't know this (the sensors are
    register-addressed at runtime, no per-sensor #define), so it must be declared.
    The ``ports: int`` board.yaml shorthand is expanded to a concrete pin-name list
    at sidecar-apply time, so templates only ever see resolved pin names."""
    device: str                        # silk label + connector value (e.g. TCRT5000)
    ports: list[str] = field(default_factory=list)   # resolved pin names: [GPA0, GPA1, ...]
    group: str = "per_sensor"          # per_sensor | per_bank | single
    power: str = "3v3"                 # 3v3 | 5v | none
    net_prefix: str = ""               # net base name; filled from device at apply if empty


@dataclass
class Mcu:
    ref: str
    part: str
    lib_id: str
    footprint: Optional[str] = None


@dataclass
class Gap:
    kind: str
    detail: str = ""
    resolved: bool = False
    resolved_by: Optional[str] = None             # template name or "user"
    resolved_components: list[str] = field(default_factory=list)


@dataclass
class Bus:
    """A grouped set of bus pins (e.g. an I2S output bus), the unit a bus-driven
    template expands into a peripheral sub-circuit."""
    name: str                       # the pin-name stem, e.g. "CMCA_I2S"
    type: str                       # I2C | I2S_OUT | I2S_IN | UART | SPI
    signals: dict[str, int] = field(default_factory=dict)  # role -> gpio
    address: Optional[int] = None   # I2C devices
    origin: str = "imported"
    # Part resolution (C4): the SPECIFIC part the user declared for this bus,
    # determined from the firmware corpus — NEVER invented. ``resolved_part`` is a
    # canonical key (e.g. "INMP441"); None = unresolved (no/ambiguous evidence).
    # ``part_provenance``: "corpus" (found in firmware) | "user" (board.yaml).
    # ``part_is_assumption``: set True only when a template falls back to a default
    # part because the resolved one is absent (then a gap discloses it).
    resolved_part: Optional[str] = None
    part_provenance: Optional[str] = None
    part_is_assumption: bool = False


@dataclass
class DesignIntent:
    schema_version: int = SCHEMA_VERSION
    source: dict[str, Any] = field(default_factory=dict)
    mcu: Optional[Mcu] = None
    peripherals: list[Peripheral] = field(default_factory=list)
    buses: list[Bus] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    connector_legends: list[ConnectorLegend] = field(default_factory=list)
    # board.yaml placement directives, keyed by bus stem or peripheral ref.
    placements: dict[str, "Placement"] = field(default_factory=dict)
    # Per-ref PCB placement overrides keyed by KiCad ref → {edge, rotation, fixed}.
    # PCB-layer concern, distinct from `placements` (the firmware-semantic locus
    # channel).  Validated by edge_terminal.normalize_hint before use.
    placement_hints: dict[str, dict[str, Any]] = field(default_factory=dict)
    # board.yaml expander_terminals: tap an I/O-expander's GPA/GPB port pins out to
    # labeled terminals. Keyed by the expander's peripheral ref. Set by
    # apply_sidecar, consumed by the expander_terminals template.
    expander_terminals: dict[str, "ExpanderSpec"] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)


# --- locus helpers (single source for the remote check) ----------------------

VALID_LOCI = ("on_board", "remote", "on_board_with_remote_io")


def placement_for(intent: "DesignIntent", key: str) -> Optional[Placement]:
    """The placement directive for a bus stem or peripheral ref, or None."""
    return intent.placements.get(key)


def is_remote(intent: "DesignIntent", key: str) -> bool:
    """True if ``key`` (bus stem or peripheral ref) is declared fully remote."""
    pl = intent.placements.get(key)
    return pl is not None and pl.locus == "remote"


# --- platformio board detection ----------------------------------------------

_BOARD_RE = re.compile(r"^\s*board\s*=\s*(\S+)", re.MULTILINE)


def find_board_id(start_path: str) -> Optional[str]:
    """Walk up from a firmware file/dir to find platformio.ini and read its
    ``board =`` id. A platformio.ini can declare MULTIPLE envs (e.g. a legacy
    ``esp32dev`` then a production ``esp32-s3-devkitc-1``); prefer the LAST one
    that resolves to a known MCU (else the last declared). Returns None if not
    found."""
    p = Path(start_path)
    cur = p if p.is_dir() else p.parent
    for _ in range(6):  # bounded walk-up
        ini = cur / "platformio.ini"
        if ini.exists():
            boards: list[str] = [str(b).strip()
                                 for b in _BOARD_RE.findall(ini.read_text(errors="replace"))]
            if boards:
                known = [b for b in boards if resolve_mcu(b) is not None]
                return known[-1] if known else boards[-1]
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


# --- importer ----------------------------------------------------------------

def _peripheral_type_from_addr(name: str) -> str:
    # Delegates to parse.address_base — the single source of truth for the
    # addr-macro name shape (incl. the multi-instance ``_2`` qualifier).
    return address_base(name) or name


def _bus_type(roles: set[str]) -> Optional[str]:
    """Classify a bus instance from the signal roles present on it."""
    if {"SDA", "SCL"} <= roles:
        return "I2C"
    has_clk = bool(roles & {"BCLK", "SCK"})
    has_ws = bool(roles & {"LRC", "LRCK", "WS"})
    if has_clk and has_ws and "DIN" in roles:
        return "I2S_OUT"
    if has_clk and has_ws and (roles & {"SD", "DOUT"}):
        return "I2S_IN"
    if {"RX", "TX"} <= roles or {"RXD", "TXD"} <= roles:
        return "UART"
    if {"MOSI", "MISO"} <= roles:
        return "SPI"
    return None


def _build_buses(parsed: ParsedFirmware, known_types: set[str]) -> list[Bus]:
    """Group stemmed pin macros into typed buses. Pins whose stem is a recognized
    peripheral (HX711, …) or has no stem (bare ``I2C_SDA``) are left to the
    existing peripheral/bus-net path — this captures project-convention buses
    like ``CMCA_I2S`` that have no ``_ADDR`` device."""
    groups: dict[str, dict[str, int]] = {}
    for m in parsed.pins:
        stem, role = m.peripheral_hint, m.signal_role
        if not stem or stem in known_types or role is None or m.gpio is None:
            continue
        groups.setdefault(stem, {})[role] = m.gpio
    addr_by_stem = {_peripheral_type_from_addr(a.name): a.address for a in parsed.addresses}
    buses: list[Bus] = []
    for stem, signals in groups.items():
        btype = _bus_type(set(signals))
        if btype is None:
            continue
        buses.append(Bus(name=stem, type=btype, signals=signals,
                         address=addr_by_stem.get(stem)))
    return buses


def candidate_devices(parsed: ParsedFirmware) -> list[tuple[str, Optional[int]]]:
    """The devices a firmware references, as ``(type, address)`` pairs — the
    single source of truth for "what's on this board" (consumed by ``build_intent``
    to place known devices, and by auto-draft to propose cards for unknown ones).

    Address-declared devices: ONE instance per ``*_ADDR`` macro (two MPU6050 at
    0x68/0x69 are two chips). Pin-hint devices (named only by pin macros, no
    address, e.g. HX711): one per type. Ordered by ``(type, address)`` so refs
    are deterministic and stable; hint devices (no address) sort first per type.
    """
    addr_devices = [(_peripheral_type_from_addr(a.name), a.address or 0)
                    for a in parsed.addresses]
    addr_types = {t for t, _ in addr_devices}
    hint_types = {
        m.peripheral_hint for m in parsed.pins
        if m.peripheral_hint and m.bus is None and m.peripheral_hint not in addr_types
    }
    out: list[tuple[str, Optional[int]]] = (
        [(t, a) for t, a in addr_devices]
        + [(t, None) for t in hint_types if t]
    )
    out.sort(key=lambda ta: (ta[0], -1 if ta[1] is None else ta[1]))
    return out


def build_intent(
    parsed: ParsedFirmware, *, firmware_path: str, board_id: Optional[str],
) -> DesignIntent:
    """Assemble a DesignIntent from parsed firmware. Deterministic ordering."""
    intent = DesignIntent()
    intent.source = {"firmware_path": firmware_path, "board": board_id}

    # --- MCU ---
    mcu_info = resolve_mcu(board_id)
    if mcu_info is not None:
        intent.mcu = Mcu(ref="U1", part=mcu_info["part"], lib_id=mcu_info["lib_id"],
                         footprint=mcu_info["footprint"])
    else:
        intent.gaps.append(Gap("mcu_unknown",
                               f"Could not resolve MCU from board id {board_id!r}."))

    # --- materialize peripherals we have a symbol for ---
    to_place = candidate_devices(parsed)

    peripherals: list[Peripheral] = []
    periph_by_type: dict[str, Peripheral] = {}
    unknown_types: set[str] = set()
    placed_module = False
    next_ref = 2
    for t, address in to_place:
        info = resolve_peripheral(t)
        if info is None:
            if t not in unknown_types:   # one gap per unknown type, not per instance
                unknown_types.add(t)
                intent.gaps.append(Gap(
                    "unknown_peripheral",
                    f"Peripheral {t!r} is referenced by firmware but has no known "
                    f"symbol; not placed.",
                ))
            continue
        placed_module = placed_module or info["module"]
        p = Peripheral(
            ref=f"U{next_ref}", type=t, lib_id=info["lib_id"],
            alt_lib_ids=list(info.get("alt_lib_ids", []) or []),
            value=info["value"], footprint=info["footprint"], bus=info["bus"],
            address=address,
        )
        next_ref += 1
        peripherals.append(p)
        periph_by_type.setdefault(t, p)   # first instance anchors the non-bus net path
    intent.peripherals = peripherals

    if placed_module:
        intent.gaps.append(Gap(
            "module_assumption",
            "One or more I2C devices are modeled as breakout-module headers "
            "(carrier-board assumption); for chip-down placement supply the bare "
            "IC symbol + its support passives.",
        ))

    by_bus: dict[str, list[Peripheral]] = {}
    for p in peripherals:
        if p.bus:
            by_bus.setdefault(p.bus, []).append(p)

    # --- typed buses (project-convention buses a bus-driven template expands) ---
    intent.buses = _build_buses(parsed, set(periph_by_type))
    bus_stems = {b.name for b in intent.buses}

    # --- pin conflicts: one GPIO claimed by two different signals (flag, never
    # silently merge — e.g. I2S-bus-1 and the presence UART both on GPIO 5/6) ---
    by_gpio: dict[int, list[str]] = {}
    for m in parsed.pins:
        if m.gpio is not None:
            by_gpio.setdefault(m.gpio, []).append(m.name)
    for gpio, names in sorted(by_gpio.items()):
        if len(set(names)) > 1:
            intent.gaps.append(Gap(
                "pin_conflict",
                f"GPIO{gpio} is claimed by multiple signals {sorted(set(names))} "
                "(mutually exclusive at runtime); resolve before fabrication.",
            ))

    # --- nets, with first-wins on duplicate net names ---
    mcu_ref = intent.mcu.ref if intent.mcu else "U1"
    seen_names: set[str] = set()
    for m in parsed.pins:
        # Bus pins are wired by their bus-driven template, not as orphan nets.
        if m.peripheral_hint in bus_stems:
            continue
        name = _strip_pin_suffix(m.name)
        if name in seen_names:
            intent.gaps.append(Gap(
                "duplicate_signal",
                f"Duplicate signal name {name!r} (macro {m.name}); first-wins.",
            ))
            continue
        seen_names.add(name)
        mcu_ep = Endpoint(ref=mcu_ref, gpio=m.gpio)

        if m.bus is not None:
            devices = by_bus.get(m.bus, [])
            if devices:
                eps = [mcu_ep] + [Endpoint(ref=d.ref, role=m.signal_role) for d in devices]
                intent.nets.append(Net(name, "bus", "high", eps, bus=m.bus))
            else:
                intent.nets.append(Net(name, "orphan", "low", [mcu_ep], bus=m.bus))
                intent.gaps.append(Gap(
                    "unknown_peripheral",
                    f"{m.bus} signal {name!r} on GPIO{m.gpio}: no {m.bus} device "
                    f"declared in firmware — far end unknown.",
                ))
        elif m.peripheral_hint and m.peripheral_hint in periph_by_type:
            dev = periph_by_type[m.peripheral_hint]
            eps = [mcu_ep, Endpoint(ref=dev.ref, role=m.signal_role)]
            intent.nets.append(Net(name, "peripheral", "high", eps))
        else:
            intent.nets.append(Net(name, "orphan", "low", [mcu_ep]))
            intent.gaps.append(Gap(
                "unknown_peripheral",
                f"Signal {name!r} on GPIO{m.gpio}: peripheral "
                f"{m.peripheral_hint or '?'!r} has no known symbol — far end unknown.",
            ))

    # --- invalid pins (kept + flagged, never wired) ---
    for m in parsed.invalid_pins:
        intent.gaps.append(Gap(
            "invalid_pin",
            f"{m.name} = {m.raw_value} ({m.note}); not wired.",
        ))

    # --- always-on firmware-blind gaps ---
    for kind, detail in _ALWAYS_GAPS:
        intent.gaps.append(Gap(kind, detail))

    # --- provenance: retain every unmodeled macro (data-capture rule) ---
    intent.provenance = {
        "source_file": firmware_path,
        "board": board_id,
        "unparsed": [
            {"name": m.name, "value": m.raw_value, "line": m.line_no}
            for m in parsed.other
        ],
        "unparsed_count": len(parsed.other),
    }
    return intent


# --- (de)serialization --------------------------------------------------------

def to_dict(intent: DesignIntent) -> dict[str, Any]:
    return dataclasses.asdict(intent)


def _only_fields(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys that are fields of dataclass ``cls``, so a newer doc with
    extra fields loads (silently dropping the unknowns) instead of raising
    ``TypeError`` on ``cls(**d)`` — matching from_dict's documented forward-compat
    contract (a v4 schema or a typo'd hand-edited key must not crash load_intent)."""
    known = {f.name: f for f in dataclasses.fields(cls)}
    out: dict[str, Any] = {}
    for k, v in d.items():
        f = known.get(k)
        if f is None:
            continue
        # A hand-edited doc with `signals: null` (or any list/dict field set to
        # null) would override the field's default_factory with None, so e.g.
        # Bus(signals=None) then crashes on `bus.signals.get(...)`. Drop the key
        # so the factory default (empty dict/list) applies. Optional scalar
        # fields (resolved_part, etc.) keep their None.
        if v is None and f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
            continue
        out[k] = v
    return out


def from_dict(d: dict[str, Any]) -> DesignIntent:
    ver = d.get("schema_version", 1)
    if ver != SCHEMA_VERSION:
        # Warn, don't raise: optional fields default-fill, so older docs still
        # load. A newer doc loaded by older code silently drops unknown fields.
        logger.warning(
            "design-intent schema_version %s != current %s; loading anyway "
            "(fields may default-fill).", ver, SCHEMA_VERSION,
        )
    mcu_d = d.get("mcu")
    # `d.get(k, [])` returns None when k is PRESENT with a null value (a
    # hand-edited `buses: null`), and `[X(..) for x in None]` raises TypeError.
    # Use `or []` like placements/placement_hints below so an explicit null
    # degrades to empty rather than crashing load_intent.
    return DesignIntent(
        schema_version=d.get("schema_version", SCHEMA_VERSION),
        source=d.get("source", {}),
        mcu=Mcu(**_only_fields(Mcu, mcu_d)) if mcu_d else None,
        peripherals=[Peripheral(**_only_fields(Peripheral, p)) for p in (d.get("peripherals") or [])],
        buses=[Bus(**_only_fields(Bus, b)) for b in (d.get("buses") or [])],
        nets=[
            Net(
                name=n["name"], kind=n["kind"], confidence=n["confidence"],
                endpoints=[Endpoint(**_only_fields(Endpoint, e)) for e in (n.get("endpoints") or [])],
                bus=n.get("bus"), origin=n.get("origin", "imported"),
            )
            for n in (d.get("nets") or [])
        ],
        gaps=[Gap(**_only_fields(Gap, g)) for g in (d.get("gaps") or [])],
        connector_legends=[
            ConnectorLegend(**_only_fields(ConnectorLegend, c))
            for c in (d.get("connector_legends") or [])
        ],
        placements={
            k: Placement(**_only_fields(Placement, v))
            for k, v in (d.get("placements", {}) or {}).items()
        },
        placement_hints=d.get("placement_hints", {}) or {},
        expander_terminals={
            k: ExpanderSpec(**_only_fields(ExpanderSpec, v))
            for k, v in (d.get("expander_terminals", {}) or {}).items()
        },
        provenance=d.get("provenance", {}),
    )


def save_intent(intent: DesignIntent, path: str) -> None:
    with open(path, "w") as f:
        yaml.safe_dump(to_dict(intent), f, sort_keys=False, default_flow_style=False)


def load_intent(path: str) -> DesignIntent:
    with open(path) as f:
        return from_dict(yaml.safe_load(f))


def merge(existing: DesignIntent, fresh: DesignIntent) -> DesignIntent:
    """Re-import without clobbering hand edits. Three-way by ``origin``:

    * ``imported`` — ``fresh`` (the new firmware parse) wins.
    * ``user`` — hand-authored items from ``existing`` are always preserved.
    * ``template`` — NOT carried here; template-added circuitry is *re-runnable*
      via ``expand_templates`` (re-running it regenerates the items), so merge
      drops them rather than risk duplicating on the next expand.

    Gaps the user/existing marked resolved keep ``resolved`` + its provenance.
    """
    out = dataclasses.replace(fresh)
    existing_user_peri = [p for p in existing.peripherals if p.origin == "user"]
    existing_user_nets = [n for n in existing.nets if n.origin == "user"]
    out.peripherals = list(fresh.peripherals) + existing_user_peri
    out.nets = list(fresh.nets) + existing_user_nets
    resolved = {(g.kind, g.detail): g for g in existing.gaps if g.resolved}
    for g in out.gaps:
        src = resolved.get((g.kind, g.detail))
        if src is not None:
            g.resolved = True
            g.resolved_by = src.resolved_by
            g.resolved_components = list(src.resolved_components)
    return out
