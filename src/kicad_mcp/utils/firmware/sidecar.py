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
placement:                     # WHERE each device lives (board-level, firmware-blind)
  CMCA_MIC:                    # keyed by bus stem (or a peripheral ref like U2)
    locus: remote              # not placed; its signals cross to a screw terminal
    device: INMP441            # human-supplied identity (firmware names it only in a comment)
  CMCA_I2S:
    locus: on_board_with_remote_io   # amp stays on board; speaker leads cross out
    device: MAX98357A
    external_io: [outp, outn]
placement_hints:               # per-ref PCB placement overrides (keyed by KiCad ref)
  J6:                          # the ref as it appears in the generated schematic
    edge: none                 # top|bottom|left|right|none ("none" = keep interior)
    rotation: 90               # 0|90|180|270 (omit to use the auto heuristic)
    fixed: [40, 12]            # absolute [x, y] mm (wins over edge/rotation)
mounting_holes:                # corner fixture holes (firmware-blind mechanical)
  count: 4                     # 4 (default) | 2 (diagonal) | 0 (none)
  drill_mm: 3.2                # M3 = 3.2, M2.5 = 2.7
  inset_mm: 3.5                # hole-centre inset from each board edge
  keepout_mm: 1.5              # no-copper margin beyond the drill
```

Unknown top-level keys are REJECTED (a typo like ``board_size`` for
``board_size_mm`` is a loud error, never a silently-ignored directive).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from kicad_mcp.utils.firmware.cards import valid_lib_id
from kicad_mcp.utils.firmware.connectors import (
    ConnectorPosition,
    VALID_CONNECTOR_TYPES,
    synthesize_connector,
)
from kicad_mcp.utils.firmware.intent import (
    DesignIntent,
    Gap,
    Net,
    Placement,
    VALID_LOCI,
)
from kicad_mcp.utils.firmware.power_names import RAILS as _RAILS

_POWER_SOURCES = frozenset({"usb_c", "usb", "barrel", "header", "battery", "screw_terminal"})
_CONNECTOR_KINDS = VALID_CONNECTOR_TYPES
_SIDECAR_NAME = "board.yaml"

# A valid KiCad reference is a letter prefix + number (e.g. J1) — no underscores.
_VALID_REF = re.compile(r"^[A-Za-z]+\d+$")


def _normalize_ref(ref: str, existing: set[str]) -> str:
    """Return a KiCad-valid, collision-free reference. An already-valid ref is
    kept ONLY if it doesn't collide; a friendly ``J_PWR`` (or a colliding ``J1``)
    is reallocated to ``<prefix><next-free>``."""
    if _VALID_REF.match(ref) and ref not in existing:
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
    # placement directives, keyed by bus stem (CMCA_MIC) or peripheral ref (U2).
    placement: dict[str, dict[str, Any]] = field(default_factory=dict)
    # PCB placement-hint overrides {edge, rotation, fixed}, keyed by KiCad ref.
    placement_hints: dict[str, dict[str, Any]] = field(default_factory=dict)
    # corner mounting holes {count, drill_mm, inset_mm, keepout_mm}; None = use
    # the pipeline default (4 × M3). Defaults are resolved in pcb_pipeline, not
    # here — this layer only validates + carries the user's overrides.
    mounting_holes: Optional[dict[str, Any]] = None
    # part-resolution overrides, keyed by bus stem (e.g. CMCA_MIC): the user
    # DECLARES the bus's part {part: "INMP441"} when firmware doesn't name it (or
    # to correct an assumption/ambiguity). Provenance "user" — wins over corpus.
    bus_part_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # terminal distribution across board edges: "single_edge" (default — all field
    # terminals on the antenna-opposite edge) or "multi_edge" (spill onto the side
    # edges to square the board). See SPEC_Multi_Edge_Terminal_Distribution.md.
    terminal_distribution: Optional[str] = None
    # centre each edge's field-terminal group within its edge (vs the default
    # pack-from-one-end). Layout balance only — does not change board size.
    terminal_centering: Optional[bool] = None
    # re-fit the auto-sized board to the MEASURED interior cluster after placement
    # (a tighter second pass). Size lever — only applies to auto-sized boards. See
    # SPEC_Post_Placement_Board_Refit.md.
    board_refit: Optional[bool] = None


# Top-level board.yaml keys. An unknown key is a loud error (catches a typo like
# `board_size` for `board_size_mm` instead of silently ignoring the directive).
# MUST stay in sync with the BoardSidecar fields above AND the data.get() calls
# in load_sidecar — test_known_keys_all_accepted guards that all three agree.
_KNOWN_SIDECAR_KEYS = frozenset({
    "power_source", "board_size_mm", "extra_connectors",
    "placement", "placement_hints", "mounting_holes", "bus_part_overrides",
    "terminal_distribution", "terminal_centering", "board_refit",
})

_TERMINAL_DISTRIBUTIONS = frozenset({"single_edge", "multi_edge"})

_KNOWN_BUS_OVERRIDE_KEYS = frozenset({"part", "footprint"})

_KNOWN_MOUNTING_HOLES_KEYS = frozenset({"count", "drill_mm", "inset_mm", "keepout_mm"})


def _validate_placement(d: dict[str, Any], errs: list[str]) -> None:
    """Structural validation of the ``placement`` section (no intent yet — the
    key-exists-in-intent check happens in ``apply_sidecar``)."""
    placement = d.get("placement")
    if placement is None:
        return
    if not isinstance(placement, dict):
        errs.append("placement must be a mapping of device-key -> directive")
        return
    for key, spec in placement.items():
        where = f"placement[{key!r}]"
        if not isinstance(spec, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        locus = spec.get("locus")
        if locus not in VALID_LOCI:
            errs.append(f"{where}: locus {locus!r} not in {list(VALID_LOCI)}")
        conn = spec.get("connector")
        if conn is not None and conn not in _CONNECTOR_KINDS:
            errs.append(f"{where}: connector {conn!r} not in {sorted(_CONNECTOR_KINDS)}")
        if "device" in spec and not isinstance(spec["device"], str):
            errs.append(f"{where}: device must be a string")
        if "footprint" in spec and not isinstance(spec["footprint"], str):
            errs.append(f"{where}: footprint must be a string")
        eio = spec.get("external_io")
        if eio is not None and not (isinstance(eio, list)
                                    and all(isinstance(r, str) for r in eio)):
            errs.append(f"{where}: external_io must be a list of role strings")
        # external_io is OPTIONAL, documentary metadata: the realized
        # on_board_with_remote_io path (an I2S_OUT amp bus) crosses its speaker
        # outputs intrinsically. It is NOT required — requiring a field no
        # template consumes would be a footgun (see _REALIZED_LOCI for what is
        # actually realized; an unrealized locus is flagged at apply time).


def _validate_hints(d: dict[str, Any], errs: list[str]) -> None:
    """Structural validation of ``placement_hints``: keyed by KiCad ref, each
    entry a mapping.  Per-directive value validation (edge/rotation/fixed) is
    deferred to ``edge_terminal.normalize_hint`` at build time (drop-with-warning,
    so a misspelled value doesn't abort the whole build); here only the
    ref→mapping shape is enforced."""
    hints = d.get("placement_hints")
    if hints is None:
        return
    if not isinstance(hints, dict):
        errs.append("placement_hints must be a mapping of ref -> directive")
        return
    for key, spec in hints.items():
        if not isinstance(spec, dict):
            errs.append(f"placement_hints[{key!r}]: must be a mapping")


def _validate_mounting_holes(d: dict[str, Any], errs: list[str]) -> None:
    """``mounting_holes`` is optional; when present it's a mapping with an integer
    ``count`` in {0, 2, 4} and positive numeric ``drill_mm`` / ``inset_mm`` /
    ``keepout_mm``. Missing fields fall back to pipeline defaults (validated
    there); only supplied fields are checked here."""
    mh = d.get("mounting_holes")
    if mh is None:
        return
    if not isinstance(mh, dict):
        errs.append("mounting_holes must be a mapping")
        return
    # Reject unknown sub-keys too — consistency with the top-level rejection, so a
    # typo like `counnt: 4` is loud, not silently dropped (and silently default).
    unknown = set(mh) - _KNOWN_MOUNTING_HOLES_KEYS
    if unknown:
        errs.append(
            f"mounting_holes: unknown key(s) {sorted(unknown)} — "
            f"valid: {sorted(_KNOWN_MOUNTING_HOLES_KEYS)}"
        )
    # bool is an int subclass and False == 0, so `False in (0,2,4)` is True — guard
    # explicitly or `count: false` would slip through as 0.
    if "count" in mh and (isinstance(mh["count"], bool) or mh["count"] not in (0, 2, 4)):
        errs.append(f"mounting_holes.count {mh['count']!r} must be 0, 2, or 4")
    for key in ("drill_mm", "inset_mm", "keepout_mm"):
        if key in mh:
            v = mh[key]
            # bool is an int subclass — reject True/False masquerading as a number
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                errs.append(f"mounting_holes.{key} must be a positive number")


def _validate(d: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    unknown = set(d) - _KNOWN_SIDECAR_KEYS
    if unknown:
        errs.append(
            f"unknown board.yaml key(s) {sorted(unknown)} — "
            f"valid keys: {sorted(_KNOWN_SIDECAR_KEYS)}"
        )
    ps = d.get("power_source")
    if ps is not None and ps not in _POWER_SOURCES:
        errs.append(f"power_source {ps!r} not in {sorted(_POWER_SOURCES)}")
    bs = d.get("board_size_mm")
    if bs is not None and not (isinstance(bs, list) and len(bs) == 2
                               and all(isinstance(x, (int, float)) for x in bs)):
        errs.append("board_size_mm must be [width, height] numbers")
    td = d.get("terminal_distribution")
    if td is not None and td not in _TERMINAL_DISTRIBUTIONS:
        errs.append(f"terminal_distribution {td!r} not in "
                    f"{sorted(_TERMINAL_DISTRIBUTIONS)}")
    tc = d.get("terminal_centering")
    if tc is not None and not isinstance(tc, bool):
        errs.append(f"terminal_centering must be true/false, got {tc!r}")
    br = d.get("board_refit")
    if br is not None and not isinstance(br, bool):
        errs.append(f"board_refit must be true/false, got {br!r}")
    for i, c in enumerate(d.get("extra_connectors", []) or []):
        where = f"extra_connectors[{i}]"
        if not isinstance(c, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        # footprint is REQUIRED: a placed connector with no footprint silently
        # produces an unrouteable board (no pads at the PCB step).
        for req in ("ref", "lib_id", "nets", "footprint"):
            if req not in c:
                errs.append(f"{where}: missing required field {req!r}")
        if "lib_id" in c and not valid_lib_id(c["lib_id"]):
            errs.append(f"{where}: lib_id {c['lib_id']!r} must be 'Library:Symbol'")
        nets = c.get("nets")
        if not isinstance(nets, dict) or not nets:
            errs.append(f"{where}: nets must be a non-empty {{pin: net_name}} mapping")
        else:
            for pin, net in nets.items():
                if not isinstance(pin, str) or not isinstance(net, str):
                    errs.append(f"{where}: nets entries must be pin_str -> net_str")
    _validate_placement(d, errs)
    _validate_hints(d, errs)
    _validate_mounting_holes(d, errs)
    _validate_bus_part_overrides(d, errs)
    return errs


def _validate_bus_part_overrides(d: dict[str, Any], errs: list[str]) -> None:
    """``bus_part_overrides`` is optional: a mapping of bus stem → ``{part?,
    footprint?}`` (both strings). Whether ``part`` names a recognized device is
    NOT checked here (that needs the card registry); an unrealizable part surfaces
    as a ``part_unavailable`` gap at realization — consistent with the honest-gap
    model, not a silent drop."""
    ov = d.get("bus_part_overrides")
    if ov is None:
        return
    if not isinstance(ov, dict):
        errs.append("bus_part_overrides must be a mapping of bus-stem -> {part?, footprint?}")
        return
    for stem, spec in ov.items():
        where = f"bus_part_overrides[{stem!r}]"
        if not isinstance(spec, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        unknown = set(spec) - _KNOWN_BUS_OVERRIDE_KEYS
        if unknown:
            errs.append(f"{where}: unknown key(s) {sorted(unknown)} — "
                        f"valid: {sorted(_KNOWN_BUS_OVERRIDE_KEYS)}")
        if not spec:
            errs.append(f"{where}: empty — give a part and/or footprint")
        for k in ("part", "footprint"):
            if k in spec and not (isinstance(spec[k], str) and spec[k].strip()):
                errs.append(f"{where}: {k} must be a non-empty string")


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
        placement=dict(data.get("placement", {}) or {}),
        placement_hints=dict(data.get("placement_hints", {}) or {}),
        mounting_holes=(dict(data["mounting_holes"])
                        if data.get("mounting_holes") is not None else None),
        bus_part_overrides=dict(data.get("bus_part_overrides", {}) or {}),
        terminal_distribution=data.get("terminal_distribution"),
        terminal_centering=data.get("terminal_centering"),
        board_refit=data.get("board_refit"),
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
    if sidecar.mounting_holes is not None:
        intent.source["mounting_holes"] = dict(sidecar.mounting_holes)
    if sidecar.terminal_distribution is not None:
        intent.source["terminal_distribution"] = sidecar.terminal_distribution
    if sidecar.terminal_centering is not None:
        intent.source["terminal_centering"] = sidecar.terminal_centering
    if sidecar.board_refit is not None:
        intent.source["board_refit"] = sidecar.board_refit

    nets_by_name = {n.name: n for n in intent.nets}
    existing_refs = {p.ref for p in intent.peripherals}
    if intent.mcu is not None:
        existing_refs.add(intent.mcu.ref)
    added_refs: list[str] = []
    for c in sidecar.extra_connectors:
        friendly = str(c["ref"])
        ref = _normalize_ref(friendly, existing_refs)   # KiCad-valid (J1, …)
        existing_refs.add(ref)
        # Route through the unified §4 helper for ref/pin-count validation + a silk
        # legend (the two mechanics this fragment lacked). The user supplies the
        # symbol + explicit pin→net map, so lib_id/pins are passed verbatim and the
        # pre-normalized ref is handed back by a fixed allocator.
        positions = [ConnectorPosition(net_name=str(net), label=str(net), pin=str(pin))
                     for pin, net in c["nets"].items()]

        def _fixed_alloc(_prefix: str, _r: str = ref) -> str:
            return _r   # ref was already normalized above; honor it verbatim

        conn, joins, legend = synthesize_connector(
            positions, alloc=_fixed_alloc, connector_type="pluggable",
            device=str(c.get("value", friendly)), lib_id=c["lib_id"],
            footprint=c.get("footprint"), value=c.get("value", friendly),
            origin="user",
        )
        intent.peripherals.append(conn)
        intent.connector_legends.append(legend)
        added_refs.append(ref)
        for net_name, ep in joins:
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

    _apply_placement(intent, sidecar.placement)

    # PCB placement-hint overrides (edge/rotation/fixed), keyed by KiCad ref.
    # Stored raw; normalized + warned-on at build time (build_pcb_from_schematic).
    for ref, hint in sidecar.placement_hints.items():
        intent.placement_hints[ref] = dict(hint)

    # Part overrides (C7): the user DECLARES a bus's part. Stamps provenance
    # "user" so the resolver (which runs after this) leaves it untouched. Applied
    # before resolution, so the human's choice wins over corpus evidence. A stem
    # matching no bus is surfaced as a gap, never silently ignored.
    bus_by_name = {b.name: b for b in intent.buses}
    for stem, spec in sidecar.bus_part_overrides.items():
        bus = bus_by_name.get(stem)
        if bus is None:
            intent.gaps.append(Gap(
                "bus_part_override_unknown",
                f"board.yaml bus_part_overrides key {stem!r} matches no bus "
                f"{sorted(bus_by_name)}; ignored."))
            continue
        if spec.get("part"):
            bus.resolved_part = str(spec["part"])
            bus.part_provenance = "user"
            bus.part_is_assumption = False

    return intent


# Bus types that are USUALLY field-wired transducers/sensors. Open Decision 1:
# locus defaults to on_board (never a silent remote), but the importer NUDGES the
# user to declare a locus for these so a remote device isn't forgotten.
_COMMONLY_REMOTE_BUS_TYPES = frozenset({"I2S_IN", "I2S_OUT", "UART"})


def advise_unspecified_placement(intent: DesignIntent) -> None:
    """Append one advisory gap listing commonly-field-wired buses that have no
    ``placement:`` directive — a prompt to set their locus, NOT an assumption.
    Idempotent: does nothing if such an advisory is already present (so a
    re-import doesn't stack duplicates)."""
    if any(g.kind == "placement_unspecified" for g in intent.gaps):
        return
    unset = sorted(b.name for b in intent.buses
                   if b.type in _COMMONLY_REMOTE_BUS_TYPES
                   and b.name not in intent.placements)
    if unset:
        intent.gaps.append(Gap(
            "placement_unspecified",
            f"Buses {unset} are commonly field-wired (mic / speaker / sensor) but "
            "have no board.yaml `placement:` directive — defaulting to on_board. "
            "Set locus: remote (or on_board_with_remote_io) for any wired in from "
            "off-board.",
        ))


# (target-kind, bus-type, locus) combinations a template ACTUALLY realizes. A
# directive outside this set passes structural validation but no template
# consumes it, so the device would be placed/crossed the WRONG way silently —
# we flag it instead (honest-by-construction). bus-type is None for a ref target.
#   bus  I2S_IN  remote                  -> i2s_mic terminal
#   bus  UART    remote                  -> uart_device_header terminal
#   bus  I2S_OUT on_board_with_remote_io -> i2s_output_amps speaker terminals
#   ref  *       remote                  -> remote_peripherals terminal
_REALIZED_LOCI: frozenset[tuple[str, Optional[str], str]] = frozenset({
    ("bus", "I2S_IN", "remote"),
    ("bus", "UART", "remote"),
    ("bus", "I2S_OUT", "on_board_with_remote_io"),
    ("ref", None, "remote"),
})


def _apply_placement(intent: DesignIntent, placement: dict[str, dict[str, Any]]) -> None:
    """Record placement directives into ``intent.placements`` (the single source
    consulted by the locus-aware templates + generator) and project the realized
    locus onto each addressed peripheral. A key that binds to no target — or a
    target/locus combination no template realizes — is surfaced as a gap, never
    silently ignored."""
    if not placement:
        return
    bus_by_name = {b.name: b for b in intent.buses}
    periph_by_ref = {p.ref: p for p in intent.peripherals}
    for key, spec in placement.items():
        pl = Placement(
            locus=spec.get("locus", "on_board"),
            device=spec.get("device"),
            connector=spec.get("connector", "screw_terminal"),
            footprint=spec.get("footprint"),
            external_io=list(spec.get("external_io", []) or []),
        )
        intent.placements[key] = pl
        if key in periph_by_ref:
            periph_by_ref[key].locus = pl.locus
            kind, bus_type = "ref", None
        elif key in bus_by_name:
            kind, bus_type = "bus", bus_by_name[key].type
        else:
            intent.gaps.append(Gap(
                "placement_unknown_target",
                f"board.yaml placement key {key!r} matches no bus stem "
                f"{sorted(bus_by_name)} or peripheral ref "
                f"{sorted(periph_by_ref)}; directive ignored.",
            ))
            continue
        # on_board is the default no-op; any other locus must have a realization.
        if pl.locus != "on_board" and (kind, bus_type, pl.locus) not in _REALIZED_LOCI:
            tgt = f"{bus_type} bus" if kind == "bus" else "peripheral"
            intent.gaps.append(Gap(
                "placement_unsupported",
                f"board.yaml placement {key!r}: locus {pl.locus!r} on a {tgt} is "
                "not realized by any template (the device would be placed "
                "on-board unchanged); supported: I2S_IN/UART bus + remote, "
                "I2S_OUT bus + on_board_with_remote_io, peripheral ref + remote.",
            ))
