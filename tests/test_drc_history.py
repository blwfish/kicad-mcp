"""Unit tests for drc_history.py -- pure file I/O, no KiCad/pcbnew needed.

Regression coverage for a CRITICAL data-capture finding: save_drc_result used
to persist only total_violations/violation_categories/violations, silently
dropping unconnected_items/schematic_parity/status/method/pcb_file from every
history entry -- a board with unrouted nets showed a misleadingly thin
history record even though the live DRC result itself had the full picture.
Also covers the HIGH finding that a corrupted history file was silently
reset via print() with no signal distinguishable from "first run".
"""
import json
import logging

import pytest

from kicad_mcp.utils import drc_history


@pytest.fixture(autouse=True)
def _isolated_history_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(drc_history, "DRC_HISTORY_DIR", str(tmp_path))


_FULL_DRC_RESULT = {
    "status": "ok",
    "method": "cli",
    "pcb_file": "/tmp/board.kicad_pcb",
    "total_violations": 5,
    "violation_categories": {"clearance": 3, "unconnected": 2},
    "violations": [{"type": "clearance"}, {"type": "clearance"}, {"type": "clearance"}],
    "unconnected_items": [{"code": "x"}, {"code": "y"}],
    "unconnected_count": 2,
    "schematic_parity": [],
    "parity_count": 0,
}


class TestSaveDrcResultCapturesAllFields:

    def test_persists_unconnected_and_parity_arrays(self):
        drc_history.save_drc_result("/proj/board.kicad_pro", _FULL_DRC_RESULT)
        [entry] = drc_history.get_drc_history("/proj/board.kicad_pro")
        assert entry["unconnected_items"] == [{"code": "x"}, {"code": "y"}]
        assert entry["unconnected_count"] == 2
        assert entry["schematic_parity"] == []
        assert entry["parity_count"] == 0

    def test_persists_status_method_pcb_file(self):
        drc_history.save_drc_result("/proj/board.kicad_pro", _FULL_DRC_RESULT)
        [entry] = drc_history.get_drc_history("/proj/board.kicad_pro")
        assert entry["status"] == "ok"
        assert entry["method"] == "cli"
        assert entry["pcb_file"] == "/tmp/board.kicad_pcb"

    def test_missing_optional_fields_default_safely(self):
        """A minimal DRC result (e.g. an older caller) must not crash the
        save -- missing fields default to empty/zero, not KeyError."""
        drc_history.save_drc_result("/proj/board.kicad_pro", {"total_violations": 0})
        [entry] = drc_history.get_drc_history("/proj/board.kicad_pro")
        assert entry["status"] is None
        assert entry["unconnected_items"] == []
        assert entry["unconnected_count"] == 0

    def test_round_trip_preserves_all_fields_across_reload(self):
        drc_history.save_drc_result("/proj/board.kicad_pro", _FULL_DRC_RESULT)
        # A second, independent read (fresh file load) must see the same data.
        [entry] = drc_history.get_drc_history("/proj/board.kicad_pro")
        for key in ("status", "method", "pcb_file", "unconnected_items",
                    "unconnected_count", "schematic_parity", "parity_count"):
            assert entry[key] == _FULL_DRC_RESULT[key if key != "unconnected_items" else "unconnected_items"]


class TestCorruptedHistoryFileIsLoud:

    def test_corrupted_history_logs_warning_not_print(self, caplog):
        history_path = drc_history.get_project_history_path("/proj/board.kicad_pro")
        drc_history.ensure_history_dir()
        with open(history_path, "w") as f:
            f.write("{not valid json")

        with caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.drc_history"):
            result = drc_history.save_drc_result("/proj/board.kicad_pro", _FULL_DRC_RESULT)
        assert result is None  # save_drc_result has no return value; call must not raise
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_corrupted_history_recovers_by_starting_fresh(self):
        history_path = drc_history.get_project_history_path("/proj/board.kicad_pro")
        drc_history.ensure_history_dir()
        with open(history_path, "w") as f:
            f.write("{not valid json")

        drc_history.save_drc_result("/proj/board.kicad_pro", _FULL_DRC_RESULT)
        entries = drc_history.get_drc_history("/proj/board.kicad_pro")
        assert len(entries) == 1
        assert entries[0]["total_violations"] == 5

    def test_get_drc_history_on_corrupted_file_logs_warning(self, caplog):
        history_path = drc_history.get_project_history_path("/proj/board.kicad_pro")
        drc_history.ensure_history_dir()
        with open(history_path, "w") as f:
            f.write("not json at all")

        with caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.drc_history"):
            result = drc_history.get_drc_history("/proj/board.kicad_pro")
        assert result == []
        assert any("corrupt" in r.message.lower() for r in caplog.records)


class TestHistoryTruncation:

    def test_keeps_only_last_10_entries(self):
        for i in range(12):
            drc_history.save_drc_result(
                "/proj/board.kicad_pro",
                {**_FULL_DRC_RESULT, "total_violations": i},
            )
        entries = drc_history.get_drc_history("/proj/board.kicad_pro")
        assert len(entries) == 10
        # newest-first, so the 2 oldest (total_violations 0, 1) were dropped
        assert {e["total_violations"] for e in entries} == set(range(2, 12))
