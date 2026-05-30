"""``#define`` extraction + classification — the single source of truth for
"what kind of thing is this macro" (CLAUDE.md Rule 3).

The hard lesson from real firmware (mr-esp32 speed-cal ``config.h``): a macro's
*value* cannot classify it. The file is full of GPIO-range integers that are NOT
pins (``NUM_SENSORS 6``, ``MAX_SENSORS 16``, ``AUDIO_DMA_BUF_COUNT 4``) and hex
values that are NOT I2C addresses (the ``MCP_IODIRA 0x00`` … ``MCP_GPIOB 0x13``
register table). So:

  * The **name** decides candidacy: a macro is a *pin* only if its name ends in
    ``_PIN`` / ``_GPIO`` or is a known bare alias (``I2C_SDA`` / ``I2C_SCL``); an
    *address* only if its name ends in ``_ADDR`` / ``_ADDRESS``.
  * The **value** only *validates* (a pin's value must be a plausible GPIO int;
    an address's value must be an int).

Every macro is returned with a disposition. Nothing is silently dropped — macros
we don't model are kept as ``MacroKind.OTHER`` with their raw text (data-capture
rule), so a caller can surface them in ``provenance.unparsed``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ESP32 GPIO numbers span 0..48 across the family (classic ~0..39, S2/S3 up to
# 48). We accept the generous upper bound here as a *plausibility* gate; the
# authoritative check is whether the MCU symbol actually exposes an IO{n} pin,
# which the generator does later. A pin-named macro whose value is outside this
# range (e.g. the common ``-1`` "unused" sentinel) is kept but flagged.
GPIO_MIN = 0
GPIO_MAX = 48

# Bare signal names that are pins despite lacking a ``_PIN`` suffix. This is an
# explicit, audited allow-list — NOT a naming-convention guess. speed-cal uses
# bare ``I2C_SDA`` / ``I2C_SCL``; track-geometry uses ``I2C_SDA_PIN`` (caught by
# the suffix rule). Extend deliberately, with evidence.
BARE_PIN_ALIASES = frozenset({"I2C_SDA", "I2C_SCL"})

_PIN_SUFFIXES = ("_PIN", "_GPIO")
_ADDR_SUFFIXES = ("_ADDR", "_ADDRESS")

# Bus prefixes recognized in a pin macro's name -> canonical bus id.
_BUS_PREFIXES = {
    "I2C": "I2C",
    "I2S": "I2S",
    "SPI": "SPI",
    "UART": "UART",
}

# --- signal-role tokens: the curated SINGLE SOURCE OF TRUTH (CLAUDE.md Rule 3)
# for "this name segment names a pin signal". A macro whose last meaningful
# segment is a role token is a pin candidate (then value-validated). This
# generalizes pin detection beyond the `_PIN` suffix to project conventions like
# `CMCA_I2S_BCLK` — WITHOUT loosening into config (`_SAMPLE_RATE`, `_BITS`,
# `_VOLUME`, `MAX_CHANNELS` have no role-token last segment, so stay OTHER).
#
# Roles split by how strongly they imply a bus:
#   _BUS_ROLES      — unambiguous (SDA→I2C, BCLK→I2S, MOSI→SPI)
#   _AMBIGUOUS_ROLES — data/clock/serial that depend on context (DOUT is HX711
#                      data OR I2S; SD is mic data OR SD-card). Per-pin bus stays
#                      None; the bus TYPE is decided when pins are grouped.
#   _GENERIC_ROLES  — control pins with no bus (INT, EN, GAIN, ADC, …)
_BUS_ROLES = {
    "SDA": "I2C", "SCL": "I2C",
    "BCLK": "I2S", "LRC": "I2S", "LRCK": "I2S", "WS": "I2S", "MCLK": "I2S",
    "MOSI": "SPI", "MISO": "SPI",
}
_AMBIGUOUS_ROLES = frozenset({"DIN", "DOUT", "SD", "SCK", "CS", "SS",
                              "RX", "TX", "RXD", "TXD"})
_GENERIC_ROLES = frozenset({"INT", "INTA", "INTB", "EN", "RST", "RESET",
                            "GAIN", "ADC", "DAC", "BUSY", "DC", "BL"})
_ALL_ROLES = frozenset(_BUS_ROLES) | _AMBIGUOUS_ROLES | _GENERIC_ROLES

# Trailing instance qualifiers stripped before locating the role token, so
# `..._GAIN_BUS0` / `..._DIN_CH1` / `..._OUT_L` resolve to role GAIN/DIN/OUT.
_INSTANCE_QUALIFIER_RE = re.compile(r"BUS\d+|CH\d+|[LR]|\d+")

# #define NAME[(args)] value... [// comment]   (object-like only; function-like
# macros — NAME immediately followed by '(' — are captured but flagged so we
# never treat ``MIN(a,b)`` as a value.)
_DEFINE_RE = re.compile(
    r"^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)(?P<args>\([^)]*\))?"
    r"(?:[ \t]+(?P<value>[^\n]*?))?[ \t]*$"
)
_TRAILING_COMMENT_RE = re.compile(r"(?P<value>.*?)\s*//(?P<comment>.*)$")


class MacroKind(str, Enum):
    PIN = "pin"
    ADDRESS = "address"
    OTHER = "other"          # numeric/string config, registers, counts, etc.
    FUNCTION = "function"    # function-like macro, not a value
    EMPTY = "empty"          # include guard / value-less define


@dataclass
class Macro:
    """One ``#define`` and its classification. ``raw_value`` is always retained
    verbatim so nothing is lost even when we don't model the macro."""
    name: str
    raw_value: str
    line_no: int
    comment: str = ""
    kind: MacroKind = MacroKind.OTHER
    # Populated for PIN: the validated GPIO number, or None if the name says
    # "pin" but the value isn't a plausible GPIO (kept + flagged).
    gpio: Optional[int] = None
    pin_value_valid: bool = True
    # Populated for ADDRESS: the integer address (hex or decimal source).
    address: Optional[int] = None
    # Populated for PIN: parsed (peripheral_hint, signal_role, bus) from the name.
    peripheral_hint: Optional[str] = None
    signal_role: Optional[str] = None
    bus: Optional[str] = None
    # Reason a pin-named macro was rejected, for provenance.
    note: str = ""


