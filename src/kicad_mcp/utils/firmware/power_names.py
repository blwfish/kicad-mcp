"""Single source of truth (CLAUDE.md Rule 3) for classifying a symbol PIN NAME as
supply / ground / leave-floating-control / no-connect, and for the set of power
rail net names. Previously these regexes + the ``_RAILS`` set were duplicated
across ``autodraft``, ``prefetch``, ``sidecar`` and ``cards`` and drifted (the
supply pattern missed ``VDDIO``/``VDDA`` — the names most real I2C IMUs use, so
the pre-fetch never auto-synthesized them). Everything consumes this module now.
"""
from __future__ import annotations

import re

# Power rail NET names (not pin names) recognized across the front end.
RAILS = frozenset({"+3V3", "+5V", "GND", "VBUS", "VCC", "+1V8", "+12V"})

# Supply pin NAMES. The ``V{DD,CC}`` families allow an arbitrary suffix so
# VDDIO / VDDA / VDD_IO / VCCIO / V_{DD} all match; plus explicit + numeric rails.
_SUPPLY_RE = re.compile(
    r"^[+]?(?:"
    r"V_?\{?DD\}?[A-Z0-9_]*"      # VDD, VDDIO, VDDA, VDD_IO, V_{DD}
    r"|V_?\{?CC\}?[A-Z0-9_]*"     # VCC, VCCIO
    r"|A?VDD[A-Z0-9_]*|DVDD|IOVDD|VDDD"
    r"|VBAT|VIN|VLOGIC|VREGIN|VS"
    r"|\d+V\d*"                   # 3V3, 5V, 1V8
    r")$",
    re.I,
)

# Ground pin NAMES, incl. analog/digital/IO variants and exposed-pad.
_GROUND_RE = re.compile(
    r"^(?:"
    r"V_?\{?SS\}?[A-Z0-9_]*"      # VSS, VSSIO, V_{SS}
    r"|[ADP]?GND[A-Z0-9_]*"       # GND, GNDA, GNDIO, AGND, DGND, PGND
    r"|EP|EPAD|PAD"               # exposed thermal pad tied to GND
    r")$",
    re.I,
)

# Output/strap control pins that a *draft* card may legitimately leave
# unconnected (interrupt/data-ready/alert outputs) — they don't block a clean
# high-confidence synthesis. NOT included: clock/sync inputs and address straps
# (CLKIN/FSYNC/AD0/A0..A2/CS) which genuinely need wiring → keep those flagged.
_CONTROL_RE = re.compile(r"^(?:n?INT[AB0-9]*|DRDY|N?DRDY|ALERT|OS|GPOUT|READY)$", re.I)

_NC_RE = re.compile(r"^(?:NC|DNC|DNI|~|N/?C)\d*$", re.I)


def is_supply_pin(name: str) -> bool:
    return bool(_SUPPLY_RE.match(name.strip()))


def is_ground_pin(name: str) -> bool:
    return bool(_GROUND_RE.match(name.strip()))


def is_control_pin(name: str) -> bool:
    return bool(_CONTROL_RE.match(name.strip()))


def is_nc_pin(name: str) -> bool:
    return not name.strip() or bool(_NC_RE.match(name.strip()))
