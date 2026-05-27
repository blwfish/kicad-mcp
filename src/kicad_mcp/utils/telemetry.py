"""Telemetry persistence for kicad-mcp tools.

Per `docs/SPEC_Feedback_Infrastructure.md`. Action-attribution + calibration
measurement persisted to a local SQLite. Synchronous at call boundary; no
daemon; safe under multi-process concurrency via SQLite file locking.

Telemetry write failures are surfaced via `mcp_events.soft_failure` but never
propagated to the calling tool's main behavior.

Opt-out: set `KICAD_MCP_NO_TELEMETRY=1` to short-circuit all writes. Reads still
work; the analyze tool returns empty rows on a fresh disable-then-read sequence.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp_events import emit_event, soft_failure

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

_SCHEMA_VERSION = 1
_SCHEMA_SQL_PATH = Path(__file__).parent / "telemetry_schema.sql"
_SWEEP_THROTTLE_SECONDS = 3600  # 1 hour
_DEFAULT_MAX_AGE_DAYS = 7

# --------------------------------------------------------------------------
# Module-level state (paths and initialization tracking)
# --------------------------------------------------------------------------

_db_path_override: Path | None = None
_init_lock = threading.Lock()
_initialized_paths: set[Path] = set()


# --------------------------------------------------------------------------
# Opt-out and path resolution
# --------------------------------------------------------------------------


def _telemetry_disabled() -> bool:
    """Check env var for opt-out. Cheap; checked on each write."""
    return os.environ.get("KICAD_MCP_NO_TELEMETRY", "") == "1"


def _resolve_db_path() -> Path:
    """Resolve the telemetry DB path.

    Test override (via `_reset_for_tests`) takes precedence; then `XDG_CACHE_HOME`;
    then `~/.cache/kicad-mcp/telemetry.db`.
    """
    if _db_path_override is not None:
        return _db_path_override

    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "kicad-mcp" / "telemetry.db"


# --------------------------------------------------------------------------
# Connection management and schema migration
# --------------------------------------------------------------------------


def _connect_raw(db_path: Path) -> sqlite3.Connection:
    """Open a raw connection without ensuring initialization."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the schema SQL (idempotent via CREATE TABLE IF NOT EXISTS)."""
    schema_sql = _SCHEMA_SQL_PATH.read_text()
    conn.executescript(schema_sql)


def _migrate_if_needed(conn: sqlite3.Connection) -> None:
    """Apply migrations forward to `_SCHEMA_VERSION`. Idempotent."""
    cur = conn.execute("SELECT MAX(version) FROM schema_version")
    row = cur.fetchone()
    current = row[0] if row and row[0] is not None else 0

    if current >= _SCHEMA_VERSION:
        return

    # v0 → v1: initial schema; tables already created by _apply_schema.
    # Just record the version.
    if current == 0:
        now_iso = _now_iso()
        message = "Telemetry schema initialized at v1"
        context = {"from_version": 0, "to_version": 1}
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (1, now_iso),
        )
        # Persist to system_events directly (inside the init's open conn — calling
        # the fresh-conn persist helper here would deadlock on _init_lock).
        conn.execute(
            """INSERT INTO system_events (timestamp, severity, code, message, context)
               VALUES (?, ?, ?, ?, ?)""",
            (now_iso, "info", "telemetry_schema_migrated", message, json.dumps(context)),
        )
        conn.commit()
        emit_event("info", "telemetry_schema_migrated", message, context)
    # Future versions: add elif branches here as the schema evolves.


def _ensure_initialized(db_path: Path) -> bool:
    """Ensure the schema is applied for this path. Returns True on success."""
    if db_path in _initialized_paths:
        return True

    with _init_lock:
        if db_path in _initialized_paths:
            return True

        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = _connect_raw(db_path)
            try:
                _apply_schema(conn)
                _migrate_if_needed(conn)
            finally:
                conn.close()
            _initialized_paths.add(db_path)
            return True
        except Exception as e:
            soft_failure(
                "telemetry_init_failed",
                f"Failed to initialize telemetry DB at {db_path}: {e}",
                {"db_path": str(db_path)},
            )
            return False


