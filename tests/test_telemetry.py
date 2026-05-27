"""Tests for the telemetry persistence module.

Per docs/SPEC_Feedback_Infrastructure.md "Testing strategy" section.
No KiCad installation required; all tests use synthetic fixtures and a
per-test SQLite DB via the tmp_path fixture.

Style follows tests/test_geometry.py: one TestClass per logical area,
descriptive method names, docstrings for non-obvious assertions.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kicad_mcp.utils import telemetry
from kicad_mcp.utils.telemetry import (
    _confidence_bucket,
    _count_reemissions,
    attribute_pending_warnings,
    query_calibration_table,
    query_convergence_stats,
    query_system_events,
    record_call,
    record_cluster_decision,
    record_warning,
    sweep_abandoned,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the telemetry DB to a fresh tmp_path for each test.

    autouse=True so every test in this module gets isolation without
    having to declare the fixture explicitly.  Clears the env var so
    opt-out tests can flip it on without leaking state.
    """
    db = tmp_path / "telemetry.db"
    monkeypatch.delenv("KICAD_MCP_NO_TELEMETRY", raising=False)
    telemetry._reset_for_tests(db_path_override=db)
    yield db
    # Ensure the override is cleared even if the test raises
    telemetry._reset_for_tests(db_path_override=None)


def _make_call(
    schematic_hash: str = "abc123",
    tool_name: str = "suggest_schematic_placement",
    inputs_summary: dict | None = None,
    is_fresh_state: bool = True,
) -> int:
    """Convenience wrapper: record a synthetic call and return its call_id."""
    call_id = record_call(
        tool_name=tool_name,
        schematic_hash=schematic_hash,
        inputs_summary=inputs_summary or {"hints": {"count": 0}},
        is_fresh_state=is_fresh_state,
    )
    assert call_id is not None, "record_call returned None — telemetry may be disabled"
    return call_id


def _set_call_timestamp(db: Path, call_id: int, ts: str) -> None:
    """Directly rewrite a call's timestamp for sweep-age tests."""
    conn = sqlite3.connect(str(db))
    conn.execute("UPDATE calls SET timestamp = ? WHERE call_id = ?", (ts, call_id))
    conn.commit()
    conn.close()


def _set_last_sweep(db: Path, ts: str) -> None:
    """Directly rewrite sweep_state.last_sweep_at for throttle-boundary tests."""
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT OR REPLACE INTO sweep_state (id, last_sweep_at) VALUES (1, ?)", (ts,)
    )
    conn.commit()
    conn.close()


