"""
Tests for KiCadCLIManager — focused on the startup-validation retry that
hardens detection against transient failures on cold/busy CI runners.
"""

import subprocess
from unittest.mock import MagicMock, patch

from kicad_mcp.config import KICAD_CLI_VALIDATE_ATTEMPTS
from kicad_mcp.utils.kicad_cli import KiCadCLIManager

_FAKE_CLI = "/fake/kicad-cli"


def _ok():
    """A successful `--version` CompletedProcess."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 0
    result.stdout = "kicad-cli version 9.0.0"
    return result


def _nonzero():
    """A `--version` CompletedProcess that exited nonzero (transient under load)."""
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = 1
    result.stdout = ""
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
