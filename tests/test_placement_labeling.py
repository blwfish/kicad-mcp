"""Unit tests for Layers 2/3/4 of the schematic placement subsystem.

Per ``docs/SPEC_Schematic_Placement.md`` § Testing Strategy:
  - Layer 2: pattern_recognition wrap (MCU, LDO, op-amp identifiable)
  - Layer 3: description-keyword classifier; resolved-part dominates Layer 2
  - Layer 4: caller hints override all lower layers; absent hints don't crash
  - Precedence: hints > resolved > pattern > topology

Per ``CLAUDE.md`` Rule 3 (syntactic seams):
  - ``classify_description`` is the single source of truth; tests pin its
    behavior on overlapping inputs and the ordering of patterns
"""

from __future__ import annotations

import pytest

from kicad_mcp.utils.placement.labeling import (
    LABEL_CONNECTOR,
    LABEL_CRYSTAL,
    LABEL_DIGITAL_INTERFACE,
    LABEL_FILTER,
    LABEL_LDO,
    LABEL_MCU,
    LABEL_OP_AMP,
    LABEL_OSCILLATOR,
    LABEL_SENSOR,
    LABEL_SOURCE_HINT,
    LABEL_SOURCE_PATTERN,
    LABEL_SOURCE_RESOLVED,
    LABEL_SOURCE_TOPOLOGY,
    LABEL_SWITCHING_REGULATOR,
    LABEL_UNCLASSIFIED,
    classify_description,
    label_clusters,
    select_anchor,
)

# ---------------------------------------------------------------------------
# Netlist fixture helpers (compatible with extract_netlist_via_cli shape)
# ---------------------------------------------------------------------------

def _comp(ref: str, *, value: str = "", description: str = "",
          lcsc: str | None = None, lib_id: str = "") -> dict:
    out: dict = {"reference": ref}
    if value:
        out["value"] = value
    if description:
        out["description"] = description
    if lib_id:
        out["lib_id"] = lib_id
    if lcsc:
        out["properties"] = {"LCSC": lcsc}
    return out


def _pin(ref: str, pin: str = "1", pintype: str = "passive") -> dict:
    return {"component": ref, "pin": pin, "pintype": pintype}


def _netlist(comps: list[dict], nets: dict[str, list[dict]] | None = None) -> dict:
    return {
        "components": {c["reference"]: c for c in comps},
        "nets": nets or {},
    }


# ---------------------------------------------------------------------------
# classify_description (Layer 3 single source of truth)
# ---------------------------------------------------------------------------

class TestClassifyDescription:
    """Pin classifier behavior, especially on overlapping inputs that would
    surface a syntactic-seam bug if patterns shadowed each other."""

    @pytest.mark.parametrize("description,expected", [
        ("3.3V Low Drop Out Regulator, SOT-23-5", LABEL_LDO),
        ("LDO 3.3V SOT-23", LABEL_LDO),
        ("Step-down DC-DC buck converter", LABEL_SWITCHING_REGULATOR),
        ("Switching regulator, 5A, 36V input", LABEL_SWITCHING_REGULATOR),
        ("Single-Supply Operational Amplifier", LABEL_OP_AMP),
        ("Rail-to-rail Op-Amp, 1MHz GBW", LABEL_OP_AMP),
        ("Comparator with push-pull output", LABEL_OP_AMP),
        ("32-bit ARM Microcontroller", LABEL_MCU),
        ("MCU with built-in flash", LABEL_MCU),
        ("16 MHz quartz crystal HC-49", LABEL_CRYSTAL),
        ("32.768 kHz crystal", LABEL_CRYSTAL),
        ("MEMS oscillator, 25 MHz", LABEL_OSCILLATOR),
        ("RC low-pass filter network", LABEL_FILTER),
        ("USB 2.0 transceiver", LABEL_DIGITAL_INTERFACE),
        ("I2C bridge IC", LABEL_DIGITAL_INTERFACE),
        ("3-axis accelerometer sensor", LABEL_SENSOR),
        ("NTC thermistor 10kΩ", LABEL_SENSOR),
        ("USB-C receptacle, 16 pin", LABEL_CONNECTOR),
        ("Pin header 2x10 2.54mm", LABEL_CONNECTOR),
    ])
    def test_canonical_matches(self, description, expected):
        assert classify_description(description) == expected

    @pytest.mark.parametrize("description", [
        "",
        "Resistor 10k 1%",       # plain passive — no keyword
        "Ceramic capacitor 100nF",
        "Hex inverter 74HC04",   # no keyword in our vocabulary
        None,
    ])
    def test_no_match_returns_none(self, description):
        assert classify_description(description) is None

    def test_crystal_before_oscillator(self):
        """If a description contains both 'crystal' and 'oscillator', the
        more-specific crystal pattern should win (it appears earlier in
        the pattern list). This pins the syntactic-seam ordering."""
        result = classify_description("16 MHz crystal oscillator")
        assert result == LABEL_CRYSTAL

    def test_ldo_does_not_match_switching_regulator_pattern(self):
        """'Low Drop Out' should not be mis-classified as switching."""
        assert classify_description("Low Drop Out 3.3V LDO") == LABEL_LDO