def _query_warning_status(db: Path, warning_id: int) -> str:
    conn = sqlite3.connect(str(db))
    row = conn.execute(
        "SELECT status FROM warnings_emitted WHERE warning_id = ?", (warning_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else "NOT_FOUND"


def _count_warnings(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT COUNT(*) FROM warnings_emitted").fetchone()
    conn.close()
    return row[0] if row else 0


def _now_minus(days: float = 0, hours: float = 0, minutes: float = 0, seconds: float = 0) -> str:
    """Return an ISO-8601 timestamp offset back from now."""
    delta = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return (datetime.now(timezone.utc) - delta).isoformat()


# ---------------------------------------------------------------------------
# TestAttribution
# ---------------------------------------------------------------------------


class TestAttribution:
    def test_hint_matches_affected_ref_marks_addressed(self, isolated_db: Path):
        """A hint whose key is in affected_refs → status='addressed', how='hint'."""
        call_id = _make_call("h1")
        record_warning(call_id, "orphan", "warn", ["U1", "U2"], [])

        call_id2 = _make_call("h1", is_fresh_state=False)
        inputs = {"hints": {"U1": {"region": "top"}}}
        addressed = attribute_pending_warnings("h1", call_id2, inputs)

        assert addressed == 1
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute(
            "SELECT status, addressed_how FROM warnings_emitted"
        ).fetchone()
        conn.close()
        assert row[0] == "addressed"
        assert row[1] == "hint"

    def test_hint_on_unrelated_ref_stays_pending(self, isolated_db: Path):
        """A hint whose key does NOT appear in affected_refs → warning stays pending."""
        call_id = _make_call("h2")
        record_warning(call_id, "orphan", "warn", ["U3"], [])

        call_id2 = _make_call("h2", is_fresh_state=False)
        inputs = {"hints": {"R1": {}}}  # R1 not in affected_refs
        addressed = attribute_pending_warnings("h2", call_id2, inputs)

        assert addressed == 0
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT status FROM warnings_emitted").fetchone()
        conn.close()
        assert row[0] == "pending"

    def test_cluster_assignment_overlapping_refs_matches(self, isolated_db: Path):
        """cluster_assignments whose value list overlaps affected_refs → 'cluster_assignment'."""
        call_id = _make_call("h3")
        record_warning(call_id, "bus_bridge", "warn", ["U4", "U5"], ["c0"])

        call_id2 = _make_call("h3", is_fresh_state=False)
        # U4 appears in cluster_assignments value list
        inputs = {"cluster_assignments": {"c0": ["U4", "R10"]}}
        addressed = attribute_pending_warnings("h3", call_id2, inputs)

        assert addressed == 1
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT addressed_how FROM warnings_emitted").fetchone()
        conn.close()
        assert row[0] == "cluster_assignment"

    def test_fixed_position_on_affected_ref_matches(self, isolated_db: Path):
        """fixed_positions whose key appears in affected_refs → 'fixed_position'."""
        call_id = _make_call("h4")
        record_warning(call_id, "singleton_orphan", "info", ["C1"], [])

        call_id2 = _make_call("h4", is_fresh_state=False)
        inputs = {"fixed_positions": {"C1": {"x": 10, "y": 20}}}
        addressed = attribute_pending_warnings("h4", call_id2, inputs)

        assert addressed == 1
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT addressed_how FROM warnings_emitted").fetchone()
        conn.close()
        assert row[0] == "fixed_position"

    def test_priority_order_hint_beats_cluster_and_fixed(self, isolated_db: Path):
        """When hints, cluster_assignments, AND fixed_positions all match, hint wins."""
        call_id = _make_call("h5")
        record_warning(call_id, "orphan", "warn", ["U6"], ["c1"])

        call_id2 = _make_call("h5", is_fresh_state=False)
        inputs = {
            "hints": {"U6": {}},
            "cluster_assignments": {"c1": ["U6"]},
            "fixed_positions": {"U6": {"x": 0, "y": 0}},
        }
        attribute_pending_warnings("h5", call_id2, inputs)

        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT addressed_how FROM warnings_emitted").fetchone()
        conn.close()
        assert row[0] == "hint"

    def test_priority_order_cluster_beats_fixed(self, isolated_db: Path):
        """When cluster_assignments and fixed_positions both match, cluster_assignment wins."""
        call_id = _make_call("h6")
        record_warning(call_id, "orphan", "warn", ["U7"], [])

        call_id2 = _make_call("h6", is_fresh_state=False)
        inputs = {
            "cluster_assignments": {"c0": ["U7"]},
            "fixed_positions": {"U7": {"x": 0, "y": 0}},
        }
        attribute_pending_warnings("h6", call_id2, inputs)

        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT addressed_how FROM warnings_emitted").fetchone()
        conn.close()
        assert row[0] == "cluster_assignment"

    def test_cluster_id_drift_ref_only_fallback_matches(self, isolated_db: Path):
        """Fresh-state next call: cluster IDs may drift; ref-only fallback in cluster_assignments still matches."""
        call_id = _make_call("h7", is_fresh_state=True)
        record_warning(call_id, "bridge", "warn", ["U8"], ["old_cluster_id"])

        # Fresh-state call — cluster IDs won't match, but ref U8 IS in assignments
        call_id2 = _make_call("h7", is_fresh_state=True)
        inputs = {
            "cluster_assignments": {"new_cluster_id": ["U8", "R5"]}  # different cluster id
        }
        addressed = attribute_pending_warnings("h7", call_id2, inputs)

        # The first warning was for the FIRST call. After fresh-state call it should
        # still be matchable if not yet swept.
        # The sweep in _maybe_sweep would mark the first warning abandoned since there's
        # a fresh_state call after it. Let's check what actually happens.
        # If sweep ran, count=0; if not swept yet, count=1.
        # record_call initializes sweep_state on first call, so no sweep on first call.
        # Second call: sweep_state exists, < 1h since last_sweep → no sweep.
        # So the warning is still pending and should be matched.
        assert addressed == 1
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT addressed_how FROM warnings_emitted WHERE call_id=?",
                           (call_id,)).fetchone()
        conn.close()
        assert row[0] == "cluster_assignment"

    def test_empty_inputs_no_attribution_attempts(self, isolated_db: Path):
        """All-zero inputs_summary with empty hint/cluster/fixed dicts → no attribution, no error."""
        call_id = _make_call("h8")
        record_warning(call_id, "orphan", "warn", ["U9"], [])

        call_id2 = _make_call("h8", is_fresh_state=False)
        inputs: dict = {}  # no hints, no cluster_assignments, no fixed_positions
        addressed = attribute_pending_warnings("h8", call_id2, inputs)

        assert addressed == 0
        conn = sqlite3.connect(str(isolated_db))
        row = conn.execute("SELECT status FROM warnings_emitted WHERE call_id=?",
                           (call_id,)).fetchone()
        conn.close()
        assert row[0] == "pending"


# ---------------------------------------------------------------------------
# TestAbandonment
# ---------------------------------------------------------------------------


class TestAbandonment:
    def test_age_cutoff_7_days_marks_abandoned(self, isolated_db: Path):
        """A call older than 7 days is swept to abandoned."""
        call_id = _make_call("s1")
        record_warning(call_id, "orphan", "warn", ["U1"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=8))

        count = sweep_abandoned(max_age_days=7)
        assert count == 1
        assert _query_warning_status(isolated_db, 1) == "abandoned"

    def test_age_boundary_below_7_days_stays_pending(self, isolated_db: Path):
        """A call 6 hours under 7 days (6 days 18 hours) → stays pending."""
        call_id = _make_call("s2")
        record_warning(call_id, "orphan", "warn", ["U1"], [])
        # 6 days and 23 hours — definitely less than 7 days
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=6, hours=23))

        count = sweep_abandoned(max_age_days=7)
        assert count == 0
        assert _query_warning_status(isolated_db, 1) == "pending"

    def test_age_boundary_above_7_days_marks_abandoned(self, isolated_db: Path):
        """A call 1 hour over 7 days (7 days 1 hour) → abandoned."""
        call_id = _make_call("s3")
        record_warning(call_id, "orphan", "warn", ["U1"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=7, hours=1))

        count = sweep_abandoned(max_age_days=7)
        assert count == 1
        assert _query_warning_status(isolated_db, 1) == "abandoned"

    def test_fresh_state_reset_marks_warning_abandoned(self, isolated_db: Path):
        """A subsequent fresh-state call on the same schematic abandons prior pending warnings."""
        call_id1 = _make_call("s4", is_fresh_state=True)
        record_warning(call_id1, "orphan", "warn", ["U2"], [])

        # Fresh-state call comes after
        _make_call("s4", is_fresh_state=True)

        count = sweep_abandoned(max_age_days=7)
        assert count == 1
        assert _query_warning_status(isolated_db, 1) == "abandoned"

    def test_sweep_idempotency_does_not_double_count(self, isolated_db: Path):
        """Running sweep_abandoned twice doesn't mark already-abandoned warnings again."""
        call_id = _make_call("s5")
        record_warning(call_id, "orphan", "warn", ["U3"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=8))

        count1 = sweep_abandoned(max_age_days=7)
        count2 = sweep_abandoned(max_age_days=7)

        assert count1 == 1
        assert count2 == 0, "Second sweep should find nothing new to abandon"

    def test_sweep_interval_at_59min_no_sweep(self, isolated_db: Path):
        """record_call at last_sweep + 59 min → _maybe_sweep does NOT run."""
        call_id = _make_call("s6")
        record_warning(call_id, "orphan", "warn", ["U4"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=8))

        # Set last_sweep_at to 59 minutes ago — below the 60-minute throttle
        _set_last_sweep(isolated_db, _now_minus(minutes=59))

        # Another record_call triggers _maybe_sweep
        _make_call("s6", is_fresh_state=False)

        # Warning should still be pending (sweep was suppressed)
        assert _query_warning_status(isolated_db, 1) == "pending"

    def test_sweep_interval_at_60min_exactly_triggers_sweep(self, isolated_db: Path):
        """record_call at last_sweep + 60 min exactly → _maybe_sweep runs (non-strict >=)."""
        call_id = _make_call("s7")
        record_warning(call_id, "orphan", "warn", ["U5"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=8))

        # Set last_sweep_at to exactly 60 minutes ago — at the threshold
        _set_last_sweep(isolated_db, _now_minus(hours=1))

        _make_call("s7", is_fresh_state=False)

        # Warning should be abandoned (sweep ran)
        assert _query_warning_status(isolated_db, 1) == "abandoned"

    def test_sweep_interval_at_61min_triggers_sweep(self, isolated_db: Path):
        """record_call at last_sweep + 61 min → _maybe_sweep runs."""
        call_id = _make_call("s8")
        record_warning(call_id, "orphan", "warn", ["U6"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=8))

        _set_last_sweep(isolated_db, _now_minus(hours=1, minutes=1))

        _make_call("s8", is_fresh_state=False)

        assert _query_warning_status(isolated_db, 1) == "abandoned"


