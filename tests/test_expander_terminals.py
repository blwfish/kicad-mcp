"""Tests for the expander_terminals template: tap a placed MCP23017's floating
GPA/GPB pins out to labeled screw terminal(s), per a board.yaml declaration.
Behavior + edge cases; no KiCad needed (the data flow is pin-name based)."""
from __future__ import annotations


from kicad_mcp.utils.firmware.intent import (
    DesignIntent,
    Endpoint,
    Mcu,
    Net,
    Peripheral,
)
from kicad_mcp.utils.firmware.sidecar import BoardSidecar, apply_sidecar
from kicad_mcp.utils.firmware.templates import expand_intent


def _mcp_intent(**spec):
    """ESP32 + a placed MCP23017 (U3) on I2C; optionally apply a board.yaml
    expander_terminals spec for U3 (raw dict, as load_sidecar would produce)."""
    it = DesignIntent()
    it.mcu = Mcu(ref="U1", part="ESP32-WROOM-32E", lib_id="RF_Module:ESP32-WROOM-32E")
    it.peripherals = [Peripheral(
        ref="U3", type="MCP23017", lib_id="Interface_Expansion:MCP23017x-x-SO",
        alt_lib_ids=["Interface_Expansion:MCP23017_SO"], value="MCP23017", bus="I2C")]
    it.nets = [Net("I2C_SDA", "peripheral", "high",
                   [Endpoint(ref="U1", gpio=21), Endpoint(ref="U3", role="SDA")])]
    if spec:
        apply_sidecar(it, BoardSidecar(expander_terminals={"U3": spec}))
    return it


def _sensor_nets(intent, prefix):
    return [n for n in intent.nets if n.name.startswith(prefix + "_")]


def _terminals(intent, prefix):
    """The connector Peripherals this feature synthesized — identified by being an
    endpoint of one of its sensor nets (robust to other expand glue / USB block)."""
    refs = {e.ref for n in _sensor_nets(intent, prefix)
            for e in n.endpoints if e.ref.startswith("J")}
    return [p for p in intent.peripherals if p.ref in refs]


def test_per_sensor_one_terminal_per_port():
    it = _mcp_intent(device="TCRT5000", ports=6, group="per_sensor")
    expand_intent(it)
    terms = _terminals(it, "TCRT5000")
    assert len(terms) == 6                                          # N ports -> N terminals
    assert all(t.lib_id.endswith("Screw_Terminal_01x03") for t in terms)  # sig+3V3+GND
    nets = _sensor_nets(it, "TCRT5000")
    assert len(nets) == 6
    for i, n in enumerate(sorted(nets, key=lambda n: n.name)):
        refs = {(e.ref, e.pin) for e in n.endpoints}
        assert ("U3", f"GPA{i}") in refs                           # taps the right port pin
        assert any(r.startswith("J") for r, _ in refs)             # lands on a terminal pad


def test_per_bank_one_terminal_per_used_bank():
    # 10 ports span GPA0-7 + GPB0-1 -> two banks -> two terminals.
    it = _mcp_intent(device="S", ports=10, group="per_bank")
    expand_intent(it)
    terms = _terminals(it, "S")
    assert len(terms) == 2
    # GPA bank: 8 sig + 3V3 + GND = 10; GPB bank: 2 sig + 3V3 + GND = 4.
    assert sorted(int(t.lib_id[-2:]) for t in terms) == [4, 10]


def test_single_one_terminal():
    it = _mcp_intent(device="S", ports=6, group="single")
    expand_intent(it)
    assert len(_terminals(it, "S")) == 1


def test_single_over_16_falls_back_to_pin_header_with_gap():
    it = _mcp_intent(device="S", ports=16, group="single")   # 16 + 3V3 + GND = 18 > 16
    expand_intent(it)
    terms = _terminals(it, "S")
    assert len(terms) == 1 and terms[0].type == "HDR"        # pin header, not screw block
    assert any(g.kind == "expander_terminals_single_overflow" for g in it.gaps)


def test_single_at_16_positions_stays_a_screw_terminal():
    # Boundary (< vs <=): 14 ports + 3V3 + GND = 16 = the screw-terminal max — NO
    # overflow; 18 (the test above) does overflow. Pins the threshold side.
    it = _mcp_intent(device="S", ports=14, group="single")
    expand_intent(it)
    terms = _terminals(it, "S")
    assert len(terms) == 1 and terms[0].type == "TERM"       # screw block, not header
    assert not any(g.kind == "expander_terminals_single_overflow" for g in it.gaps)


def test_per_bank_gpa_only_is_one_terminal():
    # GPA0-5 only -> one bank -> exactly one terminal (no empty GPB terminal).
    it = _mcp_intent(device="S", ports=6, group="per_bank")
    expand_intent(it)
    assert len(_terminals(it, "S")) == 1


