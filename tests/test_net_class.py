"""
Tests for set_net_class tool and autoroute_pcb net_classes parameter.

Unit tests that don't require KiCad's pcbnew bindings.
"""

import json
import os

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_nets import register_pcb_net_tools, _default_net_class


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def net_server():
    """Create a FastMCP server with only net tools registered."""
    mcp = FastMCP("test-nets")
    register_pcb_net_tools(mcp)
    return mcp


def _get_tool_fn(mcp_server, tool_name):
    """Extract a tool function from the FastMCP 3.0 server by name."""
    import asyncio
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


@pytest.fixture
def pcb_and_pro(tmp_path):
    """Create matching .kicad_pcb and .kicad_pro files."""
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    pro = tmp_path / "test.kicad_pro"
    pro.write_text(json.dumps({
        "net_settings": {
            "classes": [_default_net_class()],
            "meta": {"version": 4},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        }
    }))
    return str(pcb), str(pro)


# -- set_net_class tests ----------------------------------------------------

class TestSetNetClass:

    def test_pcb_not_found(self, net_server):
        fn = _get_tool_fn(net_server, "set_net_class")
        result = fn("/nonexistent/board.kicad_pcb", "Power", ["GND"])
        assert "error" in result
        assert "not found" in result["error"]

    def test_pro_not_found(self, net_server, tmp_path):
        """set_net_class requires .kicad_pro alongside PCB."""
        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        fn = _get_tool_fn(net_server, "set_net_class")
        result = fn(str(pcb), "Power", ["GND"])
        assert "error" in result
        assert "Project file not found" in result["error"]

    def test_create_new_class(self, net_server, pcb_and_pro):
        pcb_path, pro_path = pcb_and_pro
        fn = _get_tool_fn(net_server, "set_net_class")
        result = fn(
            pcb_path, "Power", ["GND", "+5V", "+3V3"],
            track_width_mm=0.5, clearance_mm=0.3,
        )
        assert result["status"] == "ok"
        assert result["action"] == "created"
        assert result["class_name"] == "Power"
        assert result["track_width_mm"] == 0.5
        assert result["nets_assigned"] == 3

        # Verify the project file was updated
        with open(pro_path) as f:
            pro = json.load(f)
        classes = pro["net_settings"]["classes"]
        assert len(classes) == 2  # Default + Power
        power_cls = [c for c in classes if c["name"] == "Power"][0]
        assert power_cls["track_width"] == 0.5
        assert power_cls["clearance"] == 0.3
        assignments = pro["net_settings"]["netclass_assignments"]
        assert assignments["GND"] == "Power"
        assert assignments["+5V"] == "Power"
        assert assignments["+3V3"] == "Power"

    def test_update_existing_class(self, net_server, pcb_and_pro):
        pcb_path, pro_path = pcb_and_pro
        fn = _get_tool_fn(net_server, "set_net_class")
        # Create
        fn(pcb_path, "Power", ["GND"], track_width_mm=0.4)
        # Update
        result = fn(pcb_path, "Power", ["+5V"], track_width_mm=0.6)
        assert result["action"] == "updated"
        assert result["track_width_mm"] == 0.6

        with open(pro_path) as f:
            pro = json.load(f)
        classes = pro["net_settings"]["classes"]
        power_classes = [c for c in classes if c["name"] == "Power"]
        assert len(power_classes) == 1  # No duplicates
        assert power_classes[0]["track_width"] == 0.6
        # Both nets should be assigned
        assignments = pro["net_settings"]["netclass_assignments"]
        assert assignments["GND"] == "Power"
        assert assignments["+5V"] == "Power"

    def test_multiple_classes(self, net_server, pcb_and_pro):
        pcb_path, pro_path = pcb_and_pro
        fn = _get_tool_fn(net_server, "set_net_class")
        fn(pcb_path, "Power", ["GND", "+5V"], track_width_mm=0.5)
        fn(pcb_path, "HighSpeed", ["SDA", "SCL"], track_width_mm=0.15, clearance_mm=0.15)

        with open(pro_path) as f:
            pro = json.load(f)
        classes = pro["net_settings"]["classes"]
        assert len(classes) == 3  # Default + Power + HighSpeed
        names = {c["name"] for c in classes}
        assert names == {"Default", "Power", "HighSpeed"}

    def test_default_via_params(self, net_server, pcb_and_pro):
        pcb_path, pro_path = pcb_and_pro
        fn = _get_tool_fn(net_server, "set_net_class")
        result = fn(pcb_path, "Signal", ["SIG1"])
        assert result["via_diameter_mm"] == 0.6
        assert result["via_drill_mm"] == 0.3

    def test_missing_net_settings_in_pro(self, net_server, tmp_path):
        """If the .kicad_pro has no net_settings, tool creates it."""
        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        pro = tmp_path / "test.kicad_pro"
        pro.write_text(json.dumps({"board": {}}))

        fn = _get_tool_fn(net_server, "set_net_class")
        result = fn(str(pcb), "Power", ["GND"], track_width_mm=0.5)
        assert result["status"] == "ok"

        with open(str(pro)) as f:
            data = json.load(f)
        assert "net_settings" in data
        assert len(data["net_settings"]["classes"]) == 2  # Default + Power


# -- _default_net_class tests -----------------------------------------------

class TestDefaultNetClass:

    def test_has_required_fields(self):
        nc = _default_net_class()
        assert nc["name"] == "Default"
        assert nc["track_width"] == 0.2
        assert nc["clearance"] == 0.2
        assert nc["via_diameter"] == 0.6
        assert nc["via_drill"] == 0.3

    def test_returns_new_dict_each_call(self):
        a = _default_net_class()
        b = _default_net_class()
        assert a is not b
        a["name"] = "Modified"
        assert b["name"] == "Default"