# ---------------------------------------------------------------------------
# select_anchor
# ---------------------------------------------------------------------------

class TestSelectAnchor:
    def test_single_member_returns_self(self):
        assert select_anchor(["U1"], _netlist([_comp("U1")])) == "U1"

    def test_empty_returns_none(self):
        assert select_anchor([], _netlist([])) is None

    def test_highest_degree_wins(self):
        # U1 on 3 nets, R1 on 1 net → U1 anchor.
        nets = {
            "S1": [_pin("U1", "1", "output"), _pin("R1", "1", "passive")],
            "S2": [_pin("U1", "2", "output"), _pin("R2", "1", "passive")],
            "S3": [_pin("U1", "3", "output"), _pin("R3", "1", "passive")],
        }
        nl = _netlist([_comp("U1"), _comp("R1"), _comp("R2"), _comp("R3")], nets)
        assert select_anchor(["U1", "R1", "R2", "R3"], nl) == "U1"

    def test_active_pintype_breaks_degree_tie(self):
        # U1 and U2 both have degree 1, but U1's pin is "output" (active)
        # while U2's pin is "passive". U1 wins.
        nets = {
            "S1": [_pin("U1", "1", "output")],
            "S2": [_pin("U2", "1", "passive")],
        }
        nl = _netlist([_comp("U1"), _comp("U2")], nets)
        assert select_anchor(["U1", "U2"], nl) == "U1"

    def test_ref_string_breaks_full_tie_for_determinism(self):
        # Both equally passive, same degree. Lexicographic ref wins.
        nets = {"S1": [_pin("U2", "1"), _pin("U1", "1")]}
        nl = _netlist([_comp("U1"), _comp("U2")], nets)
        assert select_anchor(["U1", "U2"], nl) == "U1"


# ---------------------------------------------------------------------------
# Layer 2 — pattern_recognition wrap
# ---------------------------------------------------------------------------

class TestPatternRecognitionLayer:
    def test_atmega328p_cluster_labeled_mcu(self):
        comps = [_comp("U1", value="ATMEGA328P")]
        nl = _netlist(comps)
        labels, _ = label_clusters({"c0": ["U1"]}, nl)
        assert labels["c0"].label == LABEL_MCU
        assert labels["c0"].label_source == LABEL_SOURCE_PATTERN

    def test_no_match_falls_back_to_topology_only(self):
        comps = [_comp("R1", value="10k"), _comp("R2", value="1k")]
        nl = _netlist(comps)
        labels, _ = label_clusters({"c0": ["R1", "R2"]}, nl)
        assert labels["c0"].label == LABEL_UNCLASSIFIED
        assert labels["c0"].label_source == LABEL_SOURCE_TOPOLOGY


# ---------------------------------------------------------------------------
# Layer 3 — resolved-part labeling
# ---------------------------------------------------------------------------

class TestResolvedPartLayer:
    def test_lcsc_description_overrides_pattern_match(self):
        # Pattern would call this an MCU (ATMEGA328P), but LCSC description
        # says it's actually an LDO (contrived but tests precedence).
        comps = [_comp("U1", value="ATMEGA328P", lcsc="C12345")]
        nl = _netlist(comps)

        def fake_lookup(pn):
            return {"description": "3.3V Low Drop Out regulator"}

        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, lcsc_lookup_fn=fake_lookup,
        )
        assert labels["c0"].label == LABEL_LDO
        assert labels["c0"].label_source == LABEL_SOURCE_RESOLVED
        assert 0.7 <= labels["c0"].label_confidence <= 0.95

    def test_lcsc_miss_falls_back_to_pattern(self):
        comps = [_comp("U1", value="ATMEGA328P", lcsc="C99999")]
        nl = _netlist(comps)
        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, lcsc_lookup_fn=lambda _pn: None,
        )
        assert labels["c0"].label == LABEL_MCU
        assert labels["c0"].label_source == LABEL_SOURCE_PATTERN

    def test_no_lcsc_property_skips_layer_3(self):
        comps = [_comp("U1", value="ATMEGA328P")]
        nl = _netlist(comps)

        called: list[str] = []
        def fake_lookup(pn):
            called.append(pn)
            return {"description": "anything"}

        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, lcsc_lookup_fn=fake_lookup,
        )
        assert called == []
        assert labels["c0"].label_source == LABEL_SOURCE_PATTERN

    def test_lcsc_lookup_exception_falls_back_silently(self):
        comps = [_comp("U1", value="ATMEGA328P", lcsc="C42")]
        nl = _netlist(comps)

        def boom(_pn):
            raise RuntimeError("DB exploded")

        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, lcsc_lookup_fn=boom,
        )
        # Should NOT propagate; falls back to Layer 2.
        assert labels["c0"].label == LABEL_MCU

    def test_description_not_classifiable_no_change_to_label(self):
        comps = [_comp("U1", value="ATMEGA328P", lcsc="C1")]
        nl = _netlist(comps)
        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl,
            lcsc_lookup_fn=lambda _pn: {"description": "10kΩ resistor"},
        )
        # Layer 3 produced no usable label → stays on Layer 2 result.
        assert labels["c0"].label == LABEL_MCU
        assert labels["c0"].label_source == LABEL_SOURCE_PATTERN


