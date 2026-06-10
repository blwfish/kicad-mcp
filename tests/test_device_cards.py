"""Tests for the device-card layer (loader, structural validator, strap math,
resolution parity). No KiCad needed — pin-existence against real symbols is the
integration tier (tests/integration/test_device_cards.py).

Boundary-focused per CLAUDE.md: the strap math and the validator's accept/reject
edges are pinned at/below/above, and the migration's equivalence is frozen by a
snapshot of the 6 entries' exact field values.
"""
from __future__ import annotations

import textwrap

import pytest

from kicad_mcp.utils.firmware import knowledge as K
from kicad_mcp.utils.firmware.cards import (
    CardError,
    compute_address_straps,
    load_cards,
    recognized_part_names,
    validate_mcu_card,
    validate_peripheral_card,
)


# --- compute_address_straps: single source, reproduces both legacy fns --------

_MCP_STRAP = {"pin_bits": ["A0", "A1", "A2"], "base": 0x20,
              "rail_set": "+3V3", "rail_clear": "GND"}
_MPU_STRAP = {"pin_bits": ["5"], "base": 0x68, "rail_set": "+3V3", "rail_clear": "GND"}


@pytest.mark.parametrize("addr,expected", [
    (0x20, [("A0", "GND"), ("A1", "GND"), ("A2", "GND")]),        # base
    (0x27, [("A0", "+3V3"), ("A1", "+3V3"), ("A2", "+3V3")]),     # max
    (0x24, [("A0", "GND"), ("A1", "GND"), ("A2", "+3V3")]),       # mixed
    (0x21, [("A0", "+3V3"), ("A1", "GND"), ("A2", "GND")]),       # lsb only
    (0x1F, None),                                                 # one below base
    (0x28, None),                                                 # one above max
])
def test_strap_mcp_3bit(addr, expected):
    assert compute_address_straps(_MCP_STRAP, addr) == expected


@pytest.mark.parametrize("addr,expected", [
    (0x68, [("5", "GND")]),     # base
    (0x69, [("5", "+3V3")]),    # base + 1 (max for 1 bit)
    (0x67, None),               # one below
    (0x6A, None),               # one above
])
def test_strap_mpu_1bit(addr, expected):
    assert compute_address_straps(_MPU_STRAP, addr) == expected


def test_strap_matches_backcompat_wrappers():
    # the knowledge.py wrappers must reproduce the helper exactly
    for a in (0x20, 0x24, 0x27, 0x1F, 0x28):
        assert K.mcp23017_address_straps(a) == compute_address_straps(_MCP_STRAP, a)
    for a in (0x68, 0x69, 0x67, 0x6A):
        helper = compute_address_straps(_MPU_STRAP, a)
        assert K.mpu6050_ad0_strap(a) == (helper[0] if helper else None)


# --- structural validation ----------------------------------------------------

_GOOD_PERIPHERAL = {
    "type": "FOO", "lib_id": "Lib:Foo", "value": "Foo", "bus": "I2C",
    "footprint": "FP:Foo", "roles": {"SDA": "4"}, "supply_pins": ["2"],
    "ground_pins": ["1"], "module": True,
}
_GOOD_MCU = {
    "part": "FOO-MCU", "chip": "esp32", "lib_id": "Lib:Foo", "value": "Foo", "footprint": "FP:Foo",
    "board_match": ["foo"], "needs_3v3": True, "supply_pin": "VDD",
    "ground_pin": "GND", "en_pin": "EN", "boot_pin": "IO0",
    "uart_rx_pin": "RX", "uart_tx_pin": "TX", "native_usb": False,
}


def test_good_cards_validate_clean():
    assert validate_peripheral_card(_GOOD_PERIPHERAL) == []
    assert validate_mcu_card(_GOOD_MCU) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda c: c.pop("lib_id"), "missing required"),
    (lambda c: c.pop("bus"), "missing required"),   # was a KeyError before the fix
    (lambda c: c.update(lib_id="NoColon"), "lib_id"),
    (lambda c: c.update(bus="SDIO"), "bus"),
    (lambda c: c.update(module="yes"), "module must be a bool"),
])
def test_bad_peripheral_rejected(mutate, needle):
    card = dict(_GOOD_PERIPHERAL)
    mutate(card)
    errs = validate_peripheral_card(card)
    assert errs and any(needle in e for e in errs)


