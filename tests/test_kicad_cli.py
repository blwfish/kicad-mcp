"""
Tests for KiCadCLIManager — focused on the startup-validation retry that
hardens detection against transient failures on cold/busy CI runners.
"""

import subprocess
from unittest.mock import MagicMock, patch

from kicad_mcp.config import KICAD_CLI_VALIDATE_ATTEMPTS
from kicad_mcp.utils.kicad_cli import KiCadCLIManager, format_cli_error

_FAKE_CLI = "/fake/kicad-cli"


def _ok():
    """A successful `--version` CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 0
    result.stdout = "kicad-cli version 9.0.0"
    result.stderr = ""
    return result


def _nonzero(stderr=""):
    """A `--version` CompletedProcess that exited nonzero (transient under load)."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 1
    result.stdout = ""
    result.stderr = stderr
    return result


def _manager_with_detect():
    """A fresh manager that always 'detects' the fake CLI path."""
    mgr = KiCadCLIManager()
    mgr._detect_cli_path = MagicMock(return_value=_FAKE_CLI)
    return mgr


def test_validate_recovers_after_transient_nonzero_then_success():
    """A first nonzero exit is retried; the second success resolves the path."""
    mgr = _manager_with_detect()
    side_effects = [_nonzero(), _ok()]
    with (
        patch("kicad_mcp.utils.kicad_cli.subprocess.run", side_effect=side_effects) as run,
        patch("kicad_mcp.utils.kicad_cli.time.sleep") as sleep,
    ):
        assert mgr.find_kicad_cli() == _FAKE_CLI

    assert run.call_count == 2  # failed once, then succeeded
    assert sleep.call_count == 1  # backed off exactly once before the retry
    assert mgr._cache_validated is True


def test_validate_recovers_after_transient_exception_then_success():
    """A transient subprocess exception (e.g. timeout) is retried, then succeeds."""
    mgr = _manager_with_detect()
    side_effects = [subprocess.TimeoutExpired(cmd="kicad-cli", timeout=10.0), _ok()]
    with (
        patch("kicad_mcp.utils.kicad_cli.subprocess.run", side_effect=side_effects) as run,
        patch("kicad_mcp.utils.kicad_cli.time.sleep"),
    ):
        assert mgr.find_kicad_cli() == _FAKE_CLI

    assert run.call_count == 2


def test_validate_gives_up_after_all_attempts_fail():
    """If every attempt fails, the CLI is treated as absent and no path is cached."""
    mgr = _manager_with_detect()
    with (
        patch(
            "kicad_mcp.utils.kicad_cli.subprocess.run",
            side_effect=[_nonzero() for _ in range(KICAD_CLI_VALIDATE_ATTEMPTS)],
        ) as run,
        patch("kicad_mcp.utils.kicad_cli.time.sleep") as sleep,
    ):
        assert mgr.find_kicad_cli() is None

    assert run.call_count == KICAD_CLI_VALIDATE_ATTEMPTS
    # Backoff happens between attempts only — one fewer than the attempt count.
    assert sleep.call_count == KICAD_CLI_VALIDATE_ATTEMPTS - 1
    assert mgr._cache_validated is False


def test_validate_first_attempt_success_does_not_sleep():
    """A healthy CLI resolves on the first probe with no backoff."""
    mgr = _manager_with_detect()
    with (
        patch("kicad_mcp.utils.kicad_cli.subprocess.run", side_effect=[_ok()]) as run,
        patch("kicad_mcp.utils.kicad_cli.time.sleep") as sleep,
    ):
        assert mgr.find_kicad_cli() == _FAKE_CLI

    assert run.call_count == 1
    assert sleep.call_count == 0


# ---------------------------------------------------------------------------
# Medium findings from the 2026-09-23 full review: diagnosability gaps in
# get_version() (stderr/returncode never logged on failure) and
# _validate_cli_path (timeout vs. other-error distinction lost in the log).
# ---------------------------------------------------------------------------

