"""Phase 0 safety-net invariants (roadmap) — unit tier, property-based.

Hypothesis properties that must hold for ANY input and run every PR (no KiCad).
Two of the four Phase-0 invariants live here; the two that need a real KiCad
build — status-honesty end-to-end and firmware-as-spec connectivity — are in
tests/integration/test_firmware_invariants.py.

  1. No-silent-loss / no-crash — every macro the parser sees lands in exactly one
     disposition bucket and is retained downstream; parse/partition/build_intent
     never raise on arbitrary input. The CLAUDE.md data-capture rule made
     executable: a macro is modeled, gapped, or unparsed — never "didn't notice".
  2. Round-trip — to_dict(from_dict(to_dict(i))) == to_dict(i) for intents built
     from fuzzed firmware AND directly constructed (covering expander_terminals /
     placements / connector_legends the firmware parser never emits).
"""
from __future__ import annotations

import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kicad_mcp.utils.firmware.intent import (
    NET_KINDS,
    SCHEMA_VERSION,
    Bus,
    ConnectorLegend,
    DesignIntent,
    Endpoint,
    ExpanderSpec,
    Gap,
    Mcu,
    Net,
    Peripheral,
    Placement,
    build_intent,
    from_dict,
    to_dict,
)
from kicad_mcp.utils.firmware.parse import (
    MacroKind,
    parse_const_decls,
    parse_defines,
    parse_macros,
    partition,
)

# build_intent does device-card resolution (yaml-backed, no live KiCad) so it is
# unit-safe but not free; drop the per-example deadline rather than chase timing.
_SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
# A wider budget for the conservation/retention property: its interesting branches
# (duplicate_signal, bus nets) are rare draws — but they're also pinned by the
# deterministic tests below, so this just adds breadth.
_SETTINGS_MANY = settings(deadline=None, max_examples=300,
                          suppress_health_check=[HealthCheck.too_slow])


# --- firmware-text strategy: tokens that exercise every classifier branch, plus
#     adversarial junk so the no-crash claim is real ----------------------------
_PIN_NAMES = ["I2C_SDA", "I2C_SCL", "HX711_DOUT_PIN", "HX711_SCK_PIN",
              "BUZZER_PIN", "LED_GPIO", "CMCA_I2S_BCLK", "MIC_SD_PIN",
              "BUTTON_PIN", "SENSOR0_PIN"]
_ADDR_NAMES = ["MPU6050_ADDR", "MPU6050_ADDR_2", "MCP23017_ADDRESS", "BME280_ADDR"]
_OTHER_NAMES = ["NUM_SENSORS", "MAX_GAIN", "AUDIO_DMA_BUF_COUNT", "FW_VERSION",
                "MCP_IODIRA", "WIFI_SSID", "SAMPLE_RATE"]

_names = st.one_of(
    st.sampled_from(_PIN_NAMES + _ADDR_NAMES + _OTHER_NAMES),
    st.text(alphabet=string.ascii_uppercase + string.digits + "_",
            min_size=1, max_size=20),
)
_values = st.one_of(
    st.integers(min_value=-5, max_value=60).map(str),        # ints, incl -1 sentinel / out-of-range
    st.integers(min_value=0, max_value=0x7F).map(hex),       # hex (addresses, registers)
    st.sampled_from(["", "3.3", '"str"', "GPIO_NUM_5", "(4)", "1u", "A | B", "0x68"]),
    st.text(max_size=12),
)


@st.composite
def _define_line(draw):
    name = draw(_names)
    if draw(st.booleans()):                                  # function-like macro sometimes
        return f"#define {name}(x) ((x) + 1)"
    return f"#define {name} {draw(_values)}".rstrip()


# Coordinated project-convention bus groups: a shared stem + role tokens that
# _bus_type recognizes -> a TYPED bus whose pins are consumed as bus-stem (the
# subtle term of the conservation law; random single defines never form one).
_BUS_GROUPS = [
    ("SENSOR", ["SDA", "SCL"]),            # I2C
    ("AUDIO", ["BCLK", "WS", "DIN"]),      # I2S_OUT
    ("MIC", ["BCLK", "WS", "SD"]),         # I2S_IN
    ("RADIO", ["MOSI", "MISO", "SCK"]),    # SPI
    ("CONSOLE", ["RX", "TX"]),             # UART
]


@st.composite
def _bus_group_lines(draw):
    stem, roles = draw(st.sampled_from(_BUS_GROUPS))
    base = draw(st.integers(min_value=0, max_value=40))
    return [f"#define {stem}_{r} {base + i}" for i, r in enumerate(roles)]


_junk_line = st.one_of(
    st.just(""), st.just("// comment"), st.just("int x = 5;"),
    st.just("#include <stdio.h>"), st.just("#ifdef FOO"), st.just("#endif"),
    st.text(max_size=30),
)
# A "block" is a single line or a whole bus group; flatten to firmware text.
_block = st.one_of(
    _define_line().map(lambda s: [s]),
    _junk_line.map(lambda s: [s]),
    _bus_group_lines(),
)
_firmware_text = st.lists(_block, max_size=20).map(
    lambda blocks: "\n".join(line for b in blocks for line in b))
