"""Unit tests for Layer 1 (topology) of the schematic placement subsystem.

Per ``docs/SPEC_Schematic_Placement.md`` § Testing Strategy:
  - synthetic netlist fixtures with known cluster structure
  - verify Louvain finds them
  - missing-pintype fallback
  - singleton/sparse warnings
  - cluster_id stability across re-runs

Per ``CLAUDE.md`` Threshold-Boundary Testing Rule:
  - SPARSE_CLUSTER_MAX = 3 → at-boundary tests (member count 3, 4)
  - empty netlist → no errors
  - single-component → singleton, no other warnings
"""

from __future__ import annotations

from kicad_mcp.utils.placement.topology import (
    SPARSE_CLUSTER_MAX,
    build_signal_graph,
    cluster_components,
)

# ---------------------------------------------------------------------------
# Netlist fixture helpers
# ---------------------------------------------------------------------------

def _netlist(components: list[str], nets: dict[str, list[dict]]) -> dict:
    """Build a netlist dict in the shape returned by extract_netlist_via_cli."""
    return {
        "components": {ref: {"reference": ref} for ref in components},
        "nets": nets,
    }


def _pin(ref: str, pin: str, pintype: str | None = "passive") -> dict:
    entry: dict = {"component": ref, "pin": pin}
    if pintype is not None:
        entry["pintype"] = pintype
    return entry


# ---------------------------------------------------------------------------
# build_signal_graph
# ---------------------------------------------------------------------------

class TestBuildSignalGraph:
    def test_empty_netlist_produces_empty_graph(self):
        graph, missing, dropped = build_signal_graph(_netlist([], {}))
        assert graph.number_of_nodes() == 0
        assert graph.number_of_edges() == 0
        assert missing == 0
        assert dropped == 0

    def test_components_with_no_nets_become_isolated_nodes(self):
        graph, _, _ = build_signal_graph(_netlist(["R1", "R2"], {}))
        assert set(graph.nodes()) == {"R1", "R2"}
        assert graph.number_of_edges() == 0

    def test_signal_net_creates_clique(self):
        nl = _netlist(
            ["U1", "R1", "R2"],
            {"SIG": [_pin("U1", "1"), _pin("R1", "1"), _pin("R2", "1")]},
        )
        graph, _, _ = build_signal_graph(nl)
        assert graph.number_of_edges() == 3  # K3 clique

    def test_power_net_dropped_by_name(self):
        nl = _netlist(
            ["U1", "C1"],
            {"GND": [_pin("U1", "8"), _pin("C1", "2")]},
        )
        graph, _, dropped = build_signal_graph(nl)
        assert graph.number_of_edges() == 0
        assert dropped == 1

    def test_power_pintype_pins_dropped(self):
        nl = _netlist(
            ["U1", "R1"],
            {"SIG": [_pin("U1", "1", pintype="power_in"), _pin("R1", "1")]},
        )
        graph, _, _ = build_signal_graph(nl)
        # Only R1 contributes to "SIG"; no edge formed
        assert graph.number_of_edges() == 0

    def test_missing_pintype_counted_and_treated_as_passive(self):
        nl = _netlist(
            ["U1", "R1"],
            {"SIG": [_pin("U1", "1", pintype=None), _pin("R1", "1", pintype=None)]},
        )
        graph, missing, _ = build_signal_graph(nl)
        assert missing == 2
        assert graph.number_of_edges() == 1  # both passive → still connected

    def test_edge_weight_accumulates_across_shared_nets(self):
        nl = _netlist(
            ["U1", "U2"],
            {
                "S1": [_pin("U1", "1"), _pin("U2", "1")],
                "S2": [_pin("U1", "2"), _pin("U2", "2")],
                "S3": [_pin("U1", "3"), _pin("U2", "3")],
            },
        )
        graph, _, _ = build_signal_graph(nl)
        assert graph["U1"]["U2"]["weight"] == 3

    def test_dedup_within_single_net(self):
        # Same component on multiple pins of the same net shouldn't self-loop.
        nl = _netlist(
            ["U1", "R1"],
            {"SIG": [_pin("U1", "1"), _pin("U1", "2"), _pin("R1", "1")]},
        )
        graph, _, _ = build_signal_graph(nl)
        assert graph["U1"]["R1"]["weight"] == 1
        assert not graph.has_edge("U1", "U1")


