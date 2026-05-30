"""``board.yaml`` sidecar (Phase 6b) — the data home for what firmware is
structurally *blind* to: connectors, the board's power source, dimensions,
mechanical. Firmware never ``#define``s these, so no parser recovers them; the
person who has the knowledge supplies them once, as data, next to ``config.h``.

Honest-by-construction extends cleanly: these facts come from a file with
provenance (gaps they fill are marked ``resolved_by: "board.yaml"``), never
invented in code. Applied as a *separate step* after ``build_intent`` (which
stays untouched, equivalence-preserved).

```yaml
# board.yaml
power_source: usb_c            # usb_c | barrel | header | battery — sources +5V
board_size_mm: [90, 75]
extra_connectors:
  - ref: J_PWR
    lib_id: Connector:Barrel_Jack
    footprint: "Connector_BarrelJack:BarrelJack_Horizontal"
    nets: {"1": "+5V", "2": "GND"}
```
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from kicad_mcp.utils.firmware.intent import DesignIntent, Endpoint, Net, Peripheral

_POWER_SOURCES = frozenset({"usb_c", "usb", "barrel", "header", "battery", "screw_terminal"})
_RAILS = frozenset({"+3V3", "+5V", "GND", "VBUS", "VCC"})
_SIDECAR_NAME = "board.yaml"

# A valid KiCad reference is a letter prefix + number (e.g. J1) — no underscores.
_VALID_REF = re.compile(r"^[A-Za-z]+\d+$")


def _normalize_ref(ref: str, existing: set[str]) -> str:
    """Return a KiCad-valid, collision-free reference. A friendly ``J_PWR`` is
    normalized to ``J<next-free>`` (its letter prefix + a fresh number)."""
    if _VALID_REF.match(ref):
        return ref
    m = re.match(r"^([A-Za-z]+)", ref)
    prefix = m.group(1) if m else "J"
    n = 1
    while f"{prefix}{n}" in existing:
        n += 1
    return f"{prefix}{n}"


class SidecarError(ValueError):
    """A malformed board.yaml. Raised loudly at load — never silently ignored."""


@dataclass
class BoardSidecar:
    power_source: Optional[str] = None
    board_size_mm: Optional[list[float]] = None
    extra_connectors: list[dict[str, Any]] = field(default_factory=list)


def _validate(d: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    ps = d.get("power_source")
    if ps is not None and ps not in _POWER_SOURCES:
        errs.append(f"power_source {ps!r} not in {sorted(_POWER_SOURCES)}")
    bs = d.get("board_size_mm")
    if bs is not None and not (isinstance(bs, list) and len(bs) == 2
                               and all(isinstance(x, (int, float)) for x in bs)):
        errs.append("board_size_mm must be [width, height] numbers")
    for i, c in enumerate(d.get("extra_connectors", []) or []):
        where = f"extra_connectors[{i}]"
        if not isinstance(c, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        for req in ("ref", "lib_id", "nets"):
            if req not in c:
                errs.append(f"{where}: missing required field {req!r}")
        if ":" not in str(c.get("lib_id", ":")):
            errs.append(f"{where}: lib_id must be 'Library:Symbol'")
        nets = c.get("nets")
        if not isinstance(nets, dict) or not nets:
            errs.append(f"{where}: nets must be a non-empty {{pin: net_name}} mapping")
        else:
            for pin, net in nets.items():
                if not isinstance(pin, str) or not isinstance(net, str):
                    errs.append(f"{where}: nets entries must be pin_str -> net_str")
    return errs


def load_sidecar(path: str) -> BoardSidecar:
    """Parse + validate a board.yaml. Raises SidecarError on malformed input."""
    try:
        data = yaml.safe_load(Path(path).read_text())
    except yaml.YAMLError as e:
        raise SidecarError(f"{path} is not valid YAML: {e}") from e
    if data is None:
        return BoardSidecar()
    if not isinstance(data, dict):
        raise SidecarError(f"{path} must be a YAML mapping")
    errs = _validate(data)
    if errs:
        raise SidecarError(f"{path}:\n  " + "\n  ".join(errs))
    return BoardSidecar(
        power_source=data.get("power_source"),
        board_size_mm=data.get("board_size_mm"),
        extra_connectors=list(data.get("extra_connectors", []) or []),
    )


def find_sidecar(config_path: str) -> Optional[str]:
    """Look for a board.yaml next to config.h (or the firmware root one dir up)."""
    cfg = Path(config_path)
    for d in (cfg.parent, cfg.parent.parent):
        cand = d / _SIDECAR_NAME
        if cand.is_file():
            return str(cand)
    return None


def _resolve_gap(intent: DesignIntent, kind: str, by: str, refs: list[str]) -> None:
    for g in intent.gaps:
        if g.kind == kind and not g.resolved:
            g.resolved = True
            g.resolved_by = by
            g.resolved_components = refs
            return


def apply_sidecar(
    intent: DesignIntent, sidecar: BoardSidecar, *, source_name: str = "board.yaml",
) -> DesignIntent:
    """Merge sidecar facts into ``intent`` (mutated + returned). Records power
    source / board size in ``source``, materializes ``extra_connectors`` as
    user-origin parts + nets, and marks the ``connectors`` gap resolved."""
    if sidecar.power_source is not None:
        intent.source["power_source"] = sidecar.power_source
    if sidecar.board_size_mm is not None:
        intent.source["board_size_mm"] = list(sidecar.board_size_mm)

    nets_by_name = {n.name: n for n in intent.nets}
    existing_refs = {p.ref for p in intent.peripherals}
    if intent.mcu is not None:
        existing_refs.add(intent.mcu.ref)
    added_refs: list[str] = []
    for c in sidecar.extra_connectors:
        friendly = str(c["ref"])
        ref = _normalize_ref(friendly, existing_refs)   # KiCad-valid (J1, …)
        existing_refs.add(ref)
        p = Peripheral(
            ref=ref, type="CONN", lib_id=c["lib_id"],
            value=c.get("value", friendly),              # keep the friendly name
            footprint=c.get("footprint"), origin="user",
        )
        intent.peripherals.append(p)
        added_refs.append(ref)
        for pin, net_name in c["nets"].items():
            ep = Endpoint(ref=ref, pin=str(pin))
            tgt = nets_by_name.get(net_name)
            if tgt is not None:
                tgt.endpoints.append(ep)
            else:
                kind = "power" if net_name in _RAILS else "passive"
                n = Net(name=net_name, kind=kind, confidence="high",
                        origin="user", endpoints=[ep])
                intent.nets.append(n)
                nets_by_name[net_name] = n

    if added_refs:
        _resolve_gap(intent, "connectors", source_name, added_refs)
    return intent
