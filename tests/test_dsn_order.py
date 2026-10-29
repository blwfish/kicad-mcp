"""Deterministic DSN ordering (utils/dsn_order.py).

FreeRouter is a pure function of its input, but pcbnew exports components,
nets, images and pins in a different order on every export of the SAME board,
and that order alone changes the routing result.  These tests pin the contract
the autorouter relies on: output order is a function of (content, seed) only.

The two fixtures are real ``ExportSpecctraDSN`` outputs of one firmware-derived
board (same placements and nets, different emitted order and temp-path header).
"""

import re
from collections import Counter
from pathlib import Path

import pytest

from kicad_mcp.utils.dsn_order import reorder_dsn

FIX = Path(__file__).parent / "fixtures" / "dsn"
EXPORT_A = (FIX / "firmware_esp32dev_a.dsn").read_text()
EXPORT_B = (FIX / "firmware_esp32dev_b.dsn").read_text()


def _tokens(text: str) -> Counter:
    """Multiset of tokens, ignoring the per-export path atom after ``(pcb``."""
    body = text.replace('(string_quote ")', "(string_quote Q)")
    toks = re.findall(r'"[^"]*"|\(|\)|[^\s()"]+', body)
    assert toks[:2] == ["(", "pcb"] and toks[2] != "("
    return Counter(toks[:2] + toks[3:])


def test_fixtures_really_are_differently_ordered_exports():
    """Guard the premise: if the raw exports were already equal the tests below
    would prove nothing."""
    assert EXPORT_A != EXPORT_B
    assert _tokens(EXPORT_A) == _tokens(EXPORT_B)       # same content, only order differs


@pytest.mark.parametrize("seed", [None, 0, 1, 2, 9])
def test_two_exports_of_one_board_reorder_to_identical_text(seed):
    """The headline: the order no longer depends on which export we were handed."""
    assert reorder_dsn(EXPORT_A, seed) == reorder_dsn(EXPORT_B, seed)
    assert reorder_dsn(EXPORT_A, seed) is not None


@pytest.mark.parametrize("seed", [None, 1, 2])
def test_reordering_is_idempotent(seed):
    once = reorder_dsn(EXPORT_A, seed)
    assert reorder_dsn(once, seed) == once


def test_same_seed_is_reproducible_and_different_seeds_differ():
    s1, s1_again = reorder_dsn(EXPORT_A, 1), reorder_dsn(EXPORT_A, 1)
    assert s1 == s1_again
    canonical, s2 = reorder_dsn(EXPORT_A, None), reorder_dsn(EXPORT_A, 2)
    assert len({canonical, s1, s2}) == 3                # a real ensemble, not 3 copies


def test_seed_zero_is_a_permutation_not_the_canonical_order():
    """None and 0 are different on purpose: ``if seed:`` would conflate them."""
    assert reorder_dsn(EXPORT_A, 0) != reorder_dsn(EXPORT_A, None)


@pytest.mark.parametrize("seed", [None, 1, 7])
def test_content_is_preserved_only_order_changes(seed):
    assert _tokens(reorder_dsn(EXPORT_A, seed)) == _tokens(EXPORT_A)


@pytest.mark.parametrize("seed", [None, 3])
def test_every_component_placement_survives(seed):
    out = reorder_dsn(EXPORT_A, seed)
    placed = lambda t: sorted(re.findall(r"\(place \S+ [-\d.]+ [-\d.]+ \w+ \d+", t))  # noqa: E731
    assert placed(out) == placed(EXPORT_A)
    assert len(placed(out)) == 24


def test_temp_path_header_is_dropped():
    out = reorder_dsn(EXPORT_A)
    assert out.startswith("(pcb board\n")
    assert "/var/" not in out and "kicad_autoroute_" not in out


def test_string_quote_declaration_is_kept():
    assert reorder_dsn(EXPORT_A).count('(string_quote ")') == 1


def test_quoted_values_containing_parens_and_spaces_survive():
    out = reorder_dsn(EXPORT_A)
    assert '"Piezo (terminal)"' in out                  # quoted, paren + space inside
    assert reorder_dsn(out) == out                      # and it still parses


def test_structure_section_order_is_not_touched():
    """Only unordered collections are reordered; (structure ...) rules are not."""
    out = reorder_dsn(EXPORT_A)
    assert "(structure" in out
    assert out.index("(structure") < out.index("(placement") < out.index("(library")


class TestSyntheticShapes:
    """Small hand-written DSNs so each rule is pinned in isolation."""

    HEAD = '(pcb /tmp/x.dsn\n  (parser (string_quote ") (space_in_quoted_tokens on))\n'

    def test_pins_are_sorted_within_a_net(self):
        t = self.HEAD + "  (network (net N1 (pins U1-2 C3-1 R10-1 R2-1)))\n)\n"
        assert "(pins C3-1 R10-1 R2-1 U1-2)" in reorder_dsn(t)

    def test_class_net_atoms_sorted_rules_kept_after(self):
        t = self.HEAD + (
            "  (network (class default ZED ALPHA MID (circuit (use_via V)) "
            "(rule (width 250))))\n)\n"
        )
        out = reorder_dsn(t)
        assert out.index("ALPHA") < out.index("MID") < out.index("ZED") < out.index("(circuit")

    def test_nets_in_network_are_sorted_by_content(self):
        t = self.HEAD + "  (network (net B (pins U1-1)) (net A (pins U1-2)))\n)\n"
        out = reorder_dsn(t)
        assert out.index("(net A") < out.index("(net B")

    def test_components_and_their_places_sorted(self):
        t = self.HEAD + (
            "  (placement (component Z (place Z1 0 0 front 0)) "
            "(component A (place A2 0 0 front 0) (place A1 0 0 front 0)))\n)\n"
        )
        out = reorder_dsn(t)
        assert out.index("component A") < out.index("component Z")
        assert out.index("place A1") < out.index("place A2")


@pytest.mark.parametrize("bad", [
    "",                                   # empty
    "not a dsn",                          # wrong head
    "(kicad_pcb (version 1))",            # a different s-expression format
    "(pcb x (placement (component",       # truncated / unbalanced
    "(pcb x) (extra)",                    # trailing form after the root
    "(pcb x))",                           # surplus close paren
])
def test_unparseable_input_returns_none_never_raises(bad):
    assert reorder_dsn(bad) is None
    assert reorder_dsn(bad, 3) is None