def _connect() -> sqlite3.Connection | None:
    """Open a connection to the initialized DB. Returns None on failure or opt-out."""
    if _telemetry_disabled():
        return None
    db_path = _resolve_db_path()
    if not _ensure_initialized(db_path):
        return None
    try:
        return _connect_raw(db_path)
    except Exception as e:
        soft_failure(
            "telemetry_connect_failed",
            f"Failed to connect to telemetry DB: {e}",
            {"db_path": str(db_path)},
        )
        return None


def _connect_for_read() -> sqlite3.Connection | None:
    """Open a connection for read-only operations.

    Unlike `_connect`, this ignores the opt-out env var — reads still work
    even when writes are disabled. Schema is still migrated on first read.
    """
    db_path = _resolve_db_path()
    if not _ensure_initialized(db_path):
        return None
    try:
        return _connect_raw(db_path)
    except Exception as e:
        soft_failure(
            "telemetry_connect_failed",
            f"Failed to connect to telemetry DB for read: {e}",
            {"db_path": str(db_path)},
        )
        return None


# --------------------------------------------------------------------------
# Time helper
# --------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 timestamp in UTC."""
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Event persistence helper (system_events table)
# --------------------------------------------------------------------------


def _persist_event(
    severity: str, code: str, message: str, context: dict[str, Any] | None = None
) -> None:
    """Best-effort write of an event to the `system_events` table.

    Opens a fresh connection. Skipped when:
    - Telemetry is disabled (opt-out)
    - The DB isn't initialized yet (avoids recursion through `_ensure_initialized`)
    - Any exception (best-effort; never raises)

    Used by `_emit_soft_failure` to persist warn events. Migration and sweep
    events persist directly via their own open connections (no fresh-conn
    helper) to stay inside the existing init/sweep transaction.
    """
    if _telemetry_disabled():
        return
    db_path = _resolve_db_path()
    if db_path not in _initialized_paths:
        return
    try:
        conn = _connect_raw(db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO system_events
                       (timestamp, severity, code, message, context)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        _now_iso(),
                        severity,
                        code,
                        message,
                        json.dumps(context) if context else None,
                    ),
                )
        finally:
            conn.close()
    except Exception:
        pass


def _emit_soft_failure(
    code: str, message: str, context: dict[str, Any] | None = None
) -> None:
    """Emit a warn-severity event via both mcp_events AND `system_events` persistence.

    Use this for telemetry's own failures during normal operation (write paths,
    read paths). Do NOT use during init — `_ensure_initialized` should use
    plain `soft_failure` because the DB might not exist yet.
    """
    soft_failure(code, message, context)
    _persist_event("warn", code, message, context)


# --------------------------------------------------------------------------
# Public write API
# --------------------------------------------------------------------------


