# SPEC — Feedback Infrastructure

**Status:** Approved for implementation
**Author:** Brian + Claude
**Date:** 2026-05-27
**Target:** Local telemetry + action-attribution for kicad-mcp tools
**First consumer:** `suggest_schematic_placement` (in design; see `project_schematic_placement.md` in memory)

## Motivation

Two intertwined needs surfaced from the schematic-placement spec session:

1. **Calibration measurement.** When a tool labels a result with confidence C, what fraction of those labels actually hold up under caller scrutiny? When a warning fires, what fraction get acted on vs. ignored as noise? Without this, "calibration > coverage" is a design principle with no measurement loop, and we can't tell whether our confidence scores mean anything.

2. **Iteration debugging.** When a caller iterates against a tool, what's the convergence pattern? Which warnings keep re-firing? Which hints get repeated across calls (signal that the algorithm is being stubbornly wrong about the same thing)?

Together these answer the load-bearing question: *is our tool's signal honest?* For an MCP designed around an AI assistant as the consumer, an honestly-uncertain answer beats a confidently-wrong one. We need data to know which we're delivering.

## Non-goals

- **Not** a general-purpose observability platform. This is local-only SQLite for the maintainer; no network, no upload, no dashboards.
- **Not** in scope for v1: cross-tool telemetry beyond placement. The schema is designed to extend, but only placement writes in v1.
- **Not** a real-time monitoring system. Queries run on demand; freshness is whatever the last call wrote.
- **Not** storing schematic contents. We persist ref-level identifiers (component refs like "U1", net names like "VCC", cluster IDs), aggregate counts, and timing data. We do NOT store the schematic file, component values, or anything that would let an observer reconstruct the design. Schematic identity is captured by an opaque hash only.

## Dependencies

This SPEC depends on the **Out-of-Band Events Subsystem**, distributed as the `mcp-events` Python package (see [`docs/SPEC_OOB_Events.md`](SPEC_OOB_Events.md), 2026-05-27). The package lives at `/Volumes/Files/claude/mcp-events/` and is consumed via editable path-source in dev + `mcp-events>=0.1.0` in `pyproject.toml`. The package provides:

- The `mcp_events` Python module — `from mcp_events import event_context, soft_failure, emit_event, with_events`
- A `soft_failure(code, message, context=None)` function that records a `warn`-severity event without raising
- An `emit_event(severity, code, message, context=None)` function for general-purpose events at any severity
- An `events` field in tool response envelopes for surfacing accumulated events to the calling LLM

Telemetry persistence (the `system_events` table, defined below) is **kicad-mcp's local concern** — it's the durable-store side of the picture, for events that need to survive between tool calls. The shared module handles in-call accumulation; this SPEC handles persistence.

Telemetry uses the OOB subsystem for:

- **Failure surfacing**: telemetry write failures call `mcp_events.soft_failure("telemetry_write_failed", ...)`. The event flushes into the calling tool's `events` envelope and is also persisted to `system_events`. Failures do NOT propagate to the calling tool's main behavior.
- **Schema migration events**: applied migrations call `emit_event("info", "telemetry_schema_migrated", ...)`. Persisted (between-call event surfaces on next call's response).
- **Sweep events**: `sweep_abandoned` emits an info-severity event with a count summary. Also persisted.

**Implementation order**: OOB → telemetry → placement. This SPEC should not be implemented before the OOB subsystem.

If OOB ships with a different API surface than sketched above, this section gets updated to match.

## Design principles applied

- **Synchronous at call boundary.** Action attribution happens inline at `suggest_schematic_placement` entry, before computation. No daemon, no periodic backfill pass, no scheduling. See the `synchronous-at-call-boundary` feedback memory for rationale.
- **Context-frugal.** The write path is invisible to tool responses. The `analyze_placement_telemetry` read path defaults to terse summary output. Verbose mode is opt-in. See the `context-frugality` feedback memory.
- **Opt-out.** `KICAD_MCP_NO_TELEMETRY=1` disables writes entirely; reads still work on whatever's accumulated.

## Data model

SQLite at `~/.cache/kicad-mcp/telemetry.db`. Four primary tables + one schema-version table.

### Schema (v1)

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMP NOT NULL
);

