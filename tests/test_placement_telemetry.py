"""Telemetry integration tests for ``schematic_layout``.

Per ``docs/SPEC_Schematic_Placement.md`` § Telemetry integration:
  - every schematic_layout call → calls table row with proper tool_name
  - every cluster decision → cluster_decisions table row
  - every emitted warning → warnings_emitted row (with attribution chain)
  - output_summary carries cluster_count, tier_count,
    label_source_breakdown, low_confidence_cluster_count,
    warnings_emitted_count, lcsc_resolved_component_count, verbosity

Tests isolate telemetry via ``telemetry._reset_for_tests(tmp_path)`` so
they don't pollute the developer's local telemetry DB.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from kicad_mcp.server import create_server
from kicad_mcp.utils import telemetry


@pytest.fixture(autouse=True)
def isolated_telemetry(tmp_path, monkeypatch):
    db_path = tmp_path / "telemetry.db"
    telemetry._reset_for_tests(db_path)
    monkeypatch.setenv("KICAD_MCP_PLACEMENT_CACHE_DIR", str(tmp_path / "cache"))
    yield db_path
    telemetry._reset_for_tests(None)


@pytest.fixture
def schematic_layout_fn():
    server = create_server()
    tool = asyncio.run(server.get_tool("schematic_layout"))
    return tool.fn


@pytest.fixture
def stub_netlist(monkeypatch):
    """Stub kicad-cli netlist extraction with a small but realistic netlist."""
    fake = {
        "components": {
            "U1": {"reference": "U1", "value": "ATMEGA328P",
                   "properties": {"LCSC": "C12345"}},
            "Y1": {"reference": "Y1", "value": "16MHz",
                   "lib_id": "Device:Crystal"},
            "C1": {"reference": "C1", "value": "100nF"},
            "R1": {"reference": "R1", "value": "10k"},
        },
        "nets": {
            "SIG": [
                {"component": "U1", "pin": "1", "pintype": "output"},
                {"component": "R1", "pin": "1", "pintype": "passive"},
            ],
            "XTAL1": [
                {"component": "U1", "pin": "9", "pintype": "input"},
                {"component": "Y1", "pin": "1", "pintype": "passive"},
            ],
            "VCC": [
                {"component": "U1", "pin": "7", "pintype": "power_in"},
                {"component": "C1", "pin": "1", "pintype": "passive"},
            ],
        },
    }
    monkeypatch.setattr(
        "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
        lambda _path: fake,
    )
    return fake


def _query(db_path, sql, params=()):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


class TestSuggestTelemetry:
    def test_suggest_records_call_row(
        self, schematic_layout_fn, tmp_path, stub_netlist, isolated_telemetry,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        result = schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        assert result["status"] == "ok"

        rows = _query(isolated_telemetry, "SELECT * FROM calls")
        assert len(rows) == 1
        row = rows[0]
        assert row["tool_name"] == "schematic_layout.suggest"
        assert row["state_id"] == result["state_id"]
        out = json.loads(row["output_summary"])
        # Spec § output_summary fields.
        for k in (
            "cluster_count", "tier_count", "label_source_breakdown",
            "low_confidence_cluster_count", "warnings_emitted_count",
            "lcsc_resolved_component_count", "verbosity",
        ):
            assert k in out, f"missing output_summary key: {k}"
        assert out["verbosity"] == "minimal"

    def test_suggest_records_cluster_decisions(
        self, schematic_layout_fn, tmp_path, stub_netlist, isolated_telemetry,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        result = schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        assert result["status"] == "ok"

        cluster_rows = _query(isolated_telemetry, "SELECT * FROM cluster_decisions")
        assert len(cluster_rows) >= 1
        first = cluster_rows[0]
        # Spec § cluster_decisions schema fields populated.
        assert first["cluster_id"]
        assert first["member_count"] > 0
        assert first["label_source"] in (
            "topology_only", "pattern_recognition", "resolved_part", "caller_hint",
        )

    def test_lcsc_resolved_count_in_output_summary(
        self, schematic_layout_fn, tmp_path, stub_netlist, isolated_telemetry,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        row = _query(isolated_telemetry, "SELECT output_summary FROM calls")[0]
        out = json.loads(row["output_summary"])
        # U1 in the fixture has an LCSC property.
        assert out["lcsc_resolved_component_count"] == 1

    def test_fresh_state_flag_first_call_then_iteration(
        self, schematic_layout_fn, tmp_path, stub_netlist, isolated_telemetry,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        rows = _query(isolated_telemetry, "SELECT is_fresh_state, iteration_index FROM calls ORDER BY call_id")
        assert len(rows) == 2
        assert rows[0]["is_fresh_state"] == 1
        assert rows[1]["is_fresh_state"] == 0  # second call sees cached state
        assert rows[0]["iteration_index"] == 0
        assert rows[1]["iteration_index"] == 1

    def test_hint_addresses_prior_warning(
        self, schematic_layout_fn, tmp_path, monkeypatch, isolated_telemetry,
    ):
        """First call emits singleton_orphan for an isolated R1; second
        call hints R1 → its warning should attribute to the hint action."""
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        # One isolated component → singleton_orphan
        fake = {
            "components": {"R1": {"reference": "R1"}},
            "nets": {},
        }
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: fake,
        )
        schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        warning_rows = _query(
            isolated_telemetry,
            "SELECT * FROM warnings_emitted WHERE warning_type = 'singleton_orphan'",
        )
        assert len(warning_rows) == 1
        assert warning_rows[0]["status"] == "pending"

        # Second call with a hint targeting R1.
        schematic_layout_fn(
            operation="suggest", schematic_path=str(sch),
            hints={"R1": "connector"},
        )
        warning_rows_after = _query(
            isolated_telemetry,
            "SELECT * FROM warnings_emitted WHERE warning_type = 'singleton_orphan'",
        )
        # The first warning was addressed; a fresh one from the new call
        # may exist as 'pending'.
        addressed = [w for w in warning_rows_after if w["status"] == "addressed"]
        assert len(addressed) >= 1
        assert addressed[0]["addressed_how"] == "hint"


class TestApplyTelemetry:
    def test_apply_records_call_row(
        self, schematic_layout_fn, tmp_path, monkeypatch, isolated_telemetry,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")

        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "tele_apply",
            "schematic_path": str(sch),
            "schematic_hash": "",
            "components": {},
            "clusters": {},
        })

        class _StubSch:
            def __init__(self):
                self.components = self._Filter()
            class _Filter:
                def filter(self, reference=None):
                    return []
            def save(self):
                pass

        monkeypatch.setattr(
            "kicad_sch_api.load_schematic", lambda _path: _StubSch(),
        )
        schematic_layout_fn(operation="apply", state_id="tele_apply")
        rows = _query(
            isolated_telemetry,
            "SELECT * FROM calls WHERE tool_name = 'schematic_layout.apply'",
        )
        assert len(rows) == 1
        out = json.loads(rows[0]["output_summary"])
        assert "applied" in out
        assert "errors_count" in out


class TestClearCacheTelemetry:
    def test_clear_cache_records_call_row(
        self, schematic_layout_fn, isolated_telemetry,
    ):
        schematic_layout_fn(operation="clear_cache")
        rows = _query(
            isolated_telemetry,
            "SELECT * FROM calls WHERE tool_name = 'schematic_layout.clear_cache'",
        )
        assert len(rows) == 1
        out = json.loads(rows[0]["output_summary"])
        assert "cleared_count" in out
