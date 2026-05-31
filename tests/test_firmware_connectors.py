"""Unit tests for the unified ``synthesize_connector`` helper (placement-locus L2).

No KiCad needed — these pin the structural mechanics (symbol sizing, pin-count
validation at/below/above N, ref allocation, position labeling, legend dedup).
Pin-existence against real symbols is covered in the integration tier.
"""
import pytest

from kicad_mcp.utils.firmware.connectors import (
    ConnectorError,
    ConnectorPosition,
    default_footprint,
    symbol_pin_count,
    synthesize_connector,
)


class _Alloc:
    """Minimal collision-free ref allocator (mirrors templates.RefAllocator)."""

    def __init__(self) -> None:
        self._n: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self._n[prefix] = self._n.get(prefix, 0) + 1
        return f"{prefix}{self._n[prefix]}"


def _pos(*pairs):
    return [ConnectorPosition(net_name=n, label=lbl) for n, lbl in pairs]


# --- symbol sizing -----------------------------------------------------------

def test_screw_terminal_sized_to_n():
    conn, joins, legend = synthesize_connector(
        _pos(("MIC_BCLK", "BCLK"), ("MIC_WS", "WS"), ("MIC_SD", "SD"),
             ("+3V3", "+3V3"), ("GND", "GND")),
        alloc=_Alloc(), device="INMP441",
    )
    assert conn.lib_id == "Connector:Screw_Terminal_01x05"
    assert conn.footprint == ("TerminalBlock_Phoenix:TerminalBlock_Phoenix_"
                              "MKDS-1,5-5-5.08_1x05_P5.08mm_Horizontal")
    assert conn.value == "INMP441"
    assert conn.type == "TERM"
    assert len(joins) == 5
    # pads auto-assigned 1..5 in order
    assert [ep.pin for _net, ep in joins] == ["1", "2", "3", "4", "5"]
    assert [net for net, _ep in joins] == ["MIC_BCLK", "MIC_WS", "MIC_SD", "+3V3", "GND"]
    assert legend.ref == conn.ref
    assert legend.device == "INMP441"
    assert legend.positions == ["BCLK", "WS", "SD", "+3V3", "GND"]


def test_pin_header_uses_conn_symbol_and_header_footprint():
    conn, _j, _l = synthesize_connector(
        _pos(("SPK_P", "L+"), ("SPK_N", "L-")),
        alloc=_Alloc(), device="SPK0L", connector_type="pin_header",
    )
    assert conn.lib_id == "Connector_Generic:Conn_01x02"
    assert conn.footprint == "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical"
    assert conn.type == "HDR"


# --- pin-count validation: at / below / above N ------------------------------

def test_symbol_pin_count_parses_01xNN():
    assert symbol_pin_count("Connector:Screw_Terminal_01x05") == 5
    assert symbol_pin_count("Connector_Generic:Conn_01x12") == 12
    assert symbol_pin_count("Connector:Barrel_Jack") is None  # no count encoded