CREATE TABLE calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,                -- e.g. 'suggest_schematic_placement'
    schematic_hash TEXT NOT NULL,           -- content hash of the schematic file at call time
    timestamp TIMESTAMP NOT NULL,

    -- Iteration tracking (within a schematic)
    iteration_index INTEGER NOT NULL,       -- N-th call on this schematic_hash
    state_id TEXT,                          -- for stateful mode; correlates to placement_state cache
    is_fresh_state BOOLEAN NOT NULL,        -- caller passed state=null AND no state_id

    -- Inputs (terse summary, not full payload)
    inputs_summary TEXT NOT NULL,           -- JSON: counts and key types of hints/constraints/scope
    output_summary TEXT,                    -- JSON: tool-specific; for placement: cluster_count, etc.

    -- Performance
    elapsed_ms INTEGER NOT NULL,
    phase_breakdown_ms TEXT,                -- JSON: {"netlist_export": 150, "cluster": 80, ...}

    -- Environment
    kicad_cli_version TEXT,                 -- e.g. '10.0.3'
    netlist_schema_hash TEXT                -- for external-interface-verification correlation
);

CREATE INDEX idx_calls_schematic ON calls(schematic_hash, timestamp);
CREATE INDEX idx_calls_tool ON calls(tool_name, timestamp);

CREATE TABLE cluster_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(call_id),
    cluster_id TEXT NOT NULL,               -- e.g. 'c0', 'c3'

    member_count INTEGER NOT NULL,
    anchor_ref TEXT,                        -- ref of the anchor component, or NULL

    label TEXT,                             -- e.g. 'mcu', 'ldo', 'unclassified'
    label_confidence REAL,                  -- 0.0 - 1.0
    label_source TEXT,                      -- 'pattern_recognition' | 'resolved_part' | 'caller_hint' | 'topology_only' | 'none'

    tier INTEGER,
    louvain_modularity REAL                 -- the modularity score for this cluster (if topology-based)
);

CREATE INDEX idx_cluster_call ON cluster_decisions(call_id);

CREATE TABLE warnings_emitted (
    warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(call_id),

    warning_type TEXT NOT NULL,             -- 'bus_bridge_suspected' | 'singleton_orphan' | ...
    severity TEXT NOT NULL,                 -- 'info' | 'warn' | 'error'
    affected_refs TEXT NOT NULL,            -- JSON array of refs
    affected_cluster_ids TEXT NOT NULL,     -- JSON array of cluster_ids

    -- Action attribution (synchronously updated)
    status TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'addressed' | 'abandoned'
    addressed_in_call_id INTEGER REFERENCES calls(call_id),
    addressed_how TEXT,                     -- 'hint' | 'cluster_assignment' | 'fixed_position' | NULL
    addressed_at TIMESTAMP
);

CREATE INDEX idx_warnings_pending ON warnings_emitted(status, call_id) WHERE status = 'pending';
CREATE INDEX idx_warnings_type ON warnings_emitted(warning_type, status);

CREATE TABLE system_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP NOT NULL,
    severity TEXT NOT NULL,                 -- 'info' | 'warn' | 'error'
    code TEXT NOT NULL,                     -- short identifier, e.g. 'telemetry_write_failed', 'telemetry_schema_migrated'
    message TEXT NOT NULL,                  -- human-readable
    context TEXT,                           -- JSON; relevant state at time of event
    seen INTEGER NOT NULL DEFAULT 0         -- flipped to 1 when returned by an analyze query
);

CREATE INDEX idx_system_events_unseen ON system_events(seen, timestamp);
CREATE INDEX idx_system_events_severity ON system_events(severity, timestamp);

