"""v1 convention rule tests.

Per ``docs/SPEC_Schematic_Placement.md`` § Convention rules:
  - decap_below_ic
  - regulator_input_left
  - mcu_crystal_proximate
  - connector_edge
  - power_symbols_top_bottom

Per ``CLAUDE.md`` Rule 2 (threshold-boundary):
  - Test the trigger predicates at boundary (exactly 1 IC vs 0 vs 2)
  - Test the "skipped" path with the reason event
  - Test that a rule that can't apply leaves positions untouched
"""

from __future__ import annotations

from kicad_mcp.utils.placement.conventions import (
    RULE_CONNECTOR_EDGE,
    RULE_DECAP_BELOW_IC,
    RULE_MCU_CRYSTAL_PROXIMATE,
    RULE_POWER_SYMBOLS_TOP_BOTTOM,
    RULE_REGULATOR_INPUT_LEFT,
    apply_conventions,
)
from kicad_mcp.utils.placement.labeling import (
    LABEL_CONNECTOR,
    LABEL_LDO,
    LABEL_MCU,
    LABEL_SOURCE_PATTERN,
    LABEL_UNCLASSIFIED,
    ClusterLabel,
)
from kicad_mcp.utils.placement.pack import PackResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _label(label: str, anchor: str | None) -> ClusterLabel:
    return ClusterLabel(
        label=label, label_confidence=0.7,
        label_source=LABEL_SOURCE_PATTERN, anchor=anchor,
    )


def _pack(positions: dict[str, tuple[float, float]]) -> PackResult:
    return PackResult(
        positions=dict(positions),
        bboxes={cid: {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0} for cid in positions},
    )


def _comp(ref: str, *, value: str = "", lib_id: str = "") -> dict:
    out: dict = {"reference": ref}
    if value:
        out["value"] = value
    if lib_id:
        out["lib_id"] = lib_id
    return out


def _pin(ref: str, pin: str = "1", pintype: str = "passive",
         pinfunction: str = "") -> dict:
    entry: dict = {"component": ref, "pin": pin, "pintype": pintype}
    if pinfunction:
        entry["pinfunction"] = pinfunction
    return entry


def _events_by_rule(events, rule: str):
    return [e for e in events if e.rule == rule]


# ---------------------------------------------------------------------------
# Rule 1 — decap_below_ic
# ---------------------------------------------------------------------------

