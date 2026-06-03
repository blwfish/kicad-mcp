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


def _build_real_sch(tmp_path, components=()):
    """Build a real kicad-sch-api schematic on disk; return its path.

    Use this instead of a stub when testing the apply path, so unknown-
    kwarg silent-all bugs in kicad_sch_api (like the filter(reference=)
    issue that originally shipped) can't hide behind a hand-rolled stub.

    components: iterable of (ref, value, (x, y)) tuples.
    """
    import kicad_sch_api as ksa
    sch = ksa.create_schematic("x")
    for ref, value, pos in components:
        sch.components.add("Device:R", ref, value, position=pos)
    path = tmp_path / "x.kicad_sch"
    sch.save(str(path))
    return path


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
            "state_id": "aaaa1111bbbb2222",
            "schematic_path": str(sch_path),
            "schematic_hash": "",
            "components": {"R1": {"x_mm": 100.0, "y_mm": 100.0}},
            "clusters": {},
        })

        result = schematic_layout_fn(operation="apply", state_id="aaaa1111bbbb2222")
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
        # valid hex but never saved → state_not_found
        result = schematic_layout_fn(operation="apply", state_id="ffff0000ffff0000")
        assert result["status"] == "error"
        assert result["code"] == "state_not_found"

    def test_apply_drift_warning_when_hash_differs(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        """Use a real schematic on disk; populate the cache with a bogus
        hash so drift detection trips. Real ksa.load_schematic runs end-
        to-end so no stub can mask the apply path."""
        self._isolate_cache(tmp_path, monkeypatch)
        sch_path = _build_real_sch(tmp_path, [
            ("R1", "10k", (50, 50)),
        ])

        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "cccc3333dddd4444",
            "schematic_path": str(sch_path),
            "schematic_hash": "deadbeef" * 8,  # 64-char fake → trips drift
            "components": {},
            "clusters": {},
        })

        result = schematic_layout_fn(operation="apply", state_id="cccc3333dddd4444")
        assert result["status"] == "ok"
        codes = [e.get("code") for e in result.get("events", [])]
        assert "placement_state_stale" in codes

    def test_apply_handles_missing_refs_as_errors(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        """Real schematic with no U1; state targets U1; apply must report
        an error for that ref against the real ksa lookup, not via a
        stub-induced AttributeError.

        Because U1 is the ONLY target, applied==0 and every target failed —
        the save is a no-op, so apply must return status="error"
        (code="no_components_applied") rather than a misleading "ok" that the
        caller would trust as a successful placement. (Mirrors the
        _finalize_pad_assignment "requested work, did nothing → hard error"
        principle.)"""
        self._isolate_cache(tmp_path, monkeypatch)
        sch_path = _build_real_sch(tmp_path, [
            ("R1", "10k", (50, 50)),
        ])
        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "eeee5555ffff6666",
            "schematic_path": str(sch_path),
            "schematic_hash": "",  # disable drift
            "components": {"U1": {"x_mm": 10.0, "y_mm": 20.0}},
            "clusters": {},
        })

        result = schematic_layout_fn(operation="apply", state_id="eeee5555ffff6666")
        assert result["status"] == "error"
        assert result["code"] == "no_components_applied"
        assert result["applied"] == 0
        assert len(result["errors"]) == 1
        assert result["errors"][0]["ref"] == "U1"
        assert "not found" in result["errors"][0]["error"]


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


