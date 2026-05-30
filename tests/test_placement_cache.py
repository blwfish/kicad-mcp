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

    def test_save_state_returns_none_when_write_fails(
        self, isolated_cache_dir, monkeypatch,
    ):
        """OSError on disk write must surface as a None return, not a
        ghost state_id that later load_state can't find."""
        # Make Path.write_text raise.
        def boom(self, *args, **kwargs):
            raise OSError("disk full")
        monkeypatch.setattr("pathlib.Path.write_text", boom)
        result = placement_cache.save_state(_state("aaaa1111bbbb2222"))
        assert result is None


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
        files = list(isolated_cache_dir.glob("*/*.json"))
        assert len(files) == placement_cache.MAX_STATES_PER_SCHEMATIC

    def test_above_max_evicts_oldest_by_mtime(self, isolated_cache_dir):
        for i in range(placement_cache.MAX_STATES_PER_SCHEMATIC + 2):
            placement_cache.save_state(_state(f"id{i}", "/tmp/x.kicad_sch"))
            time.sleep(0.01)  # stagger mtimes
        files = list(isolated_cache_dir.glob("*/*.json"))
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
        files = list(isolated_cache_dir.glob("*/*.json"))
        # All 10 should still be there.
        assert len(files) == 2 * placement_cache.MAX_STATES_PER_SCHEMATIC


class TestExpiry:
    def test_fresh_state_loads(self):
        placement_cache.save_state(_state("fresh"))
        assert placement_cache.load_state(_hex_id("fresh")) is not None

    def test_stale_state_returns_none_and_is_deleted(self, isolated_cache_dir):
        placement_cache.save_state(_state("stale"))
        # Sharded layout: locate the file via the shared shard helper.
        shard = placement_cache._shard_dir("/tmp/x.kicad_sch")
        stale_path = shard / f"{_hex_id('stale')}.json"
        # Backdate by 31 days.
        old_mtime = time.time() - 31 * 86400
        os.utime(stale_path, (old_mtime, old_mtime))
        assert placement_cache.load_state(_hex_id("stale")) is None
        assert not stale_path.exists()

    def test_boundary_just_inside_expiry(self, isolated_cache_dir):
        placement_cache.save_state(_state("borderline"))
        shard = placement_cache._shard_dir("/tmp/x.kicad_sch")
        path = shard / f"{_hex_id('borderline')}.json"
        old_mtime = time.time() - 29 * 86400  # still fresh
        os.utime(path, (old_mtime, old_mtime))
        assert placement_cache.load_state(_hex_id("borderline")) is not None


class TestClearCache:
    def test_clear_all_with_no_path(self, isolated_cache_dir):
        placement_cache.save_state(_state("a", "/tmp/a.kicad_sch"))
        placement_cache.save_state(_state("b", "/tmp/b.kicad_sch"))
        count = placement_cache.clear_cache()
        assert count == 2
        assert list(isolated_cache_dir.glob("*/*.json")) == []

    def test_clear_only_specific_schematic(self, isolated_cache_dir):
        placement_cache.save_state(_state("a", "/tmp/a.kicad_sch"))
        placement_cache.save_state(_state("b", "/tmp/b.kicad_sch"))
        count = placement_cache.clear_cache("/tmp/a.kicad_sch")
        assert count == 1
        remaining = list(isolated_cache_dir.glob("*/*.json"))
        assert len(remaining) == 1
        assert remaining[0].stem == _hex_id("b")
        # Confirm it landed in /tmp/b.kicad_sch's shard, not /tmp/a's.
        assert remaining[0].parent.name == placement_cache._schematic_shard("/tmp/b.kicad_sch")

    def test_clear_empty_cache_returns_zero(self):
        assert placement_cache.clear_cache() == 0


