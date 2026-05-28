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


class TestWiresWillBeStaleDetection:
    """The _detect_stale_wires helper uses real kicad-sch-api to compute
    absolute pin positions and compare them against wire endpoints. These
    tests build small in-memory schematics rather than mocking, since the
    geometry transform is the load-bearing part."""

    def _build_sch_with_wire_on_pin(self):
        import kicad_sch_api as ksa
        sch = ksa.create_schematic("x")
        sch.components.add("Device:R", "R1", "10k", position=(50, 50))
        # Pin 1 of R1 at (50, 50) sits at component_y + 3.81 (KiCad inverts y).
        # Add a wire whose endpoint touches that pin.
        from kicad_mcp.tools.schematic_impl import _kicad_pin_position
        comp = sch.components.get("R1")
        pin_pos = _kicad_pin_position(comp, "1")
        sch.add_wire((pin_pos.x, pin_pos.y), (pin_pos.x + 10, pin_pos.y))
        return sch

    def test_no_wires_returns_empty(self, tmp_path):
        import kicad_sch_api as ksa
        sch = ksa.create_schematic("x")
        sch.components.add("Device:R", "R1", "10k", position=(50, 50))
        from kicad_mcp.tools.schematic_layout import _detect_stale_wires
        result = _detect_stale_wires(sch, {"R1": {"x_mm": 100, "y_mm": 100}})
        assert result == []

    def test_wire_on_moving_pin_is_stale(self):
        from kicad_mcp.tools.schematic_layout import _detect_stale_wires
        sch = self._build_sch_with_wire_on_pin()
        # Target is a new position → R1 will move → its wire goes stale.
        result = _detect_stale_wires(sch, {"R1": {"x_mm": 100, "y_mm": 100}})
        assert result == ["R1"]

    def test_wire_on_stationary_component_not_stale(self):
        from kicad_mcp.tools.schematic_layout import _detect_stale_wires
        sch = self._build_sch_with_wire_on_pin()
        # kicad-sch-api snaps positions to grid (~1.27 mm), so read back the
        # actual position rather than reusing the requested value.
        pos = sch.components.get("R1").position
        # Target equals current position → no move → no stale wire.
        result = _detect_stale_wires(sch, {"R1": {"x_mm": pos.x, "y_mm": pos.y}})
        assert result == []

    def test_wire_far_from_pins_not_stale(self):
        import kicad_sch_api as ksa
        sch = ksa.create_schematic("x")
        sch.components.add("Device:R", "R1", "10k", position=(50, 50))
        # Wire nowhere near R1's pins.
        sch.add_wire((200, 200), (220, 200))
        from kicad_mcp.tools.schematic_layout import _detect_stale_wires
        result = _detect_stale_wires(sch, {"R1": {"x_mm": 100, "y_mm": 100}})
        assert result == []

    def test_apply_emits_wires_will_be_stale_warning(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        """End-to-end: a cached state that moves R1 surfaces the warning."""
        monkeypatch.setenv(
            "KICAD_MCP_PLACEMENT_CACHE_DIR", str(tmp_path / "cache"),
        )
        # Build and save a real schematic with a wire on R1's pin.
        import kicad_sch_api as ksa
        sch_obj = ksa.create_schematic("x")
        sch_obj.components.add("Device:R", "R1", (50, 50))
        from kicad_mcp.tools.schematic_impl import _kicad_pin_position
        pin_pos = _kicad_pin_position(sch_obj.components.get("R1"), "1")
        sch_obj.add_wire((pin_pos.x, pin_pos.y), (pin_pos.x + 10, pin_pos.y))
        sch_path = tmp_path / "x.kicad_sch"
        sch_obj.save(str(sch_path))

        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "stale_check",
            "schematic_path": str(sch_path),
            "schematic_hash": "",
            "components": {"R1": {"x_mm": 100.0, "y_mm": 100.0}},
            "clusters": {},
        })

        result = schematic_layout_fn(operation="apply", state_id="stale_check")
        assert result["status"] == "ok"
        codes = [e.get("code") for e in result.get("events", [])]
        assert "wires_will_be_stale" in codes