CREATE TABLE sweep_state (
    -- Single-row table holding the timestamp of the last sweep_abandoned run.
    -- Used to throttle sweep frequency.
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_sweep_at TIMESTAMP
);
```

### Schema versioning and migration

`schema_version` starts at row `(1, <install_time>)`. Future migrations append rows and run forward-only DDL. No rollback path needed; this is local development data.

**When migrations run:** lazily, on first connection per process. The `telemetry` module checks `schema_version.MAX(version)` on its first DB operation. If less than the current code's expected version, it runs the missing migrations in order, then inserts a row recording each application.

**On migration:** emit an info-severity event via `emit_event("info", "telemetry_schema_migrated", ...)`. The event is persisted (will appear in the next call's `events` envelope if surfacing rules apply).

**On migration failure:** emit an error-severity event; subsequent telemetry writes are skipped for the lifetime of the process; the calling tool is unaffected. A future `kicad-mcp doctor` command (out of scope here) could surface this for repair.

## Write API

Module: `kicad_mcp/utils/telemetry.py`

```python
def record_call(
    tool_name: str,
    schematic_hash: str,
    inputs_summary: dict,
    output_summary: dict | None = None,
    elapsed_ms: int = 0,
    phase_breakdown_ms: dict | None = None,
    state_id: str | None = None,
    is_fresh_state: bool = True,
    kicad_cli_version: str | None = None,
    netlist_schema_hash: str | None = None,
) -> int:  # returns call_id
    """Insert one row into calls. Auto-computes iteration_index by counting prior
    calls on the same schematic_hash. Returns the new call_id."""

def record_cluster_decision(
    call_id: int,
    cluster_id: str,
    member_count: int,
    anchor_ref: str | None,
    label: str | None,
    label_confidence: float | None,
    label_source: str,
    tier: int | None,
    louvain_modularity: float | None,
) -> None:
    """Insert one row into cluster_decisions."""

def record_warning(
    call_id: int,
    warning_type: str,
    severity: str,
    affected_refs: list[str],
    affected_cluster_ids: list[str],
) -> None:
    """Insert one row into warnings_emitted with status='pending'.

    Validation: at least one of affected_refs or affected_cluster_ids must be
    non-empty. A warning with no targets has no way to be matched against
    caller actions. If both are empty, the function emits a soft_failure
    event (code='warning_emit_invalid') and skips the insert — this catches
    bad emitter code at source rather than letting a useless row accumulate."""

def attribute_pending_warnings(
    schematic_hash: str,
    new_call_id: int,
    inputs: dict,
) -> int:
    """For each pending warning on this schematic, scan the new call's inputs for
    a matching action. Update status to 'addressed' for matches. Returns count
    of newly-addressed warnings.

    `inputs` is the RAW caller payload (e.g., the kwargs dict passed to
    suggest_schematic_placement), NOT the inputs_summary stored in the calls
    table. The matching logic needs to walk the actual structure of hints /
    cluster_assignments / fixed_positions to compare against affected_refs.

    Called by the consumer tool BEFORE it does its main computation, so the
    new call_id is already known (record_call ran first)."""

def sweep_abandoned(max_age_days: int = 7) -> int:
    """Mark warnings older than max_age_days as 'abandoned'. Also sweeps
    warnings whose next call on the same schematic used fresh state.
    Returns count of newly-abandoned warnings.

    Called opportunistically — at the start of any record_call() invocation,
    if a configurable interval has passed since last sweep."""