class TestDecapBelowIc:
    def test_one_ic_one_decap_applies(self):
        members = {"c0": ["U1", "C1"]}
        labels = {"c0": _label(LABEL_UNCLASSIFIED, "U1")}
        components = {"U1": _comp("U1"), "C1": _comp("C1")}
        nets = {
            "VCC": [_pin("U1", "8", "power_in"), _pin("C1", "1", "passive")],
            "GND": [_pin("U1", "4", "power_in"), _pin("C1", "2", "passive")],
        }
        pack = _pack({"U1": (50.0, 30.0), "C1": (50.0, 42.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": components, "nets": nets},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        applied = _events_by_rule(events, RULE_DECAP_BELOW_IC)
        assert any(e.code == "convention_applied" for e in applied)
        # C1 moved to be one row below U1, x-aligned.
        assert pack.positions["C1"] == (50.0, 42.7)

    def test_two_ics_skips(self):
        members = {"c0": ["U1", "U2", "C1"]}
        labels = {"c0": _label(LABEL_UNCLASSIFIED, "U1")}
        components = {"U1": _comp("U1"), "U2": _comp("U2"), "C1": _comp("C1")}
        pack = _pack({"U1": (0, 0), "U2": (10, 0), "C1": (5, 0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": components, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        skipped = _events_by_rule(events, RULE_DECAP_BELOW_IC)
        assert all(e.code == "convention_skipped" for e in skipped)
        # Positions untouched.
        assert pack.positions["C1"] == (5, 0)

    def test_zero_ics_skips(self):
        members = {"c0": ["R1", "C1"]}
        labels = {"c0": _label(LABEL_UNCLASSIFIED, "R1")}
        pack = _pack({"R1": (0, 0), "C1": (1, 0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"R1": _comp("R1"), "C1": _comp("C1")}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        skipped = _events_by_rule(events, RULE_DECAP_BELOW_IC)
        assert all(e.code == "convention_skipped" for e in skipped)

    def test_cap_with_signal_pin_not_a_decap(self):
        # C1 connects to a signal net AND a power net → not pure power.
        members = {"c0": ["U1", "C1"]}
        labels = {"c0": _label(LABEL_UNCLASSIFIED, "U1")}
        components = {"U1": _comp("U1"), "C1": _comp("C1")}
        nets = {
            "GND": [_pin("U1", "4", "power_in"), _pin("C1", "2", "passive")],
            "SIG": [_pin("U1", "2", "input"), _pin("C1", "1", "passive")],
        }
        pack = _pack({"U1": (50.0, 30.0), "C1": (50.0, 42.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": components, "nets": nets},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        skipped = _events_by_rule(events, RULE_DECAP_BELOW_IC)
        assert all(e.code == "convention_skipped" for e in skipped)
        # C1 position untouched.
        assert pack.positions["C1"] == (50.0, 42.0)


# ---------------------------------------------------------------------------
# Rule 2 — regulator_input_left
# ---------------------------------------------------------------------------

class TestRegulatorInputLeft:
    def test_ldo_with_vin_and_vout_passives(self):
        members = {"c0": ["U1", "C1", "C2"]}
        labels = {"c0": _label(LABEL_LDO, "U1")}
        components = {"U1": _comp("U1", value="LM1117"), "C1": _comp("C1"), "C2": _comp("C2")}
        nets = {
            "VIN_NET": [
                _pin("U1", "3", "power_in", "VIN"),
                _pin("C1", "1", "passive"),
            ],
            "VOUT_NET": [
                _pin("U1", "2", "power_out", "VOUT"),
                _pin("C2", "1", "passive"),
            ],
        }
        pack = _pack({"U1": (50.0, 30.0), "C1": (50.0, 42.0), "C2": (50.0, 54.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": components, "nets": nets},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        applied = _events_by_rule(events, RULE_REGULATOR_INPUT_LEFT)
        assert any(e.code == "convention_applied" for e in applied)
        # C1 (VIN side) should be to the left of U1; C2 (VOUT) to the right.
        assert pack.positions["C1"][0] < 50.0
        assert pack.positions["C2"][0] > 50.0

    def test_non_ldo_cluster_not_processed(self):
        members = {"c0": ["U1"]}
        labels = {"c0": _label(LABEL_MCU, "U1")}
        pack = _pack({"U1": (50.0, 30.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"U1": _comp("U1")}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        # No events for this rule at all (not applicable).
        assert _events_by_rule(events, RULE_REGULATOR_INPUT_LEFT) == []

    def test_ldo_without_pinfunction_skips(self):
        members = {"c0": ["U1", "C1"]}
        labels = {"c0": _label(LABEL_LDO, "U1")}
        nets = {"X": [_pin("U1", "1"), _pin("C1", "1")]}
        pack = _pack({"U1": (50.0, 30.0), "C1": (50.0, 42.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"U1": _comp("U1"), "C1": _comp("C1")}, "nets": nets},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        skipped = _events_by_rule(events, RULE_REGULATOR_INPUT_LEFT)
        assert all(e.code == "convention_skipped" for e in skipped)
        # No positions changed.
        assert pack.positions["C1"] == (50.0, 42.0)


# ---------------------------------------------------------------------------
# Rule 3 — mcu_crystal_proximate
# ---------------------------------------------------------------------------

class TestMcuCrystalProximate:
    def test_crystal_in_mcu_cluster_repositioned_adjacent(self):
        members = {"c0": ["U1", "Y1"]}
        labels = {"c0": _label(LABEL_MCU, "U1")}
        components = {"U1": _comp("U1"), "Y1": _comp("Y1", lib_id="Device:Crystal")}
        pack = _pack({"U1": (50.0, 30.0), "Y1": (50.0, 42.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": components, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        applied = _events_by_rule(events, RULE_MCU_CRYSTAL_PROXIMATE)
        assert any(e.code == "convention_applied" for e in applied)
        # Y1 placed to the left of U1, same y.
        y1_x, y1_y = pack.positions["Y1"]
        assert y1_x < 50.0
        assert y1_y == 30.0

    def test_mcu_without_crystal_skips(self):
        members = {"c0": ["U1"]}
        labels = {"c0": _label(LABEL_MCU, "U1")}
        pack = _pack({"U1": (50.0, 30.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"U1": _comp("U1")}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        skipped = _events_by_rule(events, RULE_MCU_CRYSTAL_PROXIMATE)
        assert all(e.code == "convention_skipped" for e in skipped)


# ---------------------------------------------------------------------------
# Rule 4 — connector_edge
# ---------------------------------------------------------------------------

class TestConnectorEdge:
    def test_tier_0_connector_pushed_left(self):
        members = {"c0": ["J1"], "c1": ["U1"]}
        labels = {"c0": _label(LABEL_CONNECTOR, "J1"), "c1": _label(LABEL_MCU, "U1")}
        pack = _pack({"J1": (50.0, 30.0), "U1": (100.0, 30.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"J1": _comp("J1"), "U1": _comp("U1")}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0, "c1": 1},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        applied = _events_by_rule(events, RULE_CONNECTOR_EDGE)
        assert any(e.code == "convention_applied" for e in applied)
        # Pin the exact target: 0.5 column left of the original leftmost (50.0).
        # A relational `< 50.0` would pass even for a near-zero offset.
        assert pack.positions["J1"][0] == 50.0 - 25.4 * 0.5

    def test_last_tier_connector_pushed_right(self):
        members = {"c0": ["U1"], "c1": ["J1"]}
        labels = {"c0": _label(LABEL_MCU, "U1"), "c1": _label(LABEL_CONNECTOR, "J1")}
        pack = _pack({"U1": (50.0, 30.0), "J1": (100.0, 30.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"U1": _comp("U1"), "J1": _comp("J1")}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0, "c1": 1},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        applied = _events_by_rule(events, RULE_CONNECTOR_EDGE)
        assert any(e.code == "convention_applied" for e in applied)
        # Pin the exact target: 0.5 column right of the original rightmost (100.0).
        assert pack.positions["J1"][0] == 100.0 + 25.4 * 0.5

    def test_two_tier_0_connectors_do_not_cascade(self):
        # Both tier-0 connectors must target the SAME column (0.5 left of the
        # pre-convention leftmost). The old bug read the MUTATED pack per call,
        # so the 2nd connector pushed off the 1st's already-moved position and
        # cascaded further left.
        members = {"c0": ["J1"], "c1": ["J2"], "c2": ["U1"]}
        labels = {
            "c0": _label(LABEL_CONNECTOR, "J1"),
            "c1": _label(LABEL_CONNECTOR, "J2"),
            "c2": _label(LABEL_MCU, "U1"),
        }
        pack = _pack({"J1": (50.0, 30.0), "J2": (60.0, 30.0), "U1": (100.0, 30.0)})
        apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {r: _comp(r) for r in ["J1", "J2", "U1"]}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0, "c1": 0, "c2": 1},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        target = 50.0 - 25.4 * 0.5            # 0.5 col left of pre-convention leftmost
        assert pack.positions["J1"][0] == target
        assert pack.positions["J2"][0] == target   # NOT cascaded further left

    def test_middle_tier_connector_skipped(self):
        members = {"c0": ["U1"], "c1": ["J1"], "c2": ["U2"]}
        labels = {
            "c0": _label(LABEL_MCU, "U1"),
            "c1": _label(LABEL_CONNECTOR, "J1"),
            "c2": _label(LABEL_MCU, "U2"),
        }
        pack = _pack({"U1": (0, 0), "J1": (25, 0), "U2": (50, 0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {r: _comp(r) for r in ["U1", "J1", "U2"]}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0, "c1": 1, "c2": 2},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        skipped = _events_by_rule(events, RULE_CONNECTOR_EDGE)
        assert all(e.code == "convention_skipped" for e in skipped)
        assert pack.positions["J1"] == (25, 0)


# ---------------------------------------------------------------------------
# Rule 5 — power_symbols_top_bottom
# ---------------------------------------------------------------------------

class TestPowerSymbolsTopBottom:
    def test_ground_below_rail_above(self):
        members = {"c0": ["U1", "#PWR01", "#PWR02"]}
        labels = {"c0": _label(LABEL_UNCLASSIFIED, "U1")}
        components = {
            "U1": _comp("U1"),
            "#PWR01": _comp("#PWR01", value="GND", lib_id="power:GND"),
            "#PWR02": _comp("#PWR02", value="+3V3", lib_id="power:+3V3"),
        }
        pack = _pack({"U1": (50.0, 30.0), "#PWR01": (50.0, 42.0), "#PWR02": (50.0, 54.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": components, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        applied = _events_by_rule(events, RULE_POWER_SYMBOLS_TOP_BOTTOM)
        assert any(e.code == "convention_applied" for e in applied)
        # GND below U1, +3V3 above.
        _, gnd_y = pack.positions["#PWR01"]
        _, rail_y = pack.positions["#PWR02"]
        assert gnd_y > 30.0  # below U1
        assert rail_y < 30.0  # above U1

    def test_no_power_symbols_silent(self):
        members = {"c0": ["U1", "R1"]}
        labels = {"c0": _label(LABEL_UNCLASSIFIED, "U1")}
        pack = _pack({"U1": (50.0, 30.0), "R1": (50.0, 42.0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {r: _comp(r) for r in ["U1", "R1"]}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        # No events for this rule at all when no power symbols present.
        assert _events_by_rule(events, RULE_POWER_SYMBOLS_TOP_BOTTOM) == []


# ---------------------------------------------------------------------------
# Sanity — empty / single-cluster don't crash
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_input(self):
        events = apply_conventions(
            members_by_cluster={}, cluster_labels={},
            netlist={"components": {}, "nets": {}},
            pack=PackResult(), cluster_tier={},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        assert events == []

    def test_unlabeled_cluster_safely_skipped(self):
        members = {"c0": ["U1"]}
        labels: dict = {}  # missing
        pack = _pack({"U1": (0, 0)})
        events = apply_conventions(
            members_by_cluster=members, cluster_labels=labels,
            netlist={"components": {"U1": _comp("U1")}, "nets": {}},
            pack=pack, cluster_tier={"c0": 0},
            column_pitch_mm=25.4, row_pitch_mm=12.7,
        )
        # No exception; positions intact.
        assert pack.positions["U1"] == (0, 0)
        assert events == []
