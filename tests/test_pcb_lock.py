"""Tests for utils/pcb_lock.py -- the in-process, per-resolved-path lock
that makes AGENT-INSTRUCTIONS.md's "no concurrent PCB writes" rule actually
enforced within one running server process. See that module's docstring
for what this deliberately does NOT cover (cross-process, cross-filesystem).
"""
import os
import threading
import time

from kicad_mcp.utils.pcb_lock import busy_error, pcb_write_lock


class TestBasicAcquireRelease:
    def test_uncontended_acquire_yields_true(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with pcb_write_lock(pcb) as acquired:
            assert acquired is True

    def test_released_after_context_exits(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with pcb_write_lock(pcb) as acquired:
            assert acquired is True
        # Lock must be free again -- a second acquire also succeeds.
        with pcb_write_lock(pcb) as acquired2:
            assert acquired2 is True

    def test_released_on_exception(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        try:
            with pcb_write_lock(pcb) as acquired:
                assert acquired is True
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        # Must still be released despite the exception.
        with pcb_write_lock(pcb) as acquired2:
            assert acquired2 is True


class TestContention:
    def test_second_acquire_on_same_path_fails_without_blocking(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with pcb_write_lock(pcb) as first:
            assert first is True
            start = time.monotonic()
            with pcb_write_lock(pcb) as second:
                elapsed = time.monotonic() - start
            assert second is False
            # Non-blocking: must return near-instantly, not wait.
            assert elapsed < 0.5

    def test_lock_is_free_again_after_holder_releases(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        with pcb_write_lock(pcb):
            pass
        with pcb_write_lock(pcb) as acquired:
            assert acquired is True

    def test_different_paths_do_not_contend(self, tmp_path):
        a = str(tmp_path / "a.kicad_pcb")
        b = str(tmp_path / "b.kicad_pcb")
        with pcb_write_lock(a) as lock_a:
            assert lock_a is True
            with pcb_write_lock(b) as lock_b:
                assert lock_b is True

    def test_relative_and_absolute_paths_to_same_file_contend(self, tmp_path, monkeypatch):
        """The lock is keyed by realpath -- two different spellings of the
        same file must map to the same underlying lock."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("")
        monkeypatch.chdir(tmp_path)
        absolute = str(pcb)
        relative = "board.kicad_pcb"

        with pcb_write_lock(absolute) as first:
            assert first is True
            with pcb_write_lock(relative) as second:
                assert second is False

    def test_symlink_and_target_contend(self, tmp_path):
        """A symlink and its target resolve to the same realpath, so they
        must share the same lock."""
        target = tmp_path / "real.kicad_pcb"
        target.write_text("")
        link = tmp_path / "link.kicad_pcb"
        os.symlink(target, link)

        with pcb_write_lock(str(target)) as first:
            assert first is True
            with pcb_write_lock(str(link)) as second:
                assert second is False


class TestConcurrentThreads:
    def test_only_one_thread_gets_the_lock_at_a_time(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            with pcb_write_lock(pcb) as acquired:
                results.append(acquired)
                time.sleep(0.05)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert sorted(results) == [False, True]


class TestBusyError:
    def test_shape(self, tmp_path):
        pcb = str(tmp_path / "board.kicad_pcb")
        err = busy_error(pcb)
        assert err["status"] == "error"
        assert pcb in err["error"]
        assert "retry" in err["error"].lower()
