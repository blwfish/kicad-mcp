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
    # ESP32 board -> power_tree adds the AMS1117 (+5V regulator input) BEFORE this
    # template runs, so power: 5v is honored: a +5V terminal position is emitted.
    it = _mcp_intent(device="S", ports=2, group="per_sensor", power="5v")
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