def test_explicit_lib_id_at_capacity_ok():
    # 5 positions into a 5-pin symbol — exactly at N, allowed.
    conn, _j, _l = synthesize_connector(
        _pos(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"), ("e", "E")),
        alloc=_Alloc(), device="X", lib_id="Connector:Screw_Terminal_01x05",
    )
    assert conn.lib_id == "Connector:Screw_Terminal_01x05"


def test_explicit_lib_id_below_capacity_ok():
    # 4 positions into a 5-pin symbol — below N, allowed (spare position).
    conn, joins, _l = synthesize_connector(
        _pos(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")),
        alloc=_Alloc(), device="X", lib_id="Connector:Screw_Terminal_01x05",
    )
    assert len(joins) == 4


def test_explicit_lib_id_above_capacity_raises():
    # 6 positions into a 5-pin symbol — above N, the latent bug the helper closes.
    with pytest.raises(ConnectorError, match="exceed"):
        synthesize_connector(
            _pos(("a", "A"), ("b", "B"), ("c", "C"), ("d", "D"), ("e", "E"), ("f", "F")),
            alloc=_Alloc(), device="X", lib_id="Connector:Screw_Terminal_01x05",
        )


def test_explicit_pad_above_capacity_raises():
    with pytest.raises(ConnectorError, match="exceed"):
        synthesize_connector(
            [ConnectorPosition("a", "A", pin="9")],
            alloc=_Alloc(), device="X", lib_id="Connector:Screw_Terminal_01x05",
        )


# --- footprint defaults ------------------------------------------------------

def test_phoenix_in_range_vs_pinheader_fallback():
    assert "MKDS-1,5-2-5.08_1x02" in default_footprint("screw_terminal", 2)
    assert "MKDS-1,5-16-5.08_1x16" in default_footprint("screw_terminal", 16)
    # N=1 (below Phoenix min) and N=17 (above max) fall back to a pin header.
    assert default_footprint("screw_terminal", 1).startswith("Connector_PinHeader")
    assert default_footprint("screw_terminal", 17).startswith("Connector_PinHeader")


# --- ref allocation ----------------------------------------------------------

def test_alloc_is_called_per_prefix_collision_free():
    alloc = _Alloc()
    c1, _, _ = synthesize_connector(_pos(("a", "A"), ("b", "B")), alloc=alloc, device="X")
    c2, _, _ = synthesize_connector(_pos(("c", "C"), ("d", "D")), alloc=alloc, device="Y")
    assert c1.ref == "J1" and c2.ref == "J2"   # distinct, collision-free


# --- explicit pins (fragment c: arbitrary pinout) ----------------------------

def test_explicit_pins_honored():
    conn, joins, legend = synthesize_connector(
        [ConnectorPosition("+5V", "+5V", pin="1"),
         ConnectorPosition("GND", "GND", pin="2")],
        alloc=_Alloc(), device="PWR_IN",
        lib_id="Connector:Barrel_Jack",
        footprint="Connector_BarrelJack:BarrelJack_Horizontal",
    )
    assert [ep.pin for _n, ep in joins] == ["1", "2"]
    assert conn.lib_id == "Connector:Barrel_Jack"
    assert conn.footprint == "Connector_BarrelJack:BarrelJack_Horizontal"
    assert legend.positions == ["+5V", "GND"]


def test_mixed_explicit_and_auto_pads_avoid_collision():
    # one explicit pad 2; the auto ones must skip it.
    conn, joins, _l = synthesize_connector(
        [ConnectorPosition("a", "A"),                 # auto -> 1
         ConnectorPosition("b", "B", pin="2"),        # explicit 2
         ConnectorPosition("c", "C")],                # auto -> 3 (skips taken 2)
        alloc=_Alloc(), device="X", connector_type="pin_header",
    )
    assert [ep.pin for _n, ep in joins] == ["1", "2", "3"]


# --- legend dedup (Open Decision 6) ------------------------------------------

def test_two_gnd_positions_share_label():
    _c, _j, legend = synthesize_connector(
        _pos(("GND", "GND"), ("GND", "GND")),
        alloc=_Alloc(), device="X",
    )
    assert legend.positions == ["GND", "GND"]   # same net -> share, not suffixed


def test_different_nets_same_label_suffixed():
    _c, _j, legend = synthesize_connector(
        _pos(("GND_A", "GND"), ("GND_B", "GND")),
        alloc=_Alloc(), device="X",
    )
    assert legend.positions == ["GND", "GND_2"]  # different nets -> disambiguate


# --- error paths -------------------------------------------------------------

def test_empty_positions_raises():
    with pytest.raises(ConnectorError, match="at least one"):
        synthesize_connector([], alloc=_Alloc(), device="X")


def test_unknown_connector_type_raises():
    with pytest.raises(ConnectorError, match="connector_type"):
        synthesize_connector(_pos(("a", "A")), alloc=_Alloc(), device="X",
                             connector_type="bananas")


def test_duplicate_explicit_pads_raise():
    with pytest.raises(ConnectorError, match="duplicate"):
        synthesize_connector(
            [ConnectorPosition("a", "A", pin="1"),
             ConnectorPosition("b", "B", pin="1")],
            alloc=_Alloc(), device="X",
        )
