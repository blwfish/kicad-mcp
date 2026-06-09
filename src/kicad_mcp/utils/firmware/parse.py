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

# An I2C device-address macro name: ``<TYPE>_ADDR[ESS][ _<n> | <n> ]``. The
# optional trailing instance qualifier distinguishes multiple chips of the SAME
# type on one bus (``MPU6050_ADDR`` 0x68, ``MPU6050_ADDR_2`` 0x69). This is the
# SINGLE SOURCE OF TRUTH (CLAUDE.md Rule 3) for "is this an address-macro name,
# and what base type does it name" — both the classifier here and the importer
# (intent._peripheral_type_from_addr) consume it, so neither re-encodes the rule.
_ADDR_NAME_RE = re.compile(r"(?P<base>.+?)_(?:ADDRESS|ADDR)(?:_?\d+)?$")


def address_base(name: str) -> Optional[str]:
    """Return the base peripheral type if ``name`` is an address-macro name
    (``MPU6050_ADDR`` / ``MPU6050_ADDR_2`` -> ``MPU6050``), else None."""
    m = _ADDR_NAME_RE.match(name)
    return m.group("base") if m else None


def canonical_type(name: str) -> str:
    """Canonical peripheral-type KEY — UPPER, alphanumerics only (``MPU-6050`` ->
    ``MPU6050``). Single source (Rule 3) so firmware (``MPU6050_ADDR`` →
    ``address_base`` → ``MPU6050``), card lookup, auto-draft and pre-fetch all
    agree — a symbol named with a dash must resolve the same as the macro."""
    return re.sub(r"[^A-Z0-9]", "", name.upper())

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
#   _GENERIC_ROLES  — control pins with no bus (INT, EN, RST, …)
#
# GAIN / ADC / DAC are deliberately NOT role tokens: they name an amplifier/codec
# CONFIG setting (`MAX_GAIN 12`, `AUDIO_DAC 5`) far more often than a pin, and the
# 0..48 value gate can't tell a gain-in-dB from a GPIO. A bare token is therefore
# too weak to be load-bearing alone (CLAUDE.md seam rule). An EXPLICIT `_PIN`/
# `_GPIO` suffix still classifies them as pins and recovers the role via the
# legacy peripheral/role split (e.g. `PIEZO_ADC_PIN` -> role ADC); the MAX98357A
# GAIN pin is template-owned, so nothing real is lost.
_BUS_ROLES = {
    "SDA": "I2C", "SCL": "I2C",
    "BCLK": "I2S", "LRC": "I2S", "LRCK": "I2S", "WS": "I2S", "MCLK": "I2S",
    "MOSI": "SPI", "MISO": "SPI",
}
_AMBIGUOUS_ROLES = frozenset({"DIN", "DOUT", "SD", "SCK", "CS", "SS",
                              "RX", "TX", "RXD", "TXD"})
_GENERIC_ROLES = frozenset({"INT", "INTA", "INTB", "EN", "RST", "RESET",
                            "BUSY", "DC", "BL"})
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


_GPIO_NUM_RE = re.compile(r"^GPIO_NUM_(\d+)$")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _as_int(raw: str) -> Optional[int]:
    """Parse an integer literal (decimal or ``0x``), tolerating C int suffixes, a
    single surrounding ``(...)``, a ``/* */`` block comment, and ESP-IDF
    ``GPIO_NUM_<n>``. Returns None for floats, strings, expressions — i.e. "not a
    plain int". (Previously these idioms were silently dropped, downgrading a
    clearly-pin-named macro to OTHER.)"""
    s = _BLOCK_COMMENT_RE.sub("", raw).strip()
    if len(s) >= 2 and s.startswith("(") and s.endswith(")"):   # unwrap (4) -> 4
        s = s[1:-1].strip()
    m = _GPIO_NUM_RE.match(s)
    if m:
        return int(m.group(1))
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
    if address_base(name) is not None:
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