@pytest.mark.parametrize("strap,ok", [
    ({"pin_bits": ["A0"], "base": 0x20}, True),
    ({"pin_bits": [], "base": 0x20}, False),          # empty pin_bits
    ({"pin_bits": ["A0"], "base": "0x20"}, False),    # base not int
    ({"pin_bits": ["A0"], "base": 1, "rail_set": "+9V"}, False),  # bad rail
])
def test_address_strap_validation(strap, ok):
    card = dict(_GOOD_PERIPHERAL, config={"address_strap": strap})
    errs = validate_peripheral_card(card)
    assert (errs == []) is ok


# --- aliases / serves (part-resolution registry fields) ----------------------

@pytest.mark.parametrize("aliases,ok", [
    (None, True),                       # optional — absent is fine
    ([], True),                         # empty list is fine
    (["SPH0645LM4H"], True),            # one alias
    (["A", "B"], True),                 # several
    ("SPH0645LM4H", False),             # a bare string is not a list
    ([""], False),                      # empty-string alias
    ([1], False),                       # non-string alias
])
def test_aliases_validation(aliases, ok):
    card = dict(_GOOD_PERIPHERAL, aliases=aliases)
    assert (validate_peripheral_card(card) == []) is ok


@pytest.mark.parametrize("serves,ok", [
    (None, True),                       # optional
    ("I2C", True),
    ("I2S_IN", True),                   # directional — the finer vocabulary
    ("I2S_OUT", True),
    ("I2S", False),                     # coarse bus name is NOT a valid serves
    ("SDIO", False),                    # unknown
])
def test_serves_validation(serves, ok):
    card = dict(_GOOD_PERIPHERAL, serves=serves)
    assert (validate_peripheral_card(card) == []) is ok


# --- alt_lib_ids + cross-version symbol resolution ---------------------------

@pytest.mark.parametrize("alts,ok", [
    (None, True),                                  # optional — absent is fine
    ([], True),                                    # empty list is fine
    (["Interface_Expansion:MCP23017_SO"], True),   # one older-KiCad name
    (["Lib:A", "Lib:B"], True),                    # several
    ("Lib:A", False),                              # a bare string is not a list
    (["NoColon"], False),                          # entry not a Library:Symbol
    ([""], False),                                 # empty entry
    ([1], False),                                  # non-string entry
])
def test_alt_lib_ids_validation(alts, ok):
    card = dict(_GOOD_PERIPHERAL, alt_lib_ids=alts)
    assert (validate_peripheral_card(card) == []) is ok


class _FakeCache:
    """Minimal symbol cache: a name resolves iff it is in ``known``."""
    def __init__(self, known):
        self.known = set(known)

    def get_symbol(self, name):
        return object() if name in self.known else None


@pytest.mark.parametrize("known,lib_id,alts,expected", [
    ({"Lib:New", "Lib:Old"}, "Lib:New", ["Lib:Old"], "Lib:New"),   # primary preferred
    ({"Lib:Old"},            "Lib:New", ["Lib:Old"], "Lib:Old"),   # MCP23017-on-KiCad-9: use the alt
    ({"Lib:Older"}, "Lib:New", ["Lib:Old", "Lib:Older"], "Lib:Older"),  # alts tried in order
    (set(),                  "Lib:New", ["Lib:Old"], None),        # nothing resolves
    ({"Lib:Old"},            None,      ["Lib:Old"], "Lib:Old"),   # None primary skipped
    ({"Lib:Old"},            "",        ["Lib:Old"], "Lib:Old"),   # empty primary skipped
    (set(),                  None,      [],          None),        # nothing to try
])
def test_resolve_symbol(known, lib_id, alts, expected):
    name, sym = K.resolve_symbol(_FakeCache(known), lib_id, alts)
    assert name == expected
    assert (sym is not None) == (expected is not None)


