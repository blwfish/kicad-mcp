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
            position=[101.6, 101.6],  # 80 × 1.27 — exactly on-grid
        )
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["lib_id"] == "Device:R"
        assert result["value"] == "10k"
        assert result["position"] == [101.6, 101.6]

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
        # 101.6 = 80 × 1.27, 203.2 = 160 × 1.27 — both exactly on-grid
        result = fn(start_pos=[101.6, 101.6], end_pos=[203.2, 101.6])
        assert result["status"] == "ok"
        assert "wire_uuid" in result
        assert result["start"] == [101.6, 101.6]
        assert result["end"] == [203.2, 101.6]

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
        result = fn(text="GND", position=[101.6, 101.6])  # 80 × 1.27 — on-grid
        assert result["status"] == "ok"
        assert result["text"] == "GND"
        assert result["position"] == [101.6, 101.6]
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


# -- grid snap tests ---------------------------------------------------------

class TestGridSnap:
    """Coordinates passed to schematic tools are snapped to the 1.27 mm KiCad grid."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_add_component_on_grid_unchanged(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_component")
        result = fn(lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])
        assert result["position"] == [101.6, 101.6]

    def test_add_component_off_grid_snaps(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_component")
        # 100.0 / 1.27 = 78.74 → 79 → 79 × 1.27 = 100.33
        result = fn(lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        assert result["position"] == [100.33, 100.33]

    def test_add_wire_on_grid_unchanged(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire")
        result = fn(start_pos=[101.6, 101.6], end_pos=[203.2, 101.6])
        assert result["start"] == [101.6, 101.6]
        assert result["end"] == [203.2, 101.6]

    def test_add_wire_off_grid_snaps(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire")
        # 100.0 → 100.33 (79 × 1.27);  200.0 → 199.39 (157 × 1.27)
        result = fn(start_pos=[100.0, 100.0], end_pos=[200.0, 100.0])
        assert result["start"] == [100.33, 100.33]
        assert result["end"] == [199.39, 100.33]

    def test_add_label_on_grid_unchanged(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        result = fn(text="GND", position=[101.6, 101.6])
        assert result["position"] == [101.6, 101.6]

    def test_add_label_off_grid_snaps(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        result = fn(text="GND", position=[100.0, 100.0])
        assert result["position"] == [100.33, 100.33]

    def test_snap_rounds_down(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        # 0.5 / 1.27 = 0.394 → rounds to 0 → 0.0
        result = fn(text="X", position=[0.5, 0.0])
        assert result["position"] == [0.0, 0.0]

    def test_snap_rounds_up(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label")
        # 1.0 / 1.27 = 0.787 → rounds to 1 → 1.27
        result = fn(text="X", position=[1.0, 0.0])
        assert result["position"] == [1.27, 0.0]


# -- add_wire_between_pins tests ---------------------------------------------

class TestAddWireBetweenPins:

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_error_first_component_not_found(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire_between_pins")
        result = fn(comp1_ref="R99", pin1="1", comp2_ref="R2", pin2="1")
        assert "error" in result
        assert "R99" in result["error"]

    def test_error_second_component_not_found(self, sch_server):
        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])
        fn = _get_tool_fn(sch_server, "add_wire_between_pins")
        # R1 exists but its pin lookup either returns None (no KiCad library)
        # or succeeds; either way R2 definitely doesn't exist → error
        result = fn(comp1_ref="R1", pin1="1", comp2_ref="R99", pin2="1")
        assert "error" in result

    def test_tool_is_registered(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_wire_between_pins")
        assert fn is not None


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


# ---------------------------------------------------------------------------
# New tests for previously untested tools
# ---------------------------------------------------------------------------

# -- load_schematic tests ----------------------------------------------------

class TestLoadSchematic:
    """Tests for load_schematic tool (loads .kicad_sch from disk)."""

    def test_load_saved_schematic(self, sch_server, tmp_path):
        """Round-trip: create + save, then load and verify component count."""
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="roundtrip")
        add_fn = _get_tool_fn(sch_server, "add_component")
        add_fn(lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        add_fn(lib_id="Device:C", reference="C1", value="100nF", position=[150.0, 100.0])

        save_fn = _get_tool_fn(sch_server, "save_schematic")
        spath = str(tmp_path / "roundtrip.kicad_sch")
        save_fn(file_path=spath)

        # Reset state, then load
        sch_module._current_schematic = None
        load_fn = _get_tool_fn(sch_server, "load_schematic")
        result = load_fn(file_path=spath)
        assert result["status"] == "ok"
        assert result["file_path"] == spath
        assert result["components"] == 2

    def test_load_sets_module_state(self, sch_server, tmp_path):
        """Loading a file sets _current_schematic so other tools work."""
        create_fn = _get_tool_fn(sch_server, "create_schematic")
        create_fn(name="state_test")
        save_fn = _get_tool_fn(sch_server, "save_schematic")
        spath = str(tmp_path / "state_test.kicad_sch")
        save_fn(file_path=spath)

        sch_module._current_schematic = None
        load_fn = _get_tool_fn(sch_server, "load_schematic")
        load_fn(file_path=spath)
        assert sch_module._current_schematic is not None

    def test_load_nonexistent_file_raises(self, sch_server):
        """Loading a missing file raises FileNotFoundError from kicad-sch-api."""
        load_fn = _get_tool_fn(sch_server, "load_schematic")
        with pytest.raises(FileNotFoundError):
            load_fn(file_path="/tmp/no_such_file_xyz.kicad_sch")


# -- backup_schematic tests --------------------------------------------------

class TestBackupSchematic:
    """Tests for backup_schematic tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_backup_default_suffix(self, sch_server, tmp_path):
        """Backup creates a file with the default .backup suffix."""
        save_fn = _get_tool_fn(sch_server, "save_schematic")
        spath = str(tmp_path / "test.kicad_sch")
        save_fn(file_path=spath)

        backup_fn = _get_tool_fn(sch_server, "backup_schematic")
        result = backup_fn()
        assert result["status"] == "ok"
        assert "backup_path" in result
        import os
        assert os.path.exists(result["backup_path"])

    def test_backup_custom_suffix(self, sch_server, tmp_path):
        """Backup honours a caller-supplied suffix."""
        save_fn = _get_tool_fn(sch_server, "save_schematic")
        spath = str(tmp_path / "test.kicad_sch")
        save_fn(file_path=spath)

        backup_fn = _get_tool_fn(sch_server, "backup_schematic")
        result = backup_fn(suffix=".bak")
        assert result["status"] == "ok"
        assert result["backup_path"].endswith(".bak")

    def test_backup_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "backup_schematic")()


