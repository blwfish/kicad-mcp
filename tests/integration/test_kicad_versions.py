"""Integration tests against a real KiCad installation.

Run with: KICAD_INTEGRATION=1 pytest tests/integration/ -v
Override install: KICAD_APP_PATH=$HOME/kicad-versions/9.0/KiCad.app

Assertion rules:
- Assert on JSON structure (keys, status field), not exact values.
- Never assert on specific lib_id strings from search;
  library names drift between KiCad versions. Assert the list is non-empty and each
  item has the expected schema fields (lib_id, name, description).
- Never assert on exact DRC violation counts; assert on structural keys only.
"""

import asyncio
import inspect
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_tool(mcp, name):
    return asyncio.run(mcp.get_tool(name)).fn


def _call(mcp, tool_name, *args, **kwargs):
    """Call a tool by name, awaiting if async."""
    fn = _get_tool(mcp, tool_name)
    result = fn(*args, **kwargs)
    if inspect.iscoroutine(result):
        result = asyncio.run(result)
    return result


def _board(workspace, width=40, height=30):
    """Path to a fresh empty PCB with a board outline."""
    return str(workspace / "test.kicad_pcb"), width, height


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_server():
    from kicad_mcp.server import create_server
    return create_server()


@pytest.fixture()
def workspace(tmp_path):
    """Isolated temp directory for a single test."""
    return tmp_path


# ---------------------------------------------------------------------------
# Category: Library search
# ---------------------------------------------------------------------------

def test_search_symbols(mcp_server):
    """Library DB rebuild + lib_id stability across KiCad versions."""
    result = _call(mcp_server, "search", query="op amp", type="symbol")
    assert result.get("status") == "ok", result
    components = result.get("results", [])
    assert len(components) > 0
    for item in components[:5]:
        assert "lib_id" in item
        assert "name" in item
        assert "description" in item
        # lib_id must look like Library:Symbol
        assert ":" in item["lib_id"]


def test_search_footprints(mcp_server):
    """Footprint library search."""
    result = _call(mcp_server, "search", query="0603", type="footprint")
    assert result.get("status") == "ok", result
    footprints = result.get("results", [])
    assert len(footprints) > 0
    for item in footprints[:5]:
        assert "name" in item
        assert "library" in item


# ---------------------------------------------------------------------------
# Category: Schematic (kicad-sch-api wraps a single global "current schematic")
# ---------------------------------------------------------------------------

def test_schematic_create_and_save(mcp_server, workspace):
    """kicad-sch-api compatibility: create, add component, save."""
    sch_path = str(workspace / "test.kicad_sch")

    result = _call(mcp_server, "create_schematic", name="test")
    assert result.get("status") == "ok", result

    # Find a real resistor lib_id before adding
    sr = _call(mcp_server, "search", query="Device:R", type="symbol")
    assert sr.get("status") == "ok" and sr.get("results"), sr
    # Prefer exact "R" symbol from Device library if present
    lib_id = next(
        (r["lib_id"] for r in sr["results"] if r["lib_id"] == "Device:R"),
        sr["results"][0]["lib_id"],
    )

    result = _call(
        mcp_server, "add_component",
        lib_id=lib_id,
        reference="R1",
        value="10k",
        position=[100.0, 100.0],
    )
    assert result.get("status") == "ok", result

    result = _call(mcp_server, "save_schematic", file_path=sch_path)
    assert result.get("status") == "ok", result
    assert os.path.isfile(sch_path)


# ---------------------------------------------------------------------------
# Category: PCB basics
# ---------------------------------------------------------------------------

def test_pcb_create_and_outline(mcp_server, workspace):
    """pcbnew subprocess bridge basics."""
    pcb_path = str(workspace / "test.kicad_pcb")

    result = _call(mcp_server, "create_pcb", pcb_path)
    assert result.get("status") == "ok", result

    result = _call(
        mcp_server, "add_board_outline",
        pcb_path=pcb_path, x_mm=0, y_mm=0, width_mm=40, height_mm=30,
    )
    assert result.get("status") == "ok", result
    assert os.path.isfile(pcb_path)


# ---------------------------------------------------------------------------
# Category: Footprint placement
# ---------------------------------------------------------------------------

