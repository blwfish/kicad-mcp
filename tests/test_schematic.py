"""
Tests for the schematic domain router (phase 5).

Uses the real kicad-sch-api library (no mocking) since it is a pure
Python library that works without KiCad installed.
All calls go through the single `schematic` router tool with operation=.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.schematic import register_schematic_router
import kicad_mcp.tools.schematic_impl as sch_module


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

    def test_remove_component_multi_removes_only_named(self, sch_server):
        # Multi-component: a "remove first in list regardless of reference" bug
        # (the move_component filter()-footgun class) would pass a 1-component
        # test but fail here.
        fn = _get_schematic_fn(sch_server)
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        _call(fn, "add_component", lib_id="Device:R", reference="R2", value="22k",
              position=[120, 100])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF",
              position=[140, 100])
        result = _call(fn, "remove_component", reference="R2")
        assert result["status"] == "ok"
        refs = {c["reference"] for c in _call(fn, "list_components")["components"]}
        assert refs == {"R1", "C1"}                 # only R2 gone, not "the first one"


# -- move_component tests ----------------------------------------------------

class TestMoveComponent:
    """Regression coverage for the critical filter()-footgun: move_component used
    `components.filter(reference=ref)` which silently returns ALL components, so
    [0] moved the WRONG one while reporting status=ok. A single-component test
    masks it (matches[0] is the only component); these use ≥2."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k",
              position=[100, 100])
        _call(fn, "add_component", lib_id="Device:R", reference="R2", value="22k",
              position=[120, 100])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF",
              position=[140, 100])
        self._fn = fn

    def _pos(self, ref):
        comps = _call(self._fn, "list_components")["components"]
        c = next(c for c in comps if c["reference"] == ref)
        return c.get("position")

    def test_moves_only_the_named_component(self, sch_server):
        before_r1 = self._pos("R1")
        before_c1 = self._pos("C1")
        result = _call(self._fn, "move_component", reference="R2", position=[60, 60])
        assert result["status"] == "ok"
        assert result["reference"] == "R2"
        # R2 actually moved to (snapped) ~(60,60)...
        r2 = self._pos("R2")
        assert abs(r2[0] - 60) < 1.0 and abs(r2[1] - 60) < 1.0
        # ...and R1 / C1 did NOT move (the bug would have moved matches[0] = R1).
        assert self._pos("R1") == before_r1
        assert self._pos("C1") == before_c1

    def test_unknown_reference_is_error_not_silent_move(self, sch_server):
        before = {r: self._pos(r) for r in ("R1", "R2", "C1")}
        result = _call(self._fn, "move_component", reference="R99", position=[60, 60])
        assert "error" in result                    # not status=ok on a phantom ref
        # nothing moved
        assert {r: self._pos(r) for r in ("R1", "R2", "C1")} == before


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


# ---------------------------------------------------------------------------
# New tests for previously untested tools
# ---------------------------------------------------------------------------

# -- load_schematic tests ----------------------------------------------------

class TestLoadSchematic:
    """Tests for load_schematic tool (loads .kicad_sch from disk)."""

    def test_load_saved_schematic(self, sch_server, tmp_path):
        """Round-trip: create + save, then load and verify component count."""
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="roundtrip")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF", position=[150.0, 100.0])

        spath = str(tmp_path / "roundtrip.kicad_sch")
        _call(fn, "save", schematic_path=spath)

        # Reset state, then load
        sch_module._current_schematic = None
        result = _call(fn, "load", schematic_path=spath)
        assert result["status"] == "ok"
        assert result["file_path"] == spath
        assert result["components"] == 2

    def test_load_sets_module_state(self, sch_server, tmp_path):
        """Loading a file sets _current_schematic so other tools work."""
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="state_test")
        spath = str(tmp_path / "state_test.kicad_sch")
        _call(fn, "save", schematic_path=spath)

        sch_module._current_schematic = None
        _call(fn, "load", schematic_path=spath)
        assert sch_module._current_schematic is not None

    def test_load_nonexistent_file_raises(self, sch_server):
        """Loading a missing file raises FileNotFoundError from kicad-sch-api."""
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(FileNotFoundError):
            _call(fn, "load", schematic_path="/tmp/no_such_file_xyz.kicad_sch")