# ---------------------------------------------------------------------------
# cluster_components — partitioning
# ---------------------------------------------------------------------------

class TestClusterComponents:
    def test_empty_netlist_returns_empty_partition_no_events(self):
        part = cluster_components(_netlist([], {}), schematic_path="/tmp/empty.kicad_sch")
        assert part.component_cluster == {}
        assert part.members == {}
        assert part.events == []
        assert part.singletons == set()

    def test_single_component_is_singleton_with_warning(self):
        part = cluster_components(
            _netlist(["U1"], {}), schematic_path="/tmp/single.kicad_sch",
        )
        assert len(part.members) == 1
        cid = next(iter(part.members))
        assert part.members[cid] == ["U1"]
        assert cid in part.singletons
        codes = [e["code"] for e in part.events]
        assert "singleton_orphan" in codes

    def test_two_disconnected_components_get_two_clusters(self):
        nl = _netlist(["U1", "U2"], {})
        part = cluster_components(nl, schematic_path="/tmp/two.kicad_sch")
        assert len(part.members) == 2
        assert len(part.singletons) == 2

    def test_dense_connected_block_clusters_together(self):
        # Star topology: U1 connects to R1..R5 via 5 separate signal nets.
        refs = ["U1"] + [f"R{i}" for i in range(1, 6)]
        nets = {
            f"S{i}": [_pin("U1", str(i)), _pin(f"R{i}", "1")]
            for i in range(1, 6)
        }
        part = cluster_components(_netlist(refs, nets), schematic_path="/tmp/star.kicad_sch")
        # All 6 components should land in the same cluster.
        cids = set(part.component_cluster.values())
        assert len(cids) == 1
        cid = next(iter(cids))
        assert set(part.members[cid]) == set(refs)


# ---------------------------------------------------------------------------
# Threshold tests per CLAUDE.md (sparse cluster boundary)
# ---------------------------------------------------------------------------

class TestSparseClusterBoundary:
    """At-boundary tests for SPARSE_CLUSTER_MAX (= 3 per spec)."""

    def _connected_cluster_of_size(self, n: int):
        # K_n via a single net that lists every component on a separate pin.
        # A single fully-connected clique is the cleanest way to guarantee
        # Louvain produces one community of exactly ``n`` members, independent
        # of seed.
        refs = [f"U{i}" for i in range(n)]
        nets = {"SIG": [_pin(ref, "1") for ref in refs]}
        return cluster_components(
            _netlist(refs, nets), schematic_path=f"/tmp/n{n}.kicad_sch",
        )

    def test_size_exactly_max_emits_sparse_warning(self):
        # 3 members (== SPARSE_CLUSTER_MAX) → warning per spec
        part = self._connected_cluster_of_size(SPARSE_CLUSTER_MAX)
        codes = [e["code"] for e in part.events]
        assert "sparse_cluster" in codes

    def test_size_above_max_does_not_emit_sparse_warning(self):
        # 4 members → no warning
        part = self._connected_cluster_of_size(SPARSE_CLUSTER_MAX + 1)
        codes = [e["code"] for e in part.events]
        assert "sparse_cluster" not in codes


# ---------------------------------------------------------------------------
# Determinism (Louvain seeded by schematic_path)
# ---------------------------------------------------------------------------