# -- clone_schematic tests ---------------------------------------------------

class TestCloneSchematic:
    """Tests for clone_schematic tool.

    KNOWN BUG: kicad-sch-api does not implement Schematic.clone() — calling
    this tool raises AttributeError.  These tests document that state and pin
    the guard/response surface once the API exists.
    """

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_clone_raises_attribute_error(self, sch_server):
        """clone_schematic raises AttributeError because kicad-sch-api lacks .clone()."""
        clone_fn = _get_tool_fn(sch_server, "clone_schematic")
        with pytest.raises(AttributeError, match="clone"):
            clone_fn(new_name="myclone")

    def test_clone_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "clone_schematic")(new_name="x")


# -- add_multi_unit_component tests ------------------------------------------

class TestAddMultiUnitComponent:
    """Tests for add_multi_unit_component tool (dual op-amp style symbols)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_add_multi_unit_lm358(self, sch_server):
        """Place all units of a 3-unit LM358 (2 op-amp + power)."""
        fn = _get_tool_fn(sch_server, "add_multi_unit_component")
        result = fn(
            lib_id="Amplifier_Operational:LM358",
            reference="U1",
            value="LM358",
            position=[100.0, 100.0],
        )
        assert result["status"] == "ok"
        assert result["reference"] == "U1"
        assert result["lib_id"] == "Amplifier_Operational:LM358"
        assert result["total_units"] == 3
        assert len(result["units"]) == 3
        # Every unit entry has a position and pin list
        for unit_entry in result["units"]:
            assert "unit" in unit_entry
            assert "pins" in unit_entry
            assert "position" in unit_entry

    def test_add_multi_unit_bad_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_multi_unit_component")
        result = fn(
            lib_id="Amplifier_Operational:LM358",
            reference="U1",
            value="LM358",
            position=[100.0],  # missing y
        )
        assert "error" in result

    def test_add_multi_unit_unknown_lib(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_multi_unit_component")
        result = fn(
            lib_id="Device:NonExistentSymbol9999",
            reference="U1",
            value="X",
            position=[100.0, 100.0],
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_add_multi_unit_invalid_unit_number(self, sch_server):
        """Requesting a unit number that doesn't exist returns an error."""
        fn = _get_tool_fn(sch_server, "add_multi_unit_component")
        result = fn(
            lib_id="Amplifier_Operational:LM358",
            reference="U1",
            value="LM358",
            position=[100.0, 100.0],
            units=[99],  # unit 99 doesn't exist in LM358
        )
        assert "error" in result
        assert "99" in result["error"]

    def test_add_multi_unit_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_multi_unit_component")(
                lib_id="Amplifier_Operational:LM358",
                reference="U1",
                value="LM358",
                position=[100.0, 100.0],
            )