# A C++ const/constexpr declaration — how Arduino sketches name pins when they
# don't use #define (``const int LED = 2;`` / ``constexpr uint8_t SDA = 21;`` /
# ``static const gpio_num_t MIC = GPIO_NUM_15;``). Captures the type then the whole
# declarator list up to the ``;``; each comma-separated ``NAME = VALUE`` is then
# value-validated by the SAME classifier as #define. A pointer/string type
# (``const char* SSID``) has ``*`` where this expects a declarator, so it never
# matches — strings are skipped, not misread. ``int x = 2`` (no const/constexpr)
# is deliberately NOT matched — a mutable global is not a pin constant.
_CONST_DECL_RE = re.compile(
    r"^\s*(?:static\s+)?(?:const|constexpr)\s+(?:unsigned\s+)?"
    r"\w+\s+(?P<decls>[A-Za-z_][^;]*);"
)
# One ``NAME = VALUE`` declarator. A bare fragment (``PINS[]``, a comma-split piece
# of a function-call value) has no scalar ``=`` here and is skipped.
_DECLARATOR_RE = re.compile(r"\s*(?P<name>[A-Za-z_]\w*)\s*=\s*(?P<value>.+?)\s*$")


def parse_const_decls(text: str) -> list[Macro]:
    """Extract C++ ``const``/``constexpr`` declarations and classify them with the
    SAME name-primary classifier as ``#define`` (CLAUDE.md Rule 3 — one source of
    truth for "is this a pin", two syntactic feeders). A const whose NAME says pin
    and whose value is a plausible GPIO is a pin; ``const int TIMEOUT = 5000`` is
    OTHER (name doesn't say pin), so the classifier is the safety net against
    over-matching. One statement may declare several comma-separated names
    (``const int TIMEOUT = 5000, LED_PIN = 2;``) — split so a pin after the first
    is NOT silently dropped. Order-preserving."""
    out: list[Macro] = []
    for i, line in enumerate(text.splitlines(), start=1):
        m = _CONST_DECL_RE.match(line)
        if not m:
            continue
        for part in m.group("decls").split(","):
            dm = _DECLARATOR_RE.match(part)
            if dm is None:
                continue
            value, comment = _split_comment(dm.group("value"))
            macro = classify(dm.group("name"), value, None)
            macro.line_no = i
            macro.comment = comment
            out.append(macro)
    return out


def _blank_block_comments(text: str) -> str:
    """Replace ``/* */`` block comments with blank lines (line numbers preserved),
    so a commented-out pin block isn't parsed as live macros."""
    return _BLOCK_COMMENT_RE.sub(lambda m: "\n" * m.group(0).count("\n"), text)


def parse_macros(text: str) -> list[Macro]:
    """Every pin/address/config macro in firmware text — both ``#define`` and
    ``const``/``constexpr`` declarations (the latter is how Arduino-IDE sketches
    name pins). Block comments are stripped first so a commented-out pinout isn't
    read as live pins; both syntactic sources then feed the one classifier (order:
    defines, then const-decls)."""
    text = _blank_block_comments(text)
    return parse_defines(text) + parse_const_decls(text)


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


# An esp32 id with a variant-FAMILY suffix — ``esp32`` then an optional separator
# then a C/S/H/P family letter + digit (``esp32c2``, ``esp32-s4``, ``esp32h2``).
# Used to FAIL CLOSED on a variant we have no target for, so it can't alias to
# classic ESP32 (a silently-wrong board). The family set is exactly C/S/H/P (every
# real ESP32 variant); a classic die/board name with a different letter
# (``esp32-cam``, ``esp32-d0wd``, ``esp32-pico``, ``esp32dev``) is NOT a variant
# and falls through to the classic target.
_ESP32_VARIANT_RE = re.compile(r"esp32[-_]?[cshp]\d")


def idf_target_defines(board_id: Optional[str]) -> set[str]:
    """Map a platformio board id / Arduino FQBN to the ESP-IDF target macro it
    defines, so the preprocessor selects the matching pin block (and so
    ``resolve_mcu``'s target guard rejects a variant that isn't classic/S3)."""
    b = (board_id or "").lower()
    for key, target in (("s3", "ESP32S3"), ("c3", "ESP32C3"), ("c6", "ESP32C6"),
                        ("s2", "ESP32S2"), ("h2", "ESP32H2"), ("p4", "ESP32P4")):
        if key in b:
            return {f"CONFIG_IDF_TARGET_{target}"}
    # Unknown esp32 variant (esp32c2, esp32x9, …): fail closed rather than alias to
    # classic ESP32 — the substring `"esp32" in b` can't tell a new die from the
    # classic one (the dangerous case of the C3/C6 silently-wrong-board seam).
    if _ESP32_VARIANT_RE.search(b):
        return set()
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