```

All writes are wrapped in `if not _telemetry_disabled():`. The env var check happens once at module load.

### `inputs_summary` shape

The terse JSON summary, NOT the full input payload. For placement:

```json
{
  "hints": {"count": 3, "refs": ["U1", "U7", "C3"]},
  "cluster_assignments": {"count": 0},
  "fixed_positions": {"count": 1, "refs": ["U1"]},
  "merge_clusters": {"count": 0},
  "split_cluster": {"count": 0},
  "redo_scope": {"count": 2, "cluster_ids": ["c3", "c5"]}
}
```

Keeps storage small; preserves what attribution needs.

### `output_summary` shape (placement-specific)

```json
{
  "cluster_count": 7,
  "tier_count": 3,
  "label_source_breakdown": {
    "pattern_recognition": 3,
    "resolved_part": 1,
    "caller_hint": 2,
    "topology_only": 1
  },
  "low_confidence_cluster_count": 2,
  "warnings_emitted_count": 4
}
```

Other future tools can define their own `output_summary` shapes. The column is opaque JSON — no schema enforced at the DB level, by design.

### Call ordering (suggest_schematic_placement integration)

The integration sequence for a consumer tool:

1. **`record_call(...)`** — get the new `call_id` for this invocation.
2. **`attribute_pending_warnings(schematic_hash, call_id, inputs=raw_kwargs)`** — backfill any pending warnings that this call's inputs address. Must run BEFORE the main computation so that the algorithm doesn't process inputs whose attribution hasn't been recorded yet (subtle but matters for consistent ordering in the DB).
3. **(tool's main computation runs)** — clustering, ranking, packing, etc.
4. **`record_cluster_decision(...)`** — one call per cluster in the output state.
5. **`record_warning(...)`** — one call per warning emitted by the algorithm.

All five steps run synchronously within the tool's call. Total telemetry overhead: a handful of small INSERTs, ~1-2 ms on a typical SSD.

## Synchronous action attribution

The matching function used by `attribute_pending_warnings`.

For each pending warning W with `affected_refs=R` and `affected_cluster_ids=K`, scan the new call's inputs:

| Input present | Match condition | `addressed_how` |
|---|---|---|
| `hints={ref: ...}` | any ref ∈ R | `'hint'` |
| `cluster_assignments={cluster: [refs...]}` | any ref in assignments ∈ R | `'cluster_assignment'` |
| `fixed_positions={ref: ...}` | any ref ∈ R | `'fixed_position'` |

First match wins. If no match, leave `status='pending'`.

### Cluster ID drift handling

If the new call is fresh-state (`is_fresh_state=True`), cluster IDs may not correspond to previous IDs in K. Fall back to matching on `affected_refs` only — if any ref in R appears in any of the new call's `cluster_assignments` value lists, count as `cluster_assignment` regardless of cluster_id matching.

### Re-emission detection (v1: query-time only)

A warning of the same `warning_type` with overlapping `affected_refs` firing again in a later call is logically a re-emission. v1 does not store this relationship at write time; the analyze tool computes it at query time using this algorithm:

```
For a given schematic_hash, group warnings_emitted by warning_type.
Within each group, sort by call timestamp.
For each pair (W_i, W_j) where i < j:
  if set(W_i.affected_refs) ∩ set(W_j.affected_refs) is non-empty:
    count W_j as a re-emission of W_i.
A given W_j may be counted as the re-emission of at most one earlier W_i
(the most recent one with overlap). This avoids triple-counting in long
chains.
```

The `convergence_stats` query uses this to compute the `warnings_reemitted` field. Future v2 may add an explicit `reemits_warning_id` column with the relationship computed at write time, if query-time computation becomes expensive (unlikely at expected volumes — a few thousand rows per schematic).

## Abandonment rules

Pending warnings get marked `abandoned` when:

1. **Age cutoff.** 7 days since `timestamp` of the call that emitted the warning, with no resolution.
2. **Fresh-state reset.** A subsequent call on the same `schematic_hash` has `is_fresh_state=True`. Caller has effectively started over and lost the iteration thread.

Sweep runs opportunistically inside `record_call` — if more than 1 hour has elapsed since last sweep, run it before inserting. The "last sweep" timestamp is stored in a single-row `sweep_state` table.

## Read API

One new MCP tool: `analyze_placement_telemetry`.

```python
@mcp.tool()
def analyze_placement_telemetry(
    query: str,                             # see query types below
    filters: dict | None = None,            # query-specific filter dict
    verbosity: str = "minimal",             # "minimal" | "full"
) -> dict:
    """Read telemetry data. Always returns {"status": "ok", "rows": [...]} or
    {"error": "..."}. Verbose mode includes raw call_ids, sample warnings, etc."""
