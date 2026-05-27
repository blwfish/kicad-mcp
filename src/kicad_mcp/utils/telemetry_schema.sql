-- Telemetry schema v1 — see docs/SPEC_Feedback_Infrastructure.md
-- Applied lazily on first DB connection per process.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calls (
    call_id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    schematic_hash TEXT NOT NULL,
    timestamp TEXT NOT NULL,

    -- Iteration tracking (within a schematic)
    iteration_index INTEGER NOT NULL,
    state_id TEXT,
    is_fresh_state INTEGER NOT NULL,

    -- Inputs (terse summary, not full payload)
    inputs_summary TEXT NOT NULL,
    output_summary TEXT,

    -- Performance
    elapsed_ms INTEGER NOT NULL,
    phase_breakdown_ms TEXT,

    -- Environment
    kicad_cli_version TEXT,
    netlist_schema_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_calls_schematic ON calls(schematic_hash, timestamp);
CREATE INDEX IF NOT EXISTS idx_calls_tool ON calls(tool_name, timestamp);

CREATE TABLE IF NOT EXISTS cluster_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(call_id),
    cluster_id TEXT NOT NULL,

    member_count INTEGER NOT NULL,
    anchor_ref TEXT,

    label TEXT,
    label_confidence REAL,
    label_source TEXT,

    tier INTEGER,
    louvain_modularity REAL
);

CREATE INDEX IF NOT EXISTS idx_cluster_call ON cluster_decisions(call_id);

CREATE TABLE IF NOT EXISTS warnings_emitted (
    warning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls(call_id),

    warning_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    affected_refs TEXT NOT NULL,
    affected_cluster_ids TEXT NOT NULL,

    -- Action attribution (synchronously updated)
    status TEXT NOT NULL DEFAULT 'pending',
    addressed_in_call_id INTEGER REFERENCES calls(call_id),
    addressed_how TEXT,
    addressed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_warnings_pending
    ON warnings_emitted(status, call_id)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_warnings_type
    ON warnings_emitted(warning_type, status);

CREATE TABLE IF NOT EXISTS system_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    severity TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    context TEXT,
    seen INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_system_events_unseen
    ON system_events(seen, timestamp);
CREATE INDEX IF NOT EXISTS idx_system_events_severity
    ON system_events(severity, timestamp);

CREATE TABLE IF NOT EXISTS sweep_state (
    -- Single-row table holding the timestamp of the last sweep_abandoned run.
    -- Used to throttle sweep frequency.
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_sweep_at TEXT
);
