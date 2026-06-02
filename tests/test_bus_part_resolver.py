"""Tests for bus→part resolution (C4). Pure, no KiCad.

The core contract: resolve a bus to a part ONLY when exactly one corpus part
serves that bus type; ambiguity (>1) is never silently broken, absence resolves
to nothing, and a user override is honored. No proximity heuristics.
"""
from __future__ import annotations

from kicad_mcp.utils.firmware.bus_part_resolver import resolve_bus_parts
from kicad_mcp.utils.firmware.cards import part_serves_map
from kicad_mcp.utils.firmware.intent import Bus
from kicad_mcp.utils.firmware.part_extractor import PartEvidence

_SERVES = {"INMP441": "I2S_IN", "SPH0645": "I2S_IN", "MAX98357A": "I2S_OUT"}


def _ev(canonical: str) -> PartEvidence:
    return PartEvidence(part_name=canonical, canonical=canonical,
                        file="config.h", line=1, kind="source", raw_text="")


def test_single_candidate_resolves_from_corpus():
    bus = Bus(name="CMCA_MIC", type="I2S_IN")
    res = resolve_bus_parts([bus], [_ev("INMP441")], _SERVES)
    assert bus.resolved_part == "INMP441"
    assert bus.part_provenance == "corpus"
    assert res[0].reason == "resolved"


def test_ambiguous_two_candidates_does_not_pick():
    # Both INMP441 and SPH0645 serve I2S_IN and both appear → NEVER silently pick.
    bus = Bus(name="CMCA_MIC", type="I2S_IN")
    res = resolve_bus_parts([bus], [_ev("INMP441"), _ev("SPH0645")], _SERVES)
    assert bus.resolved_part is None
    assert res[0].reason == "ambiguous"
    assert res[0].candidates == ["INMP441", "SPH0645"]   # sorted, for a stable gap message


def test_no_serving_part_in_corpus_resolves_none():
    # Corpus has an I2S_OUT part only; an I2S_IN bus has no candidate.
    bus = Bus(name="CMCA_MIC", type="I2S_IN")
    res = resolve_bus_parts([bus], [_ev("MAX98357A")], _SERVES)
    assert bus.resolved_part is None
    assert res[0].reason == "none"


def test_two_same_type_buses_both_resolve():
    # The audio node's two MAX98357A amps: two I2S_OUT buses, one serving part.
    b1 = Bus(name="CMCA_I2S", type="I2S_OUT")
    b2 = Bus(name="CMCA_I2S2", type="I2S_OUT")
    resolve_bus_parts([b1, b2], [_ev("MAX98357A")], _SERVES)
    assert b1.resolved_part == "MAX98357A" and b2.resolved_part == "MAX98357A"


def test_user_override_is_honored_not_overwritten():
    bus = Bus(name="CMCA_MIC", type="I2S_IN",
              resolved_part="ICS43434", part_provenance="user")
    # Even though INMP441 is the sole corpus candidate, the user's choice wins.
    res = resolve_bus_parts([bus], [_ev("INMP441")], _SERVES)
    assert bus.resolved_part == "ICS43434" and bus.part_provenance == "user"
    assert res[0].reason == "user"


def test_serves_type_must_match_bus_type():
    # MAX98357A serves I2S_OUT, so it is NOT a candidate for an I2S_IN bus.
    bus = Bus(name="CMCA_MIC", type="I2S_IN")
    resolve_bus_parts([bus], [_ev("MAX98357A"), _ev("INMP441")], _SERVES)
    assert bus.resolved_part == "INMP441"   # only the I2S_IN part matched


# --- part_serves_map -------------------------------------------------------

def test_part_serves_map_omits_cards_without_serves():
    peripherals = {
        "INMP441": {"type": "INMP441", "serves": "I2S_IN"},
        "MAX98357A": {"type": "MAX98357A", "serves": "I2S_OUT"},
        "MPU6050": {"type": "MPU6050"},               # no serves → omitted
    }
    assert part_serves_map(peripherals) == {
        "INMP441": "I2S_IN", "MAX98357A": "I2S_OUT"}
