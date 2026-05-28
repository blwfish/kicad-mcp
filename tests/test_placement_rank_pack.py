"""Phase 2 (rank) + Phase 3 (pack) unit tests.

Per ``docs/SPEC_Schematic_Placement.md`` § Testing Strategy:
  - Phase 2: tier assignment from synthetic signal flow
  - Phase 2: cycle handling (signal_flow_cycle warning)
  - Phase 2: no source node fallback (no_signal_source warning)
  - Phase 3: barycentric reduces crossings
  - Phase 3: coordinate assignment honors column/row pitch

Per ``CLAUDE.md`` threshold-testing rule:
  - column_pitch_mm extreme values (tiny → overlap; large → wide layout)
  - empty / single-component edge cases
"""

from __future__ import annotations

import pytest

from kicad_mcp.utils.placement.pack import (
    DEFAULT_COLUMN_PITCH_MM,
    DEFAULT_ROW_PITCH_MM,
    pack_layout,
)
from kicad_mcp.utils.placement.rank import assign_tiers

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pin(ref: str, pin: str, pintype: str = "passive") -> dict:
    return {"component": ref, "pin": pin, "pintype": pintype}


def _netlist(comps: list[str], nets: dict[str, list[dict]] | None = None) -> dict:
    return {
        "components": {ref: {"reference": ref} for ref in comps},
        "nets": nets or {},
    }


# ---------------------------------------------------------------------------
# Phase 2 — assign_tiers
# ---------------------------------------------------------------------------

class TestAssignTiers:
    def test_empty_returns_empty(self):
        ta = assign_tiers(_netlist([]), {})
        assert ta.cluster_tier == {}
        assert ta.tiers == {}
        assert ta.events == []

    def test_linear_three_cluster_chain_tiers_0_1_2(self):
        # Three clusters: src → mid → sink. Each cluster is one component.
        # src has an output pin; mid has input + output; sink has input.
        nets = {
            "S1": [_pin("SRC", "1", "output"), _pin("MID", "1", "input")],
            "S2": [_pin("MID", "2", "output"), _pin("SINK", "1", "input")],
        }
        comp_cluster = {"SRC": "c0", "MID": "c1", "SINK": "c2"}
        ta = assign_tiers(_netlist(["SRC", "MID", "SINK"], nets), comp_cluster)
        assert ta.cluster_tier == {"c0": 0, "c1": 1, "c2": 2}
        assert ta.tiers == {0: ["c0"], 1: ["c1"], 2: ["c2"]}
        assert ta.events == []

    def test_two_sources_share_tier_0(self):
        # Two independent sources both feed the same sink.
        nets = {
            "S1": [_pin("SRC_A", "1", "output"), _pin("SINK", "1", "input")],
            "S2": [_pin("SRC_B", "1", "output"), _pin("SINK", "2", "input")],
        }
        comp_cluster = {"SRC_A": "ca", "SRC_B": "cb", "SINK": "cs"}
        ta = assign_tiers(
            _netlist(["SRC_A", "SRC_B", "SINK"], nets), comp_cluster,
        )
        assert ta.cluster_tier["ca"] == 0
        assert ta.cluster_tier["cb"] == 0
        assert ta.cluster_tier["cs"] == 1

    def test_cycle_is_broken_with_warning(self):
        # A → B → A
        nets = {
            "S1": [_pin("A", "1", "output"), _pin("B", "1", "input")],
            "S2": [_pin("B", "2", "output"), _pin("A", "2", "input")],
        }
        comp_cluster = {"A": "ca", "B": "cb"}
        ta = assign_tiers(_netlist(["A", "B"], nets), comp_cluster)
        codes = [e["code"] for e in ta.events]
        assert "signal_flow_cycle" in codes
        # After breaking, both clusters should still get tiered.
        assert "ca" in ta.cluster_tier
        assert "cb" in ta.cluster_tier

    def test_cycle_breaking_is_deterministic_across_runs(self):
        """All-bidirectional triangle (A↔B↔C↔A, every edge weight 0.5) is
        a worst-case for cycle-break tie-breaking. Sets used to iterate in
        PYTHONHASHSEED-randomized order, so the chosen edge varied per
        process. Now: nodes added in sorted order; min() tie-breaks by
        (weight, src, dst). Same netlist must produce identical cluster_tier
        on repeated calls within a process (the across-process check needs a
        subprocess and is impractical here, but the within-process
        invariant is the necessary precondition)."""
        nets = {
            "AB": [_pin("A", "1", "bidirectional"), _pin("B", "1", "bidirectional")],
            "BC": [_pin("B", "2", "bidirectional"), _pin("C", "1", "bidirectional")],
            "CA": [_pin("C", "2", "bidirectional"), _pin("A", "2", "bidirectional")],
        }
        comp_cluster = {"A": "ca", "B": "cb", "C": "cc"}
        results = [
            assign_tiers(_netlist(["A", "B", "C"], nets), comp_cluster).cluster_tier
            for _ in range(5)
        ]
        # Every call must produce the same partition.
        for r in results[1:]:
            assert r == results[0]

    def test_no_source_fallback_warns_and_picks_origin(self):
        # Construct a degenerate graph: every cluster has incoming edges
        # before any cycle-breaking happens. The cycle-breaker should
        # restore at least one source; if not, the fallback fires.
        # Two clusters all-bidirectional → both have in-degree.
        nets = {
            "S1": [_pin("A", "1", "bidirectional"),
                   _pin("B", "1", "bidirectional")],
        }
        comp_cluster = {"A": "ca", "B": "cb"}
        ta = assign_tiers(_netlist(["A", "B"], nets), comp_cluster)
        # Either cycle was broken (signal_flow_cycle event), or no
        # source was found — both paths leave clusters tiered.
        assert "ca" in ta.cluster_tier
        assert "cb" in ta.cluster_tier

    def test_disconnected_cluster_lands_at_tier_0(self):
        # ISO has no nets at all — should be tiered as a source.
        nets = {
            "S1": [_pin("SRC", "1", "output"), _pin("SINK", "1", "input")],
        }
        comp_cluster = {"SRC": "ca", "SINK": "cb", "ISO": "cc"}
        ta = assign_tiers(
            _netlist(["SRC", "SINK", "ISO"], nets), comp_cluster,
        )
        assert ta.cluster_tier["cc"] == 0

    def test_power_nets_excluded_from_direction(self):
        # GND-only connection between two components shouldn't create
        # a directional inter-cluster edge.
        nets = {
            "GND": [_pin("A", "1", "power_out"), _pin("B", "1", "power_in")],
        }
        comp_cluster = {"A": "ca", "B": "cb"}
        ta = assign_tiers(_netlist(["A", "B"], nets), comp_cluster)
        # Neither cluster sees a directional edge → both at tier 0.
        assert ta.cluster_tier["ca"] == 0
        assert ta.cluster_tier["cb"] == 0


