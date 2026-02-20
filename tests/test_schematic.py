"""
Tests for the schematic MCP tools.

These are unit tests that use the real kicad-sch-api library (no mocking)
since it is a pure Python library that works without KiCad installed.
The tests verify the MCP tool wrappers correctly delegate to kicad-sch-api
and return well-structured results.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.schematic import register_schematic_tools, _current_schematic
import kicad_mcp.tools.schematic as sch_module


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def sch_server():
    """Create a FastMCP server with only schematic tools registered."""
    mcp = FastMCP("test-schematic")
    register_schematic_tools(mcp)
    return mcp


@pytest.fixture(autouse=True)
def reset_schematic_state():
    """Reset the module-level schematic state between tests."""
    sch_module._current_schematic = None
    yield
    sch_module._current_schematic = None


def _get_tool_fn(mcp_server, tool_name):
    """Extract a tool function from the FastMCP 3.0 server by name."""
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- create_schematic tests --------------------------------------------------

class TestCreateSchematic:

    def test_create_returns_ok(self, sch_server):
        fn = _get_tool_fn(sch_server, "create_schematic")
        result = fn(name="test_circuit")
        assert result["status"] == "ok"
        assert result["name"] == "test_circuit"

    def test_create_sets_module_state(self, sch_server):
        fn = _get_tool_fn(sch_server, "create_schematic")
        fn(name="my_sch")
        assert sch_module._current_schematic is not None

    def test_create_with_default_name(self, sch_server):
        fn = _get_tool_fn(sch_server, "create_schematic")
        result = fn()
        assert result["status"] == "ok"
        assert result["name"] == "untitled"


# -- add_component + list_components tests -----------------------------------

class TestAddAndListComponents:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        """Create a schematic before each test in this class."""
        fn = _get_tool_fn(sch_server, "create_schematic")
        fn(name="test")

    def test_add_component_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_component")
        result = fn(
            lib_id="Device:R",
            reference="R1",
            value="10k",
            position=[100.0, 100.0],
        )
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["lib_id"] == "Device:R"
        assert result["value"] == "10k"
        assert result["position"] == [100.0, 100.0]

    def test_add_component_with_footprint(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_component")
        result = fn(
            lib_id="Device:R",
            reference="R1",
            value="4.7k",
            position=[120.0, 80.0],
            footprint="Resistor_SMD:R_0805_2012Metric",
        )
        assert result["status"] == "ok"
        assert result["reference"] == "R1"

    def test_add_component_bad_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_component")
        result = fn(
            lib_id="Device:R",
            reference="R1",
            value="10k",
            position=[100.0],  # missing y
        )
        assert "error" in result

    def test_list_components_empty(self, sch_server):
        fn = _get_tool_fn(sch_server, "list_components")
        result = fn()
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["components"] == []

    def test_list_components_after_add(self, sch_server):
        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 100])
        add_fn(lib_id="Device:C", reference="C1", value="100nF", position=[150, 100])

        list_fn = _get_tool_fn(sch_server, "list_components")
        result = list_fn()
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "C1"}

    def test_remove_component(self, sch_server):
        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 100])

        rm_fn = _get_tool_fn(sch_server, "remove_component")
        result = rm_fn(reference="R1")
        assert result["status"] == "ok"

        list_fn = _get_tool_fn(sch_server, "list_components")
        result = list_fn()
        assert result["count"] == 0

    def test_remove_nonexistent_component(self, sch_server):
        rm_fn = _get_tool_fn(sch_server, "remove_component")
        result = rm_fn(reference="R99")
        assert "error" in result


# -- add_wire tests ----------------------------------------------------------

class TestAddWire:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "create_schematic")
        fn(name="test")

    def test_add_wire_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire")
        result = fn(start_pos=[100.0, 100.0], end_pos=[200.0, 100.0])
        assert result["status"] == "ok"
        assert "wire_uuid" in result
        assert result["start"] == [100.0, 100.0]
        assert result["end"] == [200.0, 100.0]

    def test_add_wire_bad_positions(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire")
        result = fn(start_pos=[100.0], end_pos=[200.0, 100.0])
        assert "error" in result

    def test_remove_wire(self, sch_server):
        add_fn = _get_tool_fn(sch_server, "add_wire")
        result = add_fn(start_pos=[100.0, 100.0], end_pos=[200.0, 100.0])
        wire_uuid = result["wire_uuid"]

        rm_fn = _get_tool_fn(sch_server, "remove_wire")
        result = rm_fn(wire_uuid=wire_uuid)
        assert result["status"] == "ok"

    def test_remove_nonexistent_wire(self, sch_server):
        rm_fn = _get_tool_fn(sch_server, "remove_wire")
        result = rm_fn(wire_uuid="nonexistent-uuid")
        assert "error" in result


# -- add_label tests ---------------------------------------------------------

class TestAddLabel:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "create_schematic")
        fn(name="test")

    def test_add_label_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        result = fn(text="GND", position=[100.0, 100.0])
        assert result["status"] == "ok"
        assert result["text"] == "GND"
        assert result["position"] == [100.0, 100.0]
        assert "label_uuid" in result

    def test_add_label_with_rotation(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        result = fn(text="VCC", position=[50.0, 50.0], rotation=90.0)
        assert result["status"] == "ok"
        assert result["text"] == "VCC"

    def test_add_label_bad_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        result = fn(text="GND", position=[100.0])
        assert "error" in result

    def test_remove_label(self, sch_server):
        add_fn = _get_tool_fn(sch_server, "add_label")
        result = add_fn(text="SDA", position=[100.0, 100.0])
        label_uuid = result["label_uuid"]

        rm_fn = _get_tool_fn(sch_server, "remove_label")
        result = rm_fn(label_uuid=label_uuid)
        assert result["status"] == "ok"

    def test_remove_nonexistent_label(self, sch_server):
        rm_fn = _get_tool_fn(sch_server, "remove_label")
        result = rm_fn(label_uuid="nonexistent-uuid")
        assert "error" in result


# -- No schematic loaded tests -----------------------------------------------

class TestNoSchematicLoaded:
    """Verify tools fail gracefully when no schematic is loaded."""

    def test_list_components_no_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "list_components")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn()

    def test_add_component_no_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_component")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 100])

    def test_add_wire_no_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn(start_pos=[100, 100], end_pos=[200, 100])

    def test_add_label_no_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn(text="GND", position=[100, 100])

    def test_validate_no_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "validate_schematic")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn()

    def test_get_info_no_schematic(self, sch_server):
        fn = _get_tool_fn(sch_server, "get_schematic_info")
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            fn()


# -- get_schematic_info tests ------------------------------------------------

class TestGetSchematicInfo:

    def test_info_empty_schematic(self, sch_server):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        info_fn = _get_tool_fn(sch_server, "get_schematic_info")
        result = info_fn()
        assert result["status"] == "ok"
        assert result["components"] == 0

    def test_info_after_adding_components(self, sch_server):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 100])
        add_fn(lib_id="Device:C", reference="C1", value="100nF", position=[150, 100])

        info_fn = _get_tool_fn(sch_server, "get_schematic_info")
        result = info_fn()
        assert result["status"] == "ok"
        assert result["components"] == 2


# -- validate_schematic tests ------------------------------------------------

class TestValidateSchematic:

    def test_validate_empty_schematic(self, sch_server):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        validate_fn = _get_tool_fn(sch_server, "validate_schematic")
        result = validate_fn()
        assert result["status"] == "ok"
        assert result["issues"] == 0

    def test_validate_schematic_with_components(self, sch_server):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 100])

        validate_fn = _get_tool_fn(sch_server, "validate_schematic")
        result = validate_fn()
        assert result["status"] == "ok"
        # Validation may or may not find issues depending on kicad-sch-api version
        assert "issues" in result


# -- add_junction tests ------------------------------------------------------

class TestAddJunction:

    def test_add_junction(self, sch_server):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        fn = _get_tool_fn(sch_server, "add_junction")
        result = fn(position=[100.0, 100.0])
        assert result["status"] == "ok"
        assert result["position"] == [100.0, 100.0]
        assert "junction_uuid" in result

    def test_add_junction_bad_position(self, sch_server):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        fn = _get_tool_fn(sch_server, "add_junction")
        result = fn(position=[100.0])
        assert "error" in result


# -- save_schematic tests (with tmp_path) ------------------------------------

class TestSaveSchematic:

    def test_save_to_file(self, sch_server, tmp_path):
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="test")

        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 100])

        save_fn = _get_tool_fn(sch_server, "save_schematic")
        save_path = str(tmp_path / "test.kicad_sch")
        result = save_fn(file_path=save_path)
        assert result["status"] == "ok"

        # Verify the file was created
        import os
        assert os.path.exists(save_path)
        content = open(save_path).read()
        assert "kicad_sch" in content


# -- Integration: full workflow test -----------------------------------------

class TestSchematicWorkflow:
    """Test a realistic workflow of creating a schematic with components, wires, and labels."""

    def test_full_workflow(self, sch_server):
        # Create schematic
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        result = create_fn(name="voltage_divider")
        assert result["status"] == "ok"

        # Add two resistors
        add_fn = _get_tool_fn(sch_server, "add_component")
        r1 = add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100, 80])
        r2 = add_fn(lib_id="Device:R", reference="R2", value="10k", position=[100, 120])
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

        # Add wire between them
        wire_fn = _get_tool_fn(sch_server, "add_wire")
        w1 = wire_fn(start_pos=[100.0, 90.0], end_pos=[100.0, 110.0])
        assert w1["status"] == "ok"

        # Add labels
        label_fn = _get_tool_fn(sch_server, "add_label")
        l1 = label_fn(text="VCC", position=[100.0, 70.0])
        l2 = label_fn(text="GND", position=[100.0, 130.0])
        l3 = label_fn(text="VOUT", position=[110.0, 100.0])
        assert l1["status"] == "ok"
        assert l2["status"] == "ok"
        assert l3["status"] == "ok"

        # Verify state
        list_fn = _get_tool_fn(sch_server, "list_components")
        result = list_fn()
        assert result["count"] == 2

        info_fn = _get_tool_fn(sch_server, "get_schematic_info")
        info = info_fn()
        assert info["components"] == 2

        # Validate
        validate_fn = _get_tool_fn(sch_server, "validate_schematic")
        result = validate_fn()
        assert result["status"] == "ok"