_boards = st.sampled_from(["esp32dev", "esp32-s3-devkitc-1", "uno", None, "zzz"])


# --- 1. no-silent-loss / no-crash -------------------------------------------

@given(st.text())
@_SETTINGS
def test_parse_partition_never_crash_on_arbitrary_text(text):
    partition(parse_defines(text))       # arbitrary unicode must not raise
    partition(parse_const_decls(text))   # the const/constexpr feeder too
    partition(parse_macros(text))        # and the combined source


@given(_firmware_text)
@_SETTINGS
def test_partition_conserves_every_macro(text):
    macros = parse_defines(text)
    pf = partition(macros)
    buckets = pf.pins + pf.invalid_pins + pf.addresses + pf.other
    # Exact conservation: each macro lands in exactly one bucket, none dropped or
    # duplicated. Identity (id) — not just count — so a copy-and-lose can't pass.
    assert len(buckets) == len(macros)
    assert {id(m) for m in buckets} == {id(m) for m in macros}
    # Each bucket honors its documented predicate (no misfiled macro).
    assert all(m.kind is MacroKind.PIN and m.pin_value_valid for m in pf.pins)
    assert all(m.kind is MacroKind.PIN and not m.pin_value_valid for m in pf.invalid_pins)
    assert all(m.kind is MacroKind.ADDRESS for m in pf.addresses)


@given(_firmware_text, _boards)
@_SETTINGS_MANY
def test_build_intent_no_crash_and_no_silent_loss(text, board):
    pf = partition(parse_defines(text))
    intent = build_intent(pf, firmware_path="config.h", board_id=board)  # must not raise

    # OTHER macros retained verbatim in provenance.unparsed (data-capture core).
    assert intent.provenance["unparsed_count"] == len(pf.other)
    assert ([u["name"] for u in intent.provenance["unparsed"]]
            == [m.name for m in pf.other])

    # Invalid pins retained as gaps, one apiece.
    assert sum(1 for g in intent.gaps if g.kind == "invalid_pin") == len(pf.invalid_pins)

    # Pin conservation law: every net in intent.nets comes 1:1 from the pin loop,
    # so every valid pin became exactly one net, OR was consumed by a typed bus
    # (bus-stem), OR — if not bus-stem — was dropped as a duplicate WITH a gap.
    # Nothing vanishes: a stray `continue` that skips a pin without recording it
    # breaks this equality.
    bus_stems = {b.name for b in intent.buses}
    bus_stem_pins = sum(1 for m in pf.pins if m.peripheral_hint in bus_stems)
    dup_gaps = sum(1 for g in intent.gaps if g.kind == "duplicate_signal")
    assert len(intent.nets) + bus_stem_pins + dup_gaps == len(pf.pins)


@pytest.mark.parametrize("defs,stem,btype", [
    (["SENSOR_SDA 21", "SENSOR_SCL 22"], "SENSOR", "I2C"),
    (["MIC_BCLK 5", "MIC_WS 6", "MIC_SD 7"], "MIC", "I2S_IN"),
])
def test_typed_bus_pins_conserved_as_bus_stem(defs, stem, btype):
    # Deterministic coverage of the conservation law's bus-stem term: a typed bus
    # forms and consumes ALL its pins (nets==0), so the fuzz above can't pass that
    # branch vacuously even when no random example happens to build a bus.
    pf = partition(parse_defines("\n".join(f"#define {d}" for d in defs)))
    intent = build_intent(pf, firmware_path="config.h", board_id="esp32dev")
    assert any(b.name == stem and b.type == btype for b in intent.buses)
    bus_stem_pins = sum(1 for m in pf.pins
                        if m.peripheral_hint in {b.name for b in intent.buses})
    assert bus_stem_pins == len(pf.pins) > 0
    dup_gaps = sum(1 for g in intent.gaps if g.kind == "duplicate_signal")
    assert len(intent.nets) + bus_stem_pins + dup_gaps == len(pf.pins)


@pytest.mark.parametrize("defs,expected_dups", [
    (["FOO_PIN 5", "FOO_PIN 10"], 1),     # same stripped name -> 2nd is a duplicate
    (["BAR_GPIO 3", "BAR_PIN 7"], 1),     # _GPIO and _PIN both strip to "BAR"
])
def test_duplicate_signal_gap_conserved(defs, expected_dups):
    # Deterministic coverage of the conservation law's duplicate term: the fuzz hits
    # duplicate_signal <1% of the time (a rare stripped-name collision), so that term
    # is ~never exercised there. Pin it: a duplicate pin is dropped WITH a gap, and
    # the law still balances.
    pf = partition(parse_defines("\n".join(f"#define {d}" for d in defs)))
    intent = build_intent(pf, firmware_path="config.h", board_id="esp32dev")
    dup_gaps = sum(1 for g in intent.gaps if g.kind == "duplicate_signal")
    assert dup_gaps == expected_dups
    bus_stem_pins = sum(1 for m in pf.pins
                        if m.peripheral_hint in {b.name for b in intent.buses})
    assert len(intent.nets) + bus_stem_pins + dup_gaps == len(pf.pins)