class TestApplyEarlyReturnsPreserveEvents:
    """Drift + wires_will_be_stale warnings are emitted before the load
    and save steps. If those steps fail, the early-return error dict
    must still carry the events envelope so the caller has context for
    why the schematic is in an inconsistent state."""

    def _isolate_cache(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "KICAD_MCP_PLACEMENT_CACHE_DIR", str(tmp_path / "cache"),
        )

    def test_load_failed_preserves_drift_warning(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        self._isolate_cache(tmp_path, monkeypatch)
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch v1)")
        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "9999aaaa1111bbbb",
            "schematic_path": str(sch),
            "schematic_hash": "deadbeef" * 8,  # forces drift warning
            "components": {},
            "clusters": {},
        })

        # Force ksa.load_schematic to raise → schematic_load_failed.
        def boom(_path):
            raise RuntimeError("corrupted file")
        monkeypatch.setattr("kicad_sch_api.load_schematic", boom)

        result = schematic_layout_fn(
            operation="apply", state_id="9999aaaa1111bbbb",
        )
        assert result["status"] == "error"
        assert result["code"] == "schematic_load_failed"
        codes = [e.get("code") for e in result.get("events", [])]
        assert "placement_state_stale" in codes, (
            "Drift warning was emitted before load failure but lost in the "
            "error response"
        )

    def test_save_failed_preserves_drift_warning(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        self._isolate_cache(tmp_path, monkeypatch)
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "8888cccc2222dddd",
            "schematic_path": str(sch),
            "schematic_hash": "deadbeef" * 8,
            "components": {},
            "clusters": {},
        })

        # Stub schematic that raises on save.
        class _StubSch:
            def __init__(self):
                self.components = self._Components()
            class _Components:
                def get(self, _ref):
                    return None
            def save(self):
                raise RuntimeError("read-only filesystem")
        monkeypatch.setattr(
            "kicad_sch_api.load_schematic", lambda _p: _StubSch(),
        )

        result = schematic_layout_fn(
            operation="apply", state_id="8888cccc2222dddd",
        )
        assert result["status"] == "error"
        assert result["code"] == "save_failed"
        codes = [e.get("code") for e in result.get("events", [])]
        assert "placement_state_stale" in codes


class TestSuggestSurfacesCacheSaveFailure:
    """When the cache write fails (disk full, read-only fs), the returned
    state_id is unusable. suggest must surface a cache_save_failed warning
    so the caller doesn't store the id and hit state_not_found later."""

    def test_warning_emitted_when_save_fails(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(
            "KICAD_MCP_PLACEMENT_CACHE_DIR", str(tmp_path / "cache"),
        )
        sch = tmp_path / "x.kicad_sch"
        sch.write_text("(kicad_sch)")
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.extract_netlist_via_cli",
            lambda _path: {"components": {}, "nets": {}},
        )
        # Force save_state to return None (simulating disk failure).
        monkeypatch.setattr(
            "kicad_mcp.tools.schematic_layout.placement_cache.save_state",
            lambda _state: None,
        )
        result = schematic_layout_fn(operation="suggest", schematic_path=str(sch))
        assert result["status"] == "ok"
        codes = [e.get("code") for e in result.get("events", [])]
        assert "cache_save_failed" in codes


class TestApplyMovesOnlyTargetedRefs:
    """Regression for the filter(reference=) silent-all bug: kicad_sch_api
    silently ignores unknown kwargs and returns ALL components, so the
    previous matches[0].position = (x, y) loop mutated whichever component
    happened to come first. This test uses a REAL schematic with multiple
    components and asserts only the targeted one moves."""

    def test_apply_moves_only_named_ref(
        self, schematic_layout_fn, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv(
            "KICAD_MCP_PLACEMENT_CACHE_DIR", str(tmp_path / "cache"),
        )
        import kicad_sch_api as ksa
        sch_obj = ksa.create_schematic("x")
        sch_obj.components.add("Device:R", "R1", "10k", position=(50, 50))
        sch_obj.components.add("Device:R", "R2", "20k", position=(60, 60))
        sch_obj.components.add("Device:C", "C1", "1nF", position=(70, 70))
        sch_path = tmp_path / "x.kicad_sch"
        sch_obj.save(str(sch_path))

        r2_pos_before = sch_obj.components.get("R2").position
        c1_pos_before = sch_obj.components.get("C1").position

        from kicad_mcp.utils.placement import cache as pc
        pc.save_state({
            "state_id": "00112233aabbccdd",
            "schematic_path": str(sch_path),
            "schematic_hash": "",  # disable drift check
            "components": {"R1": {"x_mm": 100.0, "y_mm": 100.0}},
            "clusters": {},
        })

        result = schematic_layout_fn(operation="apply", state_id="00112233aabbccdd")
        assert result["status"] == "ok"
        assert result["applied"] == 1
        assert result["errors"] == []

        # Reload from disk to confirm only R1 moved.
        sch_after = ksa.load_schematic(str(sch_path))
        r1_after = sch_after.components.get("R1").position
        r2_after = sch_after.components.get("R2").position
        c1_after = sch_after.components.get("C1").position
        # R1 moved to (or near) the requested (100, 100), grid-snapped.
        assert abs(r1_after.x - 100) < 2 and abs(r1_after.y - 100) < 2
        # R2 and C1 must NOT have moved — the bug used to overwrite them.
        assert (r2_after.x, r2_after.y) == (r2_pos_before.x, r2_pos_before.y)
        assert (c1_after.x, c1_after.y) == (c1_pos_before.x, c1_pos_before.y)