# -- backup_schematic tests --------------------------------------------------

class TestBackupSchematic:
    """Tests for backup_schematic tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_backup_default_suffix(self, sch_server, tmp_path):
        """Backup creates a file with the default .backup suffix."""
        fn = _get_schematic_fn(sch_server)
        spath = str(tmp_path / "test.kicad_sch")
        _call(fn, "save", schematic_path=spath)

        result = _call(fn, "backup")
        assert result["status"] == "ok"
        assert "backup_path" in result
        import os
        assert os.path.exists(result["backup_path"])

    def test_backup_custom_suffix(self, sch_server, tmp_path):
        """Backup honours a caller-supplied suffix."""
        fn = _get_schematic_fn(sch_server)
        spath = str(tmp_path / "test.kicad_sch")
        _call(fn, "save", schematic_path=spath)

        result = _call(fn, "backup", suffix=".bak")
        assert result["status"] == "ok"
        assert result["backup_path"].endswith(".bak")

    def test_backup_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "backup")


# -- add_multi_unit_component tests ------------------------------------------

class TestAddMultiUnitComponent:
    """Tests for add_multi_unit_component tool (dual op-amp style symbols)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_multi_unit_lm358(self, sch_server):
        """Place all units of a 3-unit LM358 (2 op-amp + power)."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_multi_unit_component",
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
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_multi_unit_component",
            lib_id="Amplifier_Operational:LM358",
            reference="U1",
            value="LM358",
            position=[100.0],  # missing y
        )
        assert "error" in result

    def test_add_multi_unit_unknown_lib(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_multi_unit_component",
            lib_id="Device:NonExistentSymbol9999",
            reference="U1",
            value="X",
            position=[100.0, 100.0],
        )
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_add_multi_unit_invalid_unit_number(self, sch_server):
        """Requesting a unit number that doesn't exist returns an error."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_multi_unit_component",
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
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_multi_unit_component",
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
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        _call(fn, "add_component", lib_id="Device:R", reference="R2", value="22k", position=[150.0, 100.0])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF", position=[200.0, 100.0])

    def test_filter_by_lib_id_matches(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "filter_components", lib_id="Device:R")
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_filter_by_lib_id_no_match(self, sch_server):
        """Filter matching zero components returns empty list, not an error."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "filter_components", lib_id="Device:LED")
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["components"] == []

    def test_filter_by_value(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "filter_components", value="10k")
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["components"][0]["reference"] == "R1"

    def test_filter_no_criteria_returns_error(self, sch_server):
        """Boundary: calling with ALL filters None → error, not silent return-all."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "filter_components")
        assert "error" in result
        assert "criterion" in result["error"].lower()

    def test_filter_by_reference_exact(self, sch_server):
        """The public `reference` arg maps to kicad-sch-api's regex
        `reference_pattern`.  A plain string matches at the start by default
        (re.compile + match), so "C1" matches exactly "C1"."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "filter_components", reference="C1")
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["components"][0]["reference"] == "C1"

    def test_filter_by_reference_regex(self, sch_server):
        """Pattern is a regex on the reference string; "R\\d" matches R1 and R2."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "filter_components", reference=r"R\d")
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_filter_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "filter_components", lib_id="Device:R")


# -- components_in_area tests ------------------------------------------------