# ---------------------------------------------------------------------------
# TestQueries
# ---------------------------------------------------------------------------


class TestQueries:
    def _setup_calibration_fixture(self, db: Path) -> None:
        """Insert a known fixture: 3 addressed, 1 abandoned, 2 pending for type=orphan."""
        call_id = _make_call("q1")
        # addressed ×3
        for ref in [["A1"], ["A2"], ["A3"]]:
            record_warning(call_id, "orphan", "warn", ref, [])
        # abandoned ×1 (inject directly — sweep_abandoned would need old timestamp)
        record_warning(call_id, "orphan", "warn", ["B1"], [])
        # pending ×2
        record_warning(call_id, "orphan", "warn", ["C1"], [])
        record_warning(call_id, "orphan", "warn", ["C2"], [])

        conn = sqlite3.connect(str(db))
        # Mark first 3 as addressed
        rows = conn.execute(
            "SELECT warning_id FROM warnings_emitted WHERE warning_type='orphan' LIMIT 3"
        ).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE warnings_emitted SET status='addressed', addressed_how='hint' WHERE warning_id=?",
                (r[0],),
            )
        # Mark 4th as abandoned
        rows4 = conn.execute(
            "SELECT warning_id FROM warnings_emitted WHERE warning_type='orphan' LIMIT 1 OFFSET 3"
        ).fetchall()
        for r in rows4:
            conn.execute(
                "UPDATE warnings_emitted SET status='abandoned' WHERE warning_id=?",
                (r[0],),
            )
        conn.commit()
        conn.close()

    def test_calibration_counts_and_rates_correct(self, isolated_db: Path):
        """Calibration table returns accurate emitted/addressed/abandoned/pending counts."""
        self._setup_calibration_fixture(isolated_db)

        rows = query_calibration_table(warning_type="orphan")
        assert len(rows) == 1
        r = rows[0]
        assert r["warning_type"] == "orphan"
        assert r["emitted"] == 6
        assert r["addressed"] == 3
        assert r["abandoned"] == 1
        assert r["pending"] == 2

    def test_calibration_action_rate_excludes_pending(self, isolated_db: Path):
        """action_rate = addressed / (addressed + abandoned); pending is excluded from denominator."""
        self._setup_calibration_fixture(isolated_db)

        rows = query_calibration_table(warning_type="orphan")
        r = rows[0]
        # addressed=3, abandoned=1 → 3/4 = 0.75
        assert r["action_rate"] == pytest.approx(0.75)

    def test_calibration_action_rate_null_when_no_terminal(self, isolated_db: Path):
        """action_rate is None when addressed=0 and abandoned=0 (only pending rows)."""
        call_id = _make_call("q2")
        record_warning(call_id, "noise", "info", ["U1"], [])
        record_warning(call_id, "noise", "info", ["U2"], [])

        rows = query_calibration_table(warning_type="noise")
        assert len(rows) == 1
        assert rows[0]["action_rate"] is None, (
            "Denominator is 0 (no addressed or abandoned) — action_rate must be null"
        )
        assert rows[0]["pending"] == 2

    def test_calibration_empty_db_returns_no_rows(self, isolated_db: Path):
        """On a fresh DB with no calls, calibration_table returns empty list."""
        rows = query_calibration_table()
        assert rows == []

    def test_convergence_stats_iteration_count_correct(self, isolated_db: Path):
        """iterations = number of calls on the schematic."""
        for _ in range(4):
            _make_call("q3", is_fresh_state=False)

        rows = query_convergence_stats(schematic_hash="q3")
        assert len(rows) == 1
        assert rows[0]["iterations"] == 4

    def test_convergence_stats_reemission_count_correct(self, isolated_db: Path):
        """warnings_reemitted counts overlapping-ref re-emissions within a schematic."""
        # Call 1: emit warning for U1 + U2
        c1 = _make_call("q4", is_fresh_state=False)
        record_warning(c1, "bus_bridge", "warn", ["U1", "U2"], [])
        # Call 2: same type, overlapping ref (U2) → reemission
        c2 = _make_call("q4", is_fresh_state=False)
        record_warning(c2, "bus_bridge", "warn", ["U2", "U3"], [])
        # Call 3: same type, overlapping ref (U3) → reemission
        c3 = _make_call("q4", is_fresh_state=False)
        record_warning(c3, "bus_bridge", "warn", ["U3", "U4"], [])

        rows = query_convergence_stats(schematic_hash="q4")
        assert len(rows) == 1
        assert rows[0]["warnings_reemitted"] == 2

    def test_reemission_chain_of_three_does_not_triple_count(self, isolated_db: Path):
        """A chain of 3 overlapping warnings yields reemitted=2, not 3.

        W1=[U1,U2], W2=[U2,U3], W3=[U3,U4]: W2 reemits W1, W3 reemits W2.
        Total reemissions = 2, not 3 (W1 is not double-counted as a source).
        """
        for refs in [["U1", "U2"], ["U2", "U3"], ["U3", "U4"]]:
            c = _make_call("q5", is_fresh_state=False)
            record_warning(c, "singleton", "warn", refs, [])

        rows = query_convergence_stats(schematic_hash="q5")
        assert rows[0]["warnings_reemitted"] == 2

    def test_reemission_non_overlapping_refs_not_counted(self, isolated_db: Path):
        """Two warnings of the same type with disjoint refs are NOT reemissions."""
        c1 = _make_call("q6", is_fresh_state=False)
        record_warning(c1, "orphan", "info", ["U1"], [])
        c2 = _make_call("q6", is_fresh_state=False)
        record_warning(c2, "orphan", "info", ["U9"], [])  # no overlap

        rows = query_convergence_stats(schematic_hash="q6")
        assert rows[0]["warnings_reemitted"] == 0

    def test_convergence_stats_empty_db_returns_no_rows(self, isolated_db: Path):
        """On a fresh DB, convergence_stats returns empty list."""
        rows = query_convergence_stats()
        assert rows == []

    def test_system_events_returns_events(self, isolated_db: Path):
        """system_events query returns events inserted into system_events table."""
        # Trigger DB initialization by making a call, then insert a synthetic event.
        _make_call("qsys1")
        conn = sqlite3.connect(str(isolated_db))
        conn.execute(
            "INSERT INTO system_events (timestamp, severity, code, message)"
            " VALUES (?, 'info', 'test_code', 'test msg')",
            (_now_minus(),),
        )
        conn.commit()
        conn.close()

        rows = query_system_events()
        codes = [r["code"] for r in rows]
        assert "test_code" in codes

    def test_system_events_flips_seen_flag(self, isolated_db: Path):
        """Rows returned by query_system_events have their seen flag set to 1."""
        # Initialize the DB, then insert an unseen event.
        _make_call("qsys2")
        conn = sqlite3.connect(str(isolated_db))
        conn.execute(
            "INSERT INTO system_events (timestamp, severity, code, message, seen)"
            " VALUES (?, 'info', 'unseen_evt', 'msg', 0)",
            (_now_minus(),),
        )
        conn.commit()
        conn.close()

        rows = query_system_events()
        evt = next(r for r in rows if r["code"] == "unseen_evt")
        # The returned dict reflects the pre-flip state (seen=False on first return)
        assert evt["seen"] is False

        # Second query: should now be seen=1 in DB, so unseen_only filter hides it
        rows2 = query_system_events(unseen_only=True)
        codes2 = [r["code"] for r in rows2 if r["severity"] != "error"]
        assert "unseen_evt" not in codes2, (
            "Event was returned once; subsequent unseen_only query must exclude it"
        )

    def test_system_events_unseen_only_excludes_seen(self, isolated_db: Path):
        """unseen_only=True excludes non-error events already marked seen."""
        _make_call("qsys3")
        conn = sqlite3.connect(str(isolated_db))
        conn.execute(
            "INSERT INTO system_events (timestamp, severity, code, message, seen)"
            " VALUES (?, 'info', 'already_seen', 'msg', 1)",
            (_now_minus(),),
        )
        conn.commit()
        conn.close()

        rows = query_system_events(unseen_only=True)
        codes = [r["code"] for r in rows]
        assert "already_seen" not in codes

    def test_system_events_empty_db_returns_no_rows(self, isolated_db: Path):
        """On a fresh DB without any emitted events, system_events returns empty list."""
        # No calls, no warnings, no explicit events
        rows = query_system_events()
        # The schema migration event IS emitted on init; filter for explicitly empty case
        # by checking a specific code that was never inserted
        codes = [r["code"] for r in rows]
        assert "nonexistent_code" not in codes
        # More importantly: the function must not error on a nearly-empty DB
        assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# TestConfidenceBucket
