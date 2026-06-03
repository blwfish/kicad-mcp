"""Phase 3 — v1 convention rules.

See ``docs/SPEC_Schematic_Placement.md`` § "Convention rules (v1 set)".

Five rules ship in v1:

  decap_below_ic            — caps connected only to power below the IC
  regulator_input_left      — LDO clusters: VIN passives left, VOUT right
  mcu_crystal_proximate     — crystal placed adjacent to MCU
  connector_edge            — connector clusters pushed to left/right edge
  power_symbols_top_bottom  — power flags placed above (+) / below (−)
                              their referenced IC

Each rule operates on a single labeled cluster's existing pack positions
and either:

  - **Applies** — mutates ``PackResult.positions`` for one or more refs;
    emits ``convention_applied`` with the rule code and affected refs.
  - **Skips** — leaves positions unchanged; emits ``convention_skipped``
    with the rule code and reason. Skipping is normal; telemetry tracks
    which rules skip most often.

No rule is required to apply for a cluster to be valid; the basic pack
already produced something usable. Conventions only refine it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kicad_mcp.utils.netlist_parser import is_power_net
from kicad_mcp.utils.placement.labeling import (
    LABEL_CONNECTOR,
    LABEL_LDO,
    LABEL_MCU,
    LABEL_SWITCHING_REGULATOR,
    ClusterLabel,
)
from kicad_mcp.utils.placement.pack import PackResult

# Rule codes — keep stable so telemetry can compare across versions.
RULE_DECAP_BELOW_IC = "decap_below_ic"
RULE_REGULATOR_INPUT_LEFT = "regulator_input_left"
RULE_MCU_CRYSTAL_PROXIMATE = "mcu_crystal_proximate"
RULE_CONNECTOR_EDGE = "connector_edge"
RULE_POWER_SYMBOLS_TOP_BOTTOM = "power_symbols_top_bottom"

# Component-reference prefix patterns we recognize without consulting
# pattern_recognition. Capacitor refs are conventionally C*, ICs U*,
# crystals Y*/X*, connectors J*/P*. Used only as a heuristic *within*
# a cluster, never to classify a cluster — that's Layer 2/3's job.
_CAP_PREFIXES = ("C",)
_IC_PREFIXES = ("U", "IC")
_CRYSTAL_PREFIXES = ("Y", "X")
_CONNECTOR_PREFIXES = ("J", "P")


@dataclass
class ConventionEvent:
    """Result of attempting a convention rule on a cluster."""

    level: str            # "info"
    code: str             # "convention_applied" | "convention_skipped"
    rule: str             # one of the RULE_* constants
    cluster_id: str
    message: str
    affected_refs: list[str]


def apply_conventions(
    *,
    members_by_cluster: dict[str, list[str]],
    cluster_labels: dict[str, ClusterLabel],
    netlist: dict[str, Any],
    pack: PackResult,
    cluster_tier: dict[str, int],
    column_pitch_mm: float,
    row_pitch_mm: float,
) -> list[ConventionEvent]:
    """Run all v1 conventions over the labeled, packed state.

    Mutates ``pack.positions`` in place (NOT ``pack.bboxes`` — no rule updates
    bboxes, so ``pack.bboxes`` reflects the pre-convention packing; don't drive
    collision/re-fit decisions off it after this runs). Returns a list of
    ``ConventionEvent`` records.
    """
    events: list[ConventionEvent] = []
    components = netlist.get("components") or {}
    nets = netlist.get("nets") or {}

    max_tier = max(cluster_tier.values(), default=0)

    # Snapshot the pre-convention horizontal extent ONCE. _rule_connector_edge
    # pushes tier-0 connectors left of this and max-tier connectors right of it;
    # reading the live (already-mutated) pack inside each call would let a second
    # tier-0 connector cascade further left than the first (and likewise right).
    _orig_xs = [x for x, _ in pack.positions.values()]
    orig_leftmost_x = min(_orig_xs, default=0.0)
    orig_rightmost_x = max(_orig_xs, default=0.0)

    for cid, members in members_by_cluster.items():
        label_obj = cluster_labels.get(cid)
        if label_obj is None:
            continue

        events.extend(_rule_decap_below_ic(
            cid, members, components, nets, pack, row_pitch_mm,
        ))
        events.extend(_rule_regulator_input_left(
            cid, members, label_obj, components, nets, pack, column_pitch_mm,
        ))
        events.extend(_rule_mcu_crystal_proximate(
            cid, members, label_obj, components, pack, column_pitch_mm,
        ))
        events.extend(_rule_connector_edge(
            cid, members, label_obj, pack, cluster_tier, max_tier, column_pitch_mm,
            orig_leftmost_x, orig_rightmost_x,
        ))
        events.extend(_rule_power_symbols_top_bottom(
            cid, members, components, pack, row_pitch_mm,
        ))

    return events


# ---------------------------------------------------------------------------
# Rule 1 — decap_below_ic
# ---------------------------------------------------------------------------

def _rule_decap_below_ic(
    cid: str,
    members: list[str],
    components: dict[str, Any],
    nets: dict[str, Any],
    pack: PackResult,
    row_pitch_mm: float,
) -> list[ConventionEvent]:
    """Caps connected only to power nets shared with a single IC go in a
    row directly below the IC."""
    rule = RULE_DECAP_BELOW_IC
    ics = [r for r in members if _ref_starts_with(r, _IC_PREFIXES)]
    caps = [r for r in members if _ref_starts_with(r, _CAP_PREFIXES)]

    if len(ics) != 1 or not caps:
        return [_skip(cid, rule, "needs exactly 1 IC + ≥1 cap in cluster", [])]

    ic = ics[0]
    decaps = [c for c in caps if _is_pure_power_cap(c, ic, nets)]
    if not decaps:
        return [_skip(cid, rule, "no caps connected only to power nets shared with the IC", [])]

    if ic not in pack.positions:
        return [_skip(cid, rule, f"IC {ic} has no pack position", [])]

    ic_x, ic_y = pack.positions[ic]
    affected: list[str] = []
    for i, cap in enumerate(sorted(decaps)):
        pack.positions[cap] = (ic_x, ic_y + (i + 1) * row_pitch_mm)
        affected.append(cap)

    return [_applied(cid, rule, f"placed {len(affected)} decap(s) below {ic}", affected)]


# ---------------------------------------------------------------------------
# Rule 2 — regulator_input_left
# ---------------------------------------------------------------------------

def _rule_regulator_input_left(
    cid: str,
    members: list[str],
    label: ClusterLabel,
    components: dict[str, Any],
    nets: dict[str, Any],
    pack: PackResult,
    column_pitch_mm: float,
) -> list[ConventionEvent]:
    """For LDO / regulator clusters, place VIN-side passives left of the
    regulator and VOUT-side passives to its right."""
    rule = RULE_REGULATOR_INPUT_LEFT
    if label.label not in (LABEL_LDO, LABEL_SWITCHING_REGULATOR):
        return []  # not applicable; no event

    anchor = label.anchor
    if anchor is None or anchor not in pack.positions:
        return [_skip(cid, rule, "no anchor or anchor unplaced", [])]

    vin_nets, vout_nets = _identify_vin_vout_nets(anchor, components, nets)
    if not vin_nets or not vout_nets:
        return [_skip(cid, rule, "could not identify VIN or VOUT pin via pinfunction", [])]

    ax, ay = pack.positions[anchor]
    side_offset = column_pitch_mm * 0.4
    affected: list[str] = []
    for ref in members:
        if ref == anchor:
            continue
        side = _component_net_side(ref, vin_nets, vout_nets, nets)
        if side == "vin":
            _, ry = pack.positions.get(ref, (ax, ay))
            pack.positions[ref] = (ax - side_offset, ry)
            affected.append(ref)
        elif side == "vout":
            _, ry = pack.positions.get(ref, (ax, ay))
            pack.positions[ref] = (ax + side_offset, ry)
            affected.append(ref)

    if not affected:
        return [_skip(cid, rule, "no non-anchor members on VIN or VOUT", [])]
    return [_applied(cid, rule, f"split {len(affected)} regulator passives by VIN/VOUT", affected)]


# ---------------------------------------------------------------------------
# Rule 3 — mcu_crystal_proximate
# ---------------------------------------------------------------------------

def _rule_mcu_crystal_proximate(
    cid: str,
    members: list[str],
    label: ClusterLabel,
    components: dict[str, Any],
    pack: PackResult,
    column_pitch_mm: float,
) -> list[ConventionEvent]:
    """If the MCU cluster contains a crystal, place the crystal one
    column to the left of the MCU."""
    rule = RULE_MCU_CRYSTAL_PROXIMATE
    if label.label != LABEL_MCU:
        return []

    mcu = label.anchor
    if mcu is None or mcu not in pack.positions:
        return [_skip(cid, rule, "MCU anchor not placed", [])]

    crystals = [r for r in members if _looks_like_crystal(r, components)]
    if not crystals:
        return [_skip(cid, rule, "no crystal member in MCU cluster", [])]

    mcu_x, mcu_y = pack.positions[mcu]
    affected: list[str] = []
    for crystal in crystals:
        pack.positions[crystal] = (mcu_x - column_pitch_mm * 0.5, mcu_y)
        affected.append(crystal)
    return [_applied(cid, rule, f"placed crystal(s) adjacent to MCU {mcu}", affected)]


# ---------------------------------------------------------------------------
# Rule 4 — connector_edge
# ---------------------------------------------------------------------------

def _rule_connector_edge(
    cid: str,
    members: list[str],
    label: ClusterLabel,
    pack: PackResult,
    cluster_tier: dict[str, int],
    max_tier: int,
    column_pitch_mm: float,
    orig_leftmost_x: float,
    orig_rightmost_x: float,
) -> list[ConventionEvent]:
    """Push connector-anchored clusters to the leftmost or rightmost
    column based on signal-flow position.

    ``orig_leftmost_x``/``orig_rightmost_x`` are the PRE-convention pack extent
    (snapshotted by the caller), so two tier-0 connectors both target the same
    column instead of cascading off the prior one's already-pushed position."""
    rule = RULE_CONNECTOR_EDGE
    if label.label != LABEL_CONNECTOR:
        return []

    tier = cluster_tier.get(cid)
    if tier is None:
        return [_skip(cid, rule, "cluster has no tier", [])]

    # Heuristic: tier 0 → push leftmost (negative offset from origin);
    # tier == max → push rightmost (one extra column right).
    # If middle tier, leave alone (rule doesn't apply cleanly).
    if tier != 0 and tier != max_tier:
        return [_skip(cid, rule, f"connector in middle tier {tier}", [])]

    affected: list[str] = []
    if tier == 0:
        # Push 0.5 column left of the pre-convention leftmost column.
        if not pack.positions:
            return [_skip(cid, rule, "empty pack", [])]
        target_x = orig_leftmost_x - column_pitch_mm * 0.5
    else:
        # Push 0.5 column right of the pre-convention rightmost column.
        if not pack.positions:
            return [_skip(cid, rule, "empty pack", [])]
        target_x = orig_rightmost_x + column_pitch_mm * 0.5

    for ref in members:
        if ref in pack.positions:
            _, ry = pack.positions[ref]
            pack.positions[ref] = (target_x, ry)
            affected.append(ref)
    side = "left" if tier == 0 else "right"
    return [_applied(cid, rule, f"pushed connector cluster to {side} edge", affected)]