# --- pin-field registry completeness (meta-gate; no KiCad) -------------------
# The integration gate (test_card_pins_exist_on_symbol) only validates pins it is
# TOLD about — via cards.peripheral_pin_refs, driven by the *_PIN_FIELDS registry.
# These meta-gates keep that registry honest so a new pin-bearing field can't slip
# past the symbol check the way `port_pins` did before it was wired in.

def test_pin_field_registries_are_disjoint():
    from kicad_mcp.utils.firmware.cards import (
        MCU_NONPIN_FIELDS,
        MCU_PIN_FIELDS,
        PERIPHERAL_NONPIN_FIELDS,
        PERIPHERAL_PIN_FIELDS,
    )
    assert not (set(PERIPHERAL_PIN_FIELDS) & set(PERIPHERAL_NONPIN_FIELDS))
    assert not (set(MCU_PIN_FIELDS) & set(MCU_NONPIN_FIELDS))


def test_every_card_field_is_classified_pin_or_nonpin():
    """A field on any real card that's in NEITHER registry fails here — forcing
    whoever adds a card field to decide: pin-bearing (→ PERIPHERAL_PIN_FIELDS, and
    the symbol gate then validates it) or not. This is the meta-gate that would
    have caught the port_pins omission."""
    from kicad_mcp.utils.firmware.cards import (
        CONFIG_SUBKEYS,
        MCU_NONPIN_FIELDS,
        MCU_PIN_FIELDS,
        PERIPHERAL_NONPIN_FIELDS,
        PERIPHERAL_PIN_FIELDS,
    )
    peris, mcus = load_cards()
    p_known = set(PERIPHERAL_PIN_FIELDS) | set(PERIPHERAL_NONPIN_FIELDS) | {"config"}
    for t, c in peris.items():
        extra = set(c) - p_known
        assert not extra, (
            f"peripheral card {t!r}: unclassified field(s) {sorted(extra)}. If they "
            f"reference symbol pins add them to PERIPHERAL_PIN_FIELDS (the symbol "
            f"gate then validates them); otherwise to PERIPHERAL_NONPIN_FIELDS.")
        extra_cfg = set(c.get("config") or {}) - set(CONFIG_SUBKEYS)
        assert not extra_cfg, (
            f"peripheral card {t!r}: config sub-key(s) {sorted(extra_cfg)} not "
            f"handled — extend peripheral_pin_refs + CONFIG_SUBKEYS.")
    m_known = set(MCU_PIN_FIELDS) | set(MCU_NONPIN_FIELDS)
    for c in mcus:
        extra = set(c) - m_known
        assert not extra, (
            f"mcu card {c.get('part')!r}: unclassified field(s) {sorted(extra)} — "
            f"add to MCU_PIN_FIELDS or MCU_NONPIN_FIELDS.")


def test_meta_gate_is_not_vacuous():
    # An injected unknown field is in neither registry, so the meta-gate's
    # set-difference is non-empty (it would fail). Guards against a gate that
    # passes no matter what.
    from kicad_mcp.utils.firmware.cards import (
        PERIPHERAL_NONPIN_FIELDS,
        PERIPHERAL_PIN_FIELDS,
    )
    known = set(PERIPHERAL_PIN_FIELDS) | set(PERIPHERAL_NONPIN_FIELDS) | {"config"}
    assert set(dict(_GOOD_PERIPHERAL, mystery_pins=["X"])) - known == {"mystery_pins"}


