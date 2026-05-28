"""Layer 1 — topology clustering via Louvain community detection.

See ``docs/SPEC_Schematic_Placement.md`` § "Layer 1 — Topology".

Inputs:
  - netlist dict from ``extract_netlist_via_cli``
    (shape: ``{"components": {ref: {...}}, "nets": {name: [{component, pin, pintype?, ...}]}}``)

Outputs:
  - ``ClusterPartition``: cluster_id per component + per-cluster modularity
  - structured ``events`` list (singleton_orphan, sparse_cluster, missing_pintypes)

The graph excludes power-net edges (per spec). Edge weights are net
multiplicity — a component pair sharing N non-power nets gets weight N.

Determinism: Louvain runs with ``seed = sha256(schematic_path)[0:16] as int``
so repeated calls produce identical partitions. When ``previous_partition``
is supplied, new cluster IDs are remapped to best-match the previous
assignment so ``cluster_id`` is stable across iteration.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any

import networkx as nx
from networkx.algorithms.community import louvain_communities

from kicad_mcp.utils.netlist_parser import is_power_net

logger = logging.getLogger(__name__)

# Pin types that mark a power connection. Edges incident only to power
# pins are dropped (per spec). We also drop edges whose net is a power
# net by name (VCC/VDD/GND/etc.), as a belt-and-braces filter.
_POWER_PINTYPES = frozenset({"power_in", "power_out"})

# Cluster member-count below which we emit `sparse_cluster` (per spec § Layer 1).
SPARSE_CLUSTER_MAX = 3


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class ClusterPartition:
    """Result of Layer 1 clustering."""

    # ref → cluster_id ("c0", "c1", ...). Singletons get their own cluster
    # id; the ``singletons`` set tracks which clusters those are.
    component_cluster: dict[str, str] = field(default_factory=dict)
    # cluster_id → set of refs
    members: dict[str, list[str]] = field(default_factory=dict)
    # cluster_id → modularity contribution (filled by Louvain)
    modularity: dict[str, float] = field(default_factory=dict)
    # cluster_ids whose only member has no signal-net edges
    singletons: set[str] = field(default_factory=set)
    # Global graph modularity score from Louvain
    graph_modularity: float = 0.0
    # Events for the caller to surface via mcp-events
    events: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_signal_graph(netlist: dict[str, Any]) -> tuple[nx.Graph, int, int]:
    """Build the undirected signal-net graph for clustering.

    Returns ``(graph, missing_pintype_count, power_nets_dropped)``.

    Each component is a node (even if it has no edges — singletons stay
    in the graph). Each non-power net contributes a clique among its
    non-power-pin members; clique edges accumulate weight when multiple
    nets connect the same pair (multiplicity).
    """
    components = netlist.get("components", {}) or {}
    nets = netlist.get("nets", {}) or {}

    g: nx.Graph = nx.Graph()
    g.add_nodes_from(components.keys())

    edge_weights: Counter[tuple[str, str]] = Counter()
    missing_pintype = 0
    power_nets_dropped = 0

    for net_name, pins in nets.items():
        if is_power_net(net_name):
            power_nets_dropped += 1
            continue

        signal_refs: list[str] = []
        for pin in pins:
            ref = pin.get("component")
            if not ref:
                continue
            pt = pin.get("pintype")
            if pt is None:
                missing_pintype += 1
                pt = "passive"  # spec § Layer 1 fallback
            if pt in _POWER_PINTYPES:
                continue
            signal_refs.append(ref)

        # Dedup within the same net so a 2-pin component on one net
        # doesn't self-loop or double-weight.
        unique_refs = sorted(set(signal_refs))
        for a, b in combinations(unique_refs, 2):
            edge_weights[(a, b)] += 1

    for (a, b), w in edge_weights.items():
        g.add_edge(a, b, weight=w)

    return g, missing_pintype, power_nets_dropped


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------

def cluster_components(
    netlist: dict[str, Any],
    *,
    schematic_path: str,
    previous_partition: ClusterPartition | None = None,
) -> ClusterPartition:
    """Run Layer 1 clustering. Returns a ``ClusterPartition``.

    ``schematic_path`` only contributes to the Louvain seed (determinism);
    the file is not opened here. The netlist must already be extracted.
    """
    graph, missing_pintype, _ = build_signal_graph(netlist)

    events: list[dict[str, Any]] = []
    if missing_pintype:
        events.append({
            "level": "info",
            "code": "missing_pintypes",
            "message": (
                f"{missing_pintype} pins had no `pintype` attribute in the "
                "netlist XML; treating as `passive` per Layer 1 fallback."
            ),
            "data": {"count": missing_pintype},
        })

    # Edge case: empty schematic.
    if graph.number_of_nodes() == 0:
        return ClusterPartition(events=events)

    seed = _louvain_seed(schematic_path)

    # Components with edges → Louvain. Isolated nodes → one cluster each.
    isolates = list(nx.isolates(graph))
    connected_subgraph = graph.copy()
    connected_subgraph.remove_nodes_from(isolates)

    if connected_subgraph.number_of_nodes() == 0:
        communities: list[set[str]] = []
    else:
        # ``louvain_communities`` returns a list of sets.
        try:
            communities = list(louvain_communities(
                connected_subgraph, weight="weight", seed=seed,
            ))
        except Exception as e:  # networkx can raise on degenerate graphs
            logger.warning("Louvain failed on subgraph; treating each node as a cluster: %s", e)
            communities = [{n} for n in connected_subgraph.nodes()]

    # Assemble partition. Isolates become singleton clusters.
    raw_groups: list[set[str]] = [set(c) for c in communities]
    for iso in isolates:
        raw_groups.append({iso})

    # Stable cluster IDs: optionally remap to match previous_partition.
    if previous_partition is not None:
        cluster_ids = _remap_for_stability(raw_groups, previous_partition.members)
    else:
        cluster_ids = [f"c{i}" for i in range(len(raw_groups))]

    component_cluster: dict[str, str] = {}
    members: dict[str, list[str]] = {}
    singletons: set[str] = set()
    for cid, group in zip(cluster_ids, raw_groups, strict=True):
        sorted_refs = sorted(group)
        members[cid] = sorted_refs
        for ref in sorted_refs:
            component_cluster[ref] = cid
        if len(sorted_refs) == 1 and sorted_refs[0] in isolates:
            singletons.add(cid)
            events.append({
                "level": "warn",
                "code": "singleton_orphan",
                "message": (
                    f"Component {sorted_refs[0]} has no signal-net edges; "
                    "placed as an isolated cluster."
                ),
                "data": {"ref": sorted_refs[0], "cluster_id": cid},
            })
        elif len(sorted_refs) <= SPARSE_CLUSTER_MAX and sorted_refs[0] not in isolates:
            events.append({
                "level": "info",
                "code": "sparse_cluster",
                "message": (
                    f"Cluster {cid} has only {len(sorted_refs)} members — "
                    "may indicate misgrouping; review before relying on its label."
                ),
                "data": {"cluster_id": cid, "member_count": len(sorted_refs)},
            })

    # Global modularity (informational; per-cluster modularity not exposed
    # by louvain_communities directly).
    graph_modularity = 0.0
    if connected_subgraph.number_of_edges() > 0 and communities:
        try:
            graph_modularity = nx.algorithms.community.modularity(
                connected_subgraph, communities, weight="weight",
            )
        except Exception:
            graph_modularity = 0.0

    return ClusterPartition(
        component_cluster=component_cluster,
        members=members,
        modularity={},  # per-cluster modularity not computed in v1
        singletons=singletons,
        graph_modularity=graph_modularity,
        events=events,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _louvain_seed(schematic_path: str) -> int:
    """Deterministic seed derived from schematic path."""
    h = hashlib.sha256(schematic_path.encode("utf-8")).digest()
    # Take first 4 bytes → 32-bit int (networkx accepts any int).
    return int.from_bytes(h[:4], "big")


def _remap_for_stability(
    new_groups: list[set[str]],
    previous_members: dict[str, list[str]],
) -> list[str]:
    """Greedy overlap matching: assign new groups to existing cluster_ids
    when membership overlap is largest.

    Per spec: ``state=previous`` should preserve ``cluster_id`` across
    minor topology changes. Brand-new clusters get fresh IDs that don't
    collide with previously-seen IDs.
    """
    prev_id_to_set = {cid: set(refs) for cid, refs in previous_members.items()}
    used: set[str] = set()
    assigned: list[str | None] = [None] * len(new_groups)

    # Score every (new_group_idx, prev_id) pair by overlap size, descending.
    scored: list[tuple[int, int, str]] = []
    for i, g in enumerate(new_groups):
        for cid, prev_set in prev_id_to_set.items():
            overlap = len(g & prev_set)
            if overlap > 0:
                scored.append((overlap, i, cid))
    scored.sort(reverse=True)

    for _overlap, i, cid in scored:
        if assigned[i] is not None or cid in used:
            continue
        assigned[i] = cid
        used.add(cid)

    # Fill in fresh IDs for unmatched new groups.
    existing_indices = set()
    for cid in previous_members:
        if cid.startswith("c") and cid[1:].isdigit():
            existing_indices.add(int(cid[1:]))
    next_idx = max(existing_indices, default=-1) + 1

    result: list[str] = []
    for a in assigned:
        if a is None:
            result.append(f"c{next_idx}")
            next_idx += 1
        else:
            result.append(a)
    return result