class TestComponentsInArea:
    """Tests for components_in_area tool (spatial filter)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])
        _call(fn, "add_component", lib_id="Device:R", reference="R2", value="22k", position=[200.0, 200.0])

    def test_area_contains_all(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "components_in_area", x1=90.0, y1=90.0, x2=300.0, y2=300.0)
        assert result["status"] == "ok"
        assert result["count"] == 2
        refs = {c["reference"] for c in result["components"]}
        assert refs == {"R1", "R2"}

    def test_area_contains_one(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "components_in_area", x1=90.0, y1=90.0, x2=120.0, y2=120.0)
        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["components"][0]["reference"] == "R1"

    def test_area_contains_none(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "components_in_area", x1=10.0, y1=10.0, x2=50.0, y2=50.0)
        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["components"] == []

    def test_area_degenerate_zero_size_at_component(self, sch_server):
        """Boundary: zero-width/height area at exactly the component position.

        kicad-sch-api uses inclusive (<=) boundary checks, so a point-area
        that coincides with a component position still returns that component.
        """
        fn = _get_schematic_fn(sch_server)
        # R1 is placed at [101.6, 101.6] (on-grid)
        result = _call(fn, "components_in_area", x1=101.6, y1=101.6, x2=101.6, y2=101.6)
        assert result["status"] == "ok"
        assert result["count"] == 1  # inclusive boundary: point coincides with component

    def test_area_degenerate_zero_size_not_at_component(self, sch_server):
        """Boundary: zero-width/height area NOT at a component returns empty."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "components_in_area", x1=50.0, y1=50.0, x2=50.0, y2=50.0)
        assert result["status"] == "ok"
        assert result["count"] == 0

    def test_area_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "components_in_area", x1=0, y1=0, x2=100, y2=100)


# -- bulk_update_components tests --------------------------------------------

class TestBulkUpdateComponents:
    """Tests for bulk_update_components tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[100.0, 100.0])
        _call(fn, "add_component", lib_id="Device:R", reference="R2", value="10k", position=[150.0, 100.0])
        _call(fn, "add_component", lib_id="Device:C", reference="C1", value="100nF", position=[200.0, 100.0])

    def test_bulk_update_matching(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "bulk_update_components", criteria={"lib_id": "Device:R"}, updates={"value": "22k"})
        assert result["status"] == "ok"
        assert result["updated"] == 2

    def test_bulk_update_zero_match_returns_zero(self, sch_server):
        """Boundary: criteria matching nothing returns updated=0, not an error."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "bulk_update_components", criteria={"lib_id": "Device:LED"}, updates={"value": "red"})
        assert result["status"] == "ok"
        assert result["updated"] == 0

    def test_bulk_update_value_reflected_in_list(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "bulk_update_components", criteria={"lib_id": "Device:R"}, updates={"value": "47k"})
        comps = _call(fn, "list_components")["components"]
        r_values = {c["value"] for c in comps if c["lib_id"] == "Device:R"}
        assert r_values == {"47k"}

    def test_bulk_update_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "bulk_update_components",
                criteria={"lib_id": "Device:R"}, updates={"value": "22k"}
            )


# -- get_component_pin_position tests ----------------------------------------

class TestGetComponentPinPosition:
    """Tests for get_component_pin_position tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])

    def test_valid_pin(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "get_component_pin_position", reference="R1", pin_number="1")
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["pin_number"] == "1"
        assert "x" in result and "y" in result
        # Coordinates are numeric
        assert isinstance(result["x"], float)
        assert isinstance(result["y"], float)

    def test_both_pins_present(self, sch_server):
        """Resistor has pins 1 and 2; both should resolve."""
        fn = _get_schematic_fn(sch_server)
        r1 = _call(fn, "get_component_pin_position", reference="R1", pin_number="1")
        r2 = _call(fn, "get_component_pin_position", reference="R1", pin_number="2")
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"
        # The two pins must be at different positions
        assert (r1["x"], r1["y"]) != (r2["x"], r2["y"])

    def test_nonexistent_pin_returns_error(self, sch_server):
        """Boundary: pin number that doesn't exist on the component."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "get_component_pin_position", reference="R1", pin_number="99")
        assert "error" in result
        assert "99" in result["error"]

    def test_nonexistent_component_returns_error(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "get_component_pin_position", reference="R99", pin_number="1")
        assert "error" in result
        assert "R99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "get_component_pin_position", reference="R1", pin_number="1")