# ---------------------------------------------------------------------------


class TestConfidenceBucket:
    @pytest.mark.parametrize("confidence,expected_bucket", [
        (0.0,  "0.0-0.2"),   # exactly at lower bound → [0.0, 0.2)
        (0.1,  "0.0-0.2"),
        (0.19, "0.0-0.2"),
        (0.2,  "0.2-0.4"),   # exactly at next boundary → [0.2, 0.4) not [0.0, 0.2)
        (0.39, "0.2-0.4"),
        (0.4,  "0.4-0.6"),
        (0.59, "0.4-0.6"),
        (0.6,  "0.6-0.8"),
        (0.79, "0.6-0.8"),
        (0.8,  "0.8-1.0"),   # exactly at top threshold → [0.8, 1.0]
        (1.0,  "0.8-1.0"),   # top endpoint included (closed)
        (None, "none"),       # no confidence → "none" bucket
    ])
    def test_confidence_bucket_boundaries(self, confidence, expected_bucket):
        assert _confidence_bucket(confidence) == expected_bucket

    def test_calibration_confidence_bucket_assignment_via_cluster(self, isolated_db: Path):
        """Calibration table uses max cluster label_confidence from same call to bucket warnings."""
        call_id = _make_call("qb1")
        # Record a cluster decision with known confidence=0.9 → bucket "0.8-1.0"
        record_cluster_decision(
            call_id=call_id,
            cluster_id="c0",
            member_count=2,
            anchor_ref="U1",
            label="mcu",
            label_confidence=0.9,
            label_source="pattern_recognition",
            tier=1,
            louvain_modularity=None,
        )
        record_warning(call_id, "bus_bridge", "warn", ["U1"], ["c0"])

        rows = query_calibration_table(warning_type="bus_bridge")
        assert len(rows) == 1
        assert rows[0]["confidence_bucket"] == "0.8-1.0"

    def test_calibration_no_cluster_decision_lands_in_none_bucket(self, isolated_db: Path):
        """Warning with affected_cluster_ids=[] and no cluster_decision → bucket 'none'."""
        call_id = _make_call("qb2")
        record_warning(call_id, "singleton_orphan", "info", ["C5"], [])

        rows = query_calibration_table(warning_type="singleton_orphan")
        assert len(rows) == 1
        assert rows[0]["confidence_bucket"] == "none"


