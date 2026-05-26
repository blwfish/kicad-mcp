"""Bridge to KiCad's pcbnew Python API.

Runs pcbnew operations via KiCad's bundled Python as a subprocess,
since pcbnew is a compiled C++ module that only works with KiCad's own Python.
"""

import glob
import json
import logging
import os
import platform
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Known-safe stderr patterns from KiCad that can be filtered out.
# Be precise — never filter by broad substrings like "assert" alone.
# Patterns are matched with re.search (unanchored); use ^ / $ to anchor.
_SAFE_STDERR_PATTERNS = [
    re.compile(r"assert.*IsOk.*wxApp", re.IGNORECASE),  # KiCad wxApp assertion
    re.compile(r"Gtk-WARNING"),                          # GTK warning chatter
    re.compile(r"^\s*$"),                                # blank lines
]


def _get_kicad_app_path() -> str:
    """Return the KiCad .app path, honoring KICAD_APP_PATH env var."""
    return os.environ.get("KICAD_APP_PATH", "/Applications/KiCad/KiCad.app")


def _bundled_python_versions(framework_versions_dir: str) -> list:
    """Discover bundled KiCad Python 3.x version directories, newest first.

    Single source of truth — both _get_kicad_python and _get_kicad_env need
    the same version selection.  Earlier code duplicated the glob, with the
    risk that they could drift and the Python binary could end up pointing
    at a different version than the site-packages path.
    """
    return sorted(glob.glob(f"{framework_versions_dir}/3.*"), reverse=True)


def _get_kicad_python() -> Optional[str]:
    """Find KiCad's bundled Python interpreter."""
    system = platform.system()

    if system == "Darwin":
        fw = (
            f"{_get_kicad_app_path()}/Contents/Frameworks"
            "/Python.framework/Versions"
        )
        candidates = []
        for vdir in _bundled_python_versions(fw):
            ver = os.path.basename(vdir)
            candidates.append(f"{vdir}/bin/python{ver}")
            candidates.append(f"{vdir}/bin/python3")
    elif system == "Linux":
        candidates = ["/usr/bin/python3"]
    elif system == "Windows":
        candidates = [r"C:\Program Files\KiCad\bin\python.exe"]
    else:
        candidates = []

    for path in candidates:
        if os.path.isfile(path):
            return path

    return None


def _get_kicad_env() -> Dict[str, str]:
    """Build environment variables for KiCad's Python."""
    env = os.environ.copy()
    system = platform.system()

    if system == "Darwin":
        kicad_app = _get_kicad_app_path()
        fw = f"{kicad_app}/Contents/Frameworks/Python.framework/Versions"
        version_dirs = _bundled_python_versions(fw)
        if version_dirs:
            vdir = version_dirs[0]
            ver = os.path.basename(vdir)
            site_packages = f"{vdir}/lib/python{ver}/site-packages"
        else:
            # No bundled Python version directory found. Fall back to the
            # legacy "Current" symlink path — but log a warning so callers
            # can diagnose `import pcbnew` failures inside the subprocess
            # (which would otherwise surface as opaque JSON-parse errors).
            site_packages = f"{fw}/Current/lib/python3/site-packages"
            logger.warning(
                "No bundled Python 3.x version found at %s; falling back to "
                "Current/lib/python3/site-packages (PYTHONPATH may not work)",
                fw,
            )
        env["PYTHONPATH"] = site_packages
        env["DYLD_FRAMEWORK_PATH"] = f"{kicad_app}/Contents/Frameworks"

    return env


def _filter_stderr(stderr: str) -> str:
    """Remove known-safe KiCad warnings from stderr, keep real errors."""
    lines = stderr.split("\n")
    filtered = []
    for line in lines:
        if any(p.search(line) for p in _SAFE_STDERR_PATTERNS):
            continue
        if line.strip():
            filtered.append(line)
    return "\n".join(filtered).strip()


def run_pcbnew_script(
    script: str,
    timeout: float = 30.0,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a Python script using KiCad's Python with pcbnew available.

    The script MUST print a single JSON object to stdout as its final output.
    Any other stdout output should be avoided; use stderr for logging.

    Args:
        script: Python source code to execute.
        timeout: Maximum execution time in seconds.
        params: Optional dict of parameters to pass to the script.  When
            provided, the dict is JSON-serialized and written to a temp file.
            The script can read it via::

                import json, sys
                params = json.loads(open(sys.argv[1]).read())

            Using *params* avoids interpolating untrusted values directly
            into the script string (which risks injection).

    Returns:
        Parsed JSON dict from the script's stdout.

    Raises:
        RuntimeError: If KiCad Python is not found or the script fails.
    """
    kicad_python = _get_kicad_python()
    if not kicad_python:
        raise RuntimeError(
            "KiCad Python interpreter not found. Ensure KiCad is installed."
        )

    # Write script to a temp file
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(script)
        script_path = f.name

    # Write params to a separate temp file if provided
    params_path = None
    if params is not None:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as pf:
            json.dump(params, pf)
            params_path = pf.name

    logger.debug("Executing pcbnew script %s (timeout=%.1fs)", script_path, timeout)
    start_time = time.monotonic()

    try:
        env = _get_kicad_env()
        cmd = [kicad_python, script_path]
        if params_path:
            cmd.append(params_path)
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        elapsed = time.monotonic() - start_time

        if result.returncode != 0:
            error_msg = _filter_stderr(result.stderr)
            truncated = "  (...truncated)" if len(error_msg) > 2000 else ""
            logger.error(
                "pcbnew script failed (exit %d, %.2fs): %s%s",
                result.returncode,
                elapsed,
                error_msg[:2000],
                truncated,
            )
            raise RuntimeError(
                f"pcbnew script failed (exit {result.returncode}): {error_msg}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError("pcbnew script produced no output")

        # Find the JSON object in stdout.  SWIG memory-leak warnings
        # (e.g. "swig/python detected a memory leak of type 'ZONE *'")
        # can appear on stdout after the script's print(), so we scan
        # backwards for the first line that parses as JSON.
        parsed = None
        for line in reversed(stdout.split("\n")):
            line = line.strip()
            if line.startswith("{"):
                try:
                    parsed = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if parsed is None:
            truncated = "  (...truncated)" if len(stdout) > 2000 else ""
            raise RuntimeError(
                f"pcbnew script output contains no valid JSON line\n"
                f"Output was: {stdout[:2000]}{truncated}"
            )

        logger.debug("pcbnew script completed in %.2fs", elapsed)
        return parsed

    except subprocess.TimeoutExpired:
        logger.error("pcbnew script timed out after %.1fs", timeout)
        raise RuntimeError(f"pcbnew script timed out after {timeout}s")
    finally:
        os.unlink(script_path)
        if params_path:
            try:
                os.unlink(params_path)
            except OSError:
                pass