def _as_int(raw: str) -> Optional[int]:
    """Parse an integer literal (decimal or ``0x``), tolerating C int suffixes.
    Returns None for floats, strings, expressions — i.e. "not a plain int"."""
    s = raw.strip()
    # Reject obvious non-ints fast (strings, floats with a dot, multi-token).
    if not s or '"' in s or "'" in s or "." in s or " " in s:
        return None
    s = s.rstrip("uUlL")
    try:
        return int(s, 0)
    except ValueError:
        return None


def _split_comment(value_field: str) -> tuple[str, str]:
    m = _TRAILING_COMMENT_RE.match(value_field)
    if m:
        return m.group("value").strip(), m.group("comment").strip()
    return value_field.strip(), ""


def _strip_pin_suffix(name: str) -> str:
    for suf in _PIN_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def _bus_prefix(seg: str) -> Optional[str]:
    """A name segment like ``I2S`` / ``I2S2`` / ``UART1`` -> its bus id."""
    base = seg.rstrip("0123456789")
    return _BUS_PREFIXES.get(base)


def extract_role(name: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(role_token, stem)`` if the name's last meaningful segment is a
    known role token, else ``(None, None)``. ``stem`` is the name with the role
    token (and any trailing instance qualifier and ``_PIN`` suffix) removed —
    the grouping key for a bus instance or a peripheral.

      ``CMCA_I2S_BCLK``      -> ("BCLK", "CMCA_I2S")
      ``CMCA_AMP_GAIN_BUS0`` -> ("GAIN", "CMCA_AMP")
      ``HX711_DOUT_PIN``     -> ("DOUT", "HX711")
      ``I2C_SDA``            -> ("SDA", "I2C")
      ``BUZZER_PIN``         -> (None, None)   # no role token
    """
    parts = _strip_pin_suffix(name).split("_")
    while len(parts) > 1 and _INSTANCE_QUALIFIER_RE.fullmatch(parts[-1]):
        parts.pop()
    if parts and parts[-1] in _ALL_ROLES:
        role = parts[-1]
        stem = "_".join(parts[:-1]) or None
        return role, stem
    return None, None


def _bus_for(role: Optional[str], stem: Optional[str]) -> Optional[str]:
    """Per-pin bus: an explicit bus-prefix segment in the stem wins; else the
    role's unambiguous implied bus; else None (ambiguous/generic — the bus TYPE
    is decided later when pins are grouped)."""
    if stem:
        for seg in stem.split("_"):
            b = _bus_prefix(seg)
            if b is not None:
                return b
    if role is not None and role in _BUS_ROLES:
        return _BUS_ROLES[role]
    return None


def parse_pin_name(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Split a pin macro name into ``(peripheral_hint/stem, signal_role, bus)``.

    Best-effort hint, validated downstream. Uses the role-token extractor; falls
    back to the legacy first-segment split for ``_PIN`` names with no role token.
    """
    role, stem = extract_role(name)
    if role is not None:
        bus = _bus_for(role, stem)
        # A bare bus prefix (``I2C`` in ``I2C_SDA``) is the bus, not a peripheral.
        if stem and "_" not in stem and _bus_prefix(stem) is not None:
            stem = None
        return (stem, role, bus)
    # No role token — a _PIN/_GPIO name with a bare peripheral (e.g. BUZZER_PIN).
    parts = _strip_pin_suffix(name).split("_")
    if parts and parts[0] in _BUS_PREFIXES:
        return (None, "_".join(parts[1:]) or None, _BUS_PREFIXES[parts[0]])
    if len(parts) == 1:
        return (parts[0], None, None)
    return (parts[0], "_".join(parts[1:]), None)