# -- list_component_pins tests -----------------------------------------------

class TestListComponentPins:
    """Tests for list_component_pins tool."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])

    def test_resistor_has_two_pins(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "list_component_pins", reference="R1")
        assert result["status"] == "ok"
        assert result["reference"] == "R1"
        assert result["count"] == 2
        pin_numbers = {p["number"] for p in result["pins"]}
        assert pin_numbers == {"1", "2"}

    def test_each_pin_has_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "list_component_pins", reference="R1")
        for pin in result["pins"]:
            assert "x" in pin and "y" in pin
            assert isinstance(pin["x"], float)
            assert isinstance(pin["y"], float)

    def test_nonexistent_component_returns_error(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "list_component_pins", reference="R99")
        assert "error" in result
        assert "R99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "list_component_pins", reference="R1")


# -- add_label_to_pin tests --------------------------------------------------

class TestAddLabelToPin:
    """Tests for add_label_to_pin tool (auto-place label at pin position)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])

    def test_add_label_to_pin_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label_to_pin", reference="R1", pin_number="1", text="GND")
        assert result["status"] == "ok"
        assert "label_uuid" in result
        assert result["text"] == "GND"
        assert result["reference"] == "R1"
        assert result["pin_number"] == "1"
        assert "position" in result
        assert len(result["position"]) == 2

    def test_label_at_pin_2(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label_to_pin", reference="R1", pin_number="2", text="VCC")
        assert result["status"] == "ok"
        assert result["text"] == "VCC"

    def test_label_sits_on_wire_endpoint_not_bare_pin(self, sch_server):
        """Regression guard: a label with no wire underneath looks connected in
        the schematic view but KiCad ERC reports zero connections (see
        circuit-synth/mcp-kicad-sch-api#3, comment by sebclaude-hub). The label
        must sit on a wire endpoint that is itself anchored at the pin -- not
        at the pin's bare coordinate with no wire at all.
        """
        fn = _get_schematic_fn(sch_server)
        sch = sch_module._current_schematic
        comp = sch_module._find_component_for_pin(sch, "R1", "1")
        pin_pos = sch_module._kicad_pin_position(comp, "1")

        result = _call(fn, "add_label_to_pin", reference="R1", pin_number="1", text="GND")
        label_pos = tuple(result["position"])

        # The label must NOT be placed directly at the pin's own coordinate.
        assert label_pos != (pin_pos.x, pin_pos.y)

        # A wire must run from the pin to the label's position.
        matching_wires = [
            w for w in sch.wires
            if (w.start.x, w.start.y) == (pin_pos.x, pin_pos.y)
            and (w.end.x, w.end.y) == label_pos
        ]
        assert len(matching_wires) == 1, (
            f"expected exactly one wire from pin {pin_pos} to label {label_pos}, "
            f"found {len(matching_wires)}"
        )

        # The label itself must be positioned at that wire's far endpoint.
        label = sch.labels.get(result["label_uuid"])
        assert (label.position.x, label.position.y) == label_pos

    def test_nonexistent_component_returns_error(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label_to_pin", reference="R99", pin_number="1", text="GND")
        assert "error" in result
        assert "R99" in result["error"]

    def test_nonexistent_pin_returns_error(self, sch_server):
        """Boundary: pin that doesn't exist on the component."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label_to_pin", reference="R1", pin_number="99", text="GND")
        assert "error" in result
        assert "99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_label_to_pin", reference="R1", pin_number="1", text="GND")


# -- connect_pins_with_labels tests ------------------------------------------

class TestConnectPinsWithLabels:
    """Tests for connect_pins_with_labels tool (net-label pair connecting two pins)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        _call(fn, "add_component", lib_id="Device:R", reference="R1", value="10k", position=[101.6, 101.6])
        _call(fn, "add_component", lib_id="Device:R", reference="R2", value="22k", position=[200.0, 200.0])

    def test_connect_two_pins(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "connect_pins_with_labels",
                       comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="VOUT")
        assert result["status"] == "ok"
        assert result["net_name"] == "VOUT"
        assert result["labels_created"] == 2
        assert len(result["label_uuids"]) == 2

    def test_connect_produces_two_distinct_uuids(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "connect_pins_with_labels",
                       comp1_ref="R1", pin1="2", comp2_ref="R2", pin2="2", net_name="GND_NET")
        uuids = result["label_uuids"]
        assert uuids[0] != uuids[1]

    def test_both_labels_sit_on_wire_endpoints_not_bare_pins(self, sch_server):
        """Regression guard: each net label must terminate a wire stub rooted
        at its own pin, not float at the pin's bare coordinate with nothing
        underneath (see circuit-synth/mcp-kicad-sch-api#3, comment by
        sebclaude-hub -- a schematic built that way looks fully wired but
        reports zero ERC connections).
        """
        fn = _get_schematic_fn(sch_server)
        sch = sch_module._current_schematic
        pins = [("R1", "1"), ("R2", "1")]
        pin_positions = []
        for ref, pnum in pins:
            comp = sch_module._find_component_for_pin(sch, ref, pnum)
            pin_positions.append(sch_module._kicad_pin_position(comp, pnum))

        result = _call(fn, "connect_pins_with_labels",
                       comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="VOUT")
        label_uuids = result["label_uuids"]

        for pin_pos, label_uuid in zip(pin_positions, label_uuids):
            label = sch.labels.get(label_uuid)
            label_pos = (label.position.x, label.position.y)

            assert label_pos != (pin_pos.x, pin_pos.y)

            matching_wires = [
                w for w in sch.wires
                if (w.start.x, w.start.y) == (pin_pos.x, pin_pos.y)
                and (w.end.x, w.end.y) == label_pos
            ]
            assert len(matching_wires) == 1, (
                f"expected exactly one wire from pin {pin_pos} to label {label_pos}, "
                f"found {len(matching_wires)}"
            )

    def test_bad_first_component(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "connect_pins_with_labels",
                       comp1_ref="R99", pin1="1", comp2_ref="R2", pin2="1", net_name="X")
        assert "error" in result
        assert "R99" in result["error"]

    def test_bad_second_component(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "connect_pins_with_labels",
                       comp1_ref="R1", pin1="1", comp2_ref="R99", pin2="1", net_name="X")
        assert "error" in result
        assert "R99" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "connect_pins_with_labels",
                  comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="X")


# -- add_hierarchical_label tests --------------------------------------------

class TestAddHierarchicalLabel:
    """Tests for add_hierarchical_label tool (shape validation + happy paths)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_input_shape(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_hierarchical_label", text="DATA_IN", position=[100.0, 100.0], shape="input")
        assert result["status"] == "ok"
        assert "label_uuid" in result
        assert result["text"] == "DATA_IN"
        assert result["shape"] == "input"

    def test_add_output_shape(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_hierarchical_label", text="DATA_OUT", position=[100.0, 120.0], shape="output")
        assert result["status"] == "ok"
        assert result["shape"] == "output"

    def test_shape_case_insensitive(self, sch_server):
        """Shape matching is case-insensitive ('OUTPUT' == 'output')."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_hierarchical_label", text="CLK", position=[100.0, 140.0], shape="OUTPUT")
        assert result["status"] == "ok"

    def test_all_valid_shapes_accepted(self, sch_server):
        """All 6 documented shape values must be accepted."""
        fn = _get_schematic_fn(sch_server)
        shapes = ["input", "output", "bidirectional", "tristate", "passive", "unspecified"]
        for i, shape in enumerate(shapes):
            result = _call(fn, "add_hierarchical_label",
                           text=f"SIG_{i}", position=[100.0, float(100 + i * 10)], shape=shape)
            assert result["status"] == "ok", f"shape={shape!r} unexpectedly rejected"

    def test_unknown_shape_returns_error(self, sch_server):
        """Guard: a misspelled shape ('inptu') returns a structured error, not silent default."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_hierarchical_label", text="X", position=[100.0, 100.0], shape="inptu")
        assert "error" in result
        assert "inptu" in result["error"]
        assert "Valid:" in result["error"]

    def test_bad_position_returns_error(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_hierarchical_label", text="X", position=[100.0], shape="input")
        assert "error" in result

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_hierarchical_label", text="X", position=[100.0, 100.0], shape="input")


# -- edit_label tests --------------------------------------------------------

class TestEditLabel:
    """Tests for edit_label tool (mutate existing label in place)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def _add_label(self, sch_server, text="GND", position=None):
        if position is None:
            position = [100.0, 100.0]
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_label", text=text, position=position)
        return result["label_uuid"]

    def test_edit_text(self, sch_server):
        uuid = self._add_label(sch_server, "GND")
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid=uuid, new_text="VCC")
        assert result["status"] == "ok"
        assert result["text"] == "VCC"
        assert result["label_uuid"] == uuid

    def test_edit_text_preserves_rotation_and_size(self, sch_server):
        """h-edit-label: a text-only edit must NOT reset rotation to 0 / size to
        1.27. The buggy code applied both unconditionally on every edit."""
        fn = _get_schematic_fn(sch_server)
        added = _call(fn, "add_label", text="GND", position=[100.0, 100.0],
                      rotation=90.0, size=2.54)
        uuid = added["label_uuid"]
        result = _call(fn, "edit_label", label_uuid=uuid, new_text="VCC")
        assert result["status"] == "ok"
        assert result["text"] == "VCC"
        assert result["rotation"] == 90.0     # preserved, not reset to 0.0
        assert result["size"] == 2.54         # preserved, not reset to 1.27

    def test_edit_position(self, sch_server):
        uuid = self._add_label(sch_server, "SDA")
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid=uuid, position=[200.0, 200.0])
        assert result["status"] == "ok"
        assert result["position"] == [200.0, 200.0]

    def test_edit_rotation(self, sch_server):
        uuid = self._add_label(sch_server, "SCL")
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid=uuid, rotation=90.0)
        assert result["status"] == "ok"
        assert result["rotation"] == 90.0

    def test_edit_size(self, sch_server):
        uuid = self._add_label(sch_server, "SIG")
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid=uuid, size=2.54)
        assert result["status"] == "ok"
        assert result["size"] == 2.54

    def test_no_modifications_returns_error(self, sch_server):
        """Boundary: calling with all None should return a structured error."""
        uuid = self._add_label(sch_server, "X")
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid=uuid)
        assert "error" in result
        assert "No modifications" in result["error"]

    def test_bad_uuid_returns_error(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid="bad-uuid-that-does-not-exist", new_text="X")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_bad_position_list_returns_error(self, sch_server):
        """Boundary: position with wrong element count."""
        uuid = self._add_label(sch_server, "X")
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "edit_label", label_uuid=uuid, position=[100.0])
        assert "error" in result

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "edit_label", label_uuid="any-uuid", new_text="X")


