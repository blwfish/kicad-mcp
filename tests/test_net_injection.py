"""Tests for utils/net_injection — the single source of truth for inserting
``(net N "name")`` lines into a .kicad_pcb via direct text editing.

This is the helper that both pcb_nets.add_net and pcb_pipeline's bulk injector
consume. The fresh-KiCad-10-no-anchor case is the one that previously broke
build_pcb_from_schematic (electrically-dead boards); it is pinned here, and the
byte-identical test proves the two call paths agree.
"""

from kicad_mcp.tools.pcb_nets import _op_add_net
import pytest

from kicad_mcp.utils.net_injection import (
    escape_net_name,
    existing_net_codes,
    find_net_insert_pos,
    format_net_line,
    inject_net_definitions,
    unescape_net_name,
)

# A board with the conventional (net 0 "") sentinel + two named nets.
PCB_WITH_NETS = (
    '(kicad_pcb (version 20240108) (generator "test")\n'
    '\t(net 0 "")\n'
    '\t(net 1 "GND")\n'
    '\t(net 2 "VCC")\n'
    ")\n"
)

# Fresh KiCad-10 board: NO (net ...) lines at all, footprints present. This is
# the case the old pipeline copy silently dropped.
PCB_FRESH_K10 = (
    '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
    '\t(general)\n'
    '\t(footprint "R_0603"\n'
    '\t\t(at 10 10)\n'
    '\t)\n'
    ")\n"
)

# Degenerate board: no nets, no footprints — only the closing paren to anchor on.
PCB_NO_NETS_NO_FP = (
    '(kicad_pcb (version 20240108) (generator "pcbnew")\n'
    '\t(general)\n'
    ")\n"
)


# -- format_net_line ---------------------------------------------------------

def test_format_net_line():
    assert format_net_line(3, "SDA") == '\n\t(net 3 "SDA")'


# -- find_net_insert_pos: three resolution branches --------------------------

def test_insert_after_last_net_line():
    pos = find_net_insert_pos(PCB_WITH_NETS)
    # Should land right after the closing paren of (net 2 "VCC").
    assert PCB_WITH_NETS[:pos].endswith('(net 2 "VCC")')


def test_insert_before_first_footprint_when_no_nets():
    """KiCad-10 fallback #1: no net lines → insert before first footprint."""
    pos = find_net_insert_pos(PCB_FRESH_K10)
    assert pos is not None
    # The next top-level token after the insertion point is the footprint block.
    assert PCB_FRESH_K10[pos:].lstrip("\n\t").startswith("(footprint")


def test_insert_before_closing_paren_when_no_nets_no_footprint():
    """KiCad-10 fallback #2: no nets and no footprints → before final paren."""
    pos = find_net_insert_pos(PCB_NO_NETS_NO_FP)
    assert pos is not None
    assert PCB_NO_NETS_NO_FP[pos:] == "\n)\n"


def test_insert_pos_none_when_no_anchor_at_all():
    assert find_net_insert_pos("garbage with no closing paren") is None


# -- inject_net_definitions --------------------------------------------------

def test_inject_empty_returns_unchanged():
    assert inject_net_definitions(PCB_WITH_NETS, []) == PCB_WITH_NETS


def test_inject_single_net_after_existing():
    out = inject_net_definitions(PCB_WITH_NETS, [(3, "SDA")])
    assert out is not None
    assert '\t(net 2 "VCC")\n\t(net 3 "SDA")\n)' in out


def test_inject_multiple_nets_in_order():
    out = inject_net_definitions(PCB_WITH_NETS, [(3, "SDA"), (4, "SCL")])
    assert out is not None
    assert '\t(net 3 "SDA")\n\t(net 4 "SCL")\n' in out


def test_inject_into_fresh_k10_board():
    """The regression case: nets must actually land in a fresh K10 board."""
    out = inject_net_definitions(PCB_FRESH_K10, [(1, "GND"), (2, "VCC")])
    assert out is not None
    assert '(net 1 "GND")' in out
    assert '(net 2 "VCC")' in out
    # Inserted before the footprint, not lost.
    assert out.index('(net 1 "GND")') < out.index('(footprint')


