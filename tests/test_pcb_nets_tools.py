"""
Tests for PCB net tools: add, assign, bulk assign, list, rename, net class.

Some tools (add_net, rename_net) use direct file editing — these are tested
with real file I/O. Others delegate to run_pcbnew_script — those are mocked.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb import register_pcb_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def pcb_server():
    mcp = FastMCP("test-pcb")
    register_pcb_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    """PCB file with existing net definitions."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '\t(net 0 "")\n'
        '\t(net 1 "GND")\n'
        '\t(net 2 "VCC")\n'
        ")\n"
    )
    return str(pcb)


@pytest.fixture
def pcb_with_project(tmp_path):
    """PCB + project file for set_net_class tests."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        '\t(net 0 "")\n'
        '\t(net 1 "GND")\n'
        ")\n"
    )
    pro = tmp_path / "test.kicad_pro"
    pro.write_text(json.dumps({"meta": {"filename": "test.kicad_pro"}}, indent=2))
    return {"pcb_path": str(pcb), "pro_path": str(pro)}


def _get_pcb_fn(mcp_server):
    tool = asyncio.run(mcp_server.get_tool("pcb"))
    if tool is None:
        raise ValueError("Tool 'pcb' not found")
    return tool.fn


# -- add_net tests -----------------------------------------------------------

class TestAddNet:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_net",
                    pcb_path="/nonexistent/board.kicad_pcb", net_name="SDA")
        assert "error" in result

    def test_adds_new_net(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_net", pcb_path=pcb_file, net_name="SDA")
        assert result["status"] == "ok"
        assert result["net"] == "SDA"
        assert result["net_code"] == 3  # next after 2

        # Verify file was updated
        with open(pcb_file) as f:
            content = f.read()
        assert '(net 3 "SDA")' in content

    def test_existing_net_returns_ok(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("add_net", pcb_path=pcb_file, net_name="GND")
        assert result["status"] == "ok"
        assert result["note"] == "Net already exists"
        assert result["net_code"] == 1

    def test_adds_multiple_nets_sequentially(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        r1 = fn("add_net", pcb_path=pcb_file, net_name="SDA")
        r2 = fn("add_net", pcb_path=pcb_file, net_name="SCL")
        assert r1["net_code"] == 3
        assert r2["net_code"] == 4


# -- rename_net tests --------------------------------------------------------

class TestRenameNet:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("rename_net",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    old_name="GND", new_name="AGND")
        assert "error" in result

    def test_renames_net(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("rename_net", pcb_path=pcb_file, old_name="GND", new_name="AGND")
        assert result["status"] == "ok"
        assert result["old_name"] == "GND"
        assert result["new_name"] == "AGND"
        assert result["replacements"] >= 1

        with open(pcb_file) as f:
            content = f.read()
        assert '"AGND"' in content
        assert '(net 1 "GND")' not in content

    def test_rename_nonexistent_net(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("rename_net", pcb_path=pcb_file, old_name="NOSUCHNET", new_name="NEWNAME")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_rename_to_existing_name(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("rename_net", pcb_path=pcb_file, old_name="GND", new_name="VCC")
        assert "error" in result
        assert "already exists" in result["error"]

    def test_same_name_noop(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("rename_net", pcb_path=pcb_file, old_name="GND", new_name="GND")
        assert result["status"] == "ok"
        assert result["replacements"] == 0


# -- assign_pad_net tests ----------------------------------------------------

class TestAssignPadNet:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("assign_pad_net",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    reference="R1", pad_number="1", net_name="GND")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_nets.run_pcbnew_script")
    def test_assigns_pad(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "reference": "R1",
            "pad": "1",
            "net": "GND",
            "sub_pads": 1,
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("assign_pad_net",
                    pcb_path=pcb_file, reference="R1", pad_number="1", net_name="GND")
        assert result["status"] == "ok"
        assert result["reference"] == "R1"

    @patch("kicad_mcp.tools.pcb_nets.run_pcbnew_script")
    def test_passes_params(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {"status": "ok", "reference": "U1",
                                  "pad": "3", "net": "VCC", "sub_pads": 1}
        fn = _get_pcb_fn(pcb_server)
        fn("assign_pad_net",
           pcb_path=pcb_file, reference="U1", pad_number="3", net_name="VCC")
        params = mock_run.call_args[1]["params"]
        assert params["reference"] == "U1"
        assert params["pad_number"] == "3"
        assert params["net_name"] == "VCC"


# -- bulk_assign_pad_nets tests ----------------------------------------------

class TestBulkAssignPadNets:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("bulk_assign_pad_nets",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    assignments=[{"reference": "R1", "pad": "1", "net": "GND"}])
        assert "error" in result

    def test_empty_assignments(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("bulk_assign_pad_nets",
                    pcb_path=pcb_file, assignments=[])
        assert "error" in result
        assert "assignments" in result["error"]

    @patch("kicad_mcp.tools.pcb_nets.run_pcbnew_script")
    def test_bulk_assign(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "assigned": 3,
            "nets_created": [],
            "errors": [],
            "results": [
                {"reference": "R1", "pad": "1", "net": "GND", "sub_pads": 1},
                {"reference": "R1", "pad": "2", "net": "VCC", "sub_pads": 1},
                {"reference": "C1", "pad": "1", "net": "VCC", "sub_pads": 1},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        assignments = [
            {"reference": "R1", "pad": "1", "net": "GND"},
            {"reference": "R1", "pad": "2", "net": "VCC"},
            {"reference": "C1", "pad": "1", "net": "VCC"},
        ]
        result = fn("bulk_assign_pad_nets", pcb_path=pcb_file, assignments=assignments)
        assert result["assigned"] == 3
        assert len(result["errors"]) == 0


# -- list_nets tests ---------------------------------------------------------

class TestListNets:

    def test_file_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("list_nets", pcb_path="/nonexistent/board.kicad_pcb")
        assert "error" in result

    @patch("kicad_mcp.tools.pcb_nets.run_pcbnew_script")
    def test_lists_nets(self, mock_run, pcb_server, pcb_file):
        mock_run.return_value = {
            "status": "ok",
            "net_count": 3,
            "nets": [
                {"code": 1, "name": "GND"},
                {"code": 2, "name": "VCC"},
                {"code": 3, "name": "SDA"},
            ],
        }
        fn = _get_pcb_fn(pcb_server)
        result = fn("list_nets", pcb_path=pcb_file)
        assert result["net_count"] == 3
        assert result["nets"][0]["name"] == "GND"


# -- set_net_class tests -----------------------------------------------------

class TestSetNetClass:

    def test_pcb_not_found(self, pcb_server):
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_net_class",
                    pcb_path="/nonexistent/board.kicad_pcb",
                    class_name="Power", nets=["GND"])
        assert "error" in result

    def test_project_not_found(self, pcb_server, pcb_file):
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_net_class",
                    pcb_path=pcb_file, class_name="Power", nets=["GND"])
        assert "error" in result
        assert "Project file not found" in result["error"]

    def test_creates_net_class(self, pcb_server, pcb_with_project):
        pcb_path = pcb_with_project["pcb_path"]
        pro_path = pcb_with_project["pro_path"]
        fn = _get_pcb_fn(pcb_server)
        result = fn("set_net_class",
                    pcb_path=pcb_path, class_name="Power", nets=["GND", "VCC"],
                    track_width_mm=0.5, clearance_mm=0.3)
        assert result["status"] == "ok"
        assert result["action"] == "created"
        assert result["nets_assigned"] == 2

        with open(pro_path) as f:
            project = json.load(f)
        assignments = project["net_settings"]["netclass_assignments"]
        assert assignments["GND"] == "Power"
        assert assignments["VCC"] == "Power"

    def test_updates_existing_class(self, pcb_server, pcb_with_project):
        pcb_path = pcb_with_project["pcb_path"]
        fn = _get_pcb_fn(pcb_server)
        # Create
        fn("set_net_class", pcb_path=pcb_path, class_name="Power",
           nets=["GND"], track_width_mm=0.5)
        # Update
        result = fn("set_net_class", pcb_path=pcb_path, class_name="Power",
                    nets=["GND"], track_width_mm=0.8)
        assert result["action"] == "updated"
        assert result["track_width_mm"] == 0.8
