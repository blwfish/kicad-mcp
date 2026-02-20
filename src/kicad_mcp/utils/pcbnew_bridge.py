"""Bridge to KiCad's pcbnew Python API.

Runs pcbnew operations via KiCad's bundled Python 3.9 as a subprocess,
since pcbnew is a compiled C++ module that only works with KiCad's own Python.
"""

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
# Be precise — never filter by broad substrings like "assert".
_SAFE_STDERR_PATTERNS = [
    re.compile(r".*assert.*IsOk.*wxApp.*", re.IGNORECASE),
    re.compile(r".*Gtk-WARNING.*"),
    re.compile(r"^\s*$"),
]


def _get_kicad_python() -> Optional[str]:
    """Find KiCad's bundled Python interpreter."""
    system = platform.system()

    if system == "Darwin":
        candidates = [
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/3.9/bin/python3.9",
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/"
            "Python.framework/Versions/3.9/bin/python3",
        ]
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
        kicad_app = "/Applications/KiCad/KiCad.app"
        env["PYTHONPATH"] = (
            f"{kicad_app}/Contents/Frameworks/Python.framework"
            f"/Versions/3.9/lib/python3.9/site-packages"
        )
        env["DYLD_FRAMEWORK_PATH"] = f"{kicad_app}/Contents/Frameworks"

    return env


def _filter_stderr(stderr: str) -> str:
    """Remove known-safe KiCad warnings from stderr, keep real errors."""
    lines = stderr.split("\n")
    filtered = []
    for line in lines:
        if any(p.match(line) for p in _SAFE_STDERR_PATTERNS):
            continue
        if line.strip():
            filtered.append(line)
    return "\n".join(filtered).strip()


def run_pcbnew_script(script: str, timeout: float = 30.0) -> Dict[str, Any]:
    """Run a Python script using KiCad's Python with pcbnew available.

    The script MUST print a single JSON object to stdout as its final output.
    Any other stdout output should be avoided; use stderr for logging.

    Args:
        script: Python source code to execute.
        timeout: Maximum execution time in seconds.

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

    logger.debug("Executing pcbnew script %s (timeout=%.1fs)", script_path, timeout)
    start_time = time.monotonic()

    try:
        env = _get_kicad_env()
        result = subprocess.run(
            [kicad_python, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )

        elapsed = time.monotonic() - start_time

        if result.returncode != 0:
            error_msg = _filter_stderr(result.stderr)
            logger.error(
                "pcbnew script failed (exit %d, %.2fs): %s",
                result.returncode,
                elapsed,
                error_msg[:2000],
            )
            raise RuntimeError(
                f"pcbnew script failed (exit {result.returncode}): {error_msg}"
            )

        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError("pcbnew script produced no output")

        # Take the last line as JSON (in case of stray prints)
        json_line = stdout.split("\n")[-1]
        parsed = json.loads(json_line)

        logger.debug("pcbnew script completed in %.2fs", elapsed)
        return parsed

    except subprocess.TimeoutExpired:
        logger.error("pcbnew script timed out after %.1fs", timeout)
        raise RuntimeError(f"pcbnew script timed out after {timeout}s")
    except json.JSONDecodeError as e:
        logger.error(
            "pcbnew script output is not valid JSON: %s\nOutput: %s",
            e,
            result.stdout[:2000],
        )
        raise RuntimeError(
            f"pcbnew script output is not valid JSON: {e}\n"
            f"Output was: {result.stdout[:2000]}"
        )
    finally:
        os.unlink(script_path)
