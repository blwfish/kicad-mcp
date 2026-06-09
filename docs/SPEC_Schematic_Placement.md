# SPEC — Schematic Auto-Placement

**Status:** Approved for implementation. All three prerequisite SPECs (mcp-events, Feedback Infrastructure, Component Intelligence) have landed; the remaining gate is the ground-truth fixture set (see Open Questions).
**Author:** Brian + Claude
**Date:** 2026-05-27 (revised 2026-05-28 for Layer 3 API correction, prereq status, tool-count drift)
**Target:** Topology-aware automatic placement of components in a KiCad schematic. Produces a structured state object that a calling LLM iterates against.
**Trigger:** [GitHub issue #11](https://github.com/blwfish/kicad-mcp/issues/11) — external user asked whether AI-generated schematics always need manual reorganization. Baseline today: yes; this SPEC's goal is "first draft is usable in 70-85% of cases."

## Motivation

AI-generated schematics today have every coordinate supplied by the LLM's imagination — no spatial awareness, no signal-flow convention, no component-size awareness, no collision detection. The result is unusable without manual reorganization for anything more than a handful of components. Issue #11 surfaced this as a real pain point for external users.

The quick wins from the same session (grid snap, `add_wire_between_pins`) addressed labels and wire elbows but did not address placement itself. This SPEC defines the placement tool.

### Quality target

**70-85% of small-to-medium AI-generated schematics produce a layout that the calling LLM can finalize via a few iterations and minor tweaks**, vs. today's baseline of "essentially 0%" usable without manual reorganization. The estimate is structural — until ground-truth fixtures exist, we don't have measurement.

We're not competing with Altium's auto-placer or a human designer. We're competing with "raw LLM coordinates."

## Non-goals

- **Not human-grade layout.** The 70-85% bar is "good first draft," not "polished final."
- **Not for non-LLM consumers.** The iteration loop assumes Claude (or another AI assistant) is the caller. Manual usage works but isn't optimized for.
- **Not autorouting** — that's `autoroute_pcb`, a separate concern.
- **Not PCB placement** — see [the existing `suggest_placement` PCB tool](../src/kicad_mcp/tools/pcb_planning.py). Schematic placement is a distinct problem (more semantic, less geometric) and diverges intentionally; see "Divergence from PCB placement" below.
- **Not hierarchical sheets in v1.** Flat schematics only. Multi-sheet support deferred (see Deferred section).
- **Not wire re-routing.** Placement output is coordinates only; existing wires aren't auto-reflowed. See "Precondition: un-wired or accept-stale-wires" below.

## Dependencies

This SPEC depends on:

- **`mcp-events` package** (per [`SPEC_OOB_Events.md`](SPEC_OOB_Events.md), implemented 2026-05-27, published as `mcp-events>=0.1.0`) — for surfacing warnings (bus-bridge detection, low-confidence labels, etc.)
- **Feedback Infrastructure** (per [`SPEC_Feedback_Infrastructure.md`](SPEC_Feedback_Infrastructure.md), merged via PR #39 on 2026-05-27) — for telemetry-driven calibration of the algorithm
- **Component Intelligence (LCSC)** (per [`SPEC_Component_Intelligence_LCSC.md`](SPEC_Component_Intelligence_LCSC.md), merged via PR #40 on 2026-05-28; module is `kicad_mcp.tools.lcsc`, not `component_intelligence`) — *optional* but enhances Layer 3 (resolved-part labeling). The tool degrades gracefully when no LCSC data is assigned. Note: the shipped jlcparts local schema does not retain the upstream `firstSortName`/`secondSortName` category columns; Layer 3 derives labels from the `ResolvedPart` fields that *are* exposed. See Layer 3 below.
- **Existing kicad-cli netlist export** with `pintype` extraction (already happens in [`extract_netlist_via_cli`](../src/kicad_mcp/utils/netlist_parser.py:472); this SPEC formalizes its use). Verified 2026-05-28 against KiCad 10's `kicadxml` format on the bundled `stm32f100-discovery-shield` template: every `<node>` element carries `pintype` and `pinfunction` attributes (62/62 on that template). The catch-all attribute capture in `extract_netlist_via_cli` already harvests them under the same key names. `pintype` may still be absent on user-defined symbols where the symbol author omitted the electrical-type field; see the "Missing `pintype` fallback" bullet in Layer 1 below.
- **Existing `pattern_recognition.py`** — but demoted from cluster-driver to labeling-layer (see Layer 2 below). The `MCP\d+` voltage-collision and RC-filter false-positive issues called out in earlier drafts of this SPEC were already fixed in PR #38 (2026-05-27) as a pre-flight pass; nothing further needed here.
- **`networkx` ≥ 3.0** — for graph construction and Louvain community detection (`networkx.algorithms.community.louvain_communities`). Added to `pyproject.toml` in the same revision as this SPEC update (2026-05-28); `uv.lock` resolves to 3.6.x at time of writing.

**Implementation order**: all three named prerequisite SPECs above have landed. This is the capstone and is now unblocked on dependency work; the remaining gate is the ground-truth fixture set.

## Design principles

1. **Layered understanding, with each layer optional.** Topology is the foundation; pattern recognition, resolved-part data, and caller hints layer in additional richness without being required. Quality scales with available information.
2. **Calibration over coverage.** The algorithm should be *honestly uncertain* when uncertain. A confidently-wrong label is worse than an honestly-uncertain one because Claude trusts the label.
3. **State machine, not magic function.** The tool returns a structured `PlacementState`. The calling LLM iterates against this state via overrides, hints, frozen regions. Each call is a state-machine transition, not a one-shot RPC.
4. **Context-frugal.** Default `verbosity="minimal"`. Stateful (server-cached) mode for iteration on context-constrained callers. See `feedback_context_frugality.md`.
5. **Synchronous at call boundary.** All telemetry recording and attribution is inline; no daemons. See `feedback_synchronous_at_call_boundary.md`.

## The 4-layer architecture

Each layer adds semantic richness when its inputs are present; the next layer down is fallback.

### Layer 1 — Topology (always present)

Community-detection clustering on the signal-net graph.

- **Graph construction**: nodes are components; edges are signal-net connections (power nets filtered via `pintype in {"power_in", "power_out"}` exclusion). Edges are weighted by net multiplicity (a component pair sharing multiple non-power nets has higher weight).
- **Missing `pintype` fallback**: pins with no `pintype` in the netlist XML are treated as `passive`. A `missing_pintypes` info-level event is emitted with a count of affected pins when this fallback fires, so telemetry can surface how often symbol authors omit it.
- **Clustering**: Louvain community detection via `networkx.algorithms.community.louvain_communities`. Produces a partition of components into clusters with no labels yet.
- **Determinism**: Louvain is seeded with a hash of the schematic path so repeated `suggest` calls on the same schematic produce the same partition. This is required for `cluster_id` stability guarantees (see Stateful mode). When `state=previous` is passed, new Louvain output is relabeled to best-match previous cluster assignments before returning (greedy overlap matching), so `cluster_id`s survive minor topology changes.
- **Output**: `cluster_id` per component; `members` and `louvain_modularity` per cluster.

Failure modes (and how the algorithm reports them):
- **Bus bridges** (SPI/I2C connecting two functional blocks heavily enough to merge them) → emit `bus_bridge_suspected` warning when two pre-merge sub-clusters had high internal modularity
- **Singletons** (components with no signal edges after power filtering — TVS diodes, fuses, ferrite beads) → emit `singleton_orphan` warning; placed but unclustered
- **Sparse clusters** (3 or fewer members) → emit `sparse_cluster` info-level event; may indicate misgrouping

### Layer 2 — Pattern recognition labeling (when applicable)

Uses the existing `pattern_recognition.py` module, but demoted to a *labeling* role with confidence scores.

For each cluster from Layer 1:
- Run `identify_microcontrollers`, `identify_power_supplies`, `identify_amplifiers`, etc. on the cluster's components
- If exactly one identifier matches with high confidence → label the cluster (e.g., `"mcu"`, `"ldo"`, `"op_amp_circuit"`) with `label_source="pattern_recognition"` and `label_confidence ∈ [0.5, 1.0]`
- If multiple identifiers match (collision) → emit `pattern_recognition_collision` warning; label with lower confidence
- If none match → label = `"unclassified"`, `label_confidence=0.0`, `label_source="topology_only"`

**Coverage**: the 2026-05-27 audit found `pattern_recognition.py` classifies ~33% of components on a representative IoT fixture. The known false-positives (`MCP\d+` collision, RC-filter false positive on I²C pull-ups) were fixed in PR #38 (2026-05-27) ahead of this SPEC; no further `pattern_recognition.py` work is in scope here. Expanding coverage (level shifters, motor drivers, voltage references, etc.) is its own future project. Calibrated honesty (33% covered, 67% honestly unclassified) is the right shape for this layer.

### Layer 3 — Resolved-part labeling (when LCSC data present)

When components have an `LCSC` property assigned (via the `lcsc` router's `assign` operation), supplier metadata is used to label clusters with higher confidence than pattern_recognition alone.

- **Interface**: call [`kicad_mcp.tools.lcsc`](../src/kicad_mcp/tools/lcsc.py)'s `resolve` operation (or `lcsc_db.get_component(lcsc_id)` directly for an in-process path that avoids the MCP tool overhead). On hit, returns a `ResolvedPart` (see `lcsc.py:72`) with fields: `mpn`, `manufacturer`, `description`, `package`, `pin_count`, `assembly_tier`, `kicad_symbol_lib_id`, `kicad_footprint_path`. On miss, returns `None`. Layer 3 is a no-op (degrades silently to Layer 2) whenever the lookup returns `None`, the ToS has not been accepted, or the jlcparts snapshot is unavailable — no error, no warning.
- **No upstream category fields**: the upstream jlcparts dataset has `firstSortName` / `secondSortName` category columns, but the kicad-mcp local SQLite schema (see [`utils/lcsc_db.py`](../src/kicad_mcp/utils/lcsc_db.py) `_create_schema`) does NOT retain them. Earlier drafts of this SPEC assumed those columns were available; they are not. Label derivation works against the fields that *are* exposed:
  - `mpn` + `manufacturer` — strong signal for well-known parts (e.g., MPN starting with `LM`, `TPS`, `LT` and manufacturer in `{Texas Instruments, Linear Technology, ...}` → likely an analog IC; matched against a small lookup table)
  - `description` — short product description string. Keyword match against label vocabulary (e.g., `"LDO"`, `"buck"`, `"op-amp"`, `"comparator"`, `"MCU"`, `"FRAM"`, `"crystal"`, `"connector"`). Substring matching is acceptable here only via a single, named classifier function — per the project's syntactic-seam rule, do NOT scatter ad-hoc `"ldo" in description.lower()` checks across the layer
  - `package` — supporting signal, never primary (`SOT-23` could be anything)
  - `pin_count` — supporting signal (a 100-pin part is not a passive)
- **Future option**: if Layer 3 quality is materially limited by the missing category columns, the jlcparts retention schema can be extended (one-line addition to `_create_schema` + rebuild). Out of scope for v1.
- **Precedence**: when a cluster's anchor component has a confident resolved-part label, that label wins over pattern_recognition's guess. `label_source="resolved_part"`, `label_confidence ∈ [0.7, 0.95]` (high but not 1.0 — we asked the supplier about the *part*, not the *role in this schematic*; the description-keyword step is still heuristic).

This is the highest-quality labeling layer when data is available, but doesn't require all components to be resolved — it dominates per-cluster based on what's known. The `lcsc` tool owns the database location and schema; this layer consumes only the public interface above.

### Layer 4 — Caller hints (override)

The calling LLM can pass explicit semantic information:

```python
hints = {
    "U1": "mcu",                # cluster anchor type
    "C3": "decoupling_for(U1)", # role within cluster
    "J1": "edge_connector",     # placement convention hint
}
```

Hints override all lower layers for the referenced components. `label_source="caller_hint"`, `label_confidence=1.0` (trust the caller).

## The 3-phase algorithm

Layered understanding feeds a 3-phase layout algorithm.

### Phase 1 — Cluster

Run Layer 1 (topology) → Layer 2 (labels) → Layer 3 (resolved-part) → Layer 4 (hints). Layers are processed in this order, with each layer able to override the previous; Layer 4 has highest precedence. Output: cluster assignments + labels + confidence per cluster.

### Phase 2 — Rank (signal flow → column tiers)

Directed BFS on the signal-net graph using `pintype`:

- Build directed edges: pin `output`/`power_out` → pin `input`/`power_in` on the same net (within a non-power net)
- For `bidirectional` pins: add soft edges in both directions (weighted lower than directed edges)
- For `passive` pins (including those whose `pintype` was absent and fell back to `passive` in Layer 1): propagate direction transitively (a resistor between MCU output and LED input forwards the direction)
- Identify source nodes: clusters whose anchor component has no signal inputs (connectors, regulators, oscillators)
- BFS from source nodes; assign each cluster a tier (column index, 0 = leftmost = sources)
- Within a tier, secondary sort by cluster size (larger clusters more central)

Output: tier per cluster.

Failure modes:
- **Cycle in the directed graph**: emit `signal_flow_cycle` warning; break the cycle at the lowest-confidence edge
- **No clear source node**: emit `no_signal_source` warning; pick the cluster with the most outgoing edges as origin

### Phase 3 — Pack (coordinates + conventions)

Within each tier, lay components out left-to-right or top-to-bottom:

- **Order within tier**: barycentric heuristic (sort components by average position of their connected components in adjacent tiers). Reduces crossings.
- **Coordinate assignment**: `x_mm = tier * column_pitch_mm`, `y_mm = row_index * row_pitch_mm`. Defaults: `column_pitch_mm=25.4`, `row_pitch_mm=12.7` (1 inch / 0.5 inch on the KiCad grid).
- **Cluster bounding box**: each cluster gets a bbox; sub-conventions place components within the bbox.

Convention rules applied within each cluster (when label permits):

| Cluster label | Convention |
|---|---|
| Any IC with decap (cap connected only to power) | Decap below IC, snapped to grid |
| Regulator | VIN-side passives left; VOUT-side passives right |
| MCU | Crystal close to OSC pins (if pin functions identifiable); power pins ungrouped from signal pins |
| Op-amp circuit | Inverting input on top, non-inverting on bottom |
| Connector (anchor only) | Push to left or right edge of board (input vs output direction) |

The convention set is **explicitly v1-minimal**. Failure to apply a convention is not an error; it emits an info-level event so we can see (via telemetry) which conventions are most often skipped and why.

Output: `x_mm`, `y_mm`, `rotation`, `mirror_x` per component.

## `PlacementState` schema

The structured output that Claude reads. Same shape returned by every call.

```python
{
    "schematic_path": str,
    "algorithm_version": str,
    "computed_at": iso8601_str,
    "schematic_hash": str,  # SHA-256 of schematic file at suggest time; checked at apply time for drift

    "components": {
        "<ref>": {
            "x_mm": float,
            "y_mm": float,
            "rotation": 0 | 90 | 180 | 270,
            "mirror_x": bool,
            "cluster_id": str,
            "fixed_by": "caller" | "tool" | None,
        },
        # ...
    },

    "clusters": {
        "<cluster_id>": {
            "members": [ref, ...],
            "anchor": ref | None,
            "label": "mcu" | "ldo" | "i2c_bus" | "unclassified" | ...,
            "label_confidence": 0.0 - 1.0,
            "label_source": "pattern_recognition" | "resolved_part" |
                            "caller_hint" | "topology_only",
            "tier": int | None,
            "bbox_mm": {"x": float, "y": float, "w": float, "h": float},
        },
        # ...
    },

    "tiers": { tier_index: [cluster_id, ...] },

    "events": [Event, ...],  # surfaced via mcp-events (warnings, info, errors)

    "inputs_honored": {
        "hints_applied": [ref, ...],
        "fixed_positions": [ref, ...],
        "redo_scope": [cluster_id, ...] | None,
    },

    "state_id": str,  # opaque ID for stateful iteration mode
}
```

### Verbosity modes

- `verbosity="minimal"` (default): coordinates + cluster_id + cluster anchor + label + label_confidence. ~50 bytes/component, ~30 bytes/cluster. A 200-component schematic returns ~12 KB.
- `verbosity="full"`: everything — provenance, decisions log, all warnings detail, bounding boxes, algorithm metadata. ~5-10× larger.

Sparse encoding: omit fields equal to defaults (`rotation: 0`, `mirror_x: false`, `fixed_by: None`, etc.).

### Stateful vs stateless modes

**Stateless** (caller has runway): caller passes full state in via `state` param; tool returns full state. Auditable, self-contained.

**Stateful** (caller is context-constrained): caller holds only `state_id`; tool persists state server-side at `~/.cache/kicad-mcp/placement_states/<state_id>.json` and returns only deltas. Last 5 states per schematic kept; 30-day expiry. The JSON file is the source of truth — in-memory state is a cache on top of it. After a server restart, a `state_id` remains valid as long as its JSON file exists; the first access after restart loads from disk.

Both modes accept the same input parameters; the difference is what gets returned and what the caller has to track.

## Tool API

A new router `schematic_layout` with three operations.

### `schematic_layout(operation="suggest", ...)`

```python
schematic_layout(
    operation="suggest",
    schematic_path: str | None = None,    # defaults to currently-loaded schematic

    # Iteration: pass previous state OR state_id
    state: PlacementState | None = None,
    state_id: str | None = None,

    # Caller-supplied semantic information (Layer 4)
    hints: dict[ref, str] = {},

    # Explicit cluster overrides (for when topology gets it wrong)
    cluster_assignments: dict[cluster_id, list[ref]] = {},

    # Positional constraints
    fixed_positions: dict[ref, dict] = {},
        # {"U1": {"x_mm": 50, "y_mm": 30, "rotation": 0}}

    # Cluster surgery
    merge_clusters: list[list[cluster_id]] = [],
    split_cluster: dict[cluster_id, list[list[ref]]] = {},

    # Scope of recomputation
    redo_scope: list[cluster_id] | None = None,
        # None = full redo; [c0, c2] = recompute only those; rest preserved

    # Algorithm knobs
    column_pitch_mm: float = 25.4,
    row_pitch_mm: float = 12.7,
    confidence_threshold: float = 0.5,

    # Output controls
    verbosity: str = "minimal",  # "minimal" | "full"
)
-> {
    "status": "ok",
    "state": PlacementState,   # full in stateless mode; delta in stateful mode
    "state_id": str,
    "events": [...],
}
```

Returns a placement state without mutating the schematic. Read-only.

### `schematic_layout(operation="apply", ...)`

```python
schematic_layout(
    operation="apply",
    state_id: str | None = None,           # use most-recent if None
    state: PlacementState | None = None,   # alternatively, pass full state
    refs: list[ref] | None = None,         # None = apply all; or partial
    schematic_path: str | None = None,
)
-> {
    "status": "ok",
    "applied": int,                        # count of components moved
    "errors": [...],                       # refs that couldn't be moved
    "events": [...],
}
```

"Dumb apply" semantics: walks `state.components` and calls `move_component` for each. Drift detection via `state.schematic_hash` recomputed at apply time — if it changed since suggest, emit a `placement_state_stale` warning (don't fail; caller decides).

### `schematic_layout(operation="clear_cache", ...)`

```python
schematic_layout(
    operation="clear_cache",
    schematic_path: str | None = None,     # None = all schematics
)
-> {"status": "ok", "cleared_count": int}
```

Manual cache eviction. Useful for testing and when the user wants a clean slate.

## Iteration loop

**Default workflow** (Claude is orchestrator):

1. Claude builds schematic (`add_component`, `add_wire`, ...)
2. Calls `schematic_layout(operation="suggest", schematic_path="foo.kicad_sch")` → first draft
3. Reads the `state` and `events`. Identifies any cluster with `bus_bridge_suspected` or `label_confidence < 0.5`
4. If needed, re-calls `schematic_layout` with `cluster_assignments` / `hints` / `fixed_positions` to fix specific issues. Each call cheap (~ms in stateful mode, full state returned in stateless)
5. Calls `schematic_layout(operation="apply", state_id=last_state_id)` → applies to schematic
6. Optional: opens schematic visually for user review or screenshots; user-fed feedback drives another iteration

**Iteration cost**: in stateful mode, each call ships only a delta — typically <1 KB even for large schematics. Caller context grows by deltas, not by full-state snapshots.

## Divergence from PCB `suggest_placement`

The PCB version returns a flat `{ref: {"x_mm", "y_mm", "mirror"}}` dict. Schematic placement returns rich state.

Reasons for divergence:
- PCB placement has fewer "what is this" decisions (schematic-level grouping is already done)
- PCB consumer use case is "smart starting position"; schematic consumer is "iterate with Claude"
- PCB tool ships today; retrofitting is real work; not justified preemptively

When/if PCB placement also needs iteration UX (the calibration loop showing frequent overrides on the PCB side), retrofit at that point. Documented as deliberate divergence; not technical debt.

## Telemetry integration

Per the Feedback Infrastructure SPEC:

- Every `schematic_layout` call records to the `calls` table with `tool_name="schematic_layout.suggest"` (or `.apply`, `.clear_cache`)
- Every cluster decision recorded to `cluster_decisions` (member_count, anchor, label, label_confidence, label_source, tier, louvain_modularity)
- Every emitted warning recorded to `warnings_emitted`
- Action attribution runs synchronously at suggest entry (per `feedback_synchronous_at_call_boundary.md`)

`output_summary` fields for placement:
```json
{
  "cluster_count": 7,
  "tier_count": 3,
  "label_source_breakdown": {"pattern_recognition": 3, "resolved_part": 1, "caller_hint": 2, "topology_only": 1},
  "low_confidence_cluster_count": 2,
  "warnings_emitted_count": 4,
  "lcsc_resolved_component_count": 5,
  "verbosity": "minimal"
}
```

## Convention rules (v1 set)

Minimal v1 set. Each is implemented as a function that takes a labeled cluster and outputs constrained positions; if no rule matches, the cluster gets generic placement (anchor centered, peripherals around).

| Rule code | Trigger | Effect |
|---|---|---|
| `decap_below_ic` | Cluster has 1 IC + ≥1 cap connected only to power nets shared with the IC | Caps placed in row below IC, x-aligned to IC center |
| `regulator_input_left` | Cluster labeled `"ldo"` or `"regulator"` | VIN pin's net on left, VOUT pin's net on right (uses `pintype` + `pinfunction`) |
| `mcu_crystal_proximate` | Cluster labeled `"mcu"` + cluster member labeled `"crystal"` | Crystal placed within `column_pitch_mm` of MCU, on the side with the OSC pins |
| `connector_edge` | Cluster anchor labeled `"connector"` or `"edge_connector"` | Push to tier 0 (left edge) or last tier (right edge) based on signal direction |
| `power_symbols_top_bottom` | Power-flag symbols (`+3V3`, `+5V`, `GND`, etc.) | Placed at top of cluster (positive rails) or bottom (ground) |

When a rule is applied, emit info-level event `convention_applied` with the rule code and refs affected. When a rule fails to apply (e.g., couldn't identify VIN pin), emit info-level event `convention_skipped`. Telemetry uses these to surface frequent skip reasons.

## v1 scope

Ship:
- `schematic_layout` router with operations `suggest`, `apply`, `clear_cache`
- 4-layer architecture (topology + pattern_recognition + resolved-part + hints)
- 3-phase algorithm (cluster → rank → pack)
- v1 convention rules (5 rules above)
- `PlacementState` schema with verbosity modes
- Stateful (server-cached) and stateless modes
- Telemetry integration per the Feedback Infrastructure SPEC
- OOB event surfacing for warnings/errors
- Cache management at `~/.cache/kicad-mcp/placement_states/`
- Pre-flight fixes to `pattern_recognition.py`: the `MCP\d+` collision and RC-filter false-positive on I²C pull-ups (these are blockers for honest calibration)
- Tests: unit tests for clustering, ranking, packing, conventions; integration tests against 3-5 ground-truth schematics (see Open Questions)

### Tool count after this PR

15 → 16 (`schematic_layout` router). Main is currently at 15 tools post-LCSC merge ([PR #40](https://github.com/blwfish/kicad-mcp/pull/40), 2026-05-28); the four-file lockstep applies per `project_docs_count_check` (`tests/test_server.py::test_current_tool_count` + `README.md` + `AGENT-INSTALL.md` + `TOOLS.md`).

### Test count target

Approximately +60-80 tests. The algorithm has many decision points; boundary coverage per project's threshold-testing rule.

### Estimated wall-clock

2-3 weeks of pairing-mode work. Longer than the other specs in this session because: (a) the algorithm has more inherent complexity, (b) heuristic tuning against real fixtures is iterative, (c) the 3-5 ground-truth fixtures must be picked before quality measurement is possible.

## Deferred from v1

- **Hierarchical schematic sheets.** Flat-only in v1. Sub-sheet support requires either (a) flattening then re-splitting, or (b) running the algorithm per-sheet with cross-sheet edge accounting. Significant scope addition; defer until a real use case emerges.
- **Convention rules beyond the v1 set.** Future candidates: bus grouping, differential pair placement, RF guard rings (probably out of scope ever — RF needs PCB layout), audio signal-path conventions, high-current trace allowances.
- **Manual iteration UX.** External users without an AI orchestrator (no Claude) get a usable first draft from `suggest`, then they tweak in KiCad GUI. No interactive cluster-clicking UI in v1.
- **Sub-second incremental updates.** v1 recomputes the entire algorithm on each `suggest` call (within `redo_scope` if specified). Truly-incremental "I just added one component" updates are deferred.
- **Cross-schematic layout consistency.** v1 treats each schematic independently. Multi-board projects with conventions (e.g., always-MCU-bottom-left) need a separate layer (defer).
- **Convention rule overrides per caller.** v1 has a fixed rule set. Caller-specifiable rules (e.g., "for this schematic, regulator goes on the right") deferred.
- **A11y / readability scoring.** Quantifying "does this look like a readable schematic" beyond crossing count and convention application. Real challenge; defer to a future v2.

## Open questions

These are decisions Brian needs to weigh in on before implementation begins:

1. **The 3-5 ground-truth test fixtures.** Without these, the 70-85% quality estimate is structural-not-measured. The fixtures should be representative AI-built schematics (MCU + peripherals, power supply, analog filter, sensor breakout, mixed-signal). Brian needs to pick (or draft) these — they're his contribution. They live at `tests/fixtures/schematic_placement/`.

2. **The wire-reflow precondition.** Three options:
   - (a) **Un-wired only**: `schematic_layout` requires the schematic to have no existing wires (caller deletes wires before placement, redraws after). Cleanest but most restrictive.
   - (b) **Accept stale wires**: tool emits a `wires_will_be_stale` warning if existing wires would no longer match component positions, but proceeds anyway. Caller decides whether to re-wire.
   - (c) **Re-wire after placement**: tool also reflows wires using `add_wire_between_pins`. Larger scope; less of a chance to ship v1 soon.
   - **Recommended: (b) for v1**, with `wires_will_be_stale` surfaced via `mcp-events`. Re-wire as v2.

3. **Whether the v1 convention rule set is the right cut.** I picked 5 rules I'm confident matter. Brian may want to add/remove based on what schematics he actually builds.

4. **The `role` field on components (deferred from earlier in this session).** Currently I'm using a 3-value enum: `"anchor" | "peripheral" | "passive"`. Brian deferred more granular options (`decoupling_cap`, `pull_up`, etc.); when telemetry shows callers wanting finer roles, expand.

5. **Pattern recognition fixes scope.** *Resolved.* The `MCP\d+` collision and RC-filter false-positive were fixed in PR #38 (2026-05-27) ahead of this SPEC. Broader coverage expansion (voltage refs, level shifters, motor drivers, etc.) remains its own future project — explicitly out of scope here.

## Testing strategy

### Unit tests (synthetic, no KiCad)

- **Layer 1 (topology)**: synthetic netlist fixtures with known cluster structure; verify Louvain finds them; verify bus-bridge detection on synthetic merge scenarios
- **Layer 2 (pattern recognition)**: verify the `MCP\d+` collision is fixed (TLV70 / MCP6002 / MCP23017 / MCP2200 all correctly classified, not misidentified as "voltage_sensor"); RC-filter detector excludes pull-ups (R to VCC, not R to signal source)
- **Layer 3 (resolved-part)**: monkeypatch `kicad_mcp.utils.lcsc_db.get_component` to return synthetic `ResolvedPart` rows (the shipped layer is JSONL-shard-backed SQLite, but Layer 3 tests should mock at the function boundary rather than building a real DB fixture); verify the description-keyword classifier maps known descriptions to expected labels; verify resolved-part dominates pattern_recognition when both present; verify clean degrade to Layer 2 when `get_component` returns `None`
- **Layer 4 (hints)**: caller hints override all lower layers; absent hints don't crash anything
- **Phase 2 (rank/BFS)**: synthetic netlist with known signal flow; verify tier assignment; verify cycle handling
- **Phase 3 (pack)**: barycentric ordering reduces crossings on synthetic test cases; convention rules apply correctly
- **PlacementState verbosity**: `minimal` and `full` differ in expected ways; sparse encoding omits default fields
- **Stateful/stateless modes**: state_id round-trips; cached state is loadable; cache cleanup works
- **Cluster ID stability**: re-running with `state=previous` preserves cluster_ids when membership unchanged; new clusters get new IDs
- **Iteration**: `redo_scope` recomputes only the named clusters; others preserved

### Boundary tests (per project CLAUDE.md threshold-testing rule)

- Confidence threshold: `confidence=0.5` (the default) — label retained; `0.499` — label becomes `"unclassified"`
- Cluster size boundary: 3 members (sparse warning); 4 members (no warning)
- Bus-bridge detection threshold: define exact edge count that triggers warning; test at boundary
- `column_pitch_mm` boundary: very small value (0.1mm) produces collisions; very large value (1000mm) is technically valid
- Empty schematic: `suggest` returns empty state, no errors
- Single-component schematic: places at origin, single-cluster, no warnings

### Integration tests (KiCad required, marked)

- Real schematics from `tests/fixtures/schematic_placement/`: run `suggest` → `apply` → verify no DRC violations; verify components are at expected approximate positions
- Round-trip: build a known schematic via tools, run placement, save, re-load, verify state survives

### Quality benchmark (the 70-85% measurement)

Not a unit test but an ongoing measurement:
- For each fixture, baseline: position-randomized layout (random within bounding box)
- For each fixture, candidate: `schematic_layout(operation="suggest")` first call output
- Score: human evaluator (Brian) rates each layout on a scale (e.g., 1-5) for readability and convention compliance
- Target: candidate scores ≥ 3.5 on 70-85% of fixtures
- Telemetry instruments the algorithm so we can identify which fixtures score below and why

## Hand-off summary

For an implementer picking this up cold:

1. Read this SPEC end-to-end.
2. Read the design-philosophy memories: `feedback_context_frugality.md`, `feedback_synchronous_at_call_boundary.md`, `feedback_prefer_packaging_over_vendoring.md`.
3. **Verify the prerequisite chain is implemented** (all expected to be ✅ as of 2026-05-28):
   - `mcp-events` package — published as `mcp-events>=0.1.0`, integrated via PR #39
   - Feedback Infrastructure — merged via [PR #39](https://github.com/blwfish/kicad-mcp/pull/39) (2026-05-27)
   - Component Intelligence — merged via [PR #40](https://github.com/blwfish/kicad-mcp/pull/40) (2026-05-28); module is `kicad_mcp.tools.lcsc`
4. **Confirm the ground-truth fixtures exist** at `tests/fixtures/schematic_placement/`. If not, this PR can begin but quality-benchmarking is blocked on Brian providing them.
5. Implement against the v1 scope. Defer everything in "Deferred from v1." Note that the previously-listed pre-flight fixes to `pattern_recognition.py` (MCP\d+ collision, RC-filter false-positive) were already shipped in PR #38 and are NOT part of this PR's scope.
6. The test suite is the acceptance criterion. Unit tests run in `uv run pytest` without KiCad. Integration tests require KiCad and are marked appropriately.
7. Update `MEMORY.md` and `project_schematic_placement.md` to mark this implemented when the PR merges; note any deviations.

Tool count after this PR: 15 → 16. Test count target: +60-80. Don't forget the four-file lockstep (`tests/test_server.py::test_current_tool_count` + `README.md` + `AGENT-INSTALL.md` + `TOOLS.md`) per `project_docs_count_check`.

Implementation order recap:

```
✅ OOB Events Subsystem        (mcp-events package implemented, published to PyPI)
✅ Feedback Infrastructure     (merged via PR #39, 2026-05-27)
✅ Component Intelligence      (merged via PR #40, 2026-05-28)
→  Schematic Auto-Placement    (this spec — capstone, ready to implement)
```