def test_i2c_side_untouched_no_double_emit():
    # Tapping GPA pins must not duplicate or retarget the MCP's existing I2C net,
    # and the on-board MCP must not be pulled into a remote terminal.
    it = _mcp_intent(device="S", ports=4)
    expand_intent(it)
    sda = [n for n in it.nets if n.name == "I2C_SDA"]
    assert len(sda) == 1                                     # not duplicated
    refs = {e.ref for e in sda[0].endpoints}
    assert not any(r.startswith("J") for r in refs)          # no terminal tapped onto I2C


def test_power_none_two_position_terminals():
    it = _mcp_intent(device="S", ports=2, group="per_sensor", power="none")
    expand_intent(it)
    terms = _terminals(it, "S")
    assert terms and all(t.lib_id.endswith("01x02") for t in terms)   # signal + GND only
    assert all("+3V3" not in lg.positions and "+5V" not in lg.positions
               for lg in it.connector_legends)


def test_power_5v_with_rail_emits_5v_position():
    # ESP32-WROOM-32E isn't native-USB, so BOTH power_tree's AMS1117 (3v3 logic,
    # NOT a +5V source -- see _FIVE_V_SOURCES) and the USB block's CP2102/USB_C
    # (the genuine +5V source, from the host's USB port) get placed before this
    # template runs. power: 5v is honored because of the CP2102, not the AMS1117.
    it = _mcp_intent(device="S", ports=2, group="per_sensor", power="5v")
    expand_intent(it)
    assert not any(g.kind == "expander_terminals_power" for g in it.gaps)
    assert any("+5V" in lg.positions for lg in it.connector_legends)


def test_power_5v_native_usb_board_with_only_ams1117_downgrades_with_gap():
    """Regression for _FIVE_V_SOURCES including AMS1117: an ESP32-S3 board
    (needs_3v3=true, native_usb=true) places an AMS1117 for the 3v3 logic
    rail, but native USB means NO CP2102/USB_C block runs at all -- there is
    no genuine +5V source anywhere on this board. AMS1117 being wrongly
    classified as a +5V source used to make has_5v true anyway, offering a
    "+5V" terminal tap with nothing actually driving that net to 5V."""
    it = DesignIntent()
    it.mcu = Mcu(ref="U1", part="ESP32-S3-WROOM-1", lib_id="RF_Module:ESP32-S3-WROOM-1")
    it.peripherals = [Peripheral(
        ref="U3", type="MCP23017", lib_id="Interface_Expansion:MCP23017x-x-SO",
        alt_lib_ids=["Interface_Expansion:MCP23017_SO"], value="MCP23017", bus="I2C")]
    it.nets = [Net("I2C_SDA", "peripheral", "high",
                   [Endpoint(ref="U1", gpio=21), Endpoint(ref="U3", role="SDA")])]
    apply_sidecar(it, BoardSidecar(
        expander_terminals={"U3": {"device": "S", "ports": 2, "power": "5v"}}))
    expand_intent(it)
    assert not any(p.type in ("CP2102", "USB_C") for p in it.peripherals)  # native USB
    assert any(p.type == "AMS1117" for p in it.peripherals)                # 3v3 logic rail
    assert any(g.kind == "expander_terminals_power" for g in it.gaps)
    assert all("+5V" not in lg.positions for lg in it.connector_legends)


def test_power_5v_sourced_by_board_yaml_extra_connector_no_downgrade():
    """finding #9 (Phase 1, 2026-09-23 full review): has_5v only checked
    peripheral TYPE against _FIVE_V_SOURCES (USB_C/CP2102, template-emitted).
    A board.yaml extra_connectors entry wired to "+5V" (e.g. a barrel jack)
    is a genuine, user-declared +5V source, but apply_sidecar's
    synthesize_connector always tags it type "CONN" -- has_5v missed it
    entirely, downgrading a legitimately-sourced power:5v request with a
    false "no +5V rail" gap. No MCU here (mirrors the sibling
    without-rail test) so the ONLY possible +5V source is the board.yaml
    connector itself."""
    it = DesignIntent()
    it.peripherals = [Peripheral(
        ref="U3", type="MCP23017", lib_id="Interface_Expansion:MCP23017x-x-SO",
        value="MCP23017", bus="I2C")]
    sc = BoardSidecar(
        extra_connectors=[{
            "ref": "J_PWR", "lib_id": "Connector:Barrel_Jack",
            "footprint": "FP:Jack", "nets": {"1": "+5V", "2": "GND"},
        }],
        expander_terminals={"U3": {"device": "S", "ports": 2, "power": "5v"}},
    )
    apply_sidecar(it, sc)
    expand_intent(it)
    assert not any(g.kind == "expander_terminals_power" for g in it.gaps)
    assert any("+5V" in lg.positions for lg in it.connector_legends)