# -- filter_components tests -------------------------------------------------

class TestFilterComponents:
    """Tests for filter_components tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        add = _get_tool_fn(sch_server, "add_component")
        add(lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        add(lib_id="Device:R", reference="R2", value="22k", position=[150.0, 100.0])
        add(lib_id="Device:C", reference="C1", value="100nF", position=[200.0, 100.0])

    def test_filter_by_lib_id_matches(self, sch_server):
        fn = _get_tool_fn(sch_server, "filter_components")
        result = fn(lib_id="Device:R")
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_filter_by_lib_id_no_match(self, sch_server):
        """Filter matching zero components returns empty list, not an error."""
        fn = _get_tool_fn(sch_server, "filter_components")
        result = fn(lib_id="Device:LED")
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["components"] == []

    def test_filter_by_value(self, sch_server):
        fn = _get_tool_fn(sch_server, "filter_components")
        result = fn(value="10k")
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["components"][0]["reference"] == "R1"

    def test_filter_no_criteria_returns_error(self, sch_server):
        """Boundary: calling with ALL filters None → error, not silent return-all."""
        fn = _get_tool_fn(sch_server, "filter_components")
        result = fn()
        assert "error" in result
        assert "criterion" in result["error"].lower()

    def test_filter_by_reference_exact(self, sch_server):
        """The public `reference` arg maps to kicad-sch-api's regex
        `reference_pattern`.  A plain string matches at the start by default
        (re.compile + match), so "C1" matches exactly "C1"."""
        fn = _get_tool_fn(sch_server, "filter_components")
        result = fn(reference="C1")
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["components"][0]["reference"] == "C1"

    def test_filter_by_reference_regex(self, sch_server):
        """Pattern is a regex on the reference string; "R\\d" matches R1 and R2."""
        fn = _get_tool_fn(sch_server, "filter_components")
        result = fn(reference=r"R\d")
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_filter_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "filter_components")(lib_id="Device:R")


# -- components_in_area tests ------------------------------------------------

class TestComponentsInArea:
    """Tests for components_in_area tool (spatial filter)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        add = _get_tool_fn(sch_server, "add_component")
        add(lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])
        add(lib_id="Device:R", reference="R2", value="22k", position=[200.0, 200.0])

    def test_area_contains_all(self, sch_server):
        fn = _get_tool_fn(sch_server, "components_in_area")
        result = fn(x1=90.0, y1=90.0, x2=300.0, y2=300.0)
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_area_contains_one(self, sch_server):
        fn = _get_tool_fn(sch_server, "components_in_area")
        result = fn(x1=90.0, y1=90.0, x2=120.0, y2=120.0)
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["components"][0]["reference"] == "R1"

    def test_area_contains_none(self, sch_server):
        fn = _get_tool_fn(sch_server, "components_in_area")
        result = fn(x1=10.0, y1=10.0, x2=50.0, y2=50.0)
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["components"] == []

    def test_area_degenerate_zero_size_at_component(self, sch_server):
        """Boundary: zero-width/height area at exactly the component position.

        kicad-sch-api uses inclusive (<=) boundary checks, so a point-area
        that coincides with a component position still returns that component.
        """
        fn = _get_tool_fn(sch_server, "components_in_area")
        # R1 is placed at [101.6, 101.6] (on-grid)
        result = fn(x1=101.6, y1=101.6, x2=101.6, y2=101.6)
        assert result["status"] == "ok"
        assert result["count"] == 1  # inclusive boundary: point coincides with component

    def test_area_degenerate_zero_size_not_at_component(self, sch_server):
        """Boundary: zero-width/height area NOT at a component returns empty."""
        fn = _get_tool_fn(sch_server, "components_in_area")
        result = fn(x1=50.0, y1=50.0, x2=50.0, y2=50.0)
        assert result["status"] == "ok"
        assert result["count"] == 0

    def test_area_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "components_in_area")(x1=0, y1=0, x2=100, y2=100)