class TestShardIsolation:
    """The sharded layout's whole point: per-schematic operations must
    only scan their own shard. Pin that other shards aren't touched on
    typical operations."""

    def test_find_latest_only_scans_target_shard(self, isolated_cache_dir):
        """Populate two unrelated schematics, then assert find_latest for
        one of them only touches files in that schematic's shard. We can't
        easily measure scan count without instrumentation, but we CAN
        confirm the function returns the right schematic's state."""
        placement_cache.save_state(_state("a1", "/tmp/a.kicad_sch"))
        time.sleep(0.02)
        placement_cache.save_state(_state("b1", "/tmp/b.kicad_sch"))
        # /tmp/b's save happened later. Without shard isolation, a global
        # mtime-sorted scan would return b1 when asked about /tmp/a.
        latest_a = placement_cache.find_latest_for_schematic("/tmp/a.kicad_sch")
        assert latest_a is not None
        assert latest_a["state_id"] == _hex_id("a1")

    def test_save_lands_in_correct_shard_subdirectory(self, isolated_cache_dir):
        placement_cache.save_state(_state("solo", "/tmp/uniq.kicad_sch"))
        expected_shard = placement_cache._schematic_shard("/tmp/uniq.kicad_sch")
        files = list((isolated_cache_dir / expected_shard).glob("*.json"))
        assert len(files) == 1
        assert files[0].stem == _hex_id("solo")
        # No stray files at the cache root.
        root_files = [p for p in isolated_cache_dir.iterdir() if p.is_file()]
        assert root_files == []


class TestShardPathNormalization:
    """suggest stores str(Path(...)); apply may pass a non-normalized but
    equivalent path. The shard key must normalize both to the same value so
    apply finds the state suggest saved (m-cache-shard)."""

    @pytest.mark.parametrize("variant", [
        "/tmp/sub/x.kicad_sch",
        "/tmp/sub//x.kicad_sch",      # doubled slash
        "/tmp/./sub/x.kicad_sch",     # ./ segment
        "/tmp/sub/x.kicad_sch/",      # trailing slash
        "/tmp/sub/../sub/x.kicad_sch",  # only collapses if Path() does
    ])
    def test_equivalent_paths_share_shard(self, variant):
        canonical = "/tmp/sub/x.kicad_sch"
        # Mirror exactly what suggest stores; the shard must match for any
        # variant Path() considers equivalent.
        from pathlib import Path as _P
        if str(_P(variant)) != str(_P(canonical)):
            pytest.skip(f"{variant!r} is not Path-equivalent to canonical")
        assert (placement_cache._schematic_shard(variant)
                == placement_cache._schematic_shard(canonical))

    def test_apply_finds_state_saved_under_normalized_path(self, isolated_cache_dir):
        """End-to-end: save under the path suggest would store, look up via a
        non-normalized equivalent — must hit, not state_not_found."""
        placement_cache.save_state(_state("norm", "/tmp/deep/board.kicad_sch"))
        found = placement_cache.find_latest_for_schematic("/tmp/deep//board.kicad_sch")
        assert found is not None
        assert found["state_id"] == _hex_id("norm")

    def test_genuinely_different_paths_do_not_collide(self, isolated_cache_dir):
        a = placement_cache._schematic_shard("/tmp/a.kicad_sch")
        b = placement_cache._schematic_shard("/tmp/b.kicad_sch")
        assert a != b

    def test_empty_path_is_unknown_shard(self):
        assert placement_cache._schematic_shard("") == "_unknown"


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
        shard = placement_cache._shard_dir("/tmp/x.kicad_sch")
        shard.mkdir(parents=True, exist_ok=True)
        (shard / f"{bogus_id}.json").write_text("not json {")
        assert placement_cache.load_state(bogus_id) is None

    def test_corrupt_files_are_skipped_in_find_latest(self, isolated_cache_dir):
        # Put a corrupt file in the same shard as the good one — the
        # corrupt one is newer (mtime), so find_latest must skip it and
        # fall back to the readable file.
        placement_cache.save_state(_state("good", "/tmp/x.kicad_sch"))
        shard = placement_cache._shard_dir("/tmp/x.kicad_sch")
        time.sleep(0.02)
        (shard / f"{_hex_id('bogus')}.json").write_text("not json {")
        latest = placement_cache.find_latest_for_schematic("/tmp/x.kicad_sch")
        assert latest is not None
        assert latest["state_id"] == _hex_id("good")