# -- add_text tests ----------------------------------------------------------

class TestAddText:
    """Tests for add_text tool (free-floating text element)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_text_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_text", text="Hello World", position=[50.0, 50.0])
        assert result["status"] == "ok"
        assert "text_uuid" in result
        assert result["text"] == "Hello World"
        assert result["position"] == [50.0, 50.0]

    def test_add_text_with_rotation(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_text", text="Rotated", position=[100.0, 100.0], rotation=90.0)
        assert result["status"] == "ok"
        assert result["text"] == "Rotated"

    def test_add_text_bad_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_text", text="X", position=[50.0])  # missing y
        assert "error" in result

    def test_add_text_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_text", text="X", position=[50.0, 50.0])


# -- add_text_box tests ------------------------------------------------------

class TestAddTextBox:
    """Tests for add_text_box tool (text inside a rectangle)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_text_box_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_text_box", text="Note here", position=[10.0, 10.0], sheet_size=[30.0, 15.0])
        assert result["status"] == "ok"
        assert "textbox_uuid" in result
        assert result["text"] == "Note here"
        assert result["position"] == [10.0, 10.0]
        assert result["size"] == [30.0, 15.0]

    def test_add_text_box_bad_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_text_box", text="X", position=[10.0], sheet_size=[30.0, 15.0])  # position missing y
        assert "error" in result

    def test_add_text_box_bad_size(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_text_box", text="X", position=[10.0, 10.0], sheet_size=[30.0])  # size missing height
        assert "error" in result

    def test_add_text_box_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_text_box", text="X", position=[10.0, 10.0], sheet_size=[30.0, 15.0])


# -- add_sheet tests ---------------------------------------------------------

class TestAddSheet:
    """Tests for add_sheet tool (hierarchical sheet element)."""

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")

    def test_add_sheet_basic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet",
            name="PowerSupply",
            filename="power.kicad_sch",
            position=[100.0, 50.0],
            sheet_size=[40.0, 25.0],
        )
        assert result["status"] == "ok"
        assert "sheet_uuid" in result
        assert result["name"] == "PowerSupply"
        assert result["filename"] == "power.kicad_sch"
        assert result["position"] == [100.0, 50.0]
        assert result["size"] == [40.0, 25.0]

    def test_add_sheet_uuid_is_string(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet", name="Sub", filename="sub.kicad_sch", position=[50.0, 50.0], sheet_size=[30.0, 20.0])
        assert isinstance(result["sheet_uuid"], str)
        assert len(result["sheet_uuid"]) > 0

    def test_add_sheet_bad_position(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet", name="Sub", filename="sub.kicad_sch", position=[50.0], sheet_size=[30.0, 20.0])
        assert "error" in result

    def test_add_sheet_bad_size(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet", name="Sub", filename="sub.kicad_sch", position=[50.0, 50.0], sheet_size=[30.0])
        assert "error" in result

    def test_add_sheet_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_sheet",
                name="Sub", filename="sub.kicad_sch",
                position=[50.0, 50.0], sheet_size=[30.0, 20.0]
            )


# -- add_sheet_pin tests -----------------------------------------------------

class TestAddSheetPin:
    """Tests for add_sheet_pin tool.

    The signature is (sheet_uuid, name, pin_type, edge, position_along_edge),
    matching kicad-sch-api's edge-based positioning.  An earlier revision
    used a position tuple and silently broke when the upstream library
    moved to edge-based positioning — this test class pins both the new
    contract and all guard paths.
    """

    @pytest.fixture(autouse=True)
    def _create_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        _call(fn, "create", name="test")
        r = _call(fn, "add_sheet", name="Sub", filename="sub.kicad_sch", position=[100.0, 50.0], sheet_size=[30.0, 20.0])
        self.sheet_uuid = r["sheet_uuid"]

    def test_add_pin_success(self, sch_server):
        """Happy path — valid pin_type + edge produces a uuid."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name="CLK",
            pin_type="input",
            edge="left",
            position_along_edge=10.0,
        )
        assert result["status"] == "ok"
        assert result["pin_uuid"]
        assert result["name"] == "CLK"
        assert result["pin_type"] == "input"
        assert result["edge"] == "left"
        assert result["position_along_edge"] == 10.0

    @pytest.mark.parametrize("pin_type", [
        "input", "output", "bidirectional", "tri_state", "passive",
    ])
    def test_all_valid_pin_types_succeed(self, sch_server, pin_type):
        """All 5 documented pin_type values reach the API and succeed."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name=f"SIG_{pin_type}",
            pin_type=pin_type,
            edge="right",
            position_along_edge=5.0,
        )
        assert result["status"] == "ok"
        assert result["pin_type"] == pin_type

    @pytest.mark.parametrize("edge", ["right", "bottom", "left", "top"])
    def test_all_valid_edges_succeed(self, sch_server, edge):
        """All 4 documented edge values reach the API and succeed."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name=f"PIN_{edge}",
            pin_type="input",
            edge=edge,
            position_along_edge=5.0,
        )
        assert result["status"] == "ok"
        assert result["edge"] == edge

    def test_pin_type_is_lowercased(self, sch_server):
        """Guard accepts mixed-case pin_type and lowercases it for the API."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name="X",
            pin_type="INPUT",
            edge="left",
            position_along_edge=5.0,
        )
        assert result["status"] == "ok"
        assert result["pin_type"] == "input"

    def test_edge_is_lowercased(self, sch_server):
        """Guard accepts mixed-case edge and lowercases it for the API."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name="Y",
            pin_type="input",
            edge="RIGHT",
            position_along_edge=5.0,
        )
        assert result["status"] == "ok"
        assert result["edge"] == "right"

    def test_invalid_pin_type_rejected(self, sch_server):
        """Guard: unknown pin_type must return a structured error, never reach API."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name="CLK",
            pin_type="not_a_valid_type",
            edge="left",
            position_along_edge=5.0,
        )
        assert "error" in result
        assert "not_a_valid_type" in result["error"]
        assert "Valid:" in result["error"]

    def test_invalid_edge_rejected(self, sch_server):
        """Guard: unknown edge value returns a structured error."""
        fn = _get_schematic_fn(sch_server)
        result = _call(fn, "add_sheet_pin",
            sheet_uuid=self.sheet_uuid,
            name="CLK",
            pin_type="input",
            edge="diagonal",
            position_along_edge=5.0,
        )
        assert "error" in result
        assert "diagonal" in result["error"]
        assert "Valid:" in result["error"]

    def test_no_schematic(self, sch_server):
        sch_module._current_schematic = None
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_sheet_pin",
                sheet_uuid="any", name="X", pin_type="input",
                edge="left", position_along_edge=0.0,
            )