class TestApplyAndClearCache:
    """Slice 5 — apply + clear_cache operations.

    These tests stub ``kicad_sch_api`` so they don't require a real
    schematic loaded; the goal is to verify the router's apply flow,
    drift detection, and cache management.
    """

    def _isolate_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "KICAD_MCP_PLACEMENT_CACHE_DIR", str(tmp_path / "cache"),
        )

    def test_clear_cache_with_no_path_returns_count(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        self._isolate_cache(tmp_path, monkeypatch)
        # Populate via two suggest calls (different schematics).
        sch1 = tmp_path / "a.kicad_sch"
        sch1.write_text("(kicad_sch)")
        sch2 = tmp_path / "b.kicad_sch"
        sch2.write_text("(kicad_sch)")
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: {"components": {}, "nets": {}},
        )
        schematic_layout_fn(operation="suggest", schematic_path=str(sch1))
        schematic_layout_fn(operation="suggest", schematic_path=str(sch2))
        result = schematic_layout_fn(operation="clear_cache")
        assert result["status"] == "ok"
        assert result["cleared_count"] >= 2

    def test_clear_cache_for_specific_path_only_clears_that_path(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        self._isolate_cache(tmp_path, monkeypatch)
        sch1 = tmp_path / "a.kicad_sch"
        sch1.write_text("(kicad_sch)")
        sch2 = tmp_path / "b.kicad_sch"
        sch2.write_text("(kicad_sch)")
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: {"components": {}, "nets": {}},
        )
        schematic_layout_fn(operation="suggest", schematic_path=str(sch1))
        schematic_layout_fn(operation="suggest", schematic_path=str(sch2))
        result = schematic_layout_fn(
            operation="clear_cache", schematic_path=str(sch1),
        )
        assert result["cleared_count"] == 1

    def test_apply_missing_both_state_id_and_path(self, schematic_layout_fn):
        result = schematic_layout_fn(operation="apply")
        assert result["status"] == "error"
        assert result["code"] == "missing_parameter"

    def test_apply_state_not_found(self, schematic_layout_fn, tmp_path, monkeypatch):
        self._isolate_cache(tmp_path, monkeypatch)
        result = schematic_layout_fn(operation="apply", state_id="nonexistent")
        assert result["status"] == "error"
        assert result["code"] == "state_not_found"

    def test_apply_drift_warning_when_hash_differs(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        """Stub kicad_sch_api; populate a cached state with a stale hash;
        verify the drift warning fires."""
        self._isolate_cache(tmp_path, monkeypatch)
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch v1)")

        # Build a cached state with a bogus hash so drift detection trips.
        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "drift_test",
            "schematic_path": str(sch),
            "schematic_hash": "deadbeef" * 8,  # 64-char fake
            "components": {},
            "clusters": {},
        })

        # Stub kicad_sch_api so apply doesn't actually need a real schematic.
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

        result = schematic_layout_fn(operation="apply", state_id="drift_test")
        assert result["status"] == "ok"
        # placement_state_stale was emitted; envelope is a list of dicts.
        events_envelope = result.get("events", [])
        codes = [e.get("code") for e in events_envelope]
        assert "placement_state_stale" in codes

    def test_apply_handles_missing_refs_as_errors(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        self._isolate_cache(tmp_path, monkeypatch)
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "missing_refs",
            "schematic_path": str(sch),
            "schematic_hash": "",  # disable drift check
            "components": {"U1": {"x_mm": 10.0, "y_mm": 20.0}},
            "clusters": {},
        })

        class _StubSch:
            def __init__(self):
                self.components = self._Filter()
            class _Filter:
                def filter(self, reference=None):
                    return []  # no components → all refs are errors
            def save(self):
                pass

        monkeypatch.setattr(
            "kicad_sch_api.load_schematic", lambda _path: _StubSch(),
        )
        result = schematic_layout_fn(operation="apply", state_id="missing_refs")
        assert result["status"] == "ok"
        assert result["applied"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["ref"] == "U1"


class TestLabelingIntegration:
    """Slice 2 — confirm Layers 2/3/4 land on cluster dicts in the state."""

    def test_pattern_recognition_label_lands_on_cluster(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        fake_netlist = {
            "components": {"U1": {"reference": "U1", "value": "ATMEGA328P"}},
            "nets": {},
        }
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: fake_netlist,
        )
        result = schematic_layout_fn(
            operation="suggest", schematic_path=str(sch), verbosity="full",
        )
        cluster = next(iter(result["state"]["clusters"].values()))
        assert cluster["label"] == "mcu"
        assert cluster["label_source"] == "pattern_recognition"
        assert cluster["anchor"] == "U1"

    def test_caller_hint_overrides_pattern(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        fake_netlist = {
            "components": {"U1": {"reference": "U1", "value": "ATMEGA328P"}},
            "nets": {},
        }
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: fake_netlist,
        )
        result = schematic_layout_fn(
            operation="suggest", schematic_path=str(sch),
            hints={"U1": "connector"}, verbosity="full",
        )
        cluster = next(iter(result["state"]["clusters"].values()))
        assert cluster["label"] == "connector"
        assert cluster["label_source"] == "caller_hint"
        assert cluster["label_confidence"] == 1.0
        assert result["state"]["inputs_honored"]["hints_applied"] == ["U1"]

    def test_lcsc_disabled_silently_when_no_db(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        """When LCSC isn't configured, Layer 3 must not raise."""
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        fake_netlist = {
            "components": {"U1": {"reference": "U1", "value": "ATMEGA328P",
                                  "properties": {"LCSC": "C12345"}}},
            "nets": {},
        }
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: fake_netlist,
        )
        # Force the lcsc_db.db_exists path to fail; Layer 3 should swallow.
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout._lcsc_lookup",
            lambda _pn: None,
        )
        result = schematic_layout_fn(
            operation="suggest", schematic_path=str(sch), verbosity="full",
        )
        assert result["status"] == "ok"
        cluster = next(iter(result["state"]["clusters"].values()))
        # Layer 3 declined → Layer 2 still wins.
        assert cluster["label"] == "mcu"
        assert cluster["label_source"] == "pattern_recognition"
