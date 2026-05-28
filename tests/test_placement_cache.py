"""Cache layer tests for the schematic placement subsystem.

Per ``docs/SPEC_Schematic_Placement.md`` § Stateful vs stateless modes:
  - state_id round-trip (save → load returns same dict)
  - LRU eviction: per-schematic >5 states drops the oldest
  - 30-day expiry: stale files don't load and are cleaned up
  - clear_cache: targeted vs nuclear delete

Per ``CLAUDE.md`` Rule 2 (threshold-boundary):
  - exactly 5 states preserved; 6th evicts the oldest
  - expiry boundary: just-fresh vs just-stale
"""

from __future__ import annotations

import hashlib
import os
import time

import pytest

from kicad_mcp.utils.placement import cache as placement_cache


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    """Point the placement cache at a fresh tmp dir for every test."""
    monkeypatch.setenv(placement_cache.CACHE_DIR_ENV, str(tmp_path))
    yield tmp_path


def _hex_id(label: str) -> str:
    """Deterministic 16-hex-char state_id from a human-readable label, so
    individual tests can keep using semantic names like 'fresh' / 'stale'
    while still passing the cache's strict id validation."""
    return hashlib.sha256(label.encode()).hexdigest()[:16]


def _state(state_id: str, schematic_path: str = "/tmp/x.kicad_sch") -> dict:
    # Allow tests to pass either a raw 16-hex id or a semantic label.
    if not placement_cache._STATE_ID_RE.match(state_id):
        state_id = _hex_id(state_id)
    return {
        "state_id": state_id,
        "schematic_path": schematic_path,
        "schematic_hash": "abc",
        "components": {"U1": {"x_mm": 1.0, "y_mm": 2.0}},
        "clusters": {"c0": {"members": ["U1"]}},
    }


class TestSaveLoadRoundTrip:
    def test_save_then_load_returns_equivalent_dict(self):
        s = _state("aa11bb22cc33dd44")
        placement_cache.save_state(s)
        loaded = placement_cache.load_state("aa11bb22cc33dd44")
        assert loaded == s

    def test_load_missing_returns_none(self):
        assert placement_cache.load_state("doesnotexist") is None

    def test_save_state_without_id_raises(self):
        with pytest.raises(ValueError):
            placement_cache.save_state({})


class TestFindLatestForSchematic:
    def test_returns_most_recently_mtimed_state(self, isolated_cache_dir, monkeypatch):
        s1 = _state("id_old", "/tmp/x.kicad_sch")
        s2 = _state("id_new", "/tmp/x.kicad_sch")
        placement_cache.save_state(s1)
        # Force a different mtime on the second save.
        time.sleep(0.02)
        placement_cache.save_state(s2)
        latest = placement_cache.find_latest_for_schematic("/tmp/x.kicad_sch")
        assert latest is not None
        assert latest["state_id"] == _hex_id("id_new")

    def test_returns_none_for_no_matching_schematic(self):
        placement_cache.save_state(_state("id_a", "/tmp/a.kicad_sch"))
        assert placement_cache.find_latest_for_schematic("/tmp/other.kicad_sch") is None


class TestLruEviction:
    def test_at_max_no_eviction(self, isolated_cache_dir):
        for i in range(placement_cache.MAX_STATES_PER_SCHEMATIC):
            placement_cache.save_state(_state(f"id{i}", "/tmp/x.kicad_sch"))
        files = list(isolated_cache_dir.glob("*.json"))
        assert len(files) == placement_cache.MAX_STATES_PER_SCHEMATIC

    def test_above_max_evicts_oldest_by_mtime(self, isolated_cache_dir):
        for i in range(placement_cache.MAX_STATES_PER_SCHEMATIC + 2):
            placement_cache.save_state(_state(f"id{i}", "/tmp/x.kicad_sch"))
            time.sleep(0.01)  # stagger mtimes
        files = list(isolated_cache_dir.glob("*.json"))
        assert len(files) == placement_cache.MAX_STATES_PER_SCHEMATIC
        # id0 and id1 (the oldest two) should be gone.
        remaining_ids = {f.stem for f in files}
        assert _hex_id("id0") not in remaining_ids
        assert _hex_id("id1") not in remaining_ids

    def test_different_schematic_does_not_count_toward_eviction(self, isolated_cache_dir):
        # 5 states for schematic A — at-limit.
        for i in range(placement_cache.MAX_STATES_PER_SCHEMATIC):
            placement_cache.save_state(_state(f"a{i}", "/tmp/a.kicad_sch"))
        # 5 states for schematic B — at-limit.
        for i in range(placement_cache.MAX_STATES_PER_SCHEMATIC):
            placement_cache.save_state(_state(f"b{i}", "/tmp/b.kicad_sch"))
        files = list(isolated_cache_dir.glob("*.json"))
        # All 10 should still be there.
        assert len(files) == 2 * placement_cache.MAX_STATES_PER_SCHEMATIC