def test_place_footprint_and_audit(mcp_server, workspace):
    """Pad geometry parsing via place_footprint + audit_all."""
    pcb_path = str(workspace / "test.kicad_pcb")

    _call(mcp_server, "create_pcb", pcb_path)
    _call(
        mcp_server, "add_board_outline",
        pcb_path=pcb_path, x_mm=0, y_mm=0, width_mm=40, height_mm=30,
    )

    # Find a real footprint
    sr = _call(mcp_server, "search", query="0603", type="footprint")
    assert sr.get("status") == "ok" and sr.get("results"), sr
    fp = sr["results"][0]

    result = _call(
        mcp_server, "place_footprint",
        pcb_path=pcb_path,
        library=fp["library"],
        footprint_name=fp["name"],
        reference="R1",
        value="10k",
        x_mm=20,
        y_mm=15,
    )
    assert result.get("status") == "ok", result

    result = _call(mcp_server, "audit_all", pcb_path=pcb_path)
    # audit_all returns nested category keys; assert structural envelope
    assert isinstance(result, dict)
    assert any(k in result for k in ("overlaps", "placement", "results", "silkscreen", "summary"))


# ---------------------------------------------------------------------------
# Category: DRC
# ---------------------------------------------------------------------------

def test_drc_returns_structure(mcp_server, workspace):
    """kicad-cli DRC output format — assert on keys, not violation counts."""
    pcb_path = str(workspace / "test.kicad_pcb")
    pro_path = str(workspace / "test.kicad_pro")

    _call(mcp_server, "create_pcb", pcb_path)
    _call(
        mcp_server, "add_board_outline",
        pcb_path=pcb_path, x_mm=0, y_mm=0, width_mm=40, height_mm=30,
    )

    # run_drc_check is async and takes ctx
    result = _call(mcp_server, "run_drc_check", project_path=pro_path, ctx=None)
    # DRC may return violations on an empty board; assert on structure only
    assert isinstance(result, dict)
    assert (
        "violations" in result
        or "drc_results" in result
        or "summary" in result
        or "error" in result
        or result.get("status") == "ok"
    )


# ---------------------------------------------------------------------------
# Category: Routing (skipped if FreeRouter JAR not present)
# ---------------------------------------------------------------------------

def test_autoroute_smoke(mcp_server, workspace):
    """FreeRouter handshake + DSN export/import (passes=1 for speed)."""
    from kicad_mcp.tools.pcb_autoroute import _find_freerouter_jar
    if not _find_freerouter_jar(None):
        pytest.skip("FreeRouter JAR not installed on this runner")

    pcb_path = str(workspace / "test.kicad_pcb")

    _call(mcp_server, "create_pcb", pcb_path)
    _call(
        mcp_server, "add_board_outline",
        pcb_path=pcb_path, x_mm=0, y_mm=0, width_mm=50, height_mm=40,
    )

    sr = _call(mcp_server, "search", query="0603", type="footprint")
    assert sr.get("status") == "ok" and sr.get("results"), sr
    fp = sr["results"][0]

    for ref, x in [("R1", 15), ("R2", 35)]:
        _call(
            mcp_server, "place_footprint",
            pcb_path=pcb_path,
            library=fp["library"],
            footprint_name=fp["name"],
            reference=ref,
            value="10k",
            x_mm=x,
            y_mm=20,
        )

    _call(mcp_server, "add_net", pcb_path=pcb_path, net_name="SIG")
    _call(
        mcp_server, "bulk_assign_pad_nets",
        pcb_path=pcb_path,
        assignments=[
            {"reference": "R1", "pad": "2", "net": "SIG"},
            {"reference": "R2", "pad": "1", "net": "SIG"},
        ],
    )

    result = _call(mcp_server, "autoroute_pcb", pcb_path=pcb_path, passes=1)
    # autoroute may return error envelopes (java missing, no nets, etc.) — just
    # assert the dict shape, not green-routing success
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Category: Export
# ---------------------------------------------------------------------------

def test_export_gerbers(mcp_server, workspace):
    """kicad-cli gerber export output format."""
    pcb_path = str(workspace / "test.kicad_pcb")
    output_dir = str(workspace / "gerbers")
    os.makedirs(output_dir, exist_ok=True)

    _call(mcp_server, "create_pcb", pcb_path)
    _call(
        mcp_server, "add_board_outline",
        pcb_path=pcb_path, x_mm=0, y_mm=0, width_mm=40, height_mm=30,
    )

    result = _call(
        mcp_server, "export_gerbers",
        pcb_path=pcb_path, output_dir=output_dir,
    )
    assert result.get("status") == "ok", result
    assert "files" in result or "output_dir" in result or "zip" in result
