"""Platform-specific configuration for KiCad integration.

Detects KiCad installation paths, footprint library locations, and
provides common constants used across tool modules.
"""

import logging
import os
import platform

logger = logging.getLogger(__name__)

system = platform.system()

# --- KiCad installation paths ---

if system == "Darwin":
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = os.environ.get("KICAD_APP_PATH", "/Applications/KiCad/KiCad.app")
    KICAD_CLI = os.path.join(KICAD_APP_PATH, "Contents/MacOS/kicad-cli")
    KICAD_PYTHON = os.path.join(
        KICAD_APP_PATH,
        "Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9",
    )
    FOOTPRINT_DIRS = [
        os.path.join(KICAD_APP_PATH, "Contents/SharedSupport/footprints"),
        os.path.expanduser("~/Documents/KiCad/footprints"),
    ]
elif system == "Windows":
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = r"C:\Program Files\KiCad"
    KICAD_CLI = os.path.join(KICAD_APP_PATH, "bin", "kicad-cli.exe")
    KICAD_PYTHON = os.path.join(KICAD_APP_PATH, "bin", "python.exe")
    FOOTPRINT_DIRS = [
        os.path.join(KICAD_APP_PATH, "share", "kicad", "footprints"),
        os.path.expanduser("~/Documents/KiCad/footprints"),
    ]
elif system == "Linux":
    KICAD_USER_DIR = os.path.expanduser("~/KiCad")
    KICAD_APP_PATH = "/usr/share/kicad"
    KICAD_CLI = "kicad-cli"  # expected on PATH
    KICAD_PYTHON = "/usr/bin/python3"  # pcbnew typically installed system-wide
    FOOTPRINT_DIRS = [
        "/usr/share/kicad/footprints",
        os.path.expanduser("~/KiCad/footprints"),
    ]
else:
    # Default to macOS paths
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = os.environ.get("KICAD_APP_PATH", "/Applications/KiCad/KiCad.app")
    KICAD_CLI = os.path.join(KICAD_APP_PATH, "Contents/MacOS/kicad-cli")
    KICAD_PYTHON = ""
    FOOTPRINT_DIRS = []


# --- Additional search paths from environment ---

def _exists_or_false(path: str) -> bool:
    """os.path.exists() at module-import time, on a mount/permission-sensitive
    path list -- an OSError here (a stale network mount, a permission-denied
    parent directory, a TOCTOU race) used to be completely unguarded and
    would crash the import of this module for every single tool call, not
    just skip that one candidate path."""
    try:
        return os.path.exists(path)
    except OSError:
        return False


def _resolve_env_search_paths(
    env_value: str, exists_check=_exists_or_false
) -> tuple[list[str], int]:
    """Resolve KICAD_SEARCH_PATHS' comma-separated entries against the
    filesystem. Returns (found_paths, dropped_count) so a caller can warn
    when the env var was SET but some/all of its entries don't exist --
    previously this was a silent filter with no way to distinguish "env var
    unset" (nothing to warn about) from "the user configured this and every
    entry is wrong" (worth a warning). Auto-detected candidate locations
    (below) are speculative guesses, not user-configured paths, so a miss
    there is expected and not counted here."""
    found: list[str] = []
    dropped = 0
    for p in env_value.split(","):
        expanded = os.path.expanduser(p.strip())
        if exists_check(expanded):
            found.append(expanded)
        else:
            dropped += 1
    return found, dropped


ADDITIONAL_SEARCH_PATHS: list[str] = []
env_paths = os.environ.get("KICAD_SEARCH_PATHS", "")
if env_paths:
    _found, _dropped = _resolve_env_search_paths(env_paths)
    ADDITIONAL_SEARCH_PATHS.extend(_found)
    if _dropped:
        logger.warning(
            "KICAD_SEARCH_PATHS: %d of %d configured path(s) do not exist "
            "and were skipped: %s",
            _dropped, _dropped + len(_found), env_paths,
        )


# Auto-detect common project locations
for loc in [
    "~/Documents/PCB",
    "~/PCB",
    "~/Electronics",
    "~/Projects/Electronics",
    "~/Projects/PCB",
    "~/Projects/KiCad",
]:
    expanded = os.path.expanduser(loc)
    if _exists_or_false(expanded) and expanded not in ADDITIONAL_SEARCH_PATHS:
        ADDITIONAL_SEARCH_PATHS.append(expanded)


# --- KiCad file extensions ---

KICAD_EXTENSIONS = {
    "project": ".kicad_pro",
    "pcb": ".kicad_pcb",
    "schematic": ".kicad_sch",
    "design_rules": ".kicad_dru",
    "footprint": ".kicad_mod",
    "netlist": "_netlist.net",
}

# --- Default component libraries ---

COMMON_LIBRARIES = {
    "basic": {
        "resistor": {"library": "Device", "symbol": "R"},
        "capacitor": {"library": "Device", "symbol": "C"},
        "inductor": {"library": "Device", "symbol": "L"},
        "led": {"library": "Device", "symbol": "LED"},
        "diode": {"library": "Device", "symbol": "D"},
    },
    "power": {
        "vcc": {"library": "power", "symbol": "VCC"},
        "gnd": {"library": "power", "symbol": "GND"},
        "+5v": {"library": "power", "symbol": "+5V"},
        "+3v3": {"library": "power", "symbol": "+3V3"},
    },
}

DEFAULT_FOOTPRINTS = {
    "R": [
        "Resistor_SMD:R_0805_2012Metric",
        "Resistor_SMD:R_0603_1608Metric",
        "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    ],
    "C": [
        "Capacitor_SMD:C_0805_2012Metric",
        "Capacitor_SMD:C_0603_1608Metric",
        "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
    ],
    "LED": ["LED_SMD:LED_0805_2012Metric", "LED_THT:LED_D5.0mm"],
}

# --- Data file extensions ---

DATA_EXTENSIONS = [
    ".csv",  # BOM or other data
    ".pos",  # Component position file
    ".net",  # Netlist files
    ".zip",  # Gerber files and other archives
    ".drl",  # Drill files
]

# --- Timeouts ---

TIMEOUT_KICAD_CLI = 30.0
TIMEOUT_PCBNEW = 30.0
TIMEOUT_ZONE_FILL = 60.0

TIMEOUT_CONSTANTS = {
    "kicad_cli_version_check": 10.0,
    "kicad_cli_export": 30.0,
    "application_open": 10.0,
    "subprocess_default": 30.0,
}

# CLI startup-validation retry: on a cold/busy CI runner the first --version
# invocation can transiently fail (timeout or nonzero exit) before the binary
# warms up. Retry a few times with a short backoff so the CLI is not falsely
# treated as absent (which would fail the firmware integration suite fast with
# a downstream KeyError: 'status').
KICAD_CLI_VALIDATE_ATTEMPTS = 3
KICAD_CLI_VALIDATE_BACKOFF = 0.5  # seconds between attempts