class TestExpiry:
    def test_fresh_state_loads(self):
        placement_cache.save_state(_state("fresh"))
        assert placement_cache.load_state(_hex_id("fresh")) is not None

    def test_stale_state_returns_none_and_is_deleted(self, isolated_cache_dir):
        placement_cache.save_state(_state("stale"))
        # Backdate the file by 31 days.
        stale_path = isolated_cache_dir / f"{_hex_id('stale')}.json"
        old_mtime = time.time() - 31 * 86400
        os.utime(stale_path, (old_mtime, old_mtime))
        assert placement_cache.load_state(_hex_id("stale")) is None
        # Cleanup-on-read also removed it.
        assert not stale_path.exists()

    def test_boundary_just_inside_expiry(self, isolated_cache_dir):
        placement_cache.save_state(_state("borderline"))
        # 29 days old — still fresh.
        path = isolated_cache_dir / f"{_hex_id('borderline')}.json"
        old_mtime = time.time() - 29 * 86400
        os.utime(path, (old_mtime, old_mtime))
        assert placement_cache.load_state(_hex_id("borderline")) is not None


class TestClearCache:
    def test_clear_all_with_no_path(self, isolated_cache_dir):
        placement_cache.save_state(_state("a", "/tmp/a.kicad_sch"))
        placement_cache.save_state(_state("b", "/tmp/b.kicad_sch"))
        count = placement_cache.clear_cache()
        assert count == 2
        assert list(isolated_cache_dir.glob("*.json")) == []

    def test_clear_only_specific_schematic(self, isolated_cache_dir):
        placement_cache.save_state(_state("a", "/tmp/a.kicad_sch"))
        placement_cache.save_state(_state("b", "/tmp/b.kicad_sch"))
        count = placement_cache.clear_cache("/tmp/a.kicad_sch")
        assert count == 1
        remaining = list(isolated_cache_dir.glob("*.json"))
        assert len(remaining) == 1
        assert remaining[0].stem == _hex_id("b")

    def test_clear_empty_cache_returns_zero(self):
        assert placement_cache.clear_cache() == 0


class TestStateIdValidation:
    """state_id is used to construct a filesystem path. Without validation,
    a caller-supplied id like '../../etc/passwd' or '/etc/hosts' would let
    Path concatenation escape the cache directory. These tests pin the
    security boundary in load_state and save_state."""

    @pytest.mark.parametrize("bad_id", [
        "../../etc/passwd",
        "/etc/hosts",
        "..",
        "subdir/aa11bb22cc33dd44",
        "aa11bb22cc33dd44/extra",
        "AA11BB22CC33DD44",          # uppercase hex disallowed
        "aa11bb22cc33dd4",           # 15 chars
        "aa11bb22cc33dd445",         # 17 chars
        "aa11bb22cc33dd4g",          # non-hex char
        "",
    ])
    def test_load_state_rejects_malformed_id(self, bad_id):
        # Should return None (treated as a miss), NOT read any file.
        assert placement_cache.load_state(bad_id) is None

    def test_load_state_does_not_read_absolute_path(self, isolated_cache_dir, tmp_path):
        # Plant a file the bad-id path would target if validation were absent.
        sneaky = tmp_path / "sneaky.json"
        sneaky.write_text('{"leaked": true}')
        # Try to read it via load_state with an absolute path id.
        result = placement_cache.load_state(str(sneaky.with_suffix("")))
        assert result is None  # rejected by validation, not leaked

    def test_save_state_rejects_malformed_id(self):
        with pytest.raises(ValueError, match="16 lowercase hex"):
            placement_cache.save_state({
                "state_id": "../../traversal",
                "schematic_path": "/tmp/x.kicad_sch",
            })


class TestCorruptCacheFile:
    def test_corrupt_json_returns_none_and_doesnt_crash(self, isolated_cache_dir):
        bogus_id = _hex_id("bogus")
        (isolated_cache_dir / f"{bogus_id}.json").write_text("not json {")
        assert placement_cache.load_state(bogus_id) is None

    def test_corrupt_files_are_skipped_in_find_latest(self, isolated_cache_dir):
        (isolated_cache_dir / f"{_hex_id('bogus')}.json").write_text("not json {")
        placement_cache.save_state(_state("good", "/tmp/x.kicad_sch"))
        latest = placement_cache.find_latest_for_schematic("/tmp/x.kicad_sch")
        assert latest is not None
        assert latest["state_id"] == _hex_id("good")