# -- bulk_update_components tests --------------------------------------------

class TestBulkUpdateComponents:
    """Tests for bulk_update_components tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        add = _get_tool_fn(sch_server, "add_component")
        add(lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        add(lib_id="Device:R", reference="R2", value="10k", position=[150.0, 100.0])
        add(lib_id="Device:C", reference="C1", value="100nF", position=[200.0, 100.0])

    def test_bulk_update_matching(self, sch_server):
        fn = _get_tool_fn(sch_server, "bulk_update_components")
        result = fn(criteria={"lib_id": "Device:R"}, updates={"value": "22k"})
        assert result["status"] == "ok"
        assert result["updated"] == 2

    def test_bulk_update_zero_match_returns_zero(self, sch_server):
        """Boundary: criteria matching nothing returns updated=0, not an error."""
        fn = _get_tool_fn(sch_server, "bulk_update_components")
        result = fn(criteria={"lib_id": "Device:LED"}, updates={"value": "red"})
        assert result["status"] == "ok"
        assert result["updated"] == 0

    def test_bulk_update_value_reflected_in_list(self, sch_server):
        bulk_fn = _get_tool_fn(sch_server, "bulk_update_components")
        bulk_fn(criteria={"lib_id": "Device:R"}, updates={"value": "47k"})
        list_fn = _get_tool_fn(sch_server, "list_components")
        comps = list_fn()["components"]
        r_values = {c["value"] for c in comps if c["lib_id"] == "Device:R"}
        assert r_values == {"47k"}

    def test_bulk_update_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "bulk_update_components")(
                criteria={"lib_id": "Device:R"}, updates={"value": "22k"}
            )


# -- get_component_pin_position tests ----------------------------------------

class TestGetComponentPinPosition:
    """Tests for get_component_pin_position tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        _get_tool_fn(sch_server, "add_component")(
            lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6]
        )

    def test_valid_pin(self, sch_server):
        fn = _get_tool_fn(sch_server, "get_component_pin_position")
        result = fn(reference="R1", pin_number="1")
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["pin_number"] == "1"
        assert "x" in result and "y" in result
        # Coordinates are numeric
        assert isinstance(result["x"], float)
        assert isinstance(result["y"], float)

    def test_both_pins_present(self, sch_server):
        """Resistor has pins 1 and 2; both should resolve."""
        fn = _get_tool_fn(sch_server, "get_component_pin_position")
        r1 = fn(reference="R1", pin_number="1")
        r2 = fn(reference="R1", pin_number="2")
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"
        # The two pins must be at different positions
        assert (r1["x"], r1["y"]) != (r2["x"], r2["y"])

    def test_nonexistent_pin_returns_error(self, sch_server):
        """Boundary: pin number that doesn't exist on the component."""
        fn = _get_tool_fn(sch_server, "get_component_pin_position")
        result = fn(reference="R1", pin_number="99")
        assert "error" in result
        assert "99" in result["error"]

    def test_nonexistent_component_returns_error(self, sch_server):
        fn = _get_tool_fn(sch_server, "get_component_pin_position")
        result = fn(reference="R99", pin_number="1")
        assert "error" in result
        assert "R99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "get_component_pin_position")(
                reference="R1", pin_number="1"
            )


