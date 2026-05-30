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


def parse_pin_name(name: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Split a pin macro name into ``(peripheral_hint, signal_role, bus)``.

    Heuristic and best-effort — the result is a *hint* the generator validates
    against real symbols; it is never load-bearing on its own.

      ``I2C_SDA``            -> (None, "SDA", "I2C")
      ``I2S_SCK_PIN``        -> (None, "SCK", "I2S")
      ``HX711_DOUT_PIN``     -> ("HX711", "DOUT", None)
      ``MCP23017_INT_PIN``   -> ("MCP23017", "INT", None)
      ``PIEZO_ADC_PIN``      -> ("PIEZO", "ADC", None)
    """
    base = name
    for suf in _PIN_SUFFIXES:
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    parts = base.split("_")
    if not parts:
        return (None, None, None)
    if parts[0] in _BUS_PREFIXES:
        bus = _BUS_PREFIXES[parts[0]]
        role = "_".join(parts[1:]) or None
        return (None, role, bus)
    if len(parts) == 1:
        # Just a peripheral name with no role, e.g. ``BUZZER_PIN``.
        return (parts[0], None, None)
    # First token = peripheral hint, remainder = role.
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

    # PIN: name says pin (suffix or bare alias) — value validates.
    is_pin_named = name.endswith(_PIN_SUFFIXES) or name in BARE_PIN_ALIASES
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