def record_call(
    tool_name: str,
    schematic_hash: str,
    inputs_summary: dict[str, Any],
    output_summary: dict[str, Any] | None = None,
    elapsed_ms: int = 0,
    phase_breakdown_ms: dict[str, int] | None = None,
    state_id: str | None = None,
    is_fresh_state: bool = True,
    kicad_cli_version: str | None = None,
    netlist_schema_hash: str | None = None,
) -> int | None:
    """Record one call. Returns the new `call_id`, or None on failure/opt-out.

    `iteration_index` is auto-computed by counting prior calls on this `schematic_hash`.
    Opportunistic sweep runs if the throttle interval has elapsed.
    """
    conn = _connect()
    if conn is None:
        return None

    try:
        with conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM calls WHERE schematic_hash = ?",
                (schematic_hash,),
            )
            row = cur.fetchone()
            iteration_index = row[0] if row else 0

            cur = conn.execute(
                """INSERT INTO calls
                   (tool_name, schematic_hash, timestamp, iteration_index,
                    state_id, is_fresh_state, inputs_summary, output_summary,
                    elapsed_ms, phase_breakdown_ms, kicad_cli_version,
                    netlist_schema_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tool_name,
                    schematic_hash,
                    _now_iso(),
                    iteration_index,
                    state_id,
                    1 if is_fresh_state else 0,
                    json.dumps(inputs_summary, sort_keys=True),
                    json.dumps(output_summary, sort_keys=True) if output_summary else None,
                    elapsed_ms,
                    json.dumps(phase_breakdown_ms, sort_keys=True)
                    if phase_breakdown_ms
                    else None,
                    kicad_cli_version,
                    netlist_schema_hash,
                ),
            )
            call_id = cur.lastrowid

            _maybe_sweep(conn)

            return call_id
    except Exception as e:
        _emit_soft_failure(
            "telemetry_write_failed",
            f"record_call failed: {e}",
            {"tool_name": tool_name, "schematic_hash": schematic_hash},
        )
        return None
    finally:
        conn.close()


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
    """Record a cluster decision for the given call."""
    conn = _connect()
    if conn is None:
        return

    try:
        with conn:
            conn.execute(
                """INSERT INTO cluster_decisions
                   (call_id, cluster_id, member_count, anchor_ref, label,
                    label_confidence, label_source, tier, louvain_modularity)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    call_id,
                    cluster_id,
                    member_count,
                    anchor_ref,
                    label,
                    label_confidence,
                    label_source,
                    tier,
                    louvain_modularity,
                ),
            )
    except Exception as e:
        _emit_soft_failure(
            "telemetry_write_failed",
            f"record_cluster_decision failed: {e}",
            {"call_id": call_id, "cluster_id": cluster_id},
        )
    finally:
        conn.close()