# -- list_component_pins tests -----------------------------------------------

class TestListComponentPins:
    """Tests for list_component_pins tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        _get_tool_fn(sch_server, "add_component")(
            lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6]
        )

    def test_resistor_has_two_pins(self, sch_server):
        fn = _get_tool_fn(sch_server, "list_component_pins")
        result = fn(reference="R1")
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["count"] == 2
        pin_numbers = {p["number"] for p in result["pins"]}
        assert pin_numbers == {"1", "2"}

    def test_each_pin_has_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "list_component_pins")
        result = fn(reference="R1")
        for pin in result["pins"]:
            assert "x" in pin and "y" in pin
            assert isinstance(pin["x"], float)
            assert isinstance(pin["y"], float)

    def test_nonexistent_component_returns_error(self, sch_server):
        fn = _get_tool_fn(sch_server, "list_component_pins")
        result = fn(reference="R99")
        assert "error" in result
        assert "R99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "list_component_pins")(reference="R1")


# -- add_label_to_pin tests --------------------------------------------------

class TestAddLabelToPin:
    """Tests for add_label_to_pin tool (auto-place label at pin position)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        _get_tool_fn(sch_server, "add_component")(
            lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6]
        )

    def test_add_label_to_pin_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label_to_pin")
        result = fn(reference="R1", pin_number="1", text="GND")
        assert result["status"] == "ok"
        assert "label_uuid" in result
        assert result["text"] == "GND"
        assert result["reference"] == "R1"
        assert result["pin_number"] == "1"
        assert "position" in result
        assert len(result["position"]) == 2

    def test_label_at_pin_2(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label_to_pin")
        result = fn(reference="R1", pin_number="2", text="VCC")
        assert result["status"] == "ok"
        assert result["text"] == "VCC"

    def test_nonexistent_component_returns_error(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_label_to_pin")
        result = fn(reference="R99", pin_number="1", text="GND")
        assert "error" in result
        assert "R99" in result["error"]

    def test_nonexistent_pin_returns_error(self, sch_server):
        """Boundary: pin that doesn't exist on the component."""
        fn = _get_tool_fn(sch_server, "add_label_to_pin")
        result = fn(reference="R1", pin_number="99", text="GND")
        assert "error" in result
        assert "99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_label_to_pin")(
                reference="R1", pin_number="1", text="GND"
            )


# -- connect_pins_with_labels tests ------------------------------------------

class TestConnectPinsWithLabels:
    """Tests for connect_pins_with_labels tool (net-label pair connecting two pins)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        add = _get_tool_fn(sch_server, "add_component")
        add(lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])
        add(lib_id="Device:R", reference="R2", value="22k", position=[200.0, 200.0])

    def test_connect_two_pins(self, sch_server):
        fn = _get_tool_fn(sch_server, "connect_pins_with_labels")
        result = fn(comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="VOUT")
        assert result["status"] == "ok"
        assert result["net_name"] == "VOUT"
        assert result["labels_created"] == 2
        assert len(result["label_uuids"]) == 2

    def test_connect_produces_two_distinct_uuids(self, sch_server):
        fn = _get_tool_fn(sch_server, "connect_pins_with_labels")
        result = fn(comp1_ref="R1", pin1="2", comp2_ref="R2", pin2="2", net_name="GND_NET")
        uuids = result["label_uuids"]
        assert uuids[0] != uuids[1]

    def test_bad_first_component(self, sch_server):
        fn = _get_tool_fn(sch_server, "connect_pins_with_labels")
        result = fn(comp1_ref="R99", pin1="1", comp2_ref="R2", pin2="1", net_name="X")
        assert "error" in result
        assert "R99" in result["error"]

    def test_bad_second_component(self, sch_server):
        fn = _get_tool_fn(sch_server, "connect_pins_with_labels")
        result = fn(comp1_ref="R1", pin1="1", comp2_ref="R99", pin2="1", net_name="X")
        assert "error" in result
        assert "R99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "connect_pins_with_labels")(
                comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="X"
            )


# -- add_hierarchical_label tests --------------------------------------------

class TestAddHierarchicalLabel:
    """Tests for add_hierarchical_label tool (shape validation + happy paths)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_add_input_shape(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_hierarchical_label")
        result = fn(text="DATA_IN", position=[100.0, 100.0], shape="input")
        assert result["status"] == "ok"
        assert "label_uuid" in result
        assert result["text"] == "DATA_IN"
        assert result["shape"] == "input"

    def test_add_output_shape(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_hierarchical_label")
        result = fn(text="DATA_OUT", position=[100.0, 120.0], shape="output")
        assert result["status"] == "ok"
        assert result["shape"] == "output"

    def test_shape_case_insensitive(self, sch_server):
        """Shape matching is case-insensitive ('OUTPUT' == 'output')."""
        fn = _get_tool_fn(sch_server, "add_hierarchical_label")
        result = fn(text="CLK", position=[100.0, 140.0], shape="OUTPUT")
        assert result["status"] == "ok"

    def test_all_valid_shapes_accepted(self, sch_server):
        """All 6 documented shape values must be accepted."""
        fn = _get_tool_fn(sch_server, "add_hierarchical_label")
        shapes = ["input", "output", "bidirectional", "tristate", "passive", "unspecified"]
        for i, shape in enumerate(shapes):
            result = fn(text=f"SIG_{i}", position=[100.0, float(100 + i * 10)], shape=shape)
            assert result["status"] == "ok", f"shape={shape!r} unexpectedly rejected"

    def test_unknown_shape_returns_error(self, sch_server):
        """Guard: a misspelled shape ('inptu') returns a structured error, not silent default."""
        fn = _get_tool_fn(sch_server, "add_hierarchical_label")
        result = fn(text="X", position=[100.0, 100.0], shape="inptu")
        assert "error" in result
        assert "inptu" in result["error"]
        assert "Valid:" in result["error"]

    def test_bad_position_returns_error(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_hierarchical_label")
        result = fn(text="X", position=[100.0], shape="input")
        assert "error" in result

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_hierarchical_label")(
                text="X", position=[100.0, 100.0], shape="input"
            )


# -- edit_label tests --------------------------------------------------------

class TestEditLabel:
    """Tests for edit_label tool (mutate existing label in place)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def _add_label(self, sch_server, text="GND", position=None):
        if position is None:
            position = [100.0, 100.0]
        result = _get_tool_fn(sch_server, "add_label")(text=text, position=position)
        return result["label_uuid"]

    def test_edit_text(self, sch_server):
        uuid = self._add_label(sch_server, "GND")
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid=uuid, new_text="VCC")
        assert result["status"] == "ok"
        assert result["text"] == "VCC"
        assert result["label_uuid"] == uuid

    def test_edit_position(self, sch_server):
        uuid = self._add_label(sch_server, "SDA")
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid=uuid, position=[200.0, 200.0])
        assert result["status"] == "ok"
        assert result["position"] == [200.0, 200.0]

    def test_edit_rotation(self, sch_server):
        uuid = self._add_label(sch_server, "SCL")
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid=uuid, rotation=90.0)
        assert result["status"] == "ok"
        assert result["rotation"] == 90.0

    def test_edit_size(self, sch_server):
        uuid = self._add_label(sch_server, "SIG")
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid=uuid, size=2.54)
        assert result["status"] == "ok"
        assert result["size"] == 2.54

    def test_no_modifications_returns_error(self, sch_server):
        """Boundary: calling with all None should return a structured error."""
        uuid = self._add_label(sch_server, "X")
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid=uuid)
        assert "error" in result
        assert "No modifications" in result["error"]

    def test_bad_uuid_returns_error(self, sch_server):
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid="bad-uuid-that-does-not-exist", new_text="X")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_bad_position_list_returns_error(self, sch_server):
        """Boundary: position with wrong element count."""
        uuid = self._add_label(sch_server, "X")
        fn = _get_tool_fn(sch_server, "edit_label")
        result = fn(label_uuid=uuid, position=[100.0])
        assert "error" in result

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "edit_label")(
                label_uuid="any-uuid", new_text="X"
            )


# -- add_text tests ----------------------------------------------------------

class TestAddText:
    """Tests for add_text tool (free-floating text element)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_add_text_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_text")
        result = fn(text="Hello World", position=[50.0, 50.0])
        assert result["status"] == "ok"
        assert "text_uuid" in result
        assert result["text"] == "Hello World"
        assert result["position"] == [50.0, 50.0]

    def test_add_text_with_rotation(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_text")
        result = fn(text="Rotated", position=[100.0, 100.0], rotation=90.0)
        assert result["status"] == "ok"
        assert result["text"] == "Rotated"

    def test_add_text_bad_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_text")
        result = fn(text="X", position=[50.0])  # missing y
        assert "error" in result

    def test_add_text_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_text")(text="X", position=[50.0, 50.0])


# -- add_text_box tests ------------------------------------------------------

class TestAddTextBox:
    """Tests for add_text_box tool (text inside a rectangle)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_add_text_box_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_text_box")
        result = fn(text="Note here", position=[10.0, 10.0], size=[30.0, 15.0])
        assert result["status"] == "ok"
        assert "textbox_uuid" in result
        assert result["text"] == "Note here"
        assert result["position"] == [10.0, 10.0]
        assert result["size"] == [30.0, 15.0]

    def test_add_text_box_bad_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_text_box")
        result = fn(text="X", position=[10.0], size=[30.0, 15.0])  # position missing y
        assert "error" in result

    def test_add_text_box_bad_size(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_text_box")
        result = fn(text="X", position=[10.0, 10.0], size=[30.0])  # size missing height
        assert "error" in result

    def test_add_text_box_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_text_box")(
                text="X", position=[10.0, 10.0], size=[30.0, 15.0]
            )


# -- add_sheet tests ---------------------------------------------------------

class TestAddSheet:
    """Tests for add_sheet tool (hierarchical sheet element)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")

    def test_add_sheet_basic(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_sheet")
        result = fn(
            name="PowerSupply",
            filename="power.kicad_sch",
            position=[100.0, 50.0],
            size=[40.0, 25.0],
        )
        assert result["status"] == "ok"
        assert "sheet_uuid" in result
        assert result["name"] == "PowerSupply"
        assert result["filename"] == "power.kicad_sch"
        assert result["position"] == [100.0, 50.0]
        assert result["size"] == [40.0, 25.0]

    def test_add_sheet_uuid_is_string(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_sheet")
        result = fn(name="Sub", filename="sub.kicad_sch", position=[50.0, 50.0], size=[30.0, 20.0])
        assert isinstance(result["sheet_uuid"], str)
        assert len(result["sheet_uuid"]) > 0

    def test_add_sheet_bad_position(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_sheet")
        result = fn(name="Sub", filename="sub.kicad_sch", position=[50.0], size=[30.0, 20.0])
        assert "error" in result

    def test_add_sheet_bad_size(self, sch_server):
        fn = _get_tool_fn(sch_server, "add_sheet")
        result = fn(name="Sub", filename="sub.kicad_sch", position=[50.0, 50.0], size=[30.0])
        assert "error" in result

    def test_add_sheet_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_sheet")(
                name="Sub", filename="sub.kicad_sch",
                position=[50.0, 50.0], size=[30.0, 20.0]
            )


# -- add_sheet_pin tests -----------------------------------------------------

class TestAddSheetPin:
    """Tests for add_sheet_pin tool.

    IMPORTANT: Only the validation guard paths are tested here.  The success
    path (valid pin_type reaching sch.add_sheet_pin()) is intentionally NOT
    tested because the kicad-sch-api signature changed — the MCP call is
    sch.add_sheet_pin(sheet_uuid, name, pin_type, tuple(position)) but the
    installed library now requires (sheet_uuid, name, pin_type, edge,
    position_along_edge).  Calling the success path raises TypeError.
    Testing the rejection guards is both safe and valuable: they short-circuit
    before reaching the broken API call.
    """

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        _get_tool_fn(sch_server, "create_schematic")(name="test")
        fn = _get_tool_fn(sch_server, "add_sheet")
        r = fn(name="Sub", filename="sub.kicad_sch", position=[100.0, 50.0], size=[30.0, 20.0])
        self.sheet_uuid = r["sheet_uuid"]

    def test_invalid_pin_type_rejected(self, sch_server):
        """Guard: unknown pin_type must return a structured error, never reach API."""
        fn = _get_tool_fn(sch_server, "add_sheet_pin")
        result = fn(
            sheet_uuid=self.sheet_uuid,
            name="CLK",
            pin_type="not_a_valid_type",
            position=[100.0, 50.0],
        )
        assert "error" in result
        assert "not_a_valid_type" in result["error"]
        assert "Valid:" in result["error"]

    def test_bad_position_rejected(self, sch_server):
        """Guard: 1-element position returns error before type-check or API call."""
        fn = _get_tool_fn(sch_server, "add_sheet_pin")
        result = fn(
            sheet_uuid=self.sheet_uuid,
            name="CLK",
            pin_type="input",
            position=[100.0],  # missing y
        )
        assert "error" in result

    def test_all_valid_pin_types_pass_guard(self, sch_server):
        """All 5 documented pin_type values pass the guard (they will then hit the broken API).

        This test expects TypeError from the broken kicad-sch-api call — confirming
        the guard correctly passes valid types through.  When the API is fixed,
        this test should be updated to assert status==ok.
        """
        fn = _get_tool_fn(sch_server, "add_sheet_pin")
        valid_types = ["input", "output", "bidirectional", "tri_state", "passive"]
        for pin_type in valid_types:
            with pytest.raises(TypeError):
                fn(
                    sheet_uuid=self.sheet_uuid,
                    name="SIG",
                    pin_type=pin_type,
                    position=[100.0, 50.0],
                )

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_sheet_pin")(
                sheet_uuid="any", name="X", pin_type="input", position=[0.0, 0.0]
            )


# -- Extended TestNoSchematicLoaded coverage ---------------------------------

class TestNoSchematicLoadedExtended:
    """Extend the existing TestNoSchematicLoaded with the new tools."""

    def test_backup_schematic_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "backup_schematic")()

    def test_filter_components_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "filter_components")(lib_id="Device:R")

    def test_components_in_area_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "components_in_area")(x1=0, y1=0, x2=100, y2=100)

    def test_bulk_update_components_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "bulk_update_components")(
                criteria={"lib_id": "Device:R"}, updates={"value": "22k"}
            )

    def test_get_component_pin_position_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "get_component_pin_position")(
                reference="R1", pin_number="1"
            )

    def test_list_component_pins_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "list_component_pins")(reference="R1")

    def test_add_label_to_pin_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_label_to_pin")(
                reference="R1", pin_number="1", text="GND"
            )

    def test_connect_pins_with_labels_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "connect_pins_with_labels")(
                comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="X"
            )

    def test_add_hierarchical_label_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_hierarchical_label")(
                text="X", position=[100.0, 100.0], shape="input"
            )

    def test_edit_label_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "edit_label")(label_uuid="x", new_text="Y")

    def test_add_text_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_text")(text="X", position=[50.0, 50.0])

    def test_add_text_box_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_text_box")(
                text="X", position=[10.0, 10.0], size=[20.0, 10.0]
            )

    def test_add_sheet_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_sheet")(
                name="S", filename="s.kicad_sch",
                position=[50.0, 50.0], size=[30.0, 20.0]
            )

    def test_add_multi_unit_no_schematic(self, sch_server):
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _get_tool_fn(sch_server, "add_multi_unit_component")(
                lib_id="Amplifier_Operational:LM358",
                reference="U1", value="LM358",
                position=[100.0, 100.0],
            )
