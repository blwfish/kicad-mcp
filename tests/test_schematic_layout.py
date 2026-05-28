"""Router-level tests for the ``schematic_layout`` tool.

Slice 1 covers ``operation="suggest"`` only. Additional operations
(``apply``, ``clear_cache``) and full state shape land in later slices.
"""

from __future__ import annotations

import asyncio

import pytest

from kicad_mcp.server import create_server


@pytest.fixture
def schematic_layout_fn():
    server = create_server()
    tool = asyncio.run(server.get_tool("schematic_layout"))
    return tool.fn


class TestUnknownOperation:
    def test_returns_error_with_listed_valid_ops(self, schematic_layout_fn):
        result = schematic_layout_fn(operation="bogus")
        assert result["status"] == "error"
        assert result["code"] == "unknown_operation"
        assert "suggest" in result["message"]


class TestSuggestValidation:
    def test_missing_schematic_path_returns_error(self, schematic_layout_fn):
        result = schematic_layout_fn(operation="suggest")
        assert result["status"] == "error"
        assert result["code"] == "missing_parameter"

    def test_invalid_verbosity_returns_error(self, schematic_layout_fn, tmp_path):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        result = schematic_layout_fn(
            operation="suggest", schematic_path=str(sch), verbosity="extreme",
        )
        assert result["status"] == "error"
        assert result["code"] == "invalid_parameter"

    def test_missing_schematic_file_returns_error(self, schematic_layout_fn):
        result = schematic_layout_fn(
            operation="suggest", schematic_path="/nonexistent/path.kicad_sch",
        )
        assert result["status"] == "error"
        assert result["code"] == "schematic_not_found"


class TestStateShape:
    """Stub out netlist extraction to test the router's state-assembly path
    without requiring KiCad."""

    def test_minimal_verbosity_omits_label_source_and_bbox(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        fake_netlist = {
            "components": {"U1": {}, "R1": {}},
            "nets": {"SIG": [
                {"component": "U1", "pin": "1", "pintype": "output"},
                {"component": "R1", "pin": "1", "pintype": "passive"},
            ]},
        }
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: fake_netlist,
        )

        result = schematic_layout_fn(
            operation="suggest", schematic_path=str(sch), verbosity="minimal",
        )
        assert result["status"] == "ok"
        state = result["state"]
        # Both components landed in the same cluster.
        assert set(state["components"].keys()) == {"U1", "R1"}
        cluster_ids = {c["cluster_id"] for c in state["components"].values()}
        assert len(cluster_ids) == 1

        # Minimal mode: cluster dict has only the minimal fields.
        cluster = next(iter(state["clusters"].values()))
        assert "label_source" not in cluster
        assert "bbox_mm" not in cluster
        assert "label" in cluster
        assert "members" in cluster

    def test_full_verbosity_keeps_label_source_and_bbox(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        fake_netlist = {
            "components": {"U1": {}},
            "nets": {},
        }
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: fake_netlist,
        )
        result = schematic_layout_fn(
            operation="suggest", schematic_path=str(sch), verbosity="full",
        )
        assert result["status"] == "ok"
        cluster = next(iter(result["state"]["clusters"].values()))
        assert "label_source" in cluster
        assert "bbox_mm" in cluster

    def test_netlist_extraction_failure_returns_error(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: None,
        )
        result = schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        assert result["status"] == "error"
        assert result["code"] == "netlist_extraction_failed"

    def test_schematic_hash_populated(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: {"components": {}, "nets": {}},
        )
        result = schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        assert result["status"] == "ok"
        # SHA-256 hex digest is 64 chars.
        assert len(result["state"]["schematic_hash"]) == 64

    def test_state_id_is_returned(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: {"components": {}, "nets": {}},
        )
        result = schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        assert "state_id" in result
        assert len(result["state_id"]) == 16
