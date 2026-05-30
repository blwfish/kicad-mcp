"""Offline card auto-draft (Phase 7) — propose a device card for an unrecognized
peripheral from signals ALREADY on the user's machine, with **no network, ever**:

  * the bundled I2C-address → identity table (pure data),
  * `*_WHO_AM_I_EXPECTED` hints already parsed into the intent's provenance,
  * symbol-pin-name matching against the *locally installed* KiCad symbol
    (role token == pin name → identity), reusing ``resolve_pin_token``.

Honest-by-construction: a draft is **offered for review**, never silently placed.
Confidence is conservative — `high` requires two independent signals agreeing
(the address-identity AND a clean name-identity role match on a single-unit
symbol); anything weaker is `low` and flagged. The cold-review false-confidence
hazard (a pin *named* SCL that isn't an I2C clock) is mitigated by requiring
address↔symbol corroboration, not name-existence alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from kicad_mcp.utils.firmware.mcu_pinmap import resolve_pin_token
from kicad_mcp.utils.firmware.parse import canonical_type
from kicad_mcp.utils.firmware.power_names import (
    is_control_pin,
    is_ground_pin,
    is_nc_pin,
    is_supply_pin,
)

# --- bundled I2C address → canonical device type (curated, offline data) ------
# The address space is a strong identity signal. A range/list per type; the most
# common maker-firmware I2C parts. Extend by data, not code.
ADDRESS_IDENTITY: dict[int, str] = {
    0x68: "MPU6050", 0x69: "MPU6050",          # MPU-6xxx / ICM IMUs
    0x3C: "OLED", 0x3D: "OLED",                # SSD1306/SSD1315 OLED
    0x76: "BME280", 0x77: "BME280",            # BME/BMP280 env sensors
    0x40: "INA219",                            # current/power monitor
    0x48: "ADS1115",                           # 4-ch ADC
    0x20: "MCP23017", 0x27: "MCP23017",        # GPIO expander (strap range 0x20-0x27)
    0x29: "VL53L0X",                           # ToF distance
    0x23: "BH1750",                            # ambient light
}

# Supply/ground/control pin-name classification lives in power_names (Rule 3).
_WHOAMI_RE = re.compile(r"^(?P<type>[A-Z0-9]+?)_WHO_?AM_?I(_EXPECTED)?$")


@dataclass
class DraftCard:
    """A proposed card + provenance. ``confidence`` is 'high' | 'low'. Never
    auto-placed — the caller surfaces it for confirm."""
    card: dict[str, Any]
    confidence: str
    reasons: list[str] = field(default_factory=list)


def identify_by_address(address: Optional[int]) -> Optional[str]:
    """Return the likely canonical device type for an I2C address, or None."""
    if address is None:
        return None
    return ADDRESS_IDENTITY.get(address)


def extract_whoami(provenance: dict[str, Any]) -> dict[str, int]:
    """Pull ``<TYPE>_WHO_AM_I_EXPECTED`` (a free identity confirmation) out of the
    intent's provenance ``unparsed`` macros. Returns ``{TYPE: expected_int}``."""
    out: dict[str, int] = {}
    for m in provenance.get("unparsed", []) or []:
        mm = _WHOAMI_RE.match(str(m.get("name", "")))
        if not mm:
            continue
        raw = str(m.get("value", "")).strip().rstrip("uUlL")
        try:
            out[mm.group("type")] = int(raw, 0)
        except ValueError:
            continue
    return out


def _symbol_pin_names(symbol: Any) -> list[str]:
    return [str(getattr(p, "name", "") or "") for p in (getattr(symbol, "pins", None) or ())]


def _symbol_unit_count(symbol: Any) -> int:
    """Best-effort unit count; >1 means multi-unit (ambiguous pin lookup)."""
    return int(getattr(symbol, "unit_count", 1) or 1)


