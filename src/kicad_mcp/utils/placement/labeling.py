"""Layers 2/3/4 — cluster labeling pipeline.

See ``docs/SPEC_Schematic_Placement.md`` § "4-layer architecture".

Each cluster from Layer 1 (topology) gets a single ``ClusterLabel`` per call
to ``label_clusters``. The pipeline runs:

  Layer 1 → topology_only baseline (label="unclassified", confidence=0.0)
  Layer 2 → pattern_recognition wrap (label_source="pattern_recognition")
  Layer 3 → LCSC resolved-part via description-keyword classifier
            (label_source="resolved_part")
  Layer 4 → caller hints (label_source="caller_hint", confidence=1.0)

Higher-layer labels win over lower-layer labels regardless of confidence
(spec § Phase 1). Within a layer, the per-cluster confidence is recorded
as-is.

This module is the single source of truth for the description-keyword
classifier (Layer 3). Per ``CLAUDE.md`` Rule 3 (syntactic seams), do NOT
add ad-hoc ``"ldo" in description.lower()`` checks elsewhere — extend the
classifier here instead.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kicad_mcp.utils.pattern_recognition import (
    identify_amplifiers,
    identify_digital_interfaces,
    identify_filters,
    identify_microcontrollers,
    identify_oscillators,
    identify_power_supplies,
    identify_sensor_interfaces,
)

# Canonical cluster-label vocabulary. New labels must be added here AND to
# the classifier / pattern_recognition mappings below so all layers agree.
LABEL_UNCLASSIFIED = "unclassified"
LABEL_MCU = "mcu"
LABEL_LDO = "ldo"
LABEL_SWITCHING_REGULATOR = "switching_regulator"
LABEL_OP_AMP = "op_amp"
LABEL_FILTER = "filter"
LABEL_OSCILLATOR = "oscillator"
LABEL_DIGITAL_INTERFACE = "digital_interface"
LABEL_SENSOR = "sensor"
LABEL_CONNECTOR = "connector"
LABEL_CRYSTAL = "crystal"

LABELS = frozenset({
    LABEL_UNCLASSIFIED, LABEL_MCU, LABEL_LDO, LABEL_SWITCHING_REGULATOR,
    LABEL_OP_AMP, LABEL_FILTER, LABEL_OSCILLATOR, LABEL_DIGITAL_INTERFACE,
    LABEL_SENSOR, LABEL_CONNECTOR, LABEL_CRYSTAL,
})

LABEL_SOURCE_TOPOLOGY = "topology_only"
LABEL_SOURCE_PATTERN = "pattern_recognition"
LABEL_SOURCE_RESOLVED = "resolved_part"
LABEL_SOURCE_HINT = "caller_hint"

# Precedence: higher index wins (spec § Phase 1).
_SOURCE_PRECEDENCE = {
    LABEL_SOURCE_TOPOLOGY: 0,
    LABEL_SOURCE_PATTERN: 1,
    LABEL_SOURCE_RESOLVED: 2,
    LABEL_SOURCE_HINT: 3,
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ClusterLabel:
    """Per-cluster labeling result from the pipeline."""

    label: str
    label_confidence: float
    label_source: str
    anchor: str | None = None


# ---------------------------------------------------------------------------
# Anchor selection
# ---------------------------------------------------------------------------

# pintypes that mark "active" pins — components with these are preferred
# as cluster anchors over pure-passive components (resistors, caps).
_ACTIVE_PINTYPES = frozenset({"input", "output", "bidirectional", "tri_state",
                              "open_collector", "open_emitter"})


def select_anchor(
    members: list[str],
    netlist: dict[str, Any],
) -> str | None:
    """Pick the cluster anchor.

    Heuristic: highest signal-net degree, breaking ties by preferring
    components that own at least one active pin (not all-passive), then by
    reference string for determinism.
    """
    if not members:
        return None
    if len(members) == 1:
        return members[0]

    member_set = set(members)
    # ref → (degree, has_active_pin)
    degree: dict[str, int] = dict.fromkeys(members, 0)
    has_active: dict[str, bool] = dict.fromkeys(members, False)

    for _net_name, pins in (netlist.get("nets") or {}).items():
        for pin in pins:
            ref = pin.get("component")
            if ref not in member_set:
                continue
            degree[ref] = degree[ref] + 1
            pt = pin.get("pintype")
            if pt in _ACTIVE_PINTYPES:
                has_active[ref] = True

    # Sort key: -degree (more is better), -active (True is better), ref
    return min(
        members,
        key=lambda r: (-degree[r], 0 if has_active[r] else 1, r),
    )


# ---------------------------------------------------------------------------
# Layer 2 — pattern_recognition wrap
# ---------------------------------------------------------------------------

# Mapping from pattern_recognition match → our cluster-label vocabulary.
# Keys are (identifier_function_name, optional_type_field).
# When no type field applies, the second element is None.
_PATTERN_TO_LABEL: dict[tuple[str, str | None], str] = {
    ("identify_microcontrollers", None): LABEL_MCU,
    ("identify_power_supplies", "linear_regulator"): LABEL_LDO,
    ("identify_power_supplies", "switching_regulator"): LABEL_SWITCHING_REGULATOR,
    ("identify_amplifiers", None): LABEL_OP_AMP,
    ("identify_filters", None): LABEL_FILTER,
    ("identify_oscillators", None): LABEL_OSCILLATOR,
    ("identify_digital_interfaces", None): LABEL_DIGITAL_INTERFACE,
    ("identify_sensor_interfaces", None): LABEL_SENSOR,
}


def _component_keys_from_match(match: dict[str, Any]) -> list[str]:
    """Extract every component ref a pattern_recognition match references.

    Different identify_* functions use different keys; we union them so
    any cluster overlapping any matched ref gets credit.
    """
    keys = ("main_component", "component", "ic", "ic_ref",
            "amplifier", "regulator", "inductor", "crystal", "oscillator")
    refs: list[str] = []
    for k in keys:
        v = match.get(k)
        if isinstance(v, str) and v:
            refs.append(v)
    # Some matches include a list of refs (e.g. "components": [r1, r2])
    components_list = match.get("components")
    if isinstance(components_list, list):
        refs.extend(c for c in components_list if isinstance(c, str))
    return refs


def _run_pattern_recognition(
    components: dict[str, Any],
    nets: dict[str, Any],
) -> dict[str, list[tuple[str, float]]]:
    """Run every identify_* function and group matches by component ref.

    Returns: ``{ref: [(label, confidence), ...]}``. A ref may appear in
    multiple matches (different identifier functions); the per-cluster
    consolidation in ``apply_pattern_recognition_labels`` handles collisions.
    """
    results: dict[str, list[tuple[str, float]]] = {}

    # Identifier name → (callable, args_kind). args_kind is "comp" if the
    # function takes only components, "comp+nets" if it takes both.
    identifiers: list[tuple[str, Callable[..., list[dict[str, Any]]], str]] = [
        ("identify_microcontrollers", identify_microcontrollers, "comp"),
        ("identify_power_supplies", identify_power_supplies, "comp+nets"),
        ("identify_amplifiers", identify_amplifiers, "comp+nets"),
        ("identify_filters", identify_filters, "comp+nets"),
        ("identify_oscillators", identify_oscillators, "comp+nets"),
        ("identify_digital_interfaces", identify_digital_interfaces, "comp+nets"),
        ("identify_sensor_interfaces", identify_sensor_interfaces, "comp+nets"),
    ]

    for name, fn, kind in identifiers:
        try:
            matches = fn(components, nets) if kind == "comp+nets" else fn(components)
        except Exception:
            # pattern_recognition is best-effort; one identifier failing
            # mustn't kill the whole layer.
            continue
        for match in matches or []:
            mtype = match.get("type") or match.get("subtype")
            label = _PATTERN_TO_LABEL.get((name, mtype)) \
                or _PATTERN_TO_LABEL.get((name, None))
            if label is None:
                continue
            confidence = 0.7  # pattern_recognition is a regex on a name field
            for ref in _component_keys_from_match(match):
                results.setdefault(ref, []).append((label, confidence))

    return results


# ---------------------------------------------------------------------------
# Layer 3 — description-keyword classifier (single source of truth)
# ---------------------------------------------------------------------------

# Per CLAUDE.md Rule 3: a single ordered list of (regex, label) pairs.
# Order matters — earlier entries match first. Each entry's regex MUST be
# explicit (no bare alternations across labels) so the dispatch is auditable.
#
# Reading order: most-specific first, generic last. Don't add bare
# substring checks anywhere else in the codebase — extend this list.
_DESCRIPTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmicrocontroller\b|\bmcu\b|\bmpu\b", re.IGNORECASE), LABEL_MCU),
    (re.compile(r"\blow\s*drop[\s-]*out\b|\bldo\b", re.IGNORECASE), LABEL_LDO),
    (re.compile(r"\bbuck\b|\bboost\b|\bdc[\s-]*dc\b|\bswitching\s+regulator\b",
                re.IGNORECASE), LABEL_SWITCHING_REGULATOR),
    (re.compile(r"\boperational\s+amplifier\b|\bop[\s-]*amp\b|\bcomparator\b",
                re.IGNORECASE), LABEL_OP_AMP),
    (re.compile(r"\bcrystal\b|\bquartz\b|\bresonator\b", re.IGNORECASE), LABEL_CRYSTAL),
    (re.compile(r"\boscillator\b", re.IGNORECASE), LABEL_OSCILLATOR),
    (re.compile(r"\bfilter\b", re.IGNORECASE), LABEL_FILTER),
    # Allow short qualifiers between protocol and function noun
    # (e.g., "USB 2.0 transceiver", "I2C 400 kHz bridge").
    (re.compile(r"\b(usb|i2c|spi|uart|can)\b[\w\s.-]{0,20}?\b(transceiver|driver|interface|bridge)\b",
                re.IGNORECASE), LABEL_DIGITAL_INTERFACE),
    (re.compile(r"\bsensor\b|\baccelerometer\b|\bgyroscope\b|\bthermistor\b",
                re.IGNORECASE), LABEL_SENSOR),
    (re.compile(r"\bconnector\b|\bheader\b|\bsocket\b|\bplug\b|\breceptacle\b",
                re.IGNORECASE), LABEL_CONNECTOR),
]


def classify_description(description: str | None) -> str | None:
    """Map a free-form description string to a cluster label.

    Returns ``None`` if no pattern matches. This is the ONLY substring/regex
    classifier for labels — see module docstring.
    """
    if not description:
        return None
    for pattern, label in _DESCRIPTION_PATTERNS:
        if pattern.search(description):
            return label
    return None


# ---------------------------------------------------------------------------
# Layer 3 — resolved-part labeling
# ---------------------------------------------------------------------------

# Callable shape for the LCSC lookup. Returns a dict with at least a
# ``description`` key, or None on miss. Matches ``lcsc_db.get_component``
# but we accept any callable so tests can monkeypatch without importing
# the LCSC subsystem.
LcscLookupFn = Callable[[str], dict[str, Any] | None]


def _lcsc_part_from_component(comp: dict[str, Any]) -> str | None:
    props = comp.get("properties") or {}
    pn = props.get("LCSC") or props.get("lcsc")
    if isinstance(pn, str) and pn.strip():
        return pn.strip()
    return None


def _label_from_resolved(
    anchor_ref: str,
    components: dict[str, Any],
    lookup_fn: LcscLookupFn,
) -> tuple[str, float] | None:
    """Look up the anchor's LCSC part and derive a label from its description.

    Returns ``(label, confidence)`` or ``None`` if the anchor isn't
    LCSC-resolved, the lookup misses, or the description doesn't match.
    """
    comp = components.get(anchor_ref) or {}
    pn = _lcsc_part_from_component(comp)
    if not pn:
        return None
    try:
        row = lookup_fn(pn)
    except Exception:
        return None
    if row is None:
        return None
    label = classify_description(row.get("description"))
    if label is None:
        return None
    # Per spec § Layer 3: confidence in [0.7, 0.95]. We start at the high
    # end since the supplier description is more authoritative than a
    # regex on the schematic value.
    return (label, 0.9)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def label_clusters(
    members_by_cluster: dict[str, list[str]],
    netlist: dict[str, Any],
    *,
    hints: dict[str, str] | None = None,
    lcsc_lookup_fn: LcscLookupFn | None = None,
) -> tuple[dict[str, ClusterLabel], list[dict[str, Any]]]:
    """Run Layers 2/3/4 over the topology partition.

    Returns ``(labels, events)`` — events surface
    ``pattern_recognition_collision`` warnings.
    """
    hints = hints or {}
    components = netlist.get("components") or {}
    nets = netlist.get("nets") or {}
    events: list[dict[str, Any]] = []

    pattern_matches = _run_pattern_recognition(components, nets)

    out: dict[str, ClusterLabel] = {}
    for cid, members in members_by_cluster.items():
        anchor = select_anchor(members, netlist)

        # Baseline: topology-only.
        current = ClusterLabel(
            label=LABEL_UNCLASSIFIED,
            label_confidence=0.0,
            label_source=LABEL_SOURCE_TOPOLOGY,
            anchor=anchor,
        )

        # Layer 2 — pattern_recognition.
        per_cluster_matches: dict[str, float] = {}
        for ref in members:
            for label, conf in pattern_matches.get(ref, []):
                # Keep the highest-confidence match for this label
                # within the cluster.
                if label not in per_cluster_matches or conf > per_cluster_matches[label]:
                    per_cluster_matches[label] = conf

        if per_cluster_matches:
            if len(per_cluster_matches) > 1:
                events.append({
                    "level": "warn",
                    "code": "pattern_recognition_collision",
                    "message": (
                        f"Cluster {cid} matched multiple pattern_recognition "
                        f"labels: {sorted(per_cluster_matches.keys())}. "
                        "Using the highest-confidence match; label_confidence "
                        "lowered to reflect ambiguity."
                    ),
                    "data": {
                        "cluster_id": cid,
                        "candidates": sorted(per_cluster_matches.keys()),
                    },
                })
                # Pick highest-confidence; halve confidence to reflect ambiguity.
                best_label = max(per_cluster_matches, key=lambda k: per_cluster_matches[k])
                best_conf = per_cluster_matches[best_label] * 0.5
            else:
                best_label, best_conf = next(iter(per_cluster_matches.items()))

            current = _maybe_replace(current, ClusterLabel(
                label=best_label,
                label_confidence=best_conf,
                label_source=LABEL_SOURCE_PATTERN,
                anchor=anchor,
            ))

        # Layer 3 — LCSC resolved-part on the anchor.
        if lcsc_lookup_fn is not None and anchor is not None:
            resolved = _label_from_resolved(anchor, components, lcsc_lookup_fn)
            if resolved is not None:
                label, conf = resolved
                current = _maybe_replace(current, ClusterLabel(
                    label=label,
                    label_confidence=conf,
                    label_source=LABEL_SOURCE_RESOLVED,
                    anchor=anchor,
                ))

        # Layer 4 — caller hints. Any hint targeting any cluster member
        # claims the cluster; if multiple members are hinted, the one
        # matching the anchor wins, else first by ref.
        hint_label = None
        for ref in members:
            if ref not in hints:
                continue
            value = hints[ref]
            if value not in LABELS:
                continue
            # Prefer the anchor's hint when multiple cluster members are
            # hinted; otherwise take the first non-anchor we see.
            if ref == anchor or hint_label is None:
                hint_label = value
        if hint_label is not None:
            current = _maybe_replace(current, ClusterLabel(
                label=hint_label,
                label_confidence=1.0,
                label_source=LABEL_SOURCE_HINT,
                anchor=anchor,
            ))

        out[cid] = current

    return out, events


def _maybe_replace(current: ClusterLabel, candidate: ClusterLabel) -> ClusterLabel:
    """Replace ``current`` with ``candidate`` iff its source has higher
    or equal precedence. Equal precedence is allowed so a same-layer
    re-application is idempotent."""
    if _SOURCE_PRECEDENCE[candidate.label_source] >= _SOURCE_PRECEDENCE[current.label_source]:
        return candidate
    return current
