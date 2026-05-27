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
import os
import shutil

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


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def mcp_server():
    from kicad_mcp.server import mcp
    return mcp


@pytest.fixture()
def workspace(tmp_path):
    """Isolated temp directory for a single test."""
    return tmp_path


# ---------------------------------------------------------------------------
# Category: Library search
# ---------------------------------------------------------------------------

def test_search_symbols(mcp_server):
    """Library DB rebuild + lib_id stability across KiCad versions."""
    fn = _get_tool(mcp_server, "search")
    result = _run(fn({"query": "op amp", "type": "symbol"}))
    assert result.get("status") == "ok"
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
    fn = _get_tool(mcp_server, "search")
    result = _run(fn({"query": "0603 resistor"}))
    assert result.get("status") == "ok"
    footprints = result.get("results", [])
    assert len(footprints) > 0
    for item in footprints[:5]:
        assert "name" in item
        assert "library" in item


# ---------------------------------------------------------------------------
# Category: Schematic
# ---------------------------------------------------------------------------

def test_schematic_create_and_save(mcp_server, workspace):
    """kicad-sch-api compatibility: create, add component, save."""
    sch_path = str(workspace / "test.kicad_sch")

    create = _get_tool(mcp_server, "create_schematic")
    result = _run(create({"schematic_path": sch_path}))
    assert result.get("status") == "ok"

    # Find a real resistor lib_id before adding
    search = _get_tool(mcp_server, "search")
    sr = _run(search({"query": "resistor", "type": "symbol"}))
    assert sr.get("status") == "ok" and sr.get("results")
    lib_id = sr["results"][0]["lib_id"]

    add = _get_tool(mcp_server, "add_component")
    result = _run(add({
        "schematic_path": sch_path,
        "lib_id": lib_id,
        "reference": "R1",
        "value": "10k",
        "position": [100, 100],
    }))
    assert result.get("status") == "ok"

    save = _get_tool(mcp_server, "save_schematic")
    result = _run(save({"schematic_path": sch_path}))
    assert result.get("status") == "ok"
    assert os.path.isfile(sch_path)


# ---------------------------------------------------------------------------
# Category: PCB basics
# ---------------------------------------------------------------------------

def test_pcb_create_and_outline(mcp_server, workspace):
    """pcbnew subprocess bridge basics."""
    pcb_path = str(workspace / "test.kicad_pcb")

    create = _get_tool(mcp_server, "create_pcb")
    result = _run(create({"pcb_path": pcb_path}))
    assert result.get("status") == "ok"

    outline = _get_tool(mcp_server, "add_board_outline")
    result = _run(outline({
        "pcb_path": pcb_path,
        "x_mm": 0,
        "y_mm": 0,
        "width_mm": 40,
        "height_mm": 30,
    }))
    assert result.get("status") == "ok"
    assert os.path.isfile(pcb_path)


# ---------------------------------------------------------------------------
# Category: Footprint placement
# ---------------------------------------------------------------------------

def test_place_footprint_and_audit(mcp_server, workspace):
    """Pad geometry parsing via place_footprint + audit_all."""
    pcb_path = str(workspace / "test.kicad_pcb")

    _run(_get_tool(mcp_server, "create_pcb")({"pcb_path": pcb_path}))
    _run(_get_tool(mcp_server, "add_board_outline")({
        "pcb_path": pcb_path, "x_mm": 0, "y_mm": 0, "width_mm": 40, "height_mm": 30,
    }))

    # Find a real footprint
    search = _get_tool(mcp_server, "search")
    sr = _run(search({"query": "0603 resistor"}))
    assert sr.get("status") == "ok" and sr.get("results")
    fp = sr["results"][0]

    place = _get_tool(mcp_server, "place_footprint")
    result = _run(place({
        "pcb_path": pcb_path,
        "library": fp["library"],
        "footprint_name": fp["name"],
        "reference": "R1",
        "value": "10k",
        "x_mm": 20,
        "y_mm": 15,
    }))
    assert result.get("status") == "ok"

    audit = _get_tool(mcp_server, "audit_all")
    result = _run(audit({"pcb_path": pcb_path}))
    assert result.get("status") == "ok"
    # audit_all returns overlap/silkscreen/keepout sub-keys
    assert "overlaps" in result or "placement" in result or "results" in result