def score_roles(symbol: Any, roles: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Resolve each firmware role to a symbol pin NUMBER by NAME identity. Returns
    ``(resolved_role->pin, unresolved_roles)``. Identity only: a role resolves
    iff a pin is *named* exactly like the role (so module-header numbered pins do
    NOT auto-resolve — they stay a hand-card, which is safe)."""
    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    names = set(_symbol_pin_names(symbol))
    for role in roles:
        if role in names:
            num = resolve_pin_token(symbol, role)
            if num is not None:
                resolved[role] = role          # role == pin NAME (identity)
                continue
        unresolved.append(role)
    return resolved, unresolved


def _unexplained_pins(symbol: Any, resolved_roles: dict[str, str]) -> list[str]:
    """Symbol I/O pins that are neither a matched role nor a power/ground/NC pin
    nor a leave-floating control output (INT/DRDY/ALERT) — i.e. nothing the draft
    can account for. A non-empty list weakens confidence (the symbol does more
    than the firmware bus, so identity is uncertain)."""
    out: list[str] = []
    for nm in _symbol_pin_names(symbol):
        if not nm or nm in resolved_roles:
            continue
        if (is_supply_pin(nm) or is_ground_pin(nm) or is_nc_pin(nm)
                or is_control_pin(nm)):
            continue
        out.append(nm)
    return out


def draft_card(
    *,
    type_name: str,
    address: Optional[int],
    bus: Optional[str],
    roles: dict[str, str],
    symbol: Any = None,
    footprint: Optional[str] = None,
    lib_id: Optional[str] = None,
    whoami: Optional[dict[str, int]] = None,
) -> Optional[DraftCard]:
    """Synthesize a draft card for ``type_name``.

    ``roles`` are the firmware bus roles seen for this device (e.g. {SDA, SCL}).
    ``symbol`` (if provided — needs locally-installed KiCad) lets us name-match
    roles to pins and classify the rest. Returns None if we can't even propose a
    skeleton. Confidence is **high** only when the address-identity corroborates
    the type AND every role name-matches a pin on a single-unit symbol with no
    unexplained signal pins; otherwise **low** (flagged for review).
    """
    reasons: list[str] = []
    addr_type = identify_by_address(address)
    if addr_type:
        reasons.append(f"I2C address {hex(address or 0)} → likely {addr_type}")
    if whoami and type_name in whoami:
        reasons.append(f"WHO_AM_I expects {hex(whoami[type_name])}")

    role_map: dict[str, str] = dict(roles)
    confidence = "low"

    if symbol is not None and lib_id:
        resolved, unresolved = score_roles(symbol, roles)
        unexplained = _unexplained_pins(symbol, resolved)
        multiunit = _symbol_unit_count(symbol) > 1
        corroborated = addr_type is not None and canonical_type(addr_type) == canonical_type(type_name)
        if resolved:
            role_map = resolved
        if (not unresolved and corroborated and not multiunit
                and not unexplained and resolved):
            confidence = "high"
            reasons.append("all roles name-match a single-unit symbol; address corroborates")
        else:
            why = []
            if unresolved:
                why.append(f"roles without a name-matched pin: {unresolved}")
            if not corroborated:
                why.append("address does not corroborate the type")
            if multiunit:
                why.append("multi-unit symbol (ambiguous pins)")
            if unexplained:
                why.append(f"unexplained symbol pins: {unexplained[:8]}")
            reasons.append("flagged: " + "; ".join(why))
    else:
        reasons.append("flagged: no local symbol available — skeleton only, "
                       "confirm lib_id/footprint/roles before use")

    if not role_map and not addr_type:
        return None

    card: dict[str, Any] = {
        "type": canonical_type(type_name),
        "lib_id": lib_id or "TODO:confirm",
        "value": type_name,
        "bus": bus,
        "footprint": footprint or "TODO:confirm",
        "roles": role_map,
        "supply_pins": [],     # confirm against the symbol/datasheet
        "ground_pins": [],
        "module": False,
        "_draft": {"confidence": confidence, "reasons": reasons},
    }
    return DraftCard(card=card, confidence=confidence, reasons=reasons)