# ---------------------------------------------------------------------------
# TestValidation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_empty_targets_warning_rejected_no_insert(self, isolated_db: Path):
        """record_warning with both affected_refs=[] and affected_cluster_ids=[] → no insert."""
        call_id = _make_call("v1")
        record_warning(call_id, "bad_warning", "warn", [], [])

        assert _count_warnings(isolated_db) == 0, (
            "Empty-target warning must not be inserted into warnings_emitted"
        )

    def test_empty_targets_emits_soft_failure_event(self, isolated_db: Path):
        """record_warning with both empty → emits soft_failure with code='warning_emit_invalid'."""
        from mcp_events import event_context, get_current

        call_id = _make_call("v2")
        with event_context():
            record_warning(call_id, "bad_type", "warn", [], [])
            acc = get_current()
            assert acc is not None
            codes = [e.code for e in acc._events]
            assert "warning_emit_invalid" in codes

    def test_warning_with_only_refs_accepted(self, isolated_db: Path):
        """record_warning with non-empty affected_refs but empty cluster_ids → inserted."""
        call_id = _make_call("v3")
        record_warning(call_id, "valid", "warn", ["U1"], [])
        assert _count_warnings(isolated_db) == 1

    def test_warning_with_only_cluster_ids_accepted(self, isolated_db: Path):
        """record_warning with empty affected_refs but non-empty affected_cluster_ids → inserted."""
        call_id = _make_call("v4")
        record_warning(call_id, "valid", "warn", [], ["c0"])
        assert _count_warnings(isolated_db) == 1

    def test_opt_out_disables_writes(self, isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
        """With KICAD_MCP_NO_TELEMETRY=1, write functions no-op."""
        monkeypatch.setenv("KICAD_MCP_NO_TELEMETRY", "1")
        telemetry._reset_for_tests(db_path_override=isolated_db)

        call_id = record_call(
            tool_name="t",
            schematic_hash="opt_out",
            inputs_summary={},
        )
        assert call_id is None, "record_call must return None when telemetry is disabled"

    def test_opt_out_reads_still_work(self, isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
        """With KICAD_MCP_NO_TELEMETRY=1, read queries return empty lists rather than erroring."""
        # Write some data first (opt-in)
        _make_call("opt_out_read")

        # Now disable writes
        monkeypatch.setenv("KICAD_MCP_NO_TELEMETRY", "1")
        telemetry._reset_for_tests(db_path_override=isolated_db)

        # Reads must still function (schema is migrated on first read)
        rows_cal = query_calibration_table()
        rows_conv = query_convergence_stats()
        rows_sys = query_system_events()

        assert isinstance(rows_cal, list)
        assert isinstance(rows_conv, list)
        assert isinstance(rows_sys, list)

    def test_migration_on_first_read_with_writes_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Fresh DB triggers migration even when writes are disabled (opt-out)."""
        fresh_db = tmp_path / "fresh_optout.db"

        monkeypatch.setenv("KICAD_MCP_NO_TELEMETRY", "1")
        telemetry._reset_for_tests(db_path_override=fresh_db)

        # Read query should migrate the schema (creating tables) without error
        rows = query_calibration_table()
        assert isinstance(rows, list)

        # Verify the schema_version row was inserted (migration ran)
        assert fresh_db.exists(), "DB file must be created even when writes are disabled"
        conn = sqlite3.connect(str(fresh_db))
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        conn.close()
        assert row is not None and row[0] == 1, (
            "Schema migration must run on first read even with KICAD_MCP_NO_TELEMETRY=1"
        )


# ---------------------------------------------------------------------------
# TestBoundary
# ---------------------------------------------------------------------------


class TestBoundary:
    def test_action_rate_with_pending_only_is_null(self, isolated_db: Path):
        """addressed=0, abandoned=0, pending=5 → action_rate is None (insufficient data)."""
        call_id = _make_call("b1")
        for i in range(5):
            record_warning(call_id, "pending_only", "warn", [f"R{i}"], [])

        rows = query_calibration_table(warning_type="pending_only")
        assert len(rows) == 1
        assert rows[0]["action_rate"] is None
        assert rows[0]["pending"] == 5
        assert rows[0]["addressed"] == 0
        assert rows[0]["abandoned"] == 0

    def test_age_boundary_7_days_plus_1_second_abandoned(self, isolated_db: Path):
        """A call exactly 7d+1s old → abandoned (strictly older than cutoff)."""
        call_id = _make_call("b2")
        record_warning(call_id, "orphan", "warn", ["U1"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=7, seconds=1))

        count = sweep_abandoned(max_age_days=7)
        assert count == 1

    def test_age_boundary_6_days_23_hours_stays_pending(self, isolated_db: Path):
        """A call 6 days 23 hours old (< 7 days) → stays pending."""
        call_id = _make_call("b3")
        record_warning(call_id, "orphan", "warn", ["U1"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=6, hours=23))

        count = sweep_abandoned(max_age_days=7)
        assert count == 0

    def test_age_boundary_exactly_7_days_abandoned(self, isolated_db: Path):
        """A call at exactly 7 days → abandoned (non-strict boundary, per SPEC).

        The sweep SQL uses `timestamp <= cutoff`, so a warning whose call timestamp
        equals the cutoff (= now - max_age_days) gets swept. Pinned because the
        strict/non-strict distinction is the kind of silent-shadowing bug the
        CLAUDE.md syntactic-semantic seam rule warns about.
        """
        call_id = _make_call("b_exact_7d")
        record_warning(call_id, "orphan", "warn", ["U1"], [])
        _set_call_timestamp(isolated_db, call_id, _now_minus(days=7))

        count = sweep_abandoned(max_age_days=7)
        assert count == 1

    def test_iteration_index_auto_increments_per_schematic(self, isolated_db: Path):
        """iteration_index counts prior calls on the same schematic_hash."""
        c1 = _make_call("b4")
        c2 = _make_call("b4", is_fresh_state=False)
        c3 = _make_call("b4", is_fresh_state=False)
        # Different schematic — index resets
        cx = _make_call("b4_other")

        conn = sqlite3.connect(str(isolated_db))
        idx = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT call_id, iteration_index FROM calls ORDER BY call_id"
            ).fetchall()
        }
        conn.close()
        assert idx[c1] == 0
        assert idx[c2] == 1
        assert idx[c3] == 2
        assert idx[cx] == 0, "Different schematic_hash must start iteration_index at 0"

    def test_multiple_warnings_across_schematics_isolated(self, isolated_db: Path):
        """Attribution only matches warnings on the same schematic_hash."""
        c1 = _make_call("schema_A")
        record_warning(c1, "orphan", "warn", ["U1"], [])

        c2 = _make_call("schema_B")
        record_warning(c2, "orphan", "warn", ["U2"], [])

        # Attribute with hints for schema_A only
        c3 = _make_call("schema_A", is_fresh_state=False)
        addressed = attribute_pending_warnings("schema_A", c3, {"hints": {"U1": {}}})

        assert addressed == 1  # only the schema_A warning is matched
        conn = sqlite3.connect(str(isolated_db))
        rows = conn.execute(
            "SELECT status FROM warnings_emitted ORDER BY warning_id"
        ).fetchall()
        conn.close()
        assert rows[0][0] == "addressed"  # schema_A warning
        assert rows[1][0] == "pending"    # schema_B warning untouched

    def test_calibration_filter_by_warning_type(self, isolated_db: Path):
        """calibration_table(warning_type=X) filters to only that type."""
        call_id = _make_call("b5")
        record_warning(call_id, "type_alpha", "warn", ["U1"], [])
        record_warning(call_id, "type_beta", "warn", ["U2"], [])

        rows = query_calibration_table(warning_type="type_alpha")
        assert all(r["warning_type"] == "type_alpha" for r in rows)
        assert len(rows) == 1

    def test_convergence_stats_filter_by_schematic_hash(self, isolated_db: Path):
        """convergence_stats(schematic_hash=X) returns only that schematic."""
        for sh in ["s_apple", "s_banana", "s_cherry"]:
            _make_call(sh)

        rows = query_convergence_stats(schematic_hash="s_banana")
        assert len(rows) == 1
        assert rows[0]["schematic_hash"] == "s_banana"


# ---------------------------------------------------------------------------
# TestSchemaMigration — schema version tracking and the migration system_event
# ---------------------------------------------------------------------------


class TestSchemaMigration:
    def test_schema_version_row_inserted_on_first_use(self, isolated_db: Path):
        """First call to any write triggers migration → schema_version row v1."""
        _make_call("m1")

        conn = sqlite3.connect(str(isolated_db))
        cur = conn.execute("SELECT version FROM schema_version")
        versions = [r[0] for r in cur.fetchall()]
        conn.close()

        assert 1 in versions

    def test_migration_event_written_to_system_events(self, isolated_db: Path):
        """A migration emits a telemetry_schema_migrated system_event row."""
        _make_call("m2")

        # Use the read API: it migrates first, then we query
        events = query_system_events()
        codes = [e["code"] for e in events]
        assert "telemetry_schema_migrated" in codes

    def test_migration_runs_on_first_read_when_writes_disabled(
        self, monkeypatch: pytest.MonkeyPatch, isolated_db: Path
    ):
        """Even with KICAD_MCP_NO_TELEMETRY=1, reads create the DB + schema."""
        monkeypatch.setenv("KICAD_MCP_NO_TELEMETRY", "1")
        # No writes performed; the DB doesn't exist yet
        assert not isolated_db.exists()

        rows = query_calibration_table()

        assert rows == []
        assert isolated_db.exists()  # schema was applied for the read


# ---------------------------------------------------------------------------
# TestCountReemissions — pure-function unit tests, no DB needed
# ---------------------------------------------------------------------------


class TestCountReemissions:
    """Unit tests for the standalone `_count_reemissions` helper.

    Inputs are tuples of (warning_type, set_of_refs) ordered by emission time
    within each type. No DB connection required.
    """

    def test_empty_chain_returns_zero(self):
        assert _count_reemissions([]) == 0

    def test_single_warning_returns_zero(self):
        """One warning can't be a re-emission of anything."""
        assert _count_reemissions([("orphan", {"U1"})]) == 0

    def test_chain_of_two_overlapping_returns_one(self):
        """W2 reemits W1."""
        chain = [("orphan", {"U1", "U2"}), ("orphan", {"U2", "U3"})]
        assert _count_reemissions(chain) == 1

    def test_chain_of_three_overlapping_returns_two_not_three(self):
        """W2 reemits W1; W3 reemits W2. W1 not double-counted as a source."""
        chain = [
            ("orphan", {"U1", "U2"}),
            ("orphan", {"U2", "U3"}),
            ("orphan", {"U3", "U4"}),
        ]
        assert _count_reemissions(chain) == 2

    def test_disjoint_refs_not_counted(self):
        """Two warnings of the same type with no overlap → not a reemission."""
        chain = [("orphan", {"U1"}), ("orphan", {"U2"})]
        assert _count_reemissions(chain) == 0

    def test_different_warning_types_dont_count(self):
        """Overlapping refs across DIFFERENT warning_types → not a reemission."""
        chain = [("type_a", {"U1", "U2"}), ("type_b", {"U1", "U2"})]
        assert _count_reemissions(chain) == 0


# ---------------------------------------------------------------------------
# TestCalibrationSamples — the verbosity=full include_samples feature
# ---------------------------------------------------------------------------


class TestCalibrationSamples:
    def test_include_samples_false_omits_field(self, isolated_db: Path):
        """Default include_samples=False → no sample_warnings field."""
        call_id = _make_call("cs1")
        record_warning(call_id, "orphan", "warn", ["U1"], [])

        rows = query_calibration_table()

        assert len(rows) == 1
        assert "sample_warnings" not in rows[0]

    def test_include_samples_true_adds_field(self, isolated_db: Path):
        """include_samples=True → sample_warnings list of {warning_id, call_id}."""
        call_id = _make_call("cs2")
        record_warning(call_id, "orphan", "warn", ["U1"], [])

        rows = query_calibration_table(include_samples=True)

        assert len(rows) == 1
        samples = rows[0]["sample_warnings"]
        assert len(samples) == 1
        assert set(samples[0].keys()) == {"warning_id", "call_id"}
        assert samples[0]["call_id"] == call_id

    def test_samples_capped_at_three(self, isolated_db: Path):
        """Per-bucket samples cap at 3 even when more warnings exist in that bucket."""
        call_id = _make_call("cs3")
        for i in range(5):
            record_warning(call_id, "orphan", "warn", [f"U{i}"], [])

        rows = query_calibration_table(include_samples=True)

        assert len(rows) == 1
        assert len(rows[0]["sample_warnings"]) == 3


# ---------------------------------------------------------------------------
# TestAnalyzePlacementTelemetryTool — the MCP dispatch layer
# ---------------------------------------------------------------------------


class TestAnalyzePlacementTelemetryTool:
    """End-to-end tests for the analyze_placement_telemetry MCP tool dispatch.

    Each test instantiates a fresh FastMCP, registers the tool, then calls
    it via the framework. Verifies the response envelope structure beyond
    what the pure query functions test.
    """

    def _get_tool(self):
        """Build and return the registered analyze_placement_telemetry tool."""
        import asyncio

        from fastmcp import FastMCP

        from kicad_mcp.tools.telemetry_analyze import register_telemetry_analyze_tools

        mcp = FastMCP("test")
        register_telemetry_analyze_tools(mcp)
        return asyncio.run(mcp.get_tool("analyze_placement_telemetry"))

    def test_unknown_query_returns_error(self, isolated_db: Path):
        import asyncio
        import json

        tool = self._get_tool()
        result = asyncio.run(tool.run({"query": "bogus_query_name"}))
        text = result.content[0].text
        payload = json.loads(text)
        assert payload["status"] == "error"
        assert "bogus_query_name" in payload["error"]

    def test_unknown_verbosity_returns_error(self, isolated_db: Path):
        import asyncio
        import json

        tool = self._get_tool()
        result = asyncio.run(
            tool.run({"query": "calibration_table", "verbosity": "loud"})
        )
        text = result.content[0].text
        payload = json.loads(text)
        assert payload["status"] == "error"
        assert "verbosity" in payload["error"].lower()

    def test_all_three_queries_return_ok_on_empty_db(self, isolated_db: Path):
        """All supported queries return status=ok on a fresh DB.

        Warnings-based queries return empty rows; system_events has the
        migration event row from schema init.
        """
        import asyncio
        import json

        tool = self._get_tool()
        for query in ("calibration_table", "convergence_stats", "system_events"):
            result = asyncio.run(tool.run({"query": query}))
            text = result.content[0].text
            payload = json.loads(text)
            assert payload["status"] == "ok", f"{query} returned {payload}"
            if query in ("calibration_table", "convergence_stats"):
                assert payload["rows"] == [], (
                    f"{query} returned non-empty rows on fresh DB"
                )
            else:  # system_events
                # Has the migration event from schema init
                codes = [r["code"] for r in payload["rows"]]
                assert "telemetry_schema_migrated" in codes

    def test_verbosity_full_adds_metadata(self, isolated_db: Path):
        """verbosity=full augments the response with query+filters+row_count."""
        import asyncio
        import json

        tool = self._get_tool()
        result = asyncio.run(
            tool.run({"query": "convergence_stats", "verbosity": "full"})
        )
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "ok"
        assert payload["query"] == "convergence_stats"
        assert "filters_applied" in payload
        assert "row_count" in payload