def test_port_pins_collected_regression():
    """The original bug: port_pins was added to the card schema but omitted from
    the integration collector, so its pins went unvalidated. Pin it: port_pins is
    a classified pin field AND peripheral_pin_refs actually collects it."""
    from kicad_mcp.utils.firmware.cards import (
        PERIPHERAL_PIN_FIELDS,
        peripheral_pin_refs,
    )
    assert "port_pins" in PERIPHERAL_PIN_FIELDS
    refs = peripheral_pin_refs({"port_pins": ["GPA0", "GPB7"], "roles": {"SDA": "13"},
                                "supply_pins": ["9"], "config": {
                                    "address_strap": {"pin_bits": ["A0"]},
                                    "static_ties": [{"pin": "~{RESET}", "rail": "+3V3"}]}})
    assert {"GPA0", "GPB7", "13", "9", "A0", "~{RESET}"} <= set(refs)


def test_peripheral_pin_refs_collects_every_registry_field():
    """C1 collector-completeness: the meta-gate above asserts every card field is
    *classified*; this asserts every PIN field the registry NAMES is actually
    *collected* by peripheral_pin_refs. The port_pins bug was a classified pin field
    the collector skipped — classification completeness alone wouldn't have caught
    it. A distinct sentinel per field; each must surface. The set-equality asserts
    are tripwires: a NEW registry/config member that isn't exercised here fails
    loudly, forcing the author to extend both the collector and this test."""
    from kicad_mcp.utils.firmware.cards import (
        CONFIG_SUBKEYS,
        PERIPHERAL_PIN_FIELDS,
        peripheral_pin_refs,
    )
    card = {
        "roles": {"R": "PIN_roles"},
        "supply_pins": ["PIN_supply_pins"],
        "ground_pins": ["PIN_ground_pins"],
        "port_pins": ["PIN_port_pins"],
    }
    assert set(card) == set(PERIPHERAL_PIN_FIELDS), \
        "new PERIPHERAL_PIN_FIELDS member not exercised — add a sentinel here"
    config = {
        "address_strap": {"pin_bits": ["PIN_address_strap"]},
        "static_ties": [{"pin": "PIN_static_ties", "rail": "+3V3"}],
    }
    assert set(config) == set(CONFIG_SUBKEYS), \
        "new CONFIG_SUBKEYS member not exercised — extend the collector + this test"
    refs = set(peripheral_pin_refs({**card, "config": config}))
    for f in PERIPHERAL_PIN_FIELDS:
        assert f"PIN_{f}" in refs, f"{f}: registry pin field NOT collected"
    for k in CONFIG_SUBKEYS:
        assert f"PIN_{k}" in refs, f"config.{k}: subkey NOT collected"


def test_mcu_pin_refs_collects_every_registry_field():
    """C1 collector-completeness for the MCU side: every MCU_PIN_FIELDS member must
    be collected by mcu_pin_refs (guards a refactor from the registry-driven loop to
    a hardcoded list)."""
    from kicad_mcp.utils.firmware.cards import MCU_PIN_FIELDS, mcu_pin_refs
    card = {f: f"PIN_{f}" for f in MCU_PIN_FIELDS}
    refs = set(mcu_pin_refs(card))
    for f in MCU_PIN_FIELDS:
        assert f"PIN_{f}" in refs, f"{f}: registry pin field NOT collected"


def test_pin_refs_keep_falsy_but_valid_pin_drop_empty():
    """Boundary (CLAUDE.md threshold rule): a pin spelled `0` is valid and must
    survive; an absent/empty pin is dropped. The peripheral and MCU collectors
    must agree — `mcu_pin_refs` used to filter the raw value, silently dropping
    int `0` while keeping `"0"`."""
    from kicad_mcp.utils.firmware.cards import mcu_pin_refs, peripheral_pin_refs
    prefs = peripheral_pin_refs({"supply_pins": [0, "", "9"]})
    assert "0" in prefs and "9" in prefs and "" not in prefs
    mrefs = mcu_pin_refs({"supply_pin": "VDD", "ground_pin": "GND", "en_pin": "EN",
                          "boot_pin": 0, "uart_rx_pin": "RX", "uart_tx_pin": ""})
    assert "0" in mrefs                 # boot_pin: 0 survives (dropped before the fix)
    assert "" not in mrefs              # empty uart_tx_pin dropped