def classify(name: str, value: str, args: Optional[str]) -> Macro:
    """Classify a single macro. Pure; name-primary, value-validating."""
    if args:
        return Macro(name=name, raw_value=value, line_no=0,
                     kind=MacroKind.FUNCTION, note="function-like macro")
    if value == "":
        return Macro(name=name, raw_value="", line_no=0, kind=MacroKind.EMPTY)

    # ADDRESS: name says address AND value is an int.
    if name.endswith(_ADDR_SUFFIXES):
        ival = _as_int(value)
        if ival is not None:
            return Macro(name=name, raw_value=value, line_no=0,
                         kind=MacroKind.ADDRESS, address=ival)
        return Macro(name=name, raw_value=value, line_no=0,
                     kind=MacroKind.OTHER,
                     note="name suggests address but value is not an integer")

    # PIN: name says pin (suffix, bare alias, OR a signal-role token) — value
    # validates. The role-token path generalizes to project conventions without
    # a `_PIN` suffix (e.g. CMCA_I2S_BCLK).
    role_tok, _ = extract_role(name)
    is_pin_named = (name.endswith(_PIN_SUFFIXES) or name in BARE_PIN_ALIASES
                    or role_tok is not None)
    if is_pin_named:
        ival = _as_int(value)
        peripheral, role, bus = parse_pin_name(name)
        if ival is None:
            return Macro(name=name, raw_value=value, line_no=0,
                         kind=MacroKind.OTHER, peripheral_hint=peripheral,
                         signal_role=role, bus=bus,
                         note="name suggests pin but value is not an integer")
        valid = GPIO_MIN <= ival <= GPIO_MAX
        return Macro(
            name=name, raw_value=value, line_no=0, kind=MacroKind.PIN,
            gpio=ival, pin_value_valid=valid,
            peripheral_hint=peripheral, signal_role=role, bus=bus,
            note="" if valid else f"value {ival} outside GPIO range "
                                  f"[{GPIO_MIN},{GPIO_MAX}] (unused/-1?)",
        )

    return Macro(name=name, raw_value=value, line_no=0, kind=MacroKind.OTHER)


def parse_defines(text: str) -> list[Macro]:
    """Extract and classify every ``#define`` in ``text``. Order-preserving;
    every macro is returned (nothing dropped)."""
    out: list[Macro] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _DEFINE_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        args = m.group("args")
        raw_value_field = m.group("value") or ""
        value, comment = _split_comment(raw_value_field)
        macro = classify(name, value, args)
        macro.line_no = i
        macro.comment = comment
        out.append(macro)
    return out