```

### v1 query types

**`"calibration_table"`** — per (warning_type, confidence bucket), the action rate.

**Confidence buckets** (5, equal width): `[0.0, 0.2)`, `[0.2, 0.4)`, `[0.4, 0.6)`, `[0.6, 0.8)`, `[0.8, 1.0]`. The top bucket is closed on both ends so `confidence=1.0` lands somewhere. Warnings with no associated confidence (most types) land in a special bucket `"none"`.

**Confidence source**: for each warning, the confidence is read from the most recent cluster_decision for any of its `affected_cluster_ids`. If none exists (warning fired without a cluster context), bucket is `"none"`.

**Action rate formula**: `addressed / (addressed + abandoned)`. Excludes `pending` so the metric stays stable as new warnings get attribution. If `addressed + abandoned == 0`, action_rate is `null` (insufficient data).

```json
{
  "rows": [
    {"warning_type": "bus_bridge_suspected", "confidence_bucket": "0.0-0.2",
     "emitted": 12, "addressed": 8, "abandoned": 1, "pending": 3, "action_rate": 0.89},
    {"warning_type": "bus_bridge_suspected", "confidence_bucket": "0.8-1.0",
     "emitted": 5, "addressed": 1, "abandoned": 0, "pending": 4, "action_rate": 1.0},
    {"warning_type": "singleton_orphan", "confidence_bucket": "none",
     "emitted": 23, "addressed": 2, "abandoned": 18, "pending": 3, "action_rate": 0.10},
    ...
  ]
}
```

Interpretation: a high action rate at high confidence = honest signal. A high action rate at low confidence = false alarms (algorithm is hedging when it shouldn't). A low action rate anywhere = noise candidate (the warning isn't useful).

**`"convergence_stats"`** — per schematic, iteration counts and repeat metrics.

```json
{
  "rows": [
    {"schematic_hash": "abc123", "iterations": 7,
     "warnings_emitted": 23, "warnings_reemitted": 5,
     "first_call_at": "2026-05-27T...", "last_call_at": "2026-05-27T..."},
    ...
  ]
}
```

`warnings_reemitted` is the count of warnings whose `warning_type` + `affected_refs` overlap repeated within the schematic — the algorithm fired, caller saw it, and it kept happening.

**`"system_events"`** — recent events from the `system_events` table for review.

```json
{
  "rows": [
    {"event_id": 47, "timestamp": "2026-05-27T14:32:11Z",
     "severity": "error", "code": "telemetry_write_failed",
     "message": "database is locked", "seen": false,
     "context": {"table": "warnings_emitted", "call_id": 102}},
    ...
  ]
}
```

Each event returned by this query has its `seen` flag flipped to `1`, so a follow-up `unseen_only=True` query naturally shows only what's new since last review. Errors are returned regardless of `seen` state unless `seen` is explicitly filtered.

### Filter dict (per query type)

For `calibration_table`: `{"warning_type": "...", "since": "ISO-8601"}` — both optional.

For `convergence_stats`: `{"schematic_hash": "...", "since": "ISO-8601"}` — both optional.

For `system_events`: `{"severity": "info" | "warn" | "error", "unseen_only": true, "since": "ISO-8601", "limit": int}` — all optional. Default `limit=100`.

### Empty-result behavior

All queries return `{"status": "ok", "rows": []}` when no data matches — never an error. A fresh install with no calls yet, or `KICAD_MCP_NO_TELEMETRY=1` flipped on at first use, returns empty rows from any query without complaint. The schema is migrated on first read even when writes are disabled, so the tables exist and queries don't fail.

### Verbosity

`"minimal"` (default): just the aggregated rows.
`"full"`: includes a `sample_warnings` array per row with up to 3 example warning_ids and their call_ids, for drilling in.

## Opt-out

`KICAD_MCP_NO_TELEMETRY=1` short-circuits all write paths (`record_*`, `attribute_pending_warnings`, `sweep_abandoned`) to no-ops. Reads still function. Schema migration still runs on first read (creates empty tables) so analyze tool doesn't error on a fresh disable-then-read sequence.

The env var is checked once at module load; toggling requires process restart. (Acceptable: kicad-mcp is a per-session subprocess.)

## v1 scope

Ship:
- Schema (four tables + version table + sweep_state table) + migration v1
- `record_call`, `record_cluster_decision`, `record_warning` write functions (with `record_warning` validation rejecting empty-target warnings)
- `attribute_pending_warnings` with `hints` / `cluster_assignments` / `fixed_positions` matching, including cluster-ID-drift fallback
- `sweep_abandoned` with the 7-day + fresh-state-reset rules, throttled by `sweep_state.last_sweep_at` to once per hour
- Lazy migration on first DB connection per process; migration events persisted to `system_events`
- `analyze_placement_telemetry` tool (standalone, not behind a router) with three queries: `calibration_table`, `convergence_stats`, `system_events`
- Integration with the OOB events subsystem for failure surfacing (write failures, schema migration outcomes, sweep summaries)
- `KICAD_MCP_NO_TELEMETRY` opt-out (writes skipped; reads still work)
- Tests using synthetic fixtures (no real `suggest_placement` needed)

**Prerequisite:** the `mcp-events` package exists at `/Volumes/Files/claude/mcp-events/` (per [`SPEC_OOB_Events.md`](SPEC_OOB_Events.md), implemented 2026-05-27, 47 tests passing). The telemetry PR adds it as a dependency:

```toml
# kicad-mcp/pyproject.toml additions
[project]
dependencies = [
    "mcp-events>=0.1.0",
    # ... existing deps ...
]