def test_recognized_part_names_maps_raw_to_canonical():
    peripherals = {
        "INMP441": dict(_GOOD_PERIPHERAL, type="INMP441"),
        "SPH0645": dict(_GOOD_PERIPHERAL, type="SPH0645",
                        aliases=["SPH0645LM4H"]),
    }
    names = recognized_part_names(peripherals)
    # raw type names are present as keys, mapping to their canonical lookup key
    assert names["INMP441"] == "INMP441"
    assert names["SPH0645"] == "SPH0645"
    # aliases are recognized too, mapping to the card's canonical key
    assert names["SPH0645LM4H"] == "SPH0645"


def test_recognized_part_names_conflicting_alias_raises():
    # Two cards claiming the same name for different canonical keys is ambiguous
    # (load order is filesystem-dependent) → loud error, not silent last-write-wins.
    peripherals = {
        "FOO": dict(_GOOD_PERIPHERAL, type="FOO", aliases=["SHARED"]),
        "BAR": dict(_GOOD_PERIPHERAL, type="BAR", aliases=["SHARED"]),
    }
    with pytest.raises(CardError) as e:
        recognized_part_names(peripherals)
    assert "SHARED" in str(e.value)


def test_recognized_part_names_hyphenated_raw_preserved_I1():
    # I1: the regex must match the RAW hyphenated name in firmware text, while
    # the canonical key (hyphen stripped) is what the resolver looks up. Both the
    # hyphenated raw and its de-hyphenated alias must collide to one card key.
    peripherals = {
        "ICS43434": dict(_GOOD_PERIPHERAL, type="ICS-43434",
                         aliases=["ICS43434"]),
    }
    names = recognized_part_names(peripherals)
    assert "ICS-43434" in names                 # raw, hyphen intact for matching
    assert names["ICS-43434"] == "ICS43434"      # canonical lookup key
    assert names["ICS43434"] == "ICS43434"       # alias → same canonical key


# --- packaged library loads + the migrated 6 entries are present --------------

def test_packaged_cards_load():
    peris, mcus = load_cards()
    # Chip-down cards + module cards + terminal-only v1 cards.
    assert set(peris) >= {
        "MCP23017", "HX711", "MPU6050", "OLED", "ICS43434",
        "PIEZO", "SWITCH", "TCRT5000",
    }
    assert {m["part"] for m in mcus} >= {"ESP32-WROOM-32E", "ESP32-S3-WROOM-1",
                                         "RaspberryPi-Pico", "Arduino-Nano-v3"}


# Frozen snapshot — the migration must reproduce the old literals EXACTLY.
_EXPECTED = {
    "MCP23017": {
        "lib_id": "Interface_Expansion:MCP23017x-x-SO", "value": "MCP23017",
        # KiCad-9 fallback name (symbol family was renamed in KiCad 10).
        "alt_lib_ids": ["Interface_Expansion:MCP23017_SO"],
        "bus": "I2C", "footprint": "Package_SO:SOIC-28W_7.5x17.9mm_P1.27mm",
        "roles": {"SDA": "SDA", "SCL": "SCK", "INT": "INTA", "INTA": "INTA"},
        # Power pins by pad NUMBER (V_{DD}/V_{SS} in KiCad 10 are VDD/VSS in
        # KiCad 9; pads 9/10 are identical) so they resolve on both versions.
        "supply_pins": ["9"], "ground_pins": ["10"], "module": False,
    },
    "HX711": {
        "lib_id": "Analog_ADC:HX711", "value": "HX711", "bus": None,
        "footprint": "Package_SO:SOIC-16_3.9x9.9mm_P1.27mm",
        "roles": {"DOUT": "DOUT", "SCK": "PD_SCK", "PD_SCK": "PD_SCK"},
        "supply_pins": ["VSUP", "AVDD", "DVDD"], "ground_pins": ["AGND"],
        "module": False,
    },
    "MPU6050": {
        "lib_id": "Connector_Generic:Conn_01x05", "value": "GY-521 (MPU-6050)",
        "bus": "I2C",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical",
        "roles": {"SDA": "4", "SCL": "3"}, "supply_pins": ["2"],
        "ground_pins": ["1"], "module": True,
    },
    "OLED": {
        "lib_id": "Connector_Generic:Conn_01x04", "value": "OLED (SSD1306)",
        "bus": "I2C",
        "footprint": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
        "roles": {"SDA": "4", "SCL": "3"}, "supply_pins": ["2"],
        "ground_pins": ["1"], "module": True,
    },
}