# --- preprocessor: select active #if branches --------------------------------
# Scoped to the dominant firmware pattern — target conditionals
# (``#if CONFIG_IDF_TARGET_ESP32S3 / #else``) and simple ``#ifdef`` /
# ``#if defined(X)`` / bare-identifier tests against a known ``defines`` set.
# Complex expressions (``&&``, comparisons) are unknown -> take the #if branch.

_PP_RE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")


def _eval_cond(directive: str, arg: str, defines: set[str]) -> Optional[bool]:
    arg = re.sub(r"//.*$", "", arg).strip()
    if directive == "ifdef":
        return arg.split()[0] in defines if arg else False
    if directive == "ifndef":
        return arg.split()[0] not in defines if arg else True
    m = re.fullmatch(r"(!)?\s*defined\s*\(\s*(\w+)\s*\)", arg)
    if m:
        v = m.group(2) in defines
        return (not v) if m.group(1) else v
    m = re.fullmatch(r"(!)?\s*(\w+)", arg)
    if m:
        v = m.group(2) in defines
        return (not v) if m.group(1) else v
    return None  # complex/unknown


def select_active_branches(text: str, defines: set[str]) -> str:
    """Keep only the active branches of ``#if/#elif/#else/#endif`` given the
    macros in ``defines``. Directive lines and inactive branches are removed so
    a later ``#define`` scan sees one consistent target."""
    out: list[str] = []
    stack: list[dict[str, bool]] = []
    for line in text.splitlines(keepends=True):
        m = _PP_RE.match(line)
        if m is None:
            if all(s["active"] for s in stack):
                out.append(line)
            continue
        d, arg = m.group(1), m.group(2)
        if d in ("if", "ifdef", "ifndef"):
            parent = all(s["active"] for s in stack)
            cond = _eval_cond(d, arg, defines)
            active = parent and (True if cond is None else cond)
            stack.append({"active": active, "taken": active, "parent": parent})
        elif d == "elif" and stack:
            top = stack[-1]
            if top["taken"]:
                top["active"] = False
            else:
                cond = _eval_cond("if", arg, defines)
                a = top["parent"] and (True if cond is None else cond)
                top["active"] = a
                top["taken"] = a
        elif d == "else" and stack:
            top = stack[-1]
            top["active"] = top["parent"] and not top["taken"]
            top["taken"] = True
        elif d == "endif" and stack:
            stack.pop()
    return "".join(out)


def idf_target_defines(board_id: Optional[str]) -> set[str]:
    """Map a platformio board id to the ESP-IDF target macro it defines, so the
    preprocessor selects the matching pin block."""
    b = (board_id or "").lower()
    for key, target in (("s3", "ESP32S3"), ("c3", "ESP32C3"),
                        ("c6", "ESP32C6"), ("s2", "ESP32S2")):
        if key in b:
            return {f"CONFIG_IDF_TARGET_{target}"}
    if "esp32" in b:
        return {"CONFIG_IDF_TARGET_ESP32"}
    return set()


@dataclass
class ParsedFirmware:
    """The structured result of parsing one firmware header."""
    pins: list[Macro] = field(default_factory=list)          # valid PIN macros
    addresses: list[Macro] = field(default_factory=list)     # ADDRESS macros
    invalid_pins: list[Macro] = field(default_factory=list)  # pin-named, bad value
    other: list[Macro] = field(default_factory=list)         # everything retained


def partition(macros: list[Macro]) -> ParsedFirmware:
    """Sort classified macros into the buckets the importer consumes. Invalid
    pins are split out (kept, flagged) rather than mixed with valid ones."""
    pf = ParsedFirmware()
    for mac in macros:
        if mac.kind is MacroKind.PIN and mac.pin_value_valid:
            pf.pins.append(mac)
        elif mac.kind is MacroKind.PIN:
            pf.invalid_pins.append(mac)
        elif mac.kind is MacroKind.ADDRESS:
            pf.addresses.append(mac)
        else:
            pf.other.append(mac)
    return pf
