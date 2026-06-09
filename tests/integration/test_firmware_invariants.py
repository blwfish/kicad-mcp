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
# The canonical ESP32 fixture (I2C + HX711 + MCP) is rich enough that it MUST always
# place components and realize >=1 modeled connection. The skips below look green; if
# this fixture ever hits one, a generate regression is hiding behind it — so we fail
# instead of skipping for this fixture specifically.
_KNOWN_GOOD = "firmware"


def _corpus() -> list[Path]:
    return sorted(_FIXROOT.glob("**/config.h"))


_CORPUS = _corpus()
if not _CORPUS:   # collection-time guard: an empty parametrize collects ONE
    raise RuntimeError(                       # [NOTSET] item and passes green — refuse.
        f"Firmware fixture corpus is empty; expected config.h under {_FIXROOT}. "
        "Did the fixtures move?")


def _intent_for(config_path: Path):
    from kicad_mcp.utils.firmware.intent import build_intent, find_board_id
    from kicad_mcp.utils.firmware.parse import parse_macros, partition
    # parse_macros (not parse_defines) — mirror the real import op: #define + const.
    parsed = partition(parse_macros(config_path.read_text()))
    return build_intent(parsed, firmware_path=str(config_path),
                        board_id=find_board_id(str(config_path)))


@pytest.mark.parametrize("config_path", _CORPUS, ids=lambda p: p.parent.name)
def test_generate_is_honest_and_realizes_intent(config_path, tmp_path):
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import is_remote
    from kicad_mcp.utils.firmware.knowledge import is_terminal_card_type
    from kicad_mcp.utils.firmware.templates import expand_intent
    from kicad_mcp.utils.netlist_parser import extract_netlist_via_cli

    # Mirror the REAL pipeline: import -> expand_templates -> generate_schematic.
    # generate_schematic wires intent.nets and does NOT expand, so a bare intent
    # leaves every typed bus (I2C/I2S/UART) unrealized — its signals only become
    # wired nets at expand. Testing the bare intent made the audio/mic fixtures
    # (whose connections live entirely in buses) realize nothing modeled, so the
    # connectivity check below skipped — hiding any bus-wiring regression behind a
    # green skip. Expand first (mcu None -> generate errors anyway, leave it bare).
    bare = _intent_for(config_path)
    intent = expand_intent(bare) if bare.mcu is not None else bare
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
        if config_path.parent.name == _KNOWN_GOOD:
            pytest.fail(f"{_KNOWN_GOOD} fixture placed nothing — a regression broke "
                        "MCU/component resolution (this fixture must always place)")
        pytest.skip(f"{config_path.parent.name}: nothing placed (MCU unresolved) — "
                    "connectivity N/A")

    # --- invariant 4: firmware-as-spec connectivity --------------------------
    nl = extract_netlist_via_cli(str(out))
    assert nl is not None, "netlist export failed"

    def member_refs(net_name: str) -> set[str]:
        return {x["component"] for x in nl["nets"].get(net_name, [])}

    unresolved_by_net: dict[str, set[str]] = {}
    for u in unres:
        unresolved_by_net.setdefault(u.get("net"), set()).add(u.get("ref"))

    # Mirror generate's OWN offboard set exactly (single source — CLAUDE.md Rule 3):
    # a peripheral that's remote (board.yaml locus) or a terminal card is realized
    # off-board / at the expand step, so on a bare intent its endpoint is deferred,
    # not lost (verified: PIEZO_ADC -> screw terminal after expand). Deriving the
    # exclusion from the same predicate generate uses means the two can't drift — a
    # terminal-only copy would silently disagree the day a fixture sets locus:remote.
    offboard_refs = {p.ref for p in intent.peripherals
                     if is_remote(intent, p.ref) or is_terminal_card_type(p.type)}

    # The third disposition bucket must report EXACTLY the off-board endpoints
    # generate skipped — so an "ok" build is transparent about which declared
    # connections it deferred. Verifying it against an INDEPENDENT derivation (not
    # generate's own report) means generate can neither omit a deferred endpoint
    # nor defer an on-board one to dodge the connectivity check below.
    reported_deferred = {(d["net"], d["ref"]) for d in res["deferred_endpoints"]}
    expected_deferred = {(net.name, ep.ref)
                         for net in intent.nets for ep in net.endpoints
                         if ep.ref in offboard_refs}
    assert reported_deferred == expected_deferred, (
        f"{config_path.parent.name}: deferred_endpoints {reported_deferred} != "
        f"off-board endpoints {expected_deferred}")

    # Over the EXPANDED intent, EVERY multi-endpoint net is a realized connection to
    # verify — not just the kind in MODELED_NET_KINDS. The bus signals expand into
    # 'passive' nets (e.g. I2S0_BCLK -> U1 + the mic), so filtering to bus/peripheral
    # would walk right past exactly the wiring the audio/mic fixtures exist to prove.
    checked = 0
    for net in intent.nets:
        if len(net.endpoints) < 2:
            continue
        # Every endpoint the intent declares that generate did NOT report as
        # unresolved (and that isn't a deferred off-board endpoint) must actually be
        # connected to this net in the schematic.
        expected = ({ep.ref for ep in net.endpoints}
                    - unresolved_by_net.get(net.name, set())
                    - offboard_refs)
        got = member_refs(net.name)
        assert expected <= got, (
            f"{config_path.parent.name}: net {net.name!r} ({net.kind}) declares "
            f"{expected} but the exported netlist connects only {got} — the "
            f"schematic does not realize the intent")
        if len(expected) >= 2:           # a genuinely realized multi-pin connection
            checked += 1
            assert len(got) >= 2

    # Post-expand, any board with a resolved MCU wires >=1 multi-pin net (a signal
    # bus, or at minimum the power rails). checked==0 therefore means generate
    # produced a board with NO realized connection — a wiring regression, for ANY
    # fixture. No skip: a skip here is precisely how an I2S-realization regression
    # would have hidden (the audio fixtures used to land in it).
    assert checked > 0, (
        f"{config_path.parent.name}: generate realized NO multi-pin connection from "
        "an expanded intent with a resolved MCU — net wiring regression")


def test_pico_non_broken_out_gpio_is_honest_partial(tmp_path):
    """A Pico firmware using GP25 (the on-board LED — NOT broken out on the module
    symbol) must produce an HONEST partial, not a crash or a silent drop: the
    generalized GPIO{n} resolver returns None for a pin the symbol doesn't expose,
    so the endpoint lands in unresolved_endpoints and status is 'partial'. Proves
    the caller handles the None all the way through (cold-review MT-2)."""
    from kicad_mcp.utils.firmware.generate import generate_schematic
    from kicad_mcp.utils.firmware.intent import build_intent
    from kicad_mcp.utils.firmware.parse import parse_macros, partition
    from kicad_mcp.utils.firmware.templates import expand_intent

    fw = "#define LED_PIN 25\n#define HX711_DOUT_PIN 2\n#define HX711_SCK_PIN 3\n"
    intent = expand_intent(build_intent(partition(parse_macros(fw)),
                                        firmware_path="c.h", board_id="pico"))
    res = generate_schematic(intent, str(tmp_path / "g.kicad_sch"))
    assert 25 in {u.get("gpio") for u in res["unresolved_endpoints"]}   # GP25 honest gap
    assert res["status"] == "partial"          # not "ok" (hid it), not "error", not a crash
    assert res["components_placed"] > 0        # the rest of the board still placed