class TestGetVersionLogsFailureDetail:
    def test_nonzero_exit_logs_returncode_and_stderr(self, caplog):
        import logging
        mgr = _manager_with_detect()
        mgr._cached_cli_path = _FAKE_CLI
        mgr._cache_validated = True
        with (
            patch("kicad_mcp.utils.kicad_cli.subprocess.run",
                  return_value=_nonzero(stderr="kicad-cli: fatal error")),
            caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.kicad_cli"),
        ):
            assert mgr.get_version() is None
        assert any(
            "exited 1" in r.message and "fatal error" in r.message
            for r in caplog.records
        )

    def test_nonzero_exit_with_empty_stderr_says_so(self, caplog):
        import logging
        mgr = _manager_with_detect()
        mgr._cached_cli_path = _FAKE_CLI
        mgr._cache_validated = True
        with (
            patch("kicad_mcp.utils.kicad_cli.subprocess.run",
                  return_value=_nonzero(stderr="")),
            caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.kicad_cli"),
        ):
            assert mgr.get_version() is None
        assert any("(no stderr)" in r.message for r in caplog.records)

    def test_success_returns_stripped_stdout(self):
        mgr = _manager_with_detect()
        mgr._cached_cli_path = _FAKE_CLI
        mgr._cache_validated = True
        with patch("kicad_mcp.utils.kicad_cli.subprocess.run", return_value=_ok()):
            assert mgr.get_version() == "kicad-cli version 9.0.0"


class TestValidateCliPathDistinguishesTimeout:
    def test_timeout_is_retried_and_logged_distinctly_from_other_errors(self, caplog):
        """Regression: TimeoutExpired and a generic SubprocessError/OSError
        used to log through the identical debug message -- an operator
        reading logs couldn't tell 'kicad-cli is hanging' from 'kicad-cli
        crashed or is missing', which call for different responses."""
        import logging
        mgr = _manager_with_detect()
        side_effects = [subprocess.TimeoutExpired(cmd="kicad-cli", timeout=10.0), _ok()]
        with (
            patch("kicad_mcp.utils.kicad_cli.subprocess.run", side_effect=side_effects),
            patch("kicad_mcp.utils.kicad_cli.time.sleep"),
            caplog.at_level(logging.DEBUG, logger="kicad_mcp.utils.kicad_cli"),
        ):
            assert mgr.find_kicad_cli() == _FAKE_CLI
        # The dedicated TimeoutExpired branch's message format has no
        # "failed:" prefix (that's the generic catch-all's format) -- str()
        # of a bare TimeoutExpired ALSO happens to contain "timed out", so
        # asserting on that substring alone would pass even if the
        # dedicated branch were removed and the exception fell through to
        # the generic handler instead.
        timeout_msgs = [r.message for r in caplog.records if "timed out" in r.message]
        assert timeout_msgs and all("failed:" not in m for m in timeout_msgs)

    def test_nonzero_exit_logs_stderr_too(self, caplog):
        import logging
        mgr = _manager_with_detect()
        side_effects = [_nonzero(stderr="permission denied"), _ok()]
        with (
            patch("kicad_mcp.utils.kicad_cli.subprocess.run", side_effect=side_effects),
            patch("kicad_mcp.utils.kicad_cli.time.sleep"),
            caplog.at_level(logging.DEBUG, logger="kicad_mcp.utils.kicad_cli"),
        ):
            assert mgr.find_kicad_cli() == _FAKE_CLI
        assert any("permission denied" in r.message for r in caplog.records)


class TestFormatCliError:
    """finding #14/#25 (Phase 1, 2026-09-23 full review): `e.stderr or
    e.stdout` drops stderr entirely whenever it's an empty string but stdout
    has real content (kicad-cli writes errors to stdout on some builds).
    Single source of truth shared by export.py and pcb_pipeline.py, which
    previously duplicated a private nested function and a bare `or` pattern
    respectively, with the pcb_pipeline.py copy never receiving the fix."""

    def _err(self, stderr, stdout, returncode=1):
        return subprocess.CalledProcessError(
            returncode, ["kicad-cli"], output=stdout, stderr=stderr,
        )

    def test_stdout_only_error_not_dropped(self):
        e = self._err(stderr="", stdout="Error: could not open board file")
        assert "could not open board file" in format_cli_error(e)

    def test_stderr_only(self):
        e = self._err(stderr="permission denied", stdout="")
        assert format_cli_error(e) == "permission denied"

    def test_both_streams_concatenated(self):
        e = self._err(stderr="stderr line", stdout="stdout line")
        text = format_cli_error(e)
        assert "stderr line" in text and "stdout line" in text

    def test_neither_stream_falls_back_to_exit_code(self):
        e = self._err(stderr="", stdout="", returncode=7)
        assert "7" in format_cli_error(e)

    def test_whitespace_only_streams_treated_as_empty(self):
        e = self._err(stderr="   \n", stdout="\t")
        assert format_cli_error(e) == "(no output; exit code 1)"