class TestDeterminism:
    def _ring_netlist(self, n: int = 6) -> dict:
        # Two triangles bridged by one edge — ambiguous enough that Louvain
        # could in principle land different ways across runs, but with a
        # fixed seed must be stable.
        refs = [f"N{i}" for i in range(n)]
        nets = {
            "S0": [_pin("N0", "1"), _pin("N1", "1"), _pin("N2", "1")],
            "S1": [_pin("N3", "1"), _pin("N4", "1"), _pin("N5", "1")],
            "BRIDGE": [_pin("N2", "2"), _pin("N3", "2")],
        }
        return _netlist(refs, nets)

    def test_same_path_yields_same_partition(self):
        nl = self._ring_netlist()
        p1 = cluster_components(nl, schematic_path="/abs/path/board.kicad_sch")
        p2 = cluster_components(nl, schematic_path="/abs/path/board.kicad_sch")
        assert p1.component_cluster == p2.component_cluster
        assert p1.members == p2.members

    def test_different_paths_may_differ_but_are_independently_stable(self):
        nl = self._ring_netlist()
        a1 = cluster_components(nl, schematic_path="/path/a.kicad_sch")
        a2 = cluster_components(nl, schematic_path="/path/a.kicad_sch")
        b1 = cluster_components(nl, schematic_path="/path/b.kicad_sch")
        b2 = cluster_components(nl, schematic_path="/path/b.kicad_sch")
        assert a1.members == a2.members
        assert b1.members == b2.members


# ---------------------------------------------------------------------------
# Cluster-ID stability across re-runs (previous_partition remap)
# ---------------------------------------------------------------------------

class TestClusterIdStability:
    def test_unchanged_membership_preserves_ids(self):
        refs = ["U1", "R1", "R2", "R3", "U2", "C1", "C2", "C3"]
        # Two clusters: {U1,R1,R2,R3} and {U2,C1,C2,C3}
        nets = {
            "A1": [_pin("U1", "1"), _pin("R1", "1")],
            "A2": [_pin("U1", "2"), _pin("R2", "1")],
            "A3": [_pin("U1", "3"), _pin("R3", "1")],
            "B1": [_pin("U2", "1"), _pin("C1", "1")],
            "B2": [_pin("U2", "2"), _pin("C2", "1")],
            "B3": [_pin("U2", "3"), _pin("C3", "1")],
        }
        nl = _netlist(refs, nets)
        first = cluster_components(nl, schematic_path="/tmp/stable.kicad_sch")
        second = cluster_components(
            nl,
            schematic_path="/tmp/stable.kicad_sch",
            previous_partition=first,
        )
        assert first.component_cluster == second.component_cluster

    def test_new_cluster_gets_fresh_id_not_recycled(self):
        # Start with one cluster.
        refs = ["U1", "R1", "R2"]
        nets = {"S": [_pin("U1", "1"), _pin("R1", "1"), _pin("R2", "1")]}
        first = cluster_components(
            _netlist(refs, nets), schematic_path="/tmp/grow.kicad_sch",
        )
        first_ids = set(first.members.keys())

        # Add a disconnected cluster.
        refs2 = refs + ["U2", "C1"]
        nets2 = dict(nets)
        nets2["T"] = [_pin("U2", "1"), _pin("C1", "1")]
        second = cluster_components(
            _netlist(refs2, nets2),
            schematic_path="/tmp/grow.kicad_sch",
            previous_partition=first,
        )

        # The original cluster's id should be preserved (largest overlap).
        u1_cid_first = first.component_cluster["U1"]
        u1_cid_second = second.component_cluster["U1"]
        assert u1_cid_first == u1_cid_second

        # The new cluster's id should not collide with the original.
        u2_cid = second.component_cluster["U2"]
        assert u2_cid not in first_ids


# ---------------------------------------------------------------------------
# missing_pintypes event surfaced at cluster_components boundary
# ---------------------------------------------------------------------------

class TestMissingPintypeEvent:
    def test_event_emitted_when_pintypes_are_absent(self):
        nl = _netlist(
            ["U1", "R1"],
            {"SIG": [_pin("U1", "1", pintype=None), _pin("R1", "1", pintype=None)]},
        )
        part = cluster_components(nl, schematic_path="/tmp/p.kicad_sch")
        codes = [e["code"] for e in part.events]
        assert "missing_pintypes" in codes
        ev = next(e for e in part.events if e["code"] == "missing_pintypes")
        assert ev["data"]["count"] == 2

    def test_event_absent_when_all_pintypes_present(self):
        nl = _netlist(
            ["U1", "R1"],
            {"SIG": [_pin("U1", "1", pintype="output"), _pin("R1", "1", pintype="passive")]},
        )
        part = cluster_components(nl, schematic_path="/tmp/p.kicad_sch")
        codes = [e["code"] for e in part.events]
        assert "missing_pintypes" not in codes
