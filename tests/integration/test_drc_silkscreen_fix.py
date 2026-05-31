"""Integration gate for the DRC autofix silkscreen path on real KiCad.

The embedded silkscreen-fix pcbnew script in ``pcb_drc_fix.py`` calls
``aabb_overlap``/``aabb_inside`` but for a long time never injected
``GEOMETRY_HELPER`` (which defines them) into its script string. The bug was
LATENT: unit tests mock ``run_pcbnew_script`` so the body never executes, and
no integration test drove ``fix_silkscreen`` against a board with real
overlapping silk text. The first time it ran for real it would raise
``NameError: name 'aabb_overlap' is not defined`` — surfaced to the caller as a
``RuntimeError`` from the subprocess bridge (non-zero exit), aborting the whole
``drc(operation="autofix")`` call.

This test builds a board with two footprints whose reference silkscreen
overlaps, runs DRC autofix with only the silkscreen fixer enabled, and asserts
the silk script actually ran and resolved the overlap — which can only happen
if ``GEOMETRY_HELPER`` is present.

Run with: KICAD_INTEGRATION=1 pytest tests/integration/ -v
"""

import asyncio
import inspect
import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)


@pytest.fixture(scope="module")
def mcp_server():
    from kicad_mcp.server import create_server
    return create_server()


def _call(mcp, name):
    """Return a callable that invokes the named tool from a kwargs dict.

    Handles both sync routers (``pcb``) and async + ctx-aware routers (``drc``):
    coroutines are driven to completion and ``ctx=None`` is injected when the
    tool declares it.
    """
    fn = asyncio.run(mcp.get_tool(name)).fn
    needs_ctx = "ctx" in inspect.signature(fn).parameters

    def call(args=None):
        a = dict(args or {})
        if needs_ctx and "ctx" not in a:
            a["ctx"] = None
        result = fn(**a)
        if inspect.iscoroutine(result):
            result = asyncio.run(result)
        return result

    return call


def test_autofix_silkscreen_resolves_overlap(mcp_server, tmp_path):
    """fix_silkscreen must run the embedded script without a NameError and
    move the overlapping silk apart.

    Regression: with ``GEOMETRY_HELPER`` missing from the silk script, this
    call raises ``RuntimeError`` (wrapping the subprocess ``NameError``) instead
    of returning a result.
    """
    pcb_path = str(tmp_path / "silk.kicad_pcb")
    pro_path = str(tmp_path / "silk.kicad_pro")
    pro_path_obj = tmp_path / "silk.kicad_pro"
    pro_path_obj.write_text(json.dumps({"board": {}, "meta": {"version": 1}}))

    pcb = _call(mcp_server, "pcb")
    assert pcb({"operation": "create", "pcb_path": pcb_path}).get("status") == "ok"
    assert pcb({
        "operation": "set_outline", "pcb_path": pcb_path,
        "x_mm": 0, "y_mm": 0, "width_mm": 40, "height_mm": 30,
    }).get("status") == "ok"

    # Find a real footprint (lib_id strings drift between KiCad versions, so
    # discover one rather than hardcoding).
    lib = _call(mcp_server, "library")
    sr = lib({"operation": "search", "query": "0603 resistor"})
    assert sr.get("status") == "ok" and sr.get("results")
    fp = sr["results"][0]

    # Two footprints essentially stacked → their reference silkscreen overlaps
    # (and DRC reports silk_overlap). The autofix silk loop repositions
    # footprint reference/value fields, so the overlap must involve footprint
    # silk text — not free-standing PCB text, which the loop never moves.
    for ref, x, y in [("R1", 20.0, 15.0), ("R2", 20.3, 15.2)]:
        assert pcb({
            "operation": "place_footprint", "pcb_path": pcb_path,
            "library": fp["library"], "footprint_name": fp["name"],
            "reference": ref, "value": "10k", "x_mm": x, "y_mm": y,
        }).get("status") == "ok"

    # Precondition: DRC sees a silkscreen overlap (else the autofix would skip
    # the silk block entirely and this test would not exercise the bug).
    from kicad_mcp.tools.drc_impl.cli_drc import run_drc_via_cli
    before = asyncio.run(run_drc_via_cli(pcb_path, ctx=None))
    assert before.get("success")
    silk_before = before["violation_categories"].get("silk_overlap", 0)
    assert silk_before >= 1, (
        "test setup failed to produce a silk_overlap violation; "
        f"categories were {before['violation_categories']}"
    )

    # Run autofix with ONLY the silkscreen fixer. With the bug this raises
    # RuntimeError; with the fix it returns a result dict.
    drc = _call(mcp_server, "drc")
    result = drc({
        "operation": "autofix",
        "pcb_path": pcb_path,
        "project_path": pro_path,
        "fix_silkscreen": True,
        "fix_routing": False,
        "fix_placement": False,
    })

    assert result.get("status") == "ok", result
    # The silk script actually executed (not silently skipped): there is a
    # silkscreen action describing the move/hide it performed.
    actions = result.get("actions_taken", [])
    assert any(a.startswith("silkscreen") for a in actions), actions
    # And it resolved the overlap — silk_overlap count strictly decreased.
    silk_after = result["after"]["categories"].get("silk_overlap", 0)
    assert silk_after < silk_before, (
        f"silk_overlap not reduced: before={silk_before} after={silk_after}; "
        f"after categories {result['after']['categories']}"
    )