# ---------------------------------------------------------------------------
# Category: DRC
# ---------------------------------------------------------------------------

def test_drc_returns_structure(mcp_server, workspace):
    """kicad-cli DRC output format — assert on keys, not violation counts."""
    pcb_path = str(workspace / "test.kicad_pcb")
    pro_path = str(workspace / "test.kicad_pro")

    _run(_get_tool(mcp_server, "create_pcb")({"pcb_path": pcb_path}))
    _run(_get_tool(mcp_server, "add_board_outline")({
        "pcb_path": pcb_path, "x_mm": 0, "y_mm": 0, "width_mm": 40, "height_mm": 30,
    }))

    drc = _get_tool(mcp_server, "run_drc_check")
    result = _run(drc({"project_path": pro_path, "pcb_path": pcb_path}))
    # DRC may return violations on an empty board; assert on structure only
    assert "violations" in result or "drc_results" in result or result.get("status") == "ok"


# ---------------------------------------------------------------------------
# Category: Routing
# ---------------------------------------------------------------------------

def test_autoroute_smoke(mcp_server, workspace):
    """FreeRouter handshake + DSN export/import (passes=1 for speed)."""
    pcb_path = str(workspace / "test.kicad_pcb")

    _run(_get_tool(mcp_server, "create_pcb")({"pcb_path": pcb_path}))
    _run(_get_tool(mcp_server, "add_board_outline")({
        "pcb_path": pcb_path, "x_mm": 0, "y_mm": 0, "width_mm": 50, "height_mm": 40,
    }))

    # Place two footprints and connect them via a net
    search = _get_tool(mcp_server, "search")
    sr = _run(search({"query": "0603 resistor"}))
    fp = sr["results"][0]

    for ref, x in [("R1", 15), ("R2", 35)]:
        _run(_get_tool(mcp_server, "place_footprint")({
            "pcb_path": pcb_path, "library": fp["library"],
            "footprint_name": fp["name"], "reference": ref, "value": "10k",
            "x_mm": x, "y_mm": 20,
        }))

    _run(_get_tool(mcp_server, "add_net")({"pcb_path": pcb_path, "net_name": "SIG"}))
    _run(_get_tool(mcp_server, "bulk_assign_pad_nets")({
        "pcb_path": pcb_path,
        "assignments": [
            {"reference": "R1", "pad": "2", "net": "SIG"},
            {"reference": "R2", "pad": "1", "net": "SIG"},
        ],
    }))

    autoroute = _get_tool(mcp_server, "autoroute_pcb")
    result = _run(autoroute({"pcb_path": pcb_path, "passes": 1}))
    assert result.get("status") == "ok"
    assert "routed" in result or "unrouted" in result or "connections" in result


# ---------------------------------------------------------------------------
# Category: Export
# ---------------------------------------------------------------------------

def test_export_gerbers(mcp_server, workspace):
    """kicad-cli gerber export output format."""
    pcb_path = str(workspace / "test.kicad_pcb")
    output_dir = str(workspace / "gerbers")
    os.makedirs(output_dir, exist_ok=True)

    _run(_get_tool(mcp_server, "create_pcb")({"pcb_path": pcb_path}))
    _run(_get_tool(mcp_server, "add_board_outline")({
        "pcb_path": pcb_path, "x_mm": 0, "y_mm": 0, "width_mm": 40, "height_mm": 30,
    }))

    export = _get_tool(mcp_server, "export_gerbers")
    result = _run(export({"pcb_path": pcb_path, "output_dir": output_dir}))
    assert result.get("status") == "ok"
    assert "files" in result or "output_dir" in result