[tool.uv.sources]
mcp-events = { path = "/Volumes/Files/claude/mcp-events", editable = true }
```

**Important: PyPI publication blocker.** kicad-mcp's CI uses `uv sync --frozen`, which fails on local-path-only dependencies. The telemetry PR cannot merge until `mcp-events 0.1.0` is published to PyPI. The `[tool.uv.sources]` block above is for local dev; CI resolves `mcp-events>=0.1.0` from PyPI. Coordinate the PyPI publication of `mcp-events` BEFORE opening the telemetry PR.

Don't reinvent OOB inside the telemetry module — that would defeat the point of having it factored out.

## Deferred from v1

- `move_component` hook for surgical-fix attribution (`addressed_how='surgical_move'`)
- `merge_clusters` / `split_cluster` action attribution
- Explicit re-emission linking (`reemits_warning_id` column)
- Cross-tool telemetry beyond placement (other tools can adopt the same primitives later)
- Pre-computed aggregate caches (compute on demand for now; not a bottleneck at expected volumes)
- Scheduled-review pattern (à la the dep-audit-automation) for weekly telemetry summaries

## Testing strategy

Tests live in `tests/test_telemetry.py`. No KiCad installation needed; no `suggest_placement` needed.

### Synthetic fixtures

Use an in-memory SQLite (`sqlite3.connect(":memory:")`) with the schema migrated, then drive sequences of fake `record_call` + `record_warning` + `attribute_pending_warnings` to exercise the attribution logic.

### Test cases

- **Attribution: hint matches affected_ref** → warning becomes `addressed`, `addressed_how='hint'`
- **Attribution: hint matches unrelated ref** → warning stays `pending`
- **Attribution: cluster_assignment whose value list overlaps affected_refs** → `cluster_assignment` match
- **Attribution: fixed_position on affected ref** → `fixed_position` match
- **Attribution: multiple potential matches** → first match (in hints/cluster_assignments/fixed_positions order) wins
- **Cluster ID drift: fresh-state next call, ref still appears in cluster_assignments** → matches anyway via ref-only fallback
- **Abandonment: 7-day age cutoff** → warning → `abandoned`
- **Abandonment: fresh-state reset** → warning → `abandoned`
- **Sweep idempotency** → running sweep twice doesn't double-mark
- **Calibration table query** → counts and rates compute correctly for a known fixture; action_rate excludes pending; null when (addressed+abandoned)==0
- **Confidence buckets** → 0.0 lands in bucket [0.0, 0.2); 0.2 lands in [0.2, 0.4); 1.0 lands in [0.8, 1.0]; missing confidence lands in "none"
- **Convergence stats query** → iteration count and reemission count correct for a known fixture
- **Re-emission detection at query time** → same warning_type + overlapping refs in later call counted as reemission; chain of three doesn't triple-count
- **System events query** → returns events; flips `seen` flag on returned rows; `unseen_only=true` excludes already-seen
- **Empty-result behavior** → all queries return `rows: []` on fresh DB; never error
- **Migration on first read** → fresh DB triggers schema migration even when writes are disabled
- **Empty-targets warning rejection** → `record_warning` with both arrays empty emits a soft_failure and does not insert
- **Opt-out** → with `KICAD_MCP_NO_TELEMETRY=1`, write functions no-op; reads still work

### Boundary tests (per project CLAUDE.md threshold-testing rule)

- Abandonment age cutoff: warning at exactly 7 days → `abandoned`. 6.99 days → still `pending`. 7.01 days → `abandoned`.
- Sweep interval: `record_call` invoked at last_sweep + 59 min → no sweep. At + 60 min exactly → sweep runs (`>=` semantics). At + 61 min → sweep runs.
- Empty inputs (`inputs_summary` with all zero counts): no attribution attempts, no errors.
- Empty-target warning at emit: `record_warning` with both arrays empty → rejected; no row inserted; soft_failure event emitted.
- Confidence bucket edges: `confidence=0.2` lands in [0.2, 0.4) not [0.0, 0.2). `confidence=1.0` lands in [0.8, 1.0]. `confidence=None` lands in "none" bucket.
- Action rate formula: with addressed=0, abandoned=0, pending=5 → action_rate is `null` not `0` (insufficient data marker).

## Implementation notes

### Module structure
```
src/kicad_mcp/utils/
  telemetry.py            # write API, attribution, sweep
  telemetry_schema.sql    # DDL, applied on first import
