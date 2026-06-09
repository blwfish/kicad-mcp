"""Phase 0 safety-net invariants — integration tier (need a real KiCad build).

The two Phase-0 invariants that can't run without KiCad symbols + kicad-cli; the
unit-tier two (no-silent-loss, round-trip) are in tests/test_firmware_invariants.
Run over the BUNDLED fixture corpus so the whole thing exercises on the K9/K10
matrix with no external firmware tree.

  3. status-honesty — generate_schematic's status == "ok" IFF it dropped nothing
     (component_errors == [] AND unresolved_endpoints == []); "error" IFF nothing
     was placed; "partial" otherwise. A build that silently reports ok on a board
     missing a chip (the MCP23017 cross-version bug) fails here, for any fixture.
  4. firmware-as-spec connectivity — every modeled connection the intent declares
     and generate did NOT flag unresolved actually appears, wired, in the exported
     netlist. This CLOSES the loop (the artifact realizes the intent); it does NOT
     SEAL it (it can't prove the intent is electrically correct — card correctness
     is the residual).

Gated by KICAD_INTEGRATION=1; runs on the self-hosted KiCad 9/10 matrix.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)

_FIXROOT = Path(__file__).resolve().parents[1] / "fixtures" / "firmware"
# Nets the importer models as real, high-confidence connections (a firmware-as-spec
# claim). Orphan/power/passive are gaps or template-supplied, not import claims.
_MODELED = {"bus", "peripheral"}


def _corpus() -> list[Path]:
    return sorted(_FIXROOT.glob("**/config.h"))


def _intent_for(config_path: Path):
    from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
    from kicad_mcp.utils.firmware.parse import parse_defines, partition
    parsed = partition(parse_defines(config_path.read_text()))
    return build_intent(parsed, firmware_path=str(config_path),
                        board_id=find_board_id(str(config_path)))


@pytest.mark.parametrize("config_path", _corpus(), ids=lambda p: p.parent.name)
def test_generate_is_honest_and_realizes_intent(config_path, tmp_path):
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.knowledge import is_terminal_card_type
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

    intent = _intent_for(config_path)
    out = tmp_path / "gen.kicad_sch"
    res = generate_schematic(intent, str(out))

    status = res["status"]
    errs = res["component_errors"]
    unres = res["unresolved_endpoints"]
    placed = res["components_placed"]

    # --- invariant 3: status-honesty (the biconditional) ---------------------
    assert status in {"ok", "partial", "error"}
    assert (status == "ok") == (not errs and not unres), (
        f"{config_path.parent.name}: status={status!r} but "
        f"component_errors={errs}, unresolved_endpoints={unres} — dishonest")
    if status == "error":
        assert placed == 0, "status 'error' must mean nothing was placed"
    if status == "partial":
        assert placed > 0 and (errs or unres), (
            "status 'partial' must mean something placed AND something failed")

    if placed == 0:
        pytest.skip(f"{config_path.parent.name}: nothing placed "
                    "(MCU unresolved) — connectivity N/A")

    # --- invariant 4: firmware-as-spec connectivity --------------------------
    nl = extract_netlist_via_cli(str(out))
    assert nl is not None, "netlist export failed"

    def member_refs(net_name: str) -> set[str]:
        return {x["component"] for x in nl["nets"].get(net_name, [])}

    unresolved_by_net: dict[str, set[str]] = {}
    for u in unres:
        unresolved_by_net.setdefault(u.get("net"), set()).add(u.get("ref"))

    # Terminal-card peripherals (PIEZO/buzzer/TCRT) are realized as screw terminals
    # at the EXPAND step, not in bare generate — so on a bare intent their endpoint
    # is legitimately not-yet-wired (deferred, not lost; the expander/pipeline tests
    # cover that leg). Exclude them so this bare-intent check tests the contract it
    # actually governs: the on-board IC connections generate is responsible for.
    terminal_refs = {p.ref for p in intent.peripherals
                     if is_terminal_card_type(p.type)}

    checked = 0
    for net in intent.nets:
        if net.kind not in _MODELED or len(net.endpoints) < 2:
            continue
        # Every endpoint the intent declares that generate did NOT report as
        # unresolved (and that isn't a deferred terminal) must actually be
        # connected to this net in the schematic.
        expected = ({ep.ref for ep in net.endpoints}
                    - unresolved_by_net.get(net.name, set())
                    - terminal_refs)
        got = member_refs(net.name)
        assert expected <= got, (
            f"{config_path.parent.name}: net {net.name!r} ({net.kind}) declares "
            f"{expected} but the exported netlist connects only {got} — the "
            f"schematic does not realize the intent")
        if len(expected) >= 2:           # a genuinely realized multi-pin connection
            checked += 1
            assert len(got) >= 2

    if checked == 0:
        pytest.skip(f"{config_path.parent.name}: no realized modeled connections "
                    "to check connectivity against")