# ---------------------------------------------------------------------------
# Rule 5 — power_symbols_top_bottom
# ---------------------------------------------------------------------------

def _rule_power_symbols_top_bottom(
    cid: str,
    members: list[str],
    components: dict[str, Any],
    pack: PackResult,
    row_pitch_mm: float,
) -> list[ConventionEvent]:
    """Power-flag symbols (lib_id starts with 'power:') get placed
    above the cluster (positive rails) or below (ground)."""
    rule = RULE_POWER_SYMBOLS_TOP_BOTTOM
    powers = [r for r in members if _is_power_symbol(r, components)]
    if not powers:
        return []  # silent — not applicable

    # Find the cluster's column x and y range from non-power members.
    non_power = [r for r in members if r not in powers and r in pack.positions]
    if not non_power:
        return [_skip(cid, rule, "no non-power members to align against", [])]

    xs = [pack.positions[r][0] for r in non_power]
    ys = [pack.positions[r][1] for r in non_power]
    cluster_x = sum(xs) / len(xs)
    top_y = min(ys) - row_pitch_mm
    bottom_y = max(ys) + row_pitch_mm

    affected: list[str] = []
    pos_offset = 0
    neg_offset = 0
    for ref in powers:
        kind = _power_symbol_kind(ref, components)
        if kind == "ground":
            pack.positions[ref] = (cluster_x, bottom_y + neg_offset * row_pitch_mm)
            neg_offset += 1
        else:
            pack.positions[ref] = (cluster_x, top_y - pos_offset * row_pitch_mm)
            pos_offset += 1
        affected.append(ref)
    return [_applied(cid, rule, f"placed {len(affected)} power symbol(s)", affected)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ref_starts_with(ref: str, prefixes: tuple[str, ...]) -> bool:
    """Component-reference prefix test, case-insensitive, with the
    requirement that the next char is a digit (so 'CR' for a current
    regulator doesn't match the 'C' cap prefix). The prefix forms KiCad
    enforces are letter-prefix-then-digit."""
    upper = ref.upper()
    for p in prefixes:
        if upper.startswith(p) and len(ref) > len(p) and ref[len(p)].isdigit():
            return True
    return False


def _is_pure_power_cap(cap_ref: str, ic_ref: str, nets: dict[str, Any]) -> bool:
    """A cap qualifies as a decap when every net it touches is a power
    net AND the IC also touches that net."""
    cap_nets: set[str] = set()
    ic_nets: set[str] = set()
    for net_name, pins in nets.items():
        for pin in pins:
            ref = pin.get("component")
            if ref == cap_ref:
                cap_nets.add(net_name)
            if ref == ic_ref:
                ic_nets.add(net_name)
    if not cap_nets:
        return False
    if not all(is_power_net(n) for n in cap_nets):
        return False
    return bool(cap_nets & ic_nets)


def _identify_vin_vout_nets(
    anchor: str,
    components: dict[str, Any],
    nets: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Find the regulator's VIN/VOUT nets via the ``pinfunction``
    attribute on the netlist node entries. Returns ``(vin, vout)``
    nets; either set may be empty if pinfunction is unavailable."""
    vin: set[str] = set()
    vout: set[str] = set()
    for net_name, pins in nets.items():
        for pin in pins:
            if pin.get("component") != anchor:
                continue
            fn = (pin.get("pinfunction") or "").upper()
            if fn in ("VIN", "VI", "IN"):
                vin.add(net_name)
            elif fn in ("VOUT", "VO", "OUT"):
                vout.add(net_name)
    return vin, vout


def _component_net_side(
    ref: str,
    vin_nets: set[str],
    vout_nets: set[str],
    nets: dict[str, Any],
) -> str | None:
    """Return 'vin', 'vout', or None for a non-anchor cluster member."""
    touched_vin = False
    touched_vout = False
    for net_name, pins in nets.items():
        for pin in pins:
            if pin.get("component") != ref:
                continue
            if net_name in vin_nets:
                touched_vin = True
            if net_name in vout_nets:
                touched_vout = True
    if touched_vin and not touched_vout:
        return "vin"
    if touched_vout and not touched_vin:
        return "vout"
    return None


def _looks_like_crystal(ref: str, components: dict[str, Any]) -> bool:
    """Crystal detection: ref starts with Y or X (KiCad convention) OR
    the component's lib_id mentions Crystal."""
    if _ref_starts_with(ref, _CRYSTAL_PREFIXES):
        return True
    comp = components.get(ref) or {}
    lib_id = (comp.get("lib_id") or "").lower()
    return "crystal" in lib_id


def _is_power_symbol(ref: str, components: dict[str, Any]) -> bool:
    """KiCad power flags use lib_id 'power:GND', 'power:+3V3', etc.
    They also conventionally start with '#PWR'."""
    if ref.startswith("#PWR"):
        return True
    comp = components.get(ref) or {}
    lib_id = (comp.get("lib_id") or "").lower()
    return lib_id.startswith("power:")


def _power_symbol_kind(ref: str, components: dict[str, Any]) -> str:
    """Return 'ground' for GND-family symbols, 'rail' for everything else.

    Heuristic: the power symbol's value or lib_id name. GND/GNDA/GNDD/
    AGND/DGND/EARTH all classify as ground.
    """
    comp = components.get(ref) or {}
    value = (comp.get("value") or "").upper()
    lib_id = (comp.get("lib_id") or "").lower()
    name = lib_id.split(":")[-1].upper() if ":" in lib_id else ""
    text = f"{value} {name}"
    if "GND" in text or "EARTH" in text:
        return "ground"
    return "rail"


def _applied(
    cluster_id: str, rule: str, message: str, refs: list[str],
) -> ConventionEvent:
    return ConventionEvent(
        level="info",
        code="convention_applied",
        rule=rule,
        cluster_id=cluster_id,
        message=f"{rule}: {message}",
        affected_refs=refs,
    )


def _skip(
    cluster_id: str, rule: str, reason: str, refs: list[str],
) -> ConventionEvent:
    return ConventionEvent(
        level="info",
        code="convention_skipped",
        rule=rule,
        cluster_id=cluster_id,
        message=f"{rule} skipped: {reason}",
        affected_refs=refs,
    )
