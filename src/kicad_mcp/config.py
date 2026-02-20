"""Platform-specific configuration for KiCad integration.

Detects KiCad installation paths, footprint library locations, and
provides common constants used across tool modules.
"""

import os
import platform

system = platform.system()

# --- KiCad installation paths ---

if system == "Darwin":
    KICAD_USER_DIR = os.path.expanduser("~/Documents/KiCad")
    KICAD_APP_PATH = "/Applications/KiCad/KiCad.app"
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
    KICAD_APP_PATH = "/Applications/KiCad/KiCad.app"
    KICAD_CLI = os.path.join(KICAD_APP_PATH, "Contents/MacOS/kicad-cli")
    KICAD_PYTHON = ""
    FOOTPRINT_DIRS = []


# --- Additional search paths from environment ---

ADDITIONAL_SEARCH_PATHS: list[str] = []
env_paths = os.environ.get("KICAD_SEARCH_PATHS", "")
if env_paths:
    for p in env_paths.split(","):
        expanded = os.path.expanduser(p.strip())
        if os.path.exists(expanded):
            ADDITIONAL_SEARCH_PATHS.append(expanded)

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
    if os.path.exists(expanded) and expanded not in ADDITIONAL_SEARCH_PATHS:
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
