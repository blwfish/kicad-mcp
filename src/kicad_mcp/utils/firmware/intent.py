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
from kicad_mcp.utils.firmware.parse import ParsedFirmware

SCHEMA_VERSION = 2  # v2: power/passive nets, footprints, template origin, gap provenance

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


@dataclass
class Peripheral:
    ref: str
    type: str
    lib_id: Optional[str] = None
    value: Optional[str] = None
    footprint: Optional[str] = None
    bus: Optional[str] = None
    address: Optional[int] = None
    origin: str = "imported"     # "imported"|"template"|"user"


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
class DesignIntent:
    schema_version: int = SCHEMA_VERSION
    source: dict[str, Any] = field(default_factory=dict)
    mcu: Optional[Mcu] = None
    peripherals: list[Peripheral] = field(default_factory=list)
    nets: list[Net] = field(default_factory=list)
    gaps: list[Gap] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


# --- platformio board detection ----------------------------------------------

_BOARD_RE = re.compile(r"^\s*board\s*=\s*(\S+)", re.MULTILINE)


def find_board_id(start_path: str) -> Optional[str]:
    """Walk up from a firmware file/dir to find platformio.ini and read its
    ``board =`` id. Returns None if not found."""
    p = Path(start_path)
    search_dirs = [p] if p.is_dir() else [p.parent]
    cur = search_dirs[0]
    for _ in range(6):  # bounded walk-up
        ini = cur / "platformio.ini"
        if ini.exists():
            m = _BOARD_RE.search(ini.read_text(errors="replace"))
            if m:
                return m.group(1).strip()
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


# --- importer ----------------------------------------------------------------

def _strip_pin_suffix(name: str) -> str:
    for suf in ("_PIN", "_GPIO"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def _peripheral_type_from_addr(name: str) -> str:
    for suf in ("_ADDRESS", "_ADDR"):
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


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

    # --- collect candidate peripheral types (addresses + pin hints) ---
    addr_by_type: dict[str, int] = {}
    for a in parsed.addresses:
        addr_by_type.setdefault(_peripheral_type_from_addr(a.name), a.address or 0)
    hint_types = {
        m.peripheral_hint for m in parsed.pins
        if m.peripheral_hint and m.bus is None
    }
    candidate_types = sorted(set(addr_by_type) | {h for h in hint_types if h})

    # --- materialize peripherals we have a symbol for ---
    peripherals: list[Peripheral] = []
    periph_by_type: dict[str, Peripheral] = {}
    next_ref = 2
    for t in candidate_types:
        info = resolve_peripheral(t)
        if info is None:
            intent.gaps.append(Gap(
                "unknown_peripheral",
                f"Peripheral {t!r} is referenced by firmware but has no known "
                f"symbol; not placed.",
            ))
            continue
        p = Peripheral(
            ref=f"U{next_ref}", type=t, lib_id=info["lib_id"], value=info["value"],
            footprint=info["footprint"], bus=info["bus"], address=addr_by_type.get(t),
        )
        next_ref += 1
        peripherals.append(p)
        periph_by_type[t] = p
    intent.peripherals = peripherals

    by_bus: dict[str, list[Peripheral]] = {}
    for p in peripherals:
        if p.bus:
            by_bus.setdefault(p.bus, []).append(p)

    # --- nets, with first-wins on duplicate net names ---
    mcu_ref = intent.mcu.ref if intent.mcu else "U1"
    seen_names: set[str] = set()
    for m in parsed.pins:
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


def from_dict(d: dict[str, Any]) -> DesignIntent:
    ver = d.get("schema_version", 1)
    if ver != SCHEMA_VERSION:
        # Warn, don't raise: optional fields default-fill, so older docs still
        # load. A newer doc loaded by older code would silently drop fields.
        logger.warning(
            "design-intent schema_version %s != current %s; loading anyway "
            "(fields may default-fill).", ver, SCHEMA_VERSION,
        )
    mcu_d = d.get("mcu")
    return DesignIntent(
        schema_version=d.get("schema_version", SCHEMA_VERSION),
        source=d.get("source", {}),
        mcu=Mcu(**mcu_d) if mcu_d else None,
        peripherals=[Peripheral(**p) for p in d.get("peripherals", [])],
        nets=[
            Net(
                name=n["name"], kind=n["kind"], confidence=n["confidence"],
                endpoints=[Endpoint(**e) for e in n.get("endpoints", [])],
                bus=n.get("bus"), origin=n.get("origin", "imported"),
            )
            for n in d.get("nets", [])
        ],
        gaps=[Gap(**g) for g in d.get("gaps", [])],
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