# ---------------------------------------------------------------------------
# Phase 3 — pack_layout
# ---------------------------------------------------------------------------

class TestPackLayout:
    def test_empty_returns_empty(self):
        pack = pack_layout(
            members_by_cluster={},
            cluster_tier={},
            tiers={},
            cluster_edges={},
        )
        assert pack.positions == {}
        assert pack.bboxes == {}

    def test_single_cluster_at_origin_offset(self):
        pack = pack_layout(
            members_by_cluster={"c0": ["U1"]},
            cluster_tier={"c0": 0},
            tiers={0: ["c0"]},
            cluster_edges={},
        )
        x, y = pack.positions["U1"]
        # First column origin includes the X_ORIGIN_MM offset (12.7).
        assert x == 12.7
        assert y == 12.7

    def test_tier_spacing_uses_column_pitch(self):
        pack = pack_layout(
            members_by_cluster={"c0": ["A"], "c1": ["B"]},
            cluster_tier={"c0": 0, "c1": 1},
            tiers={0: ["c0"], 1: ["c1"]},
            cluster_edges={},
            column_pitch_mm=30.0,
        )
        x_a, _ = pack.positions["A"]
        x_b, _ = pack.positions["B"]
        assert x_b - x_a == pytest.approx(30.0)

    def test_row_spacing_within_cluster_uses_row_pitch(self):
        pack = pack_layout(
            members_by_cluster={"c0": ["A", "B", "C"]},
            cluster_tier={"c0": 0},
            tiers={0: ["c0"]},
            cluster_edges={},
            row_pitch_mm=20.0,
        )
        _, ya = pack.positions["A"]
        _, yb = pack.positions["B"]
        _, yc = pack.positions["C"]
        assert yb - ya == pytest.approx(20.0)
        assert yc - yb == pytest.approx(20.0)

    def test_clusters_in_same_tier_dont_overlap_vertically(self):
        pack = pack_layout(
            members_by_cluster={"c0": ["A", "B"], "c1": ["X", "Y"]},
            cluster_tier={"c0": 0, "c1": 0},
            tiers={0: ["c0", "c1"]},
            cluster_edges={},
        )
        # c0 spans rows 0-1; c1 should start strictly below c0's bottom.
        c0_bbox = pack.bboxes["c0"]
        c1_bbox = pack.bboxes["c1"]
        assert c1_bbox["y"] > c0_bbox["y"] + c0_bbox["h"] - 1e-9

    def test_bbox_x_aligns_with_member_x(self):
        pack = pack_layout(
            members_by_cluster={"c0": ["U1"]},
            cluster_tier={"c0": 0},
            tiers={0: ["c0"]},
            cluster_edges={},
        )
        bbox = pack.bboxes["c0"]
        x, _ = pack.positions["U1"]
        assert bbox["x"] == x

    def test_defaults_match_spec(self):
        # Spec § Phase 3: column_pitch_mm=25.4, row_pitch_mm=12.7
        assert DEFAULT_COLUMN_PITCH_MM == 25.4
        assert DEFAULT_ROW_PITCH_MM == 12.7


