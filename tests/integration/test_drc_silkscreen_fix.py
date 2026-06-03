"""Integration regression test for the drc_autofix silkscreen step (real pcbnew).

A NameError hid here for a release cycle: the silk step's embedded pcbnew script
calls ``aabb_overlap``/``aabb_inside``, which only exist in the subprocess if
``GEOMETRY_HELPER`` is concatenated into the script source. It wasn't. Unit tests
mock ``run_pcbnew_script``, so the subprocess never ran and the missing injection
was invisible.

Worse, ``_op_autofix`` SWALLOWS a failed silk subprocess — it only appends an
action when ``silk_result["status"] == "ok"`` and otherwise still returns status
"ok". So a coarse "did it return ok" assertion would NOT catch the bug. The
discriminating signal is a ``"silkscreen:"`` entry in ``actions_taken``, present
iff the subprocess completed without error. With the helper missing, the
subprocess raises NameError, no action is appended, and this test fails.
"""
import asyncio
import inspect
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)

_MINIMAL_PRO = {
    "board": {"design_settings": {}},
    "net_settings": {"classes": [{
        "name": "Default", "clearance": 0.2, "track_width": 0.25,
        "via_diameter": 0.6, "via_drill": 0.3, "microvia_diameter": 0.3,
        "microvia_drill": 0.1, "diff_pair_gap": 0.25, "diff_pair_width": 0.2,
    }], "meta": {"version": 3}},
    "meta": {"filename": "board.kicad_pro", "version": 1},
}


@pytest.fixture(scope="module")
def mcp_server():
    from kicad_mcp.server import create_server
    return create_server()


def _call(mcp, tool_name, **kwargs):
    """Invoke an MCP router tool's fn, injecting ctx=None and awaiting if async."""
    fn = asyncio.run(mcp.get_tool(tool_name)).fn
    if "ctx" in inspect.signature(fn).parameters and "ctx" not in kwargs:
        kwargs["ctx"] = None
    result = fn(**kwargs)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return result


def test_drc_autofix_silkscreen_step_runs_on_real_board(mcp_server, tmp_path):
    pcb_path = str(tmp_path / "silk.kicad_pcb")
    pro_path = tmp_path / "silk.kicad_pro"

    # 1) Minimal board + project file (_op_autofix requires the .kicad_pro beside it).
    r = _call(mcp_server, "pcb", operation="create", pcb_path=pcb_path)
    assert r.get("status") == "ok", f"create failed: {r}"
    _call(mcp_server, "pcb", operation="set_outline", pcb_path=pcb_path,
          x_mm=0, y_mm=0, width_mm=40, height_mm=30)
    pro_path.write_text(json.dumps(_MINIMAL_PRO))

    # 2) Two footprints at the SAME position -> their reference-designator silk
    #    overlaps, which KiCad DRC flags as a silkscreen-category violation.
    #    Force a footprint-index rebuild first: this test runs first alphabetically
    #    in the integration suite, so it can't rely on a warm index, and a stale-
    #    but-not-flagged index (cold runner / KiCad-version switch) otherwise yields
    #    0 results. If the runner genuinely has no 0603 footprint we skip rather
    #    than fail — the bug class is still guarded by the unit meta-test.
    _call(mcp_server, "library", operation="rebuild_index", kind="footprints")
    sr = _call(mcp_server, "library", operation="search", query="0603 resistor")
    if not sr.get("results"):
        pytest.skip(f"no 0603 footprint in this runner's KiCad libraries: {sr}")
    fp = sr["results"][0]
    for ref in ("R1", "R2"):
        _call(mcp_server, "pcb", operation="place_footprint", pcb_path=pcb_path,
              library=fp["library"], footprint_name=fp["name"],
              reference=ref, value="10k", x_mm=20, y_mm=15)

    # 3) Autofix, silk step ONLY — isolates the embedded silk script.
    result = _call(mcp_server, "drc", operation="autofix", pcb_path=pcb_path,
                   project_path=str(pro_path),
                   fix_routing=False, fix_placement=False, fix_silkscreen=True)

    assert result.get("status") == "ok", f"autofix errored: {result}"

    # The board must actually have produced a silk violation, else the gated silk
    # branch never runs and the test would pass vacuously.
    before_cats = result["before"]["categories"]
    assert any("silk" in k.lower() for k in before_cats), (
        f"no silkscreen DRC violation produced — silk branch not exercised; "
        f"before categories={before_cats}"
    )

    # THE regression assertion: a 'silkscreen:' action is appended iff the silk
    # subprocess completed without error. A NameError from a missing helper
    # injection is swallowed by _op_autofix and leaves no silkscreen entry.
    assert any(a.startswith("silkscreen:") for a in result["actions_taken"]), (
        f"silk step did not complete — likely a subprocess error swallowed by "
        f"_op_autofix (e.g. NameError from missing GEOMETRY_HELPER injection). "
        f"actions_taken={result['actions_taken']}"
    )