def record_warning(
    call_id: int,
    warning_type: str,
    severity: str,
    affected_refs: list[str],
    affected_cluster_ids: list[str],
) -> None:
    """Record a warning emitted during a call.

    Validation: at least one of `affected_refs` or `affected_cluster_ids` must
    be non-empty. A warning with no targets has no way to be matched against
    caller actions. If both are empty, the function emits a `soft_failure`
    event (code `warning_emit_invalid`) and skips the insert — catches bad
    emitter code at the source.
    """
    if not affected_refs and not affected_cluster_ids:
        _emit_soft_failure(
            "warning_emit_invalid",
            (
                f"record_warning rejected: no affected refs or clusters for "
                f"warning_type={warning_type!r}"
            ),
            {"call_id": call_id, "warning_type": warning_type},
        )
        return

    conn = _connect()
    if conn is None:
        return

    try:
        with conn:
            conn.execute(
                """INSERT INTO warnings_emitted
                   (call_id, warning_type, severity, affected_refs,
                    affected_cluster_ids, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (
                    call_id,
                    warning_type,
                    severity,
                    json.dumps(affected_refs),
                    json.dumps(affected_cluster_ids),
                ),
            )
    except Exception as e:
        _emit_soft_failure(
            "telemetry_write_failed",
            f"record_warning failed: {e}",
            {"call_id": call_id, "warning_type": warning_type},
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Action attribution
# --------------------------------------------------------------------------


def attribute_pending_warnings(
    schematic_hash: str,
    new_call_id: int,
    inputs: dict[str, Any],
) -> int:
    """Match pending warnings on this schematic to actions in the call's inputs.

    `inputs` is the RAW caller payload (the kwargs passed to suggest_placement),
    NOT the inputs_summary. The matching logic walks the actual structure of
    hints / cluster_assignments / fixed_positions to compare against
    affected_refs.

    Returns count of newly-addressed warnings. 0 if telemetry disabled or
    DB unavailable.
    """
    conn = _connect()
    if conn is None:
        return 0

    try:
        with conn:
            cur = conn.execute(
                """SELECT w.warning_id, w.affected_refs, w.affected_cluster_ids
                   FROM warnings_emitted w
                   JOIN calls c ON w.call_id = c.call_id
                   WHERE c.schematic_hash = ?
                     AND w.status = 'pending'""",
                (schematic_hash,),
            )
            pending = cur.fetchall()

            count = 0
            now = _now_iso()
            for row in pending:
                refs = set(json.loads(row["affected_refs"]))
                cluster_ids = set(json.loads(row["affected_cluster_ids"]))

                how = _match_attribution(refs, cluster_ids, inputs)
                if how is not None:
                    conn.execute(
                        """UPDATE warnings_emitted
                           SET status = 'addressed',
                               addressed_in_call_id = ?,
                               addressed_how = ?,
                               addressed_at = ?
                           WHERE warning_id = ?""",
                        (new_call_id, how, now, row["warning_id"]),
                    )
                    count += 1
            return count
    except Exception as e:
        _emit_soft_failure(
            "telemetry_write_failed",
            f"attribute_pending_warnings failed: {e}",
            {"schematic_hash": schematic_hash, "new_call_id": new_call_id},
        )
        return 0
    finally:
        conn.close()


def _match_attribution(
    affected_refs: set[str],
    affected_cluster_ids: set[str],
    inputs: dict[str, Any],
) -> str | None:
    """Return the `addressed_how` value if inputs address the warning, else None.

    First-match-wins in this priority order:
      1. hints (most direct semantic signal)
      2. cluster_assignments (structural override)
      3. fixed_positions (positional override)

    Note: cluster_ids match falls back to ref-membership because cluster_ids
    may drift across fresh-state calls.
    """
    hints = inputs.get("hints") or {}
    if isinstance(hints, dict) and any(ref in affected_refs for ref in hints):
        return "hint"

    cluster_assignments = inputs.get("cluster_assignments") or {}
    if isinstance(cluster_assignments, dict):
        assigned_refs: set[str] = set()
        for refs_list in cluster_assignments.values():
            if isinstance(refs_list, (list, tuple, set)):
                assigned_refs.update(refs_list)
        if affected_refs & assigned_refs:
            return "cluster_assignment"

    fixed_positions = inputs.get("fixed_positions") or {}
    if isinstance(fixed_positions, dict) and any(
        ref in affected_refs for ref in fixed_positions
    ):
        return "fixed_position"

    return None


# --------------------------------------------------------------------------
# Abandonment sweep
# --------------------------------------------------------------------------


def sweep_abandoned(max_age_days: int = _DEFAULT_MAX_AGE_DAYS) -> int:
    """Mark pending warnings older than `max_age_days` as 'abandoned'.

    Also abandons warnings whose schematic had a subsequent fresh-state call.
    Returns count of newly-abandoned warnings.
    """
    conn = _connect()
    if conn is None:
        return 0

    try:
        with conn:
            return _do_sweep(conn, max_age_days)
    except Exception as e:
        _emit_soft_failure(
            "telemetry_write_failed",
            f"sweep_abandoned failed: {e}",
            {},
        )
        return 0
    finally:
        conn.close()


def _do_sweep(conn: sqlite3.Connection, max_age_days: int) -> int:
    """Sweep implementation; runs inline on the given connection."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cutoff_iso = (now - timedelta(days=max_age_days)).isoformat()

    # Age cutoff: non-strict (warning at exactly max_age_days → abandoned,
    # matching the SPEC's boundary semantics).
    cur = conn.execute(
        """UPDATE warnings_emitted
           SET status = 'abandoned', addressed_at = ?
           WHERE status = 'pending'
             AND call_id IN (
               SELECT call_id FROM calls WHERE timestamp <= ?
             )""",
        (now_iso, cutoff_iso),
    )
    count_age = cur.rowcount

    # Fresh-state reset
    cur = conn.execute(
        """UPDATE warnings_emitted
           SET status = 'abandoned', addressed_at = ?
           WHERE status = 'pending'
             AND warning_id IN (
               SELECT w.warning_id
               FROM warnings_emitted w
               JOIN calls c_emit ON w.call_id = c_emit.call_id
               JOIN calls c_later ON c_later.schematic_hash = c_emit.schematic_hash
                                  AND c_later.timestamp > c_emit.timestamp
                                  AND c_later.is_fresh_state = 1
             )""",
        (now_iso,),
    )
    count_reset = cur.rowcount

    # Update sweep state
    conn.execute(
        """INSERT OR REPLACE INTO sweep_state (id, last_sweep_at)
           VALUES (1, ?)""",
        (now_iso,),
    )

    total = count_age + count_reset
    if total > 0:
        message = (
            f"Abandoned {total} pending warnings "
            f"({count_age} aged out, {count_reset} fresh-state reset)"
        )
        context = {"aged_out": count_age, "fresh_state_reset": count_reset}
        # Persist via the open conn (no fresh-conn helper here — same reason
        # as _migrate_if_needed: stay in the existing transaction).
        conn.execute(
            """INSERT INTO system_events (timestamp, severity, code, message, context)
               VALUES (?, ?, ?, ?, ?)""",
            (now_iso, "info", "telemetry_sweep_completed", message, json.dumps(context)),
        )
        emit_event("info", "telemetry_sweep_completed", message, context)
    return total


def _maybe_sweep(conn: sqlite3.Connection) -> None:
    """Opportunistic sweep called inline from `record_call`.

    Throttled by `_SWEEP_THROTTLE_SECONDS`. Runs in the same connection (no
    extra lock contention). Failures are swallowed — sweep is best-effort.
    """
    try:
        cur = conn.execute("SELECT last_sweep_at FROM sweep_state WHERE id = 1")
        row = cur.fetchone()
        now = datetime.now(timezone.utc)

        if row is None:
            # Initialize sweep_state; no warnings to sweep on first call.
            conn.execute(
                "INSERT INTO sweep_state (id, last_sweep_at) VALUES (1, ?)",
                (now.isoformat(),),
            )
            return

        last_iso = row[0]
        if not last_iso:
            return

        last = datetime.fromisoformat(last_iso)
        if (now - last).total_seconds() < _SWEEP_THROTTLE_SECONDS:
            return

        _do_sweep(conn, _DEFAULT_MAX_AGE_DAYS)
    except Exception:
        # Sweep is opportunistic; never propagate.
        pass


# --------------------------------------------------------------------------
# Public read API (used by analyze_placement_telemetry tool)
# --------------------------------------------------------------------------


def query_calibration_table(
    warning_type: str | None = None,
    since: str | None = None,
    include_samples: bool = False,
) -> list[dict[str, Any]]:
    """Return per (warning_type, confidence_bucket) action-rate rows.

    Buckets: [0.0, 0.2), [0.2, 0.4), [0.4, 0.6), [0.6, 0.8), [0.8, 1.0],
    plus "none" for warnings whose affected clusters had no confidence recorded.

    `action_rate = addressed / (addressed + abandoned)`; null if denominator is 0.

    When `include_samples=True`, each row gains a `sample_warnings` list of up
    to 3 `{"warning_id", "call_id"}` entries for drilling in. Used by the
    analyze tool when `verbosity="full"`.
    """
    conn = _connect_for_read()
    if conn is None:
        return []

    try:
        # Collect all warnings with their best-available confidence
        # (max label_confidence across affected_cluster_ids in the same call,
        # else "none").
        sql = """
        SELECT
          w.warning_id,
          w.warning_type,
          w.status,
          w.affected_cluster_ids,
          w.call_id,
          (SELECT MAX(cd.label_confidence)
             FROM cluster_decisions cd
             WHERE cd.call_id = w.call_id
               AND cd.cluster_id IN (
                 SELECT value FROM json_each(w.affected_cluster_ids)
               )) AS confidence
        FROM warnings_emitted w
        WHERE 1=1
        """
        params: list[Any] = []
        if warning_type:
            sql += " AND w.warning_type = ?"
            params.append(warning_type)
        if since:
            sql += " AND w.call_id IN (SELECT call_id FROM calls WHERE timestamp >= ?)"
            params.append(since)

        cur = conn.execute(sql, params)
        rows = cur.fetchall()

        # Bucket the warnings
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for r in rows:
            bucket = _confidence_bucket(r["confidence"])
            key = (r["warning_type"], bucket)
            if key not in buckets:
                buckets[key] = {
                    "emitted": 0,
                    "addressed": 0,
                    "abandoned": 0,
                    "pending": 0,
                    "samples": [],
                }
            buckets[key]["emitted"] += 1
            status = r["status"]
            if status in buckets[key]:
                buckets[key][status] += 1
            samples = buckets[key]["samples"]
            if len(samples) < 3:
                samples.append({"warning_id": r["warning_id"], "call_id": r["call_id"]})

        # Format output
        out: list[dict[str, Any]] = []
        for (wt, bucket), counts in sorted(buckets.items()):
            terminal = counts["addressed"] + counts["abandoned"]
            action_rate: float | None
            action_rate = (counts["addressed"] / terminal) if terminal > 0 else None
            row: dict[str, Any] = {
                "warning_type": wt,
                "confidence_bucket": bucket,
                "emitted": counts["emitted"],
                "addressed": counts["addressed"],
                "abandoned": counts["abandoned"],
                "pending": counts["pending"],
                "action_rate": action_rate,
            }
            if include_samples:
                row["sample_warnings"] = counts["samples"]
            out.append(row)
        return out
    except Exception as e:
        _emit_soft_failure(
            "telemetry_read_failed",
            f"query_calibration_table failed: {e}",
            {},
        )
        return []
    finally:
        conn.close()


def _confidence_bucket(confidence: float | None) -> str:
    """Map a confidence value to a bucket label."""
    if confidence is None:
        return "none"
    if confidence < 0.2:
        return "0.0-0.2"
    if confidence < 0.4:
        return "0.2-0.4"
    if confidence < 0.6:
        return "0.4-0.6"
    if confidence < 0.8:
        return "0.6-0.8"
    return "0.8-1.0"


def query_convergence_stats(
    schematic_hash: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Return per-schematic convergence stats: iterations, warnings, reemissions."""
    conn = _connect_for_read()
    if conn is None:
        return []

    try:
        sql = """
        SELECT
          schematic_hash,
          COUNT(*) AS iterations,
          MIN(timestamp) AS first_call_at,
          MAX(timestamp) AS last_call_at
        FROM calls
        WHERE 1=1
        """
        params: list[Any] = []
        if schematic_hash:
            sql += " AND schematic_hash = ?"
            params.append(schematic_hash)
        if since:
            sql += " AND timestamp >= ?"
            params.append(since)
        sql += " GROUP BY schematic_hash ORDER BY last_call_at DESC"

        cur = conn.execute(sql, params)
        schematics = cur.fetchall()

        out: list[dict[str, Any]] = []
        for s in schematics:
            sh = s["schematic_hash"]
            # Count emitted warnings on this schematic
            cur = conn.execute(
                """SELECT COUNT(*) FROM warnings_emitted w
                   JOIN calls c ON w.call_id = c.call_id
                   WHERE c.schematic_hash = ?""",
                (sh,),
            )
            row = cur.fetchone()
            warnings_emitted_count = row[0] if row else 0

            # Count reemissions: same warning_type appearing in multiple calls
            # with overlapping affected_refs.
            reemits = _count_reemissions_for_schematic(conn, sh)

            out.append(
                {
                    "schematic_hash": sh,
                    "iterations": s["iterations"],
                    "warnings_emitted": warnings_emitted_count,
                    "warnings_reemitted": reemits,
                    "first_call_at": s["first_call_at"],
                    "last_call_at": s["last_call_at"],
                }
            )
        return out
    except Exception as e:
        _emit_soft_failure(
            "telemetry_read_failed",
            f"query_convergence_stats failed: {e}",
            {},
        )
        return []
    finally:
        conn.close()


def _count_reemissions(warning_chains: Iterable[tuple[str, set[str]]]) -> int:
    """Count re-emissions from a chain of (warning_type, affected_refs) tuples.

    Pure function — no DB dependency, directly unit-testable. The input must
    be ordered by emission time within each `warning_type`. A warning is a
    re-emission if it shares ≥1 affected_ref with the most-recent earlier
    warning of the same type; each row counted at most once.

    For a chain of three overlapping warnings W1=[U1,U2], W2=[U2,U3], W3=[U3,U4]:
    W2 reemits W1, W3 reemits W2 → result is 2, not 3 (W1 is not double-counted).
    """
    count = 0
    last_refs_by_type: dict[str, set[str]] = {}
    for wt, refs in warning_chains:
        prev = last_refs_by_type.get(wt)
        if prev is not None and refs & prev:
            count += 1
        last_refs_by_type[wt] = refs
    return count


def _count_reemissions_for_schematic(conn: sqlite3.Connection, schematic_hash: str) -> int:
    """Fetch the warning chain for a schematic and count re-emissions.

    Thin DB wrapper around the pure `_count_reemissions` helper.
    """
    cur = conn.execute(
        """SELECT w.warning_type, w.affected_refs
           FROM warnings_emitted w
           JOIN calls c ON w.call_id = c.call_id
           WHERE c.schematic_hash = ?
           ORDER BY w.warning_type, c.timestamp""",
        (schematic_hash,),
    )
    chains = (
        (r["warning_type"], set(json.loads(r["affected_refs"]))) for r in cur.fetchall()
    )
    return _count_reemissions(chains)


def query_system_events(
    severity: str | None = None,
    unseen_only: bool = False,
    since: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return recent system events, flipping `seen=1` on returned rows.

    Errors are returned regardless of `seen` unless the caller explicitly filters.
    """
    conn = _connect_for_read()
    if conn is None:
        return []

    try:
        sql = "SELECT * FROM system_events WHERE 1=1"
        params: list[Any] = []
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if unseen_only:
            sql += " AND (seen = 0 OR severity = 'error')"
        if since:
            sql += " AND timestamp >= ?"
            params.append(since)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(int(limit))

        cur = conn.execute(sql, params)
        rows = cur.fetchall()

        out: list[dict[str, Any]] = []
        ids_to_mark: list[int] = []
        for r in rows:
            out.append(
                {
                    "event_id": r["event_id"],
                    "timestamp": r["timestamp"],
                    "severity": r["severity"],
                    "code": r["code"],
                    "message": r["message"],
                    "context": json.loads(r["context"]) if r["context"] else None,
                    "seen": bool(r["seen"]),
                }
            )
            if not r["seen"]:
                ids_to_mark.append(r["event_id"])

        # Flip seen=1 for newly-returned rows
        if ids_to_mark:
            try:
                with conn:
                    placeholders = ",".join("?" * len(ids_to_mark))
                    conn.execute(
                        f"UPDATE system_events SET seen = 1 WHERE event_id IN ({placeholders})",
                        ids_to_mark,
                    )
            except Exception:
                # Mark-as-seen is best-effort; don't fail the read
                pass

        return out
    except Exception as e:
        _emit_soft_failure(
            "telemetry_read_failed",
            f"query_system_events failed: {e}",
            {},
        )
        return []
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Test helpers (not part of public API surface, but importable for tests)
# --------------------------------------------------------------------------


def _reset_for_tests(db_path_override: Path | None = None) -> None:
    """Reset module state for clean test isolation.

    Pass a `db_path_override` (e.g. a tmp_path Path) to redirect the DB
    location. Clears the initialized-paths cache so the override takes effect.
    """
    global _db_path_override
    _db_path_override = db_path_override
    _initialized_paths.clear()