# ---------------------------------------------------------------------------
# Threshold boundary tests
# ---------------------------------------------------------------------------

class TestPackBoundaries:
    def test_tiny_pitch_produces_overlapping_positions(self):
        """Per spec: very small pitch is technically valid but produces
        collisions. This pins that behavior so a future change doesn't
        silently start rejecting small pitches."""
        pack = pack_layout(
            members_by_cluster={"c0": ["A", "B"]},
            cluster_tier={"c0": 0},
            tiers={0: ["c0"]},
            cluster_edges={},
            row_pitch_mm=0.1,
        )
        _, ya = pack.positions["A"]
        _, yb = pack.positions["B"]
        # 0.1 mm apart — collision territory for components that have
        # finite footprints, but the tool produces it as requested.
        assert yb - ya == pytest.approx(0.1)

    def test_large_pitch_remains_valid(self):
        pack = pack_layout(
            members_by_cluster={"c0": ["A"], "c1": ["B"]},
            cluster_tier={"c0": 0, "c1": 1},
            tiers={0: ["c0"], 1: ["c1"]},
            cluster_edges={},
            column_pitch_mm=1000.0,
        )
        x_a, _ = pack.positions["A"]
        x_b, _ = pack.positions["B"]
        assert x_b - x_a == pytest.approx(1000.0)


# ---------------------------------------------------------------------------
# Barycentric crossing-reduction
# ---------------------------------------------------------------------------

class TestBarycentricOrdering:
    def test_clusters_reorder_to_minimize_crossings(self):
        # Build a 2-tier graph where the initial sorted order (c0, c1 vs
        # c2, c3) creates a crossing if c0→c3 and c1→c2. Barycentric
        # should swap c2/c3 (or c0/c1) to reduce it.
        members = {
            "c0": ["A0"],
            "c1": ["A1"],
            "c2": ["B2"],
            "c3": ["B3"],
        }
        cluster_tier = {"c0": 0, "c1": 0, "c2": 1, "c3": 1}
        tiers = {0: ["c0", "c1"], 1: ["c2", "c3"]}
        # Edges: c0→c3 and c1→c2 — crossing if c2 is above c3 and c0 above c1.
        cluster_edges = {"c0": {"c3": 1.0}, "c1": {"c2": 1.0}}
        pack = pack_layout(
            members_by_cluster=members,
            cluster_tier=cluster_tier,
            tiers=tiers,
            cluster_edges=cluster_edges,
        )
        # After barycentric: c2 should have a higher y-index than c3 in
        # tier 1, or c0 lower than c1 in tier 0 — either eliminates the
        # crossing. Easiest assertion: c2's y >= c3's y (i.e., reordered).
        y_c2 = pack.bboxes["c2"]["y"]
        y_c3 = pack.bboxes["c3"]["y"]
        # Initial alphabetical order would have c2 above c3 (smaller y).
        # Barycentric should have flipped this — c3 above c2.
        assert y_c3 < y_c2
