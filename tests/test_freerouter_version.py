"""Tests for utils/freerouter_version.py -- identifying the FreeRouter build and
warning when it is newer than what the integration suite has been validated against
(FreeRouter 2.4.1 ran ~2.1x slower and tripped five test timeouts; issue #140).

Threshold: ``version > FREEROUTER_VALIDATED_MAX`` (2.2.3).  The boundary is pinned at
value / just-below / just-above, plus the cases where a string comparison would be
wrong (2.10.0 vs 2.2.3).
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.utils import freerouter_version as fv

# Banners copied verbatim from real runs of the 2.2.3 and 2.4.1 jars (not constructed).
REAL_BANNER_241 = "2026-10-04 18:33:14.113 INFO   Freerouting v2.4.1 (build-date: 2026-09-03)"
REAL_BANNER_223 = "2026-10-04 18:33:26.361 INFO   Freerouting v2.2.3 (build-date: 2026-05-08)"
REAL_ERROR_TAIL = (
    "2026-10-04 18:33:15.236 ERROR  Couldn't load the input file '/nonexistent.dsn'\n"
    "java.io.FileNotFoundException: /nonexistent.dsn (No such file or directory)\n"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    fv._version_cache.clear()
    yield
    fv._version_cache.clear()


# --- parse_version_banner ----------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    (REAL_BANNER_241, (2, 4, 1)),
    (REAL_BANNER_223, (2, 2, 3)),
    (REAL_BANNER_241 + "\n" + REAL_ERROR_TAIL, (2, 4, 1)),
    ("some earlier line\n" + REAL_BANNER_223, (2, 2, 3)),            # banner not on line 1
    ("2026-01-01 00:00:00.000 INFO Freerouting v10.20.30 (x)", (10, 20, 30)),   # multi-digit parts
    ("2026-01-01 00:00:00.000 INFO   Freerouting v2.4.1-SNAPSHOT", (2, 4, 1)),  # suffix tolerated
])
def test_parse_banner(text, expected):
    assert fv.parse_version_banner(text) == expected


@pytest.mark.parametrize("text", [
    "",
    None,
    "no banner here",
    REAL_ERROR_TAIL,
    "2026-01-01 00:00:00.000 WARN   Freerouting v9.9.9 is deprecated",      # not INFO
    "2026-01-01 00:00:00.000 INFO   Freerouting v2.4 (build-date: x)",      # two-part: rejected
    "2026-01-01 00:00:00.000 INFO   Freerouting vX.Y.Z",
    "Freerouting v2.4.1",                                                    # no level -> not the banner
])
def test_parse_banner_rejects(text):
    assert fv.parse_version_banner(text) is None


def test_parse_banner_four_part_takes_the_first_three():
    """Pins current behavior for an ambiguous input: v2.4.1.2 -> (2, 4, 1)."""
    assert fv.parse_version_banner("2026-01-01 00:00:00.000 INFO Freerouting v2.4.1.2") == (2, 4, 1)


def test_parse_banner_first_info_banner_wins_over_a_later_one():
    text = REAL_BANNER_223 + "\n" + REAL_BANNER_241
    assert fv.parse_version_banner(text) == (2, 2, 3)


# --- classify_version: the validated-max boundary -----------------------------

@pytest.mark.parametrize("version, expected", [
    ((2, 2, 2), fv.STATUS_VALIDATED),     # just below
    ((2, 2, 3), fv.STATUS_VALIDATED),     # AT the validated maximum (contract: not newer)
    ((2, 2, 4), fv.STATUS_NEWER),         # just above (the version the docs recommend!)
    ((2, 3, 0), fv.STATUS_NEWER),
    ((2, 4, 1), fv.STATUS_NEWER),         # the measured-slow release
    ((2, 10, 0), fv.STATUS_NEWER),        # numeric, not lexicographic: "2.10.0" < "2.2.3" as text
    ((3, 0, 0), fv.STATUS_NEWER),
    ((2, 1, 0), fv.STATUS_VALIDATED),
    ((1, 9, 0), fv.STATUS_VALIDATED),
    (None, fv.STATUS_UNKNOWN),
])
def test_classify(version, expected):
    assert fv.classify_version(version) == expected


def test_validated_max_is_the_documented_baseline():
    assert fv.FREEROUTER_VALIDATED_MAX == (2, 2, 3)


# --- probe_version -------------------------------------------------------------

def _completed(stdout="", stderr="", rc=1):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=rc)


def test_probe_reads_banner_from_stdout_even_when_the_process_fails():
    with patch("subprocess.run", return_value=_completed(stdout=REAL_BANNER_241 + "\n" + REAL_ERROR_TAIL, rc=1)):
        assert fv.probe_version(["java", "-jar", "x.jar"]) == (2, 4, 1)


def test_probe_reads_banner_from_stderr():
    with patch("subprocess.run", return_value=_completed(stderr=REAL_BANNER_223)):
        assert fv.probe_version(["java"]) == (2, 2, 3)


def test_probe_uses_partial_output_of_a_timed_out_run():
    """The process is killed at the timeout, but the banner is printed first.
    TimeoutExpired carries BYTES even in text mode."""
    exc = subprocess.TimeoutExpired(cmd="java", timeout=20, output=REAL_BANNER_241.encode(), stderr=b"")
    with patch("subprocess.run", side_effect=exc):
        assert fv.probe_version(["java"]) == (2, 4, 1)


def test_probe_timeout_with_no_output_is_unknown():
    exc = subprocess.TimeoutExpired(cmd="java", timeout=20, output=None, stderr=None)
    with patch("subprocess.run", side_effect=exc):
        assert fv.probe_version(["java"]) is None


@pytest.mark.parametrize("error", [FileNotFoundError("java"), PermissionError("x"),
                                   subprocess.SubprocessError("boom")])
def test_probe_start_failure_is_unknown_not_an_exception(error):
    with patch("subprocess.run", side_effect=error):
        assert fv.probe_version(["java"]) is None


def test_probe_no_banner_is_unknown():
    with patch("subprocess.run", return_value=_completed(stdout="Error: Could not find or load main class")):
        assert fv.probe_version(["java"]) is None


def test_probe_passes_the_command_and_timeout_through_and_closes_stdin():
    with patch("subprocess.run", return_value=_completed(stdout=REAL_BANNER_223)) as run:
        fv.probe_version(["java", "-jar", "x.jar", "-de", "/nope"], timeout=7.5)
    assert run.call_args[0][0] == ["java", "-jar", "x.jar", "-de", "/nope"]
    assert run.call_args[1]["timeout"] == 7.5
    assert run.call_args[1]["stdin"] == subprocess.DEVNULL


# --- freerouter_version_info ---------------------------------------------------

@pytest.fixture
def jar(tmp_path):
    p = tmp_path / "freerouting-x.jar"
    p.write_bytes(b"jar")
    return p


def test_info_validated_has_no_warning(jar):
    info = fv.freerouter_version_info(str(jar), lambda: (2, 2, 3))
    assert info == {"version": "2.2.3", "status": fv.STATUS_VALIDATED,
                    "validated_max": "2.2.3", "warning": None}


def test_info_newer_carries_a_warning_naming_both_versions(jar):
    info = fv.freerouter_version_info(str(jar), lambda: (2, 4, 1))
    assert info["status"] == fv.STATUS_NEWER and info["version"] == "2.4.1"
    assert "2.4.1" in info["warning"] and "2.2.3" in info["warning"] and "#140" in info["warning"]


def test_info_unknown_when_banner_unreadable(jar):
    info = fv.freerouter_version_info(str(jar), lambda: None)
    assert info["status"] == fv.STATUS_UNKNOWN and info["version"] is None and info["warning"] is None


@pytest.mark.parametrize("path", ["", "/definitely/not/a/jar.jar"])
def test_info_for_a_missing_jar_is_unknown_and_runs_nothing(path):
    probe = MagicMock()
    info = fv.freerouter_version_info(path, probe)
    probe.assert_not_called()
    assert info["status"] == fv.STATUS_UNKNOWN


def test_info_probes_once_per_jar(jar):
    probe = MagicMock(return_value=(2, 2, 3))
    fv.freerouter_version_info(str(jar), probe)
    fv.freerouter_version_info(str(jar), probe)
    assert probe.call_count == 1


def test_info_reprobes_when_the_jar_changes(jar):
    probe = MagicMock(side_effect=[(2, 2, 3), (2, 4, 1)])
    assert fv.freerouter_version_info(str(jar), probe)["version"] == "2.2.3"
    jar.write_bytes(b"a longer replacement jar")        # new size -> new cache key
    assert fv.freerouter_version_info(str(jar), probe)["version"] == "2.4.1"
    assert probe.call_count == 2


def test_info_distinct_jars_are_probed_separately(tmp_path):
    a = tmp_path / "a.jar"
    a.write_bytes(b"1")
    b = tmp_path / "b.jar"
    b.write_bytes(b"2")
    probe = MagicMock(side_effect=[(2, 2, 3), (2, 4, 1)])
    assert fv.freerouter_version_info(str(a), probe)["version"] == "2.2.3"
    assert fv.freerouter_version_info(str(b), probe)["version"] == "2.4.1"


def test_info_does_not_cache_an_unknown_result(jar):
    """A transient failure (timeout under load) must not hide the warning for the
    life of the process: the next call probes again."""
    probe = MagicMock(side_effect=[None, (2, 4, 1)])
    assert fv.freerouter_version_info(str(jar), probe)["status"] == fv.STATUS_UNKNOWN
    assert fv.freerouter_version_info(str(jar), probe)["status"] == fv.STATUS_NEWER
    assert probe.call_count == 2


def test_info_logs_the_warning_once_not_on_every_call(jar, caplog):
    caplog.set_level("WARNING", logger=fv.logger.name)
    for _ in range(3):
        fv.freerouter_version_info(str(jar), lambda: (2, 4, 1))
    assert sum("newer than the newest version" in r.getMessage() for r in caplog.records) == 1


def test_info_does_not_log_for_a_validated_version(jar, caplog):
    caplog.set_level("WARNING", logger=fv.logger.name)
    fv.freerouter_version_info(str(jar), lambda: (2, 2, 3))
    assert not caplog.records
