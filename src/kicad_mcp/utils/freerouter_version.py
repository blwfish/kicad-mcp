"""FreeRouter version identification and the "newest version we have validated" gate.

FreeRouter ships no machine-readable version: every jar's manifest says
``Implementation-Version: unspecified`` and ``application.properties`` carries none
(checked on 2.1.0, 2.2.3 and 2.4.1).  The jar *does* announce itself as the first
INFO line it prints, even when handed an unreadable input::

    2026-10-04 18:33:14.113 INFO   Freerouting v2.4.1 (build-date: 2026-09-03)

FreeRouter is third-party, so there is no producer of ours to give us a typed field;
this banner is the only source.  It is used for ONE thing -- deciding whether to warn
that an install is newer than what this project's integration suite has been run
against.  Nothing branches on it for fault detection or recovery, and an
unparseable banner yields ``status == "unknown"``, never a guess.

Why warn at all: on the full integration suite FreeRouter 2.4.1 took ~2.1x as long as
2.2.3 and tripped five pytest-timeouts (issue #140).  Versions between 2.2.3 and 2.4.1
are unmeasured.
"""

import logging
import os
import re
import subprocess
import threading
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

Version = Tuple[int, int, int]

# Newest FreeRouter the integration suite has been validated against.  Raise this
# only after running the FULL suite (both KiCad legs) on the new version -- the
# 2.4.1 evaluation passed the routing-quality assertions and still failed on time.
FREEROUTER_VALIDATED_MAX: Version = (2, 2, 3)

# Closed vocabulary for the ``status`` field (consumers branch on these, not on text).
STATUS_VALIDATED = "validated"
STATUS_NEWER = "newer_than_validated"
STATUS_UNKNOWN = "unknown"

_BANNER_RE = re.compile(r"^.*?\bINFO\b\s+Freerouting v(\d+)\.(\d+)\.(\d+)\b", re.MULTILINE)

_PROBE_TIMEOUT_S = 20.0


def format_version(v: Version) -> str:
    return ".".join(str(p) for p in v)


def parse_version_banner(text: str) -> Optional[Version]:
    """First ``INFO ... Freerouting vX.Y.Z`` line in *text*, else None.

    Only an INFO-level banner counts, so a stray ``WARN ... Freerouting v9.9.9``
    (or a version quoted in an error message) can't be mistaken for the build.
    A two-part ``vX.Y`` is NOT accepted: the real banner always has three parts.
    """
    m = _BANNER_RE.search(text or "")
    if m is None:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def classify_version(version: Optional[Version]) -> str:
    """STATUS_* for *version*.  Tuple comparison, so 2.10.0 > 2.2.3 (not lexicographic)."""
    if version is None:
        return STATUS_UNKNOWN
    return STATUS_NEWER if version > FREEROUTER_VALIDATED_MAX else STATUS_VALIDATED


def _as_text(data: Any) -> str:
    # subprocess.TimeoutExpired carries bytes for stdout/stderr even in text mode.
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def probe_version(cmd: Sequence[str], timeout: float = _PROBE_TIMEOUT_S) -> Optional[Version]:
    """Run *cmd* (a FreeRouter command line aimed at an unreadable input) and read the
    version from its startup banner.

    The process is expected to fail (the input does not exist) or to be killed at
    *timeout*; either way the banner is printed first, so partial output from a
    timed-out run is still used.  Returns None -- never raises -- if the process
    can't be started or prints no recognisable banner.
    """
    try:
        proc = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
        output = _as_text(proc.stdout) + "\n" + _as_text(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + "\n" + _as_text(exc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("could not probe the FreeRouter version (%s): %s", cmd[0] if cmd else "?", exc)
        return None
    return parse_version_banner(output)


# (realpath, size, mtime_ns) -> Version.  Unknown results are NOT cached: a transient
# failure (timeout under load) must not hide the warning for the life of the process.
_version_cache: Dict[Tuple[str, int, int], Version] = {}
_version_cache_lock = threading.Lock()


def _cache_key(jar_path: str) -> Optional[Tuple[str, int, int]]:
    try:
        st = os.stat(jar_path)
    except OSError:
        return None
    return (os.path.realpath(jar_path), st.st_size, st.st_mtime_ns)


def warning_text(version: Version) -> str:
    return (
        f"FreeRouter {format_version(version)} is newer than the newest version this "
        f"project's integration suite is validated against "
        f"({format_version(FREEROUTER_VALIDATED_MAX)}). FreeRouter 2.4.1 measured about "
        f"2x slower than 2.2.3 on that suite and hit test timeouts (issue #140); other "
        f"newer versions are unmeasured. Expect longer autoroute times, or use "
        f"FreeRouter {format_version(FREEROUTER_VALIDATED_MAX)} (set FREEROUTER_JAR)."
    )


def freerouter_version_info(
    jar_path: str, probe: Callable[[], Optional[Version]],
) -> Dict[str, Any]:
    """Typed description of the installed FreeRouter for tool results.

    ``{"version": "2.4.1" | None, "status": STATUS_*, "validated_max": "2.2.3",
    "warning": str | None}`` -- ``warning`` is set only for STATUS_NEWER.
    A jar that is not a file is reported unknown WITHOUT running anything.
    ``probe`` is only called on a cache miss; it launches a JVM (~1-2 s).
    """
    key = _cache_key(jar_path) if jar_path and os.path.isfile(jar_path) else None
    version: Optional[Version] = None
    if key is not None:
        with _version_cache_lock:
            version = _version_cache.get(key)
        if version is None:
            version = probe()
            if version is not None:
                with _version_cache_lock:
                    first_time = key not in _version_cache
                    _version_cache[key] = version
                if first_time and classify_version(version) == STATUS_NEWER:
                    logger.warning(warning_text(version))
    status = classify_version(version)
    return {
        "version": format_version(version) if version else None,
        "status": status,
        "validated_max": format_version(FREEROUTER_VALIDATED_MAX),
        "warning": warning_text(version) if (version and status == STATUS_NEWER) else None,
    }