def test_invalid_pin_gap_emitted_not_dropped():
    # invalid_pins is empty in ~4/5 fuzz examples, making the retention assertion
    # trivially true there; pin it deterministically (-1 sentinel + out-of-range).
    pf = partition(parse_defines("#define BUZZER_PIN -1\n#define LED_GPIO 99"))
    assert len(pf.invalid_pins) == 2
    intent = build_intent(pf, firmware_path="config.h", board_id="esp32dev")
    assert sum(1 for g in intent.gaps if g.kind == "invalid_pin") == 2


# --- 2. round-trip ----------------------------------------------------------

@given(_firmware_text, _boards)
@_SETTINGS
def test_round_trip_firmware_built_intent(text, board):
    intent = build_intent(partition(parse_defines(text)),
                          firmware_path="config.h", board_id=board)
    d = to_dict(intent)
    assert to_dict(from_dict(d)) == d


_ref = st.text(alphabet=string.ascii_uppercase + string.digits, min_size=1, max_size=4)
_opt_str = st.one_of(st.none(), st.text(max_size=8))

_endpoint = st.builds(
    Endpoint, ref=_ref,
    gpio=st.one_of(st.none(), st.integers(min_value=0, max_value=48)),
    role=_opt_str, pin=_opt_str,
)
_net = st.builds(
    Net, name=st.text(min_size=1, max_size=8),
    kind=st.sampled_from(sorted(NET_KINDS)),
    confidence=st.sampled_from(["high", "low"]),
    endpoints=st.lists(_endpoint, max_size=3),
    bus=_opt_str, origin=st.sampled_from(["imported", "template", "user"]),
)
_bus = st.builds(
    Bus, name=st.text(min_size=1, max_size=8),
    type=st.sampled_from(["I2C", "I2S_OUT", "I2S_IN", "SPI", "UART"]),
    signals=st.dictionaries(st.sampled_from(["SDA", "SCL", "BCLK", "WS", "MOSI"]),
                            st.integers(min_value=0, max_value=48), max_size=4),
    address=st.one_of(st.none(), st.integers(min_value=0, max_value=0x7F)),
    origin=st.sampled_from(["imported", "template", "user"]),
    resolved_part=_opt_str,
    part_provenance=_opt_str,
    part_is_assumption=st.booleans(),
)
_expander = st.builds(
    ExpanderSpec, device=st.text(min_size=1, max_size=6),
    ports=st.lists(st.sampled_from(["GPA0", "GPA1", "GPB7"]), max_size=3),
    group=st.sampled_from(["per_sensor", "shared"]),
    power=st.sampled_from(["3v3", "5v"]), net_prefix=st.text(max_size=4),
)
_placement = st.builds(
    Placement,
    locus=st.sampled_from(["on_board", "remote", "on_board_with_remote_io"]),
    device=_opt_str, connector=st.sampled_from(["screw_terminal", "jst"]),
    footprint=_opt_str, external_io=st.lists(st.text(max_size=4), max_size=3),
)
_legend = st.builds(
    ConnectorLegend, ref=_ref,
    positions=st.lists(st.text(max_size=4), max_size=3), device=st.text(max_size=6),
)
# Directly-built intent: deliberately populates the fields the firmware parser
# NEVER emits (buses with non-empty signals, expander_terminals, placements,
# connector_legends, placement_hints) so the round-trip covers their serialization
# too — a Bus field renamed without updating _only_fields would surface here.
_constructed_intent = st.builds(
    DesignIntent,
    schema_version=st.just(SCHEMA_VERSION),
    source=st.just({}),
    mcu=st.one_of(st.none(), st.builds(
        Mcu, ref=_ref, part=st.text(max_size=8), lib_id=st.text(max_size=8),
        footprint=_opt_str)),
    peripherals=st.lists(st.builds(
        Peripheral, ref=_ref, type=st.text(min_size=1, max_size=6),
        lib_id=_opt_str), max_size=3),
    buses=st.lists(_bus, max_size=3),
    nets=st.lists(_net, max_size=4),
    gaps=st.lists(st.builds(Gap, kind=st.text(min_size=1, max_size=6),
                            detail=st.text(max_size=8)), max_size=3),
    connector_legends=st.lists(_legend, max_size=2),
    placements=st.dictionaries(_ref, _placement, max_size=2),
    placement_hints=st.dictionaries(_ref, st.fixed_dictionaries({
        "edge": st.sampled_from(["top", "bottom", "left", "right"]),
        "rotation": st.sampled_from([0, 90, 180, 270]),
    }), max_size=2),
    expander_terminals=st.dictionaries(_ref, _expander, max_size=2),
    provenance=st.just({}),
)


@given(_constructed_intent)
@_SETTINGS
def test_round_trip_constructed_intent(intent):
    d = to_dict(intent)
    assert to_dict(from_dict(d)) == d
