"""
Tests for the schematic domain router (phase 5).

Uses the real kicad-sch-api library (no mocking) since it is a pure
Python library that works without KiCad installed.
All calls go through the single `schematic` router tool with operation=.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.schematic_router import register_schematic_router
import kicad_mcp.tools.schematic as sch_module


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def sch_server():
    """Create a FastMCP server with only the schematic router registered."""
    mcp = FastMCP("test-schematic")
    register_schematic_router(mcp)
    return mcp


@pytest.fixture(autouse=True)
def reset_schematic_state():
    """Reset the module-level schematic state between tests."""
    sch_module._current_schematic = None
    yield
    sch_module._current_schematic = None


def _get_schematic_fn(mcp_server):
    """Extract the schematic router function."""
    tool = asyncio.run(mcp_server.get_tool("schematic"))
    if tool is None:
        raise ValueError("Tool 'schematic' not found")
    return tool.fn


def _call(fn, operation, **kwargs):
    """Call the schematic router (async) synchronously."""
    return asyncio.run(fn(operation=operation, **kwargs))


# -- create tests ------------------------------------------------------------

class TestCreate:

    def test_create_returns_ok(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "create", name="test_circuit")
        assert result["status"] == "ok"
        assert result["name"] == "test_circuit"

    def test_create_sets_module_state(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="my_sch")
        assert sch_module._current_schematic is not None

    def test_create_with_default_name(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "create")
        assert result["status"] == "ok"
        assert result["name"] == "untitled"


# -- add_component + list_components tests -----------------------------------

class TestAddAndListComponents:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        self._fn = fn

    def test_add_component_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_component",
                       lib_id="Device:R", reference="R1", value="10k",
                       position=[101.6, 101.6])
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["lib_id"] == "Device:R"
        assert result["value"] == "10k"
        assert result["position"] == [101.6, 101.6]

    def test_add_component_with_footprint(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_component",
                       lib_id="Device:R", reference="R1", value="4.7k",
                       position=[120.0, 80.0],
                       footprint="Resistor_SMD:R_0805_2012Metric")
        assert result["status"] == "ok"
        assert result["reference"] == "R1"

    def test_add_component_bad_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_component",
                       lib_id="Device:R", reference="R1", value="10k",
                       position=[100.0])  # missing y
        assert "error" in result

    def test_list_components_empty(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "list_components")
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["components"] == []

    def test_list_components_after_add(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF",
              position=[150, 100])
        result = _call(fn, "list_components")
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "C1"}

    def test_remove_component(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        result = _call(fn, "remove_component", reference="R1")
        assert result["status"] == "ok"
        result = _call(fn, "list_components")
        assert result["count"] == 0

    def test_remove_nonexistent_component(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "remove_component", reference="R99")
        assert "error" in result


# -- add_wire tests ----------------------------------------------------------

class TestAddWire:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_wire_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_wire",
                       start_pos=[101.6, 101.6], end_pos=[203.2, 101.6])
        assert result["status"] == "ok"
        assert "wire_uuid" in result
        assert result["start"] == [101.6, 101.6]
        assert result["end"] == [203.2, 101.6]

    def test_add_wire_bad_positions(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_wire", start_pos=[100.0], end_pos=[200.0, 100.0])
        assert "error" in result

    def test_remove_wire(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_wire", start_pos=[100.0, 100.0], end_pos=[200.0, 100.0])
        wire_uuid = result["wire_uuid"]
        result = _call(fn, "remove_wire", wire_uuid=wire_uuid)
        assert result["status"] == "ok"

    def test_remove_nonexistent_wire(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "remove_wire", wire_uuid="nonexistent-uuid")
        assert "error" in result


# -- add_label tests ---------------------------------------------------------

class TestAddLabel:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_label_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text="GND", position=[101.6, 101.6])
        assert result["status"] == "ok"
        assert result["text"] == "GND"
        assert result["position"] == [101.6, 101.6]
        assert "label_uuid" in result

    def test_add_label_with_rotation(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text="VCC", position=[50.0, 50.0], rotation=90.0)
        assert result["status"] == "ok"
        assert result["text"] == "VCC"

    def test_add_label_bad_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text="GND", position=[100.0])
        assert "error" in result

    def test_remove_label(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text="SDA", position=[100.0, 100.0])
        label_uuid = result["label_uuid"]
        result = _call(fn, "remove_label", label_uuid=label_uuid)
        assert result["status"] == "ok"

    def test_remove_nonexistent_label(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "remove_label", label_uuid="nonexistent-uuid")
        assert "error" in result


# -- grid snap tests ---------------------------------------------------------

class TestGridSnap:
    """Coordinates passed to schematic tools are snapped to the 1.27 mm KiCad grid."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_component_on_grid_unchanged(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_component",
                       lib_id="Device:R", reference="R1", value="10k",
                       position=[101.6, 101.6])
        assert result["position"] == [101.6, 101.6]

    def test_add_component_off_grid_snaps(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        # 100.0 / 1.27 = 78.74 → 79 → 79 × 1.27 = 100.33
        result = _call(fn, "add_component",
                       lib_id="Device:R", reference="R1", value="10k",
                       position=[100.0, 100.0])
        assert result["position"] == [100.33, 100.33]

    def test_add_wire_on_grid_unchanged(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_wire",
                       start_pos=[101.6, 101.6], end_pos=[203.2, 101.6])
        assert result["start"] == [101.6, 101.6]
        assert result["end"] == [203.2, 101.6]

    def test_add_wire_off_grid_snaps(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        # 100.0 → 100.33 (79 × 1.27);  200.0 → 199.39 (157 × 1.27)
        result = _call(fn, "add_wire",
                       start_pos=[100.0, 100.0], end_pos=[200.0, 100.0])
        assert result["start"] == [100.33, 100.33]
        assert result["end"] == [199.39, 100.33]

    def test_add_label_on_grid_unchanged(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text="GND", position=[101.6, 101.6])
        assert result["position"] == [101.6, 101.6]

    def test_add_label_off_grid_snaps(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text="GND", position=[100.0, 100.0])
        assert result["position"] == [100.33, 100.33]

    def test_snap_rounds_down(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        # 0.5 / 1.27 = 0.394 → rounds to 0 → 0.0
        result = _call(fn, "add_label", text="X", position=[0.5, 0.0])
        assert result["position"] == [0.0, 0.0]

    def test_snap_rounds_up(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        # 1.0 / 1.27 = 0.787 → rounds to 1 → 1.27
        result = _call(fn, "add_label", text="X", position=[1.0, 0.0])
        assert result["position"] == [1.27, 0.0]


# -- add_wire_between_pins tests ---------------------------------------------

class TestAddWireBetweenPins:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_error_first_component_not_found(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_wire_between_pins",
                       comp1_ref="R99", pin1="1", comp2_ref="R2", pin2="1")
        assert "error" in result
        assert "R99" in result["error"]

    def test_error_second_component_not_found(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[101.6, 101.6])
        result = _call(fn, "add_wire_between_pins",
                       comp1_ref="R1", pin1="1", comp2_ref="R99", pin2="1")
        assert "error" in result

    def test_tool_is_registered(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        assert fn is not None


# -- No schematic loaded tests -----------------------------------------------

class TestNoSchematicLoaded:
    """Verify tools fail gracefully when no schematic is loaded."""

    def test_list_components_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "list_components")

    def test_add_component_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_component",
                  lib_id="Device:R", reference="R1", value="10k", position=[100, 100])

    def test_add_wire_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_wire", start_pos=[100, 100], end_pos=[200, 100])

    def test_add_label_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_label", text="GND", position=[100, 100])

    def test_validate_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "validate")

    def test_get_info_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "info")


# -- info tests (was: get_schematic_info) ------------------------------------

class TestInfo:

    def test_info_empty_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        result = _call(fn, "info")
        assert result["status"] == "ok"
        assert result["components"] == 0

    def test_info_after_adding_components(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF",
              position=[150, 100])
        result = _call(fn, "info")
        assert result["status"] == "ok"
        assert result["components"] == 2


# -- validate tests ----------------------------------------------------------

class TestValidate:

    def test_validate_empty_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        result = _call(fn, "validate")
        assert result["status"] == "ok"
        assert result["issues"] == 0

    def test_validate_schematic_with_components(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        result = _call(fn, "validate")
        assert result["status"] == "ok"
        assert "issues" in result


# -- add_junction tests ------------------------------------------------------

class TestAddJunction:

    def test_add_junction(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        result = _call(fn, "add_junction", position=[100.0, 100.0])
        assert result["status"] == "ok"
        assert result["position"] == [100.0, 100.0]
        assert "junction_uuid" in result

    def test_add_junction_bad_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        result = _call(fn, "add_junction", position=[100.0])
        assert "error" in result


# -- save tests (with tmp_path) ----------------------------------------------

class TestSave:

    def test_save_to_file(self, sch_server, tmp_path):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        save_path = str(tmp_path / "test.kicad_sch")
        result = _call(fn, "save", schematic_path=save_path)
        assert result["status"] == "ok"

        import os
        assert os.path.exists(save_path)
        content = open(save_path).read()
        assert "kicad_sch" in content


# -- Integration: full workflow test -----------------------------------------

class TestSchematicWorkflow:
    """Test a realistic workflow of creating a schematic with components, wires, and labels."""

    def test_full_workflow(self, sch_server):
        fn = _get_schematic_fn(sch_server)

        # Create schematic
        result = _call(fn, "create", name="voltage_divider")
        assert result["status"] == "ok"

        # Add two resistors
        r1 = _call(fn, "add_component", lib_id="Device:R", reference="R1",
                   value="10k", position=[100, 80])
        r2 = _call(fn, "add_component", lib_id="Device:R", reference="R2",
                   value="10k", position=[100, 120])
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

        # Add wire between them
        w1 = _call(fn, "add_wire", start_pos=[100.0, 90.0], end_pos=[100.0, 110.0])
        assert w1["status"] == "ok"

        # Add labels
        l1 = _call(fn, "add_label", text="VCC", position=[100.0, 70.0])
        l2 = _call(fn, "add_label", text="GND", position=[100.0, 130.0])
        l3 = _call(fn, "add_label", text="VOUT", position=[110.0, 100.0])
        assert l1["status"] == "ok"
        assert l2["status"] == "ok"
        assert l3["status"] == "ok"

        # Verify state
        result = _call(fn, "list_components")
        assert result["count"] == 2

        info = _call(fn, "info")
        assert info["components"] == 2

        # Validate
        result = _call(fn, "validate")
        assert result["status"] == "ok"
