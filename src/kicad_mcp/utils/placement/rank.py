"""Phase 2 — Rank clusters into signal-flow tiers via directed BFS.

See ``docs/SPEC_Schematic_Placement.md`` § "Phase 2 — Rank".

Tier 0 is leftmost (sources — connectors, regulators, oscillators). Each
subsequent tier consumes signals from earlier tiers. Cycles are broken at
the lowest-confidence edge; if no clear source exists, the cluster with
the most outgoing edges is treated as origin.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from kicad_mcp.utils.netlist_parser import is_power_net

# Pintypes that drive signals out of a component.
_OUTPUT_PINTYPES = frozenset({"output", "power_out", "tri_state",
                              "open_collector", "open_emitter"})
# Pintypes that consume signals.
_INPUT_PINTYPES = frozenset({"input", "power_in"})
# Bidirectional pins contribute soft edges in both directions.
_BIDIR_PINTYPES = frozenset({"bidirectional"})


@dataclass
class TierAssignment:
    """Result of Phase 2 ranking."""

    # cluster_id → tier index (0 = leftmost = sources)
    cluster_tier: dict[str, int] = field(default_factory=dict)
    # tier index → list of cluster_ids in that tier
    tiers: dict[int, list[str]] = field(default_factory=dict)
    # Directed inter-cluster edge weights (cluster_a → cluster_b → weight)
    cluster_edges: dict[str, dict[str, float]] = field(default_factory=dict)
    # Events surfaced via mcp-events (signal_flow_cycle, no_signal_source)
    events: list[dict[str, Any]] = field(default_factory=list)


def assign_tiers(
    netlist: dict[str, Any],
    component_cluster: dict[str, str],
) -> TierAssignment:
    """Assign each cluster a tier index based on signal-flow direction.

    Returns ``TierAssignment``. Empty clusters → empty result.
    """
    if not component_cluster:
        return TierAssignment()

    cluster_edges = _build_inter_cluster_edges(netlist, component_cluster)
    # Use a sorted list (not a set) so DiGraph node order and downstream
    # iteration are deterministic across Python processes. Python sets
    # iterate in PYTHONHASHSEED-randomized order, which makes cycle-break
    # edge choice non-deterministic when weights tie — the spec's
    # cluster-id stability guarantee depends on this being reproducible.
    all_clusters_sorted: list[str] = sorted(set(component_cluster.values()))

    events: list[dict[str, Any]] = []

    # Build a NetworkX DiGraph so we can detect cycles cleanly.
    dg: nx.DiGraph = nx.DiGraph()
    dg.add_nodes_from(all_clusters_sorted)
    for src in sorted(cluster_edges):
        for dst in sorted(cluster_edges[src]):
            dg.add_edge(src, dst, weight=cluster_edges[src][dst])

    # Cycle breaking: while a cycle exists, drop the lowest-weight edge on
    # it. Tie-break by (src, dst) ref order so equal-weight cycles
    # (e.g. all-bidirectional with weight 0.5) break the same edge across
    # processes — see the deterministic-cycle tests in test_placement_rank_pack.
    cycles_broken: list[tuple[str, str]] = []
    while True:
        try:
            cycle = nx.find_cycle(dg, orientation="original")
        except nx.NetworkXNoCycle:
            break
        edges = [(u, v) for (u, v, *_rest) in cycle]
        u, v = min(edges, key=lambda e: (dg[e[0]][e[1]].get("weight", 1.0), e[0], e[1]))
        dg.remove_edge(u, v)
        cycles_broken.append((u, v))

    if cycles_broken:
        events.append({
            "level": "warn",
            "code": "signal_flow_cycle",
            "message": (
                f"Broke {len(cycles_broken)} signal-flow cycle(s) at the "
                "lowest-confidence edge to enable tiering."
            ),
            "data": {"edges_dropped": [list(e) for e in cycles_broken]},
        })

    # Source clusters: in-degree 0 in the now-acyclic graph.
    sources = [c for c in all_clusters_sorted if dg.in_degree(c) == 0]

    if not sources and all_clusters_sorted:
        # Fallback per spec: pick the cluster with the most outgoing edges.
        # max() over a sorted list returns the FIRST element on ties, which
        # — combined with alphabetic ordering — gives a deterministic
        # fallback across Python processes.
        fallback = max(all_clusters_sorted, key=dg.out_degree)
        sources = [fallback]
        events.append({
            "level": "warn",
            "code": "no_signal_source",
            "message": (
                "No cluster has in-degree 0 after cycle-breaking; "
                f"using {fallback!r} (most outgoing edges) as origin."
            ),
            "data": {"fallback_origin": fallback},
        })

    # BFS to assign tier as the longest distance from any source.
    # Using longest-path tier ensures a cluster sits to the right of every
    # cluster that drives it, even if a shorter path also exists.
    cluster_tier: dict[str, int] = dict.fromkeys(sources, 0)
    queue: deque[str] = deque(sources)
    while queue:
        cur = queue.popleft()
        cur_tier = cluster_tier[cur]
        for succ in dg.successors(cur):
            new_tier = cur_tier + 1
            if succ not in cluster_tier or cluster_tier[succ] < new_tier:
                cluster_tier[succ] = new_tier
                queue.append(succ)

    # Any clusters still untiered (disconnected components in another
    # cycle component) → place them at tier 0 alongside sources.
    for c in all_clusters_sorted:
        if c not in cluster_tier:
            cluster_tier[c] = 0

    tiers: dict[int, list[str]] = defaultdict(list)
    for c, t in cluster_tier.items():
        tiers[t].append(c)
    for t in tiers:
        tiers[t].sort()  # deterministic ordering before barycentric pass

    return TierAssignment(
        cluster_tier=cluster_tier,
        tiers=dict(tiers),
        cluster_edges=cluster_edges,
        events=events,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_inter_cluster_edges(
    netlist: dict[str, Any],
    component_cluster: dict[str, str],
) -> dict[str, dict[str, float]]:
    """Aggregate directed signal edges from components into clusters.

    For each non-power net:
      - find pins with output-class pintype → these are signal sources
      - find pins with input-class pintype → these are signal sinks
      - bidirectional pins contribute as both, with lower weight (0.5)
      - passive pins do not originate signals; they propagate (handled
        implicitly because passives still get clique-bridged at the
        Layer 1 graph level)

    Edge weight = number of nets contributing the directed connection.
    Edges that would be self-loops (same source/sink cluster) are dropped.
    """
    nets = netlist.get("nets") or {}
    edges: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))

    for net_name, pins in nets.items():
        if is_power_net(net_name):
            continue
        sources: list[tuple[str, float]] = []  # (cluster_id, weight)
        sinks: list[tuple[str, float]] = []
        for pin in pins:
            ref = pin.get("component")
            cid = component_cluster.get(ref)
            if cid is None:
                continue
            pt = pin.get("pintype") or "passive"
            if pt in _OUTPUT_PINTYPES:
                sources.append((cid, 1.0))
            elif pt in _INPUT_PINTYPES:
                sinks.append((cid, 1.0))
            elif pt in _BIDIR_PINTYPES:
                sources.append((cid, 0.5))
                sinks.append((cid, 0.5))

        for s_cid, s_w in sources:
            for t_cid, t_w in sinks:
                if s_cid == t_cid:
                    continue
                edges[s_cid][t_cid] += min(s_w, t_w)

    return {k: dict(v) for k, v in edges.items()}
