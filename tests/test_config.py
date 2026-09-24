"""Tests for kicad_mcp/config.py's module-import-time path resolution.

The bulk of this module runs at import time (platform detection, search-path
auto-discovery), so most of it is exercised implicitly just by importing the
package. This file targets the one pure, directly-testable seam: the
_exists_or_false guard around os.path.exists() calls made while building
ADDITIONAL_SEARCH_PATHS -- previously unguarded os.path.exists() calls there
meant an OSError (a stale network mount, a permission-denied parent
directory, a TOCTOU race) crashed the import of this module for every tool
call, not just skipped that one candidate path.
"""
import logging
from unittest.mock import patch

from kicad_mcp.config import _exists_or_false, _resolve_env_search_paths


class TestExistsOrFalse:
    def test_real_path_true(self, tmp_path):
        assert _exists_or_false(str(tmp_path)) is True

    def test_nonexistent_path_false(self, tmp_path):
        assert _exists_or_false(str(tmp_path / "does-not-exist")) is False

    def test_oserror_treated_as_false_not_raised(self):
        """Regression: os.path.exists() itself is normally exception-safe
        (returns False on most errors), but this guard exists specifically
        for the case where the underlying stat() call raises OSError instead
        of False (a stale network mount can do this) -- confirm the guard
        actually catches it rather than merely duplicating exists()'s own
        behavior."""
        with patch("kicad_mcp.config.os.path.exists", side_effect=OSError("stale mount")):
            assert _exists_or_false("/some/path") is False


class TestResolveEnvSearchPaths:
    """Regression: KICAD_SEARCH_PATHS entries that don't exist on disk were
    silently filtered out with no counter/log at all -- a caller (or a user
    debugging a misconfigured env var) couldn't distinguish "env var unset"
    from "the user configured this and every entry is wrong"."""

    def test_all_paths_exist_none_dropped(self, tmp_path):
        found, dropped = _resolve_env_search_paths(
            str(tmp_path), exists_check=lambda p: True
        )
        assert found == [str(tmp_path)] and dropped == 0

    def test_all_paths_missing_all_dropped(self):
        found, dropped = _resolve_env_search_paths(
            "/nonexistent/a,/nonexistent/b", exists_check=lambda p: False
        )
        assert found == [] and dropped == 2

    def test_mixed_paths_counts_only_the_missing_ones(self):
        found, dropped = _resolve_env_search_paths(
            "/exists,/missing", exists_check=lambda p: p == "/exists"
        )
        assert found == ["/exists"] and dropped == 1

    def test_entries_are_stripped_and_expanded(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "sub").mkdir()
        found, dropped = _resolve_env_search_paths(" ~/sub , /missing ")
        assert found == [str(tmp_path / "sub")]
        assert dropped == 1

    def test_module_logs_a_warning_when_configured_path_is_missing(self, monkeypatch, caplog):
        """End-to-end: the module-level code that consumes
        _resolve_env_search_paths must actually log when it drops entries,
        not just compute the count and discard it."""
        import importlib
        import kicad_mcp.config as config_module

        monkeypatch.setenv("KICAD_SEARCH_PATHS", "/definitely/does/not/exist")
        with caplog.at_level(logging.WARNING, logger="kicad_mcp.config"):
            importlib.reload(config_module)
        try:
            assert any(
                "KICAD_SEARCH_PATHS" in r.message and "1 of 1" in r.message
                for r in caplog.records
            )
        finally:
            # Restore the module to its normal (env-var-unset) state so
            # other tests importing kicad_mcp.config aren't affected by
            # this test's monkeypatched environment.
            monkeypatch.delenv("KICAD_SEARCH_PATHS", raising=False)
            importlib.reload(config_module)