src/kicad_mcp/tools/
  telemetry_analyze.py    # the analyze_placement_telemetry tool
tests/
  test_telemetry.py
```

### Database path

`~/.cache/kicad-mcp/telemetry.db` — same parent directory as `library_index.db`. Use `Path.home() / ".cache" / "kicad-mcp"` (with parents created if missing). Honor `XDG_CACHE_HOME` if set.

### Concurrency

Each kicad-mcp invocation is a per-session subprocess; the common case is single-writer. Multi-writer is possible if the user opens multiple Claude sessions in different terminals, each with kicad-mcp loaded. SQLite handles this with its default file locking — no extra coordination needed at this volume. Use WAL mode (`PRAGMA journal_mode=WAL`) for slightly better concurrent-read behavior; no other tuning required.

Connection per call (cheap; the file is on local SSD). Don't hold long-lived connections across tool boundaries.

### Tool registration

`analyze_placement_telemetry` is registered as a **standalone** `@mcp.tool()` — not behind a router. The router pattern (`pcb`, `schematic`, etc.) is for domain-specific operations on KiCad files; telemetry is meta-operational and belongs at the top level alongside `search`, `audit_all`, etc.

If kicad-mcp ever grows additional analyze tools (e.g., `analyze_external_interfaces` for the [external interface verification](../docs/) project), a future router might collapse them — but with one analyze tool today, standalone is right.

### Performance expectations

`attribute_pending_warnings` is `O(pending × inputs)`. At expected scale (≤100 pending warnings per schematic, ≤20 input items per call), this is microseconds. Don't optimize. If it becomes hot, add an index on `(schematic_hash, status)` — currently covered by the index on `(status, call_id)` plus the `calls.schematic_hash` join.

Read queries scan the relevant table fully (no pre-aggregation). At expected scale (≤100k rows), SQLite GROUP BY is sub-millisecond. Pre-aggregation is a complexity smell at this scale; revisit only if a real query crosses 100 ms.

### Tool-name vs router-operation handling

Today's only writer is the standalone `suggest_schematic_placement` tool, so `tool_name="suggest_schematic_placement"` is unambiguous. If future tools live behind routers (e.g., `pcb(operation="suggest_placement", ...)`), the convention is:

- `tool_name="pcb.suggest_placement"` — dotted form, router + operation
- No separate `operation` column. The dotted form keeps queries simple (`WHERE tool_name LIKE 'pcb.%'` is enough).

This is documented here rather than enforced at the schema level because no v1 consumer needs it. The first router-hosted writer is the one that has to commit to this convention.

### Hashes

### Hashes

- `schematic_hash`: SHA-256 of the schematic file's bytes at call time, truncated to 16 hex chars (64 bits, comfortably collision-free at expected volumes).
- `netlist_schema_hash`: SHA-256 of a sorted list of element/attribute names appearing in the kicad-cli netlist XML output, truncated to 8 hex chars. Used for correlation with the external-interface-verification project (see `project_external_interface_verification.md` in memory); if not computable, NULL is fine.

### Tool-count cost

Adds 1 tool (`analyze_placement_telemetry`). Current count is 13; this brings it to 14. The placement project will add `suggest_schematic_placement` and `apply_schematic_placement` separately. Stay well under the meaningful caps.

### Test count

Estimated 25-35 new tests. Current main: 651. After this PR: ~680-690.

## Open questions (resolved during spec session — documented for posterity)

- **Q**: One tool with `query=` discriminator vs. multiple analyze tools? **A**: One tool, dispatched by `query=` string. Keeps tool count down (context frugality) and the analysis surface is small enough that a single dispatch tool stays legible.
- **Q**: Pre-aggregate calibration metrics or compute on demand? **A**: Compute on demand. SQLite handles GROUP BY on a few thousand rows in microseconds; pre-aggregation is a complexity smell at this scale.
- **Q**: Daemon for backfill vs. inline at call boundary? **A**: Inline. See `synchronous-at-call-boundary` feedback memory.
- **Q**: Server-side state cache scope — same SPEC or separate? **A**: Separate — the cached `PlacementState` lives in the placement project's scope, not the telemetry project's. They reference each other via `state_id` but don't share storage. Keep them decoupled.

## Hand-off summary

For an implementer picking this up cold:

1. Read this SPEC end-to-end.
2. **Verify the `mcp-events` package exists** at `/Volumes/Files/claude/mcp-events/` (per [`SPEC_OOB_Events.md`](SPEC_OOB_Events.md), authored 2026-05-27 alongside this SPEC). If not, this PR blocks on that one — do NOT inline-reimplement OOB.
3. Add `mcp-events>=0.1.0` to `pyproject.toml` dependencies and the `[tool.uv.sources]` block pointing to the local path. Run `uv sync`.
4. Read the design-philosophy feedback memories: `feedback_context_frugality.md`, `feedback_synchronous_at_call_boundary.md`, `feedback_prefer_packaging_over_vendoring.md`.
5. Read `project_schematic_placement.md` for first-consumer context (placement itself is still in design, not implemented — your tests use synthetic fixtures, not real placement output).
6. Implement against the v1 scope above. Defer everything in "Deferred from v1."
7. The test suite is the acceptance criterion. All test cases in the Testing section must pass. Tests run in the existing `uv run pytest` invocation with no KiCad installation required.
8. Update `MEMORY.md` and `project_feedback_infrastructure.md` to mark this as **implemented** when the PR merges; note any deviations from spec.

The placement project will integrate against `record_call` / `record_cluster_decision` / `record_warning` / `attribute_pending_warnings` when it ships; for now those functions just need to exist with the right shapes.

Tool count after this PR: 13 → 14 (`analyze_placement_telemetry` added). Test count target: 651 → ~680-690.