# ---------------------------------------------------------------------------
# Layer 4 — caller hints
# ---------------------------------------------------------------------------

class TestCallerHintsLayer:
    def test_hint_overrides_pattern_match(self):
        comps = [_comp("U1", value="ATMEGA328P")]
        nl = _netlist(comps)
        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, hints={"U1": LABEL_CONNECTOR},
        )
        assert labels["c0"].label == LABEL_CONNECTOR
        assert labels["c0"].label_source == LABEL_SOURCE_HINT
        assert labels["c0"].label_confidence == 1.0

    def test_hint_overrides_resolved_part(self):
        comps = [_comp("U1", value="X", lcsc="C1")]
        nl = _netlist(comps)
        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl,
            hints={"U1": LABEL_CONNECTOR},
            lcsc_lookup_fn=lambda _pn: {"description": "Low Drop Out 3.3V LDO"},
        )
        assert labels["c0"].label == LABEL_CONNECTOR
        assert labels["c0"].label_source == LABEL_SOURCE_HINT

    def test_unknown_hint_label_is_ignored(self):
        comps = [_comp("U1", value="ATMEGA328P")]
        nl = _netlist(comps)
        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, hints={"U1": "not_a_real_label"},
        )
        # Hint dropped; pattern_recognition still wins.
        assert labels["c0"].label == LABEL_MCU
        assert labels["c0"].label_source == LABEL_SOURCE_PATTERN

    def test_hint_for_non_member_ref_is_ignored(self):
        comps = [_comp("U1"), _comp("R1")]
        nl = _netlist(comps)
        labels, _ = label_clusters(
            {"c0": ["U1"]}, nl, hints={"R1": LABEL_CONNECTOR},
        )
        assert labels["c0"].label == LABEL_UNCLASSIFIED

    def test_empty_hints_dict_doesnt_crash(self):
        comps = [_comp("U1", value="ATMEGA328P")]
        labels, _ = label_clusters({"c0": ["U1"]}, _netlist(comps), hints={})
        assert labels["c0"].label == LABEL_MCU


# ---------------------------------------------------------------------------
# Pattern-recognition collision (multiple identify_* match same cluster)
# ---------------------------------------------------------------------------

class TestPatternRecognitionCollision:
    def test_collision_emits_warning_and_lowers_confidence(self):
        # ATMEGA328P matches both microcontrollers and (via "AT" prefix
        # in some identifiers) potentially another category. To force a
        # collision deterministically, supply two components that match
        # different identifiers in the same cluster.
        comps = [
            _comp("U1", value="ATMEGA328P"),     # MCU
            _comp("U2", value="LM358"),          # op-amp
        ]
        nl = _netlist(comps)
        labels, events = label_clusters({"c0": ["U1", "U2"]}, nl)
        codes = [e["code"] for e in events]
        assert "pattern_recognition_collision" in codes
        # Best label wins but at halved confidence (signaling ambiguity).
        assert labels["c0"].label in (LABEL_MCU, LABEL_OP_AMP)
        assert labels["c0"].label_confidence < 0.7


# ---------------------------------------------------------------------------
# Anchor populated on every cluster
# ---------------------------------------------------------------------------

class TestAnchorPopulated:
    def test_anchor_set_when_cluster_non_empty(self):
        nl = _netlist(
            [_comp("U1"), _comp("R1")],
            {"S": [_pin("U1", "1", "output"), _pin("R1", "1", "passive")]},
        )
        labels, _ = label_clusters({"c0": ["U1", "R1"]}, nl)
        assert labels["c0"].anchor == "U1"
