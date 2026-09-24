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
from unittest.mock import patch

from kicad_mcp.config import _exists_or_false


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