# -- Extended TestNoSchematicLoaded coverage ---------------------------------

class TestNoSchematicLoadedExtended:
    """Extend the existing TestNoSchematicLoaded with the new tools."""

    def test_backup_schematic_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "backup")

    def test_filter_components_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "filter_components", lib_id="Device:R")

    def test_components_in_area_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "components_in_area", x1=0, y1=0, x2=100, y2=100)

    def test_bulk_update_components_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "bulk_update_components",
                criteria={"lib_id": "Device:R"}, updates={"value": "22k"}
            )

    def test_get_component_pin_position_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "get_component_pin_position", reference="R1", pin_number="1")

    def test_list_component_pins_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "list_component_pins", reference="R1")

    def test_add_label_to_pin_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_label_to_pin", reference="R1", pin_number="1", text="GND")

    def test_connect_pins_with_labels_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "connect_pins_with_labels",
                comp1_ref="R1", pin1="1", comp2_ref="R2", pin2="1", net_name="X"
            )

    def test_add_hierarchical_label_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_hierarchical_label", text="X", position=[100.0, 100.0], shape="input")

    def test_edit_label_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "edit_label", label_uuid="x", new_text="Y")

    def test_add_text_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_text", text="X", position=[50.0, 50.0])

    def test_add_text_box_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_text_box", text="X", position=[10.0, 10.0], sheet_size=[20.0, 10.0])

    def test_add_sheet_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_sheet",
                name="S", filename="s.kicad_sch",
                position=[50.0, 50.0], sheet_size=[30.0, 20.0]
            )

    def test_add_multi_unit_no_schematic(self, sch_server):
        fn = _get_schematic_fn(sch_server)
        with pytest.raises(RuntimeError, match="No schematic loaded"):
            _call(fn, "add_multi_unit_component",
                lib_id="Amplifier_Operational:LM358",
                reference="U1", value="LM358",
                position=[100.0, 100.0],
            )