def test_inject_returns_none_when_no_anchor():
    assert inject_net_definitions("no anchor here", [(1, "GND")]) is None


# -- existing_net_codes ------------------------------------------------------

def test_existing_net_codes():
    assert existing_net_codes(PCB_WITH_NETS) == [(0, ""), (1, "GND"), (2, "VCC")]


def test_existing_net_codes_empty():
    assert existing_net_codes(PCB_FRESH_K10) == []


# -- byte-identical: both call paths agree -----------------------------------

def test_add_net_matches_shared_helper_byte_identical(tmp_path):
    """_op_add_net's file output must equal a direct shared-helper injection for
    the same (code, name). This pins that add_net delegates to the one source of
    truth rather than re-implementing formatting/placement."""
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(PCB_WITH_NETS)

    result = _op_add_net(str(pcb), "SDA")
    assert result["status"] == "ok"
    assert result["net_code"] == 3
    via_tool = pcb.read_text()

    via_helper = inject_net_definitions(PCB_WITH_NETS, [(3, "SDA")])
    assert via_tool == via_helper


def test_both_paths_agree_on_fresh_k10_fallback():
    """The bug was that the bulk path lacked the fallback the single path had.
    Now both compute the insertion point through find_net_insert_pos, so a fresh
    K10 board yields the same injection regardless of which path drives it."""
    new_nets = [(1, "GND"), (2, "VCC")]

    # "single-net path" semantics: sequential injection, one at a time.
    seq = PCB_FRESH_K10
    code = 0
    for _, name in new_nets:
        code += 1
        seq = inject_net_definitions(seq, [(code, name)])
        assert seq is not None

    # "bulk path" semantics: all at once.
    bulk = inject_net_definitions(PCB_FRESH_K10, new_nets)

    assert seq == bulk
    assert '(net 1 "GND")' in bulk
    assert '(net 2 "VCC")' in bulk


# -- S-expression escaping (m-pipeline-escape) -------------------------------

class TestNetNameEscaping:
    """Externally-authored net labels (kicad-cli netlists) may contain `"` or
    `\\`. The injector must write them safely by construction, not corrupt the
    .kicad_pcb. format_net_line escapes; existing_net_codes decodes; the two
    must round-trip."""

    @pytest.mark.parametrize("name", [
        "GND", "+3V3", "BUS_A",            # clean names: escaping is a no-op
        'BUS"A',                            # embedded quote
        "PATH\\X",                          # embedded backslash
        'A"B\\C',                           # both
        '\\"',                              # adjacent escapes (backslash + quote)
        '""',                               # two quotes
    ])
    def test_escape_unescape_round_trip(self, name):
        assert unescape_net_name(escape_net_name(name)) == name

    def test_clean_name_unchanged(self):
        assert escape_net_name("SDA") == "SDA"
        assert format_net_line(3, "SDA") == '\n\t(net 3 "SDA")'

    def test_embedded_quote_is_escaped_in_line(self):
        line = format_net_line(7, 'BUS"A')
        assert line == '\n\t(net 7 "BUS\\"A")'
        # The quotes in the written line must be balanced (no corruption).
        assert line.count('"') - line.count('\\"') == 2

    def test_injection_with_quote_round_trips_via_existing_codes(self):
        """Inject a malicious name, then read it back: the in-memory name must
        equal the original raw label, proving file<->memory is a matched pair."""
        out = inject_net_definitions(PCB_WITH_NETS, [(3, 'BUS"A')])
        assert out is not None
        codes = existing_net_codes(out)
        assert (3, 'BUS"A') in codes
        # And the pre-existing clean nets are still intact.
        assert (1, "GND") in codes and (2, "VCC") in codes

    def test_backslash_name_round_trips(self):
        out = inject_net_definitions(PCB_FRESH_K10, [(1, "NET\\1")])
        assert out is not None
        assert (1, "NET\\1") in existing_net_codes(out)