def test_power_5v_without_rail_downgrades_with_gap():
    # A board with no +5V source (no MCU -> power_tree never fires, no regulator/USB
    # block) downgrades power: 5v to signal+GND with a disclosed gap — never a
    # sourceless +5V pin on the terminal.
    it = DesignIntent()
    it.peripherals = [Peripheral(
        ref="U3", type="MCP23017", lib_id="Interface_Expansion:MCP23017x-x-SO",
        value="MCP23017", bus="I2C")]
    apply_sidecar(it, BoardSidecar(
        expander_terminals={"U3": {"device": "S", "ports": 2, "power": "5v"}}))
    expand_intent(it)
    assert any(g.kind == "expander_terminals_power" for g in it.gaps)
    assert all("+5V" not in lg.positions for lg in it.connector_legends)


def test_ports_zero_is_a_noop_gap():
    it = _mcp_intent(device="S", ports=0)
    expand_intent(it)
    assert any(g.kind == "expander_terminals_empty" for g in it.gaps)
    assert not _terminals(it, "S")


def test_non_mcp_ref_is_a_gap_not_a_crash():
    it = DesignIntent()
    it.mcu = Mcu(ref="U1", part="ESP32-WROOM-32E", lib_id="RF_Module:ESP32-WROOM-32E")
    it.peripherals = [Peripheral(ref="U2", type="HX711", lib_id="Analog_ADC:HX711")]
    apply_sidecar(it, BoardSidecar(expander_terminals={"U2": {"device": "X", "ports": 2}}))
    expand_intent(it)
    assert not _terminals(it, "X")                           # nothing synthesized
    assert any(g.kind == "expander_terminals_unresolved" for g in it.gaps)


def test_net_collision_is_namespaced_with_gap():
    it = _mcp_intent()
    it.nets.append(Net("SENSOR_0", "peripheral", "high", [Endpoint(ref="U1", gpio=5)]))
    apply_sidecar(it, BoardSidecar(expander_terminals={"U3": {"device": "SENSOR", "ports": 2}}))
    expand_intent(it)
    names = {n.name for n in it.nets}
    assert "SENSOR_0" in names and "SENSOR_0_U3" in names    # namespaced, not duplicated
    assert any(g.kind == "expander_terminals_net_collision" for g in it.gaps)


def test_no_expander_block_is_a_noop():
    it = _mcp_intent()                                       # no spec applied
    expand_intent(it)
    assert not any(g.kind.startswith("expander_terminals") for g in it.gaps)
    assert not any(n.name.startswith("SENSOR_") for n in it.nets)


def test_expander_groups_dispatch_matches_sidecar_validation():
    """finding #21 (Phase 1.5, 2026-09-23 full review): sidecar._EXPANDER_GROUPS
    (what the board.yaml path validates) and templates.py's if/elif dispatch on
    spec.group (what actually happens per group) are two independent sources of
    truth for the same set of valid group values, with nothing tying them
    together. Structural check: every value _EXPANDER_GROUPS allows must have a
    literal `spec.group == "<value>"` dispatch branch in templates.py, so a group
    added to one without the other is caught here rather than silently falling
    through templates.py's final else."""
    import inspect

    from kicad_mcp.utils.firmware import templates
    from kicad_mcp.utils.firmware.sidecar import _EXPANDER_GROUPS

    source = inspect.getsource(templates.expander_terminals)
    for group in _EXPANDER_GROUPS:
        assert f'spec.group == "{group}"' in source, (
            f"group {group!r} is valid per sidecar._EXPANDER_GROUPS but has no "
            "matching dispatch branch in templates.expander_terminals")


def test_expander_unrecognized_group_raises_not_silently_treated_as_single():
    """finding #21: templates.expander_terminals can be reached directly with an
    already-built DesignIntent that never went through sidecar's _EXPANDER_GROUPS
    validation (e.g. hand-built, or a future group value added to the frozenset
    without a matching dispatch branch here). Pin that an unrecognized group
    raises loudly instead of silently falling into the 'single' behavior."""
    import pytest
    from kicad_mcp.utils.firmware.intent import ExpanderSpec
    from kicad_mcp.utils.firmware.templates import expand_intent

    it = _mcp_intent()   # no spec applied via apply_sidecar (skips its validation)
    it.expander_terminals["U3"] = ExpanderSpec(
        device="S", ports=["GPA0"], group="bogus_future_group")
    with pytest.raises(ValueError, match="bogus_future_group"):
        expand_intent(it)


def test_expander_spec_round_trips():
    # to_dict -> yaml -> from_dict must preserve the ExpanderSpec (this is the path
    # import->expand takes across two design() calls via the saved intent doc).
    from kicad_mcp.utils.firmware.intent import from_dict, to_dict
    it = _mcp_intent(device="TCRT5000", ports=3, group="per_bank", power="none")
    rt = from_dict(to_dict(it))
    assert to_dict(rt) == to_dict(it)
    spec = rt.expander_terminals["U3"]
    assert spec.ports == ["GPA0", "GPA1", "GPA2"]
    assert spec.group == "per_bank" and spec.power == "none"