@pytest.mark.parametrize("ptype", sorted(_EXPECTED))
def test_resolution_parity(ptype):
    info = K.resolve_peripheral(ptype)
    assert info is not None
    for k, v in _EXPECTED[ptype].items():
        assert info[k] == v, f"{ptype}.{k}"


def test_backcompat_constants_match_cards():
    # the mirror constants must equal the card data (so they can't drift)
    mcp = K.resolve_peripheral("MCP23017")
    mpu = K.resolve_peripheral("MPU6050")
    assert K.MCP23017_RESET_PIN == mcp["config"]["static_ties"][0]["pin"]
    assert K.MPU6050_AD0_PIN == mpu["config"]["address_strap"]["pin_bits"][0]


# --- MCU resolution (longest board_match, no hand-ordered precedence) ---------

@pytest.mark.parametrize("board,part", [
    ("esp32dev", "ESP32-WROOM-32E"),
    ("esp32", "ESP32-WROOM-32E"),
    ("esp32-s3-devkitc-1", "ESP32-S3-WROOM-1"),
    ("esp32-s3", "ESP32-S3-WROOM-1"),
    ("esp32s3", "ESP32-S3-WROOM-1"),
    ("esp32-s3-wroom", "ESP32-S3-WROOM-1"),   # substring: longest 'esp32-s3' wins
    ("esp32doit-devkit-v1", "ESP32-WROOM-32E"),  # fuzzy classic esp32, IDF agrees
    # h-resolve-mcu: C3/C6/S2 with no dedicated card must NOT fuzzy-match the
    # classic WROOM-32E via the bare 'esp32' substring — stay unknown.
    ("esp32-c3-devkitm-1", None),
    ("esp32-c6-devkitc-1", None),
    ("esp32-s2-saola-1", None),
    ("nonsense", None),
])
def test_resolve_mcu(board, part):
    info = K.resolve_mcu(board)
    assert (info["part"] if info else None) == part


# --- loader precedence + malformed handling -----------------------------------

def test_override_dir_takes_precedence(tmp_path):
    (tmp_path / "mpu_override.yaml").write_text(textwrap.dedent("""\
        type: MPU6050
        lib_id: Lib:Custom
        value: custom
        bus: I2C
        footprint: FP:Custom
        roles: {SDA: "4", SCL: "3"}
        supply_pins: ["2"]
        ground_pins: ["1"]
        module: true
    """))
    peris, _ = load_cards(extra_dirs=[str(tmp_path)])
    assert peris["MPU6050"]["lib_id"] == "Lib:Custom"   # override wins


def test_malformed_card_raises(tmp_path):
    (tmp_path / "bad.yaml").write_text("type: BAD\nlib_id: no_colon\n")
    with pytest.raises(CardError):
        load_cards(extra_dirs=[str(tmp_path)])


def test_neither_peripheral_nor_mcu_raises(tmp_path):
    (tmp_path / "huh.yaml").write_text("foo: bar\n")
    with pytest.raises(CardError):
        load_cards(extra_dirs=[str(tmp_path)])


def test_validate_mcu_card_rejects_chip_outside_vocabulary():
    # the chip vocabulary is CLOSED (parse.CHIP_TARGET_DEFINES) so two cards can
    # never spell one chip two ways and the guard always compares comparable ids.
    bad = dict(_GOOD_MCU, chip="rp2350")
    assert any("chip" in e and "vocabulary" in e for e in validate_mcu_card(bad))
    bad = dict(_GOOD_MCU, chip="ESP32")          # case matters: ids are lowercase
    assert any("chip" in e for e in validate_mcu_card(bad))
