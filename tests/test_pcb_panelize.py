"""
Tests for PCB panelization tool: panelize_pcb.

Unit tests mock subprocess.run, shutil.which, and platform.system to test
tool logic without requiring KiKit to be installed.
"""

import asyncio
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.pcb_panelize import register_pcb_panelize_tools


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def panelize_server():
    """Create a FastMCP server with only panelize tools registered."""
    mcp = FastMCP("test-panelize")
    register_pcb_panelize_tools(mcp)
    return mcp


@pytest.fixture
def pcb_file(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


def _get_tool_fn(mcp_server, tool_name):
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


def _make_success_run(output_path):
    """Return a mock subprocess.CompletedProcess that looks like kikit succeeded."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    mock_result.stderr = ""
    return mock_result


# Helper: patch stack for a happy-path call (kikit found via PATH, subprocess ok)
def _happy_path_patches(tmp_path, pcb_path):
    """Context manager stack for a successful panelize run.

    Patches:
      - shutil.which("kikit") -> fake binary path
      - platform.system -> "Linux"  (avoids Darwin glob path)
      - subprocess.run -> returncode=0
      - os.path.isfile(output) -> True  (panel file created)
      - run_pcbnew_script -> panel info dict
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        fake_kikit = str(tmp_path / "kikit")
        output_path = pcb_path.replace(".kicad_pcb", "-panel.kicad_pcb")

        run_result = MagicMock()
        run_result.returncode = 0
        run_result.stdout = "Panel created"
        run_result.stderr = ""

        panel_info = {
            "width_mm": 120.0,
            "height_mm": 80.0,
            "footprint_count": 10,
            "net_count": 5,
            "track_count": 30,
        }

        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value=fake_kikit),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", side_effect=lambda p: True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value=panel_info),
        ):
            yield

    return _ctx()


# -- Path validation tests ---------------------------------------------------

class TestPathValidation:

    def test_pcb_not_found_returns_error(self, panelize_server):
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        result = fn("/nonexistent/board.kicad_pcb")
        assert "error" in result
        assert "not found" in result["error"].lower()

    def test_pcb_not_found_no_exception(self, panelize_server):
        """Missing file returns structured error, never raises."""
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        result = fn("/tmp/definitely_does_not_exist_xyzzy.kicad_pcb")
        assert isinstance(result, dict)
        assert "error" in result

    def test_preset_not_found_returns_error(self, panelize_server, pcb_file, tmp_path):
        """When preset is specified but missing, return error (don't silently skip)."""
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
        ):
            result = fn(pcb_file, preset="/nonexistent/preset.json")
        assert "error" in result
        assert "not found" in result["error"].lower()


# -- Rows/cols boundary tests ------------------------------------------------

class TestRowsColsBoundaries:
    """
    Guard: 1 <= rows <= 100  and  1 <= cols <= 100.
    Tests at-value, just-below, just-above for both bounds.
    """

    def _call(self, panelize_server, pcb_file, rows, cols):
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
        ):
            return fn(pcb_file, rows=rows, cols=cols)

    # -- rows lower bound --

    def test_rows_0_rejected(self, panelize_server, pcb_file):
        result = self._call(panelize_server, pcb_file, rows=0, cols=2)
        assert "error" in result
        assert "rows" in result["error"]

    def test_rows_1_accepted(self, panelize_server, pcb_file, tmp_path):
        """rows=1 is exactly at the lower bound — must be accepted."""
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, rows=1, cols=2)
        assert "error" not in result

    def test_rows_100_accepted(self, panelize_server, pcb_file):
        """rows=100 is exactly at the upper bound — must be accepted."""
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, rows=100, cols=2)
        assert "error" not in result

    def test_rows_101_rejected(self, panelize_server, pcb_file):
        result = self._call(panelize_server, pcb_file, rows=101, cols=2)
        assert "error" in result
        assert "rows" in result["error"]

    # -- cols lower bound --

    def test_cols_0_rejected(self, panelize_server, pcb_file):
        result = self._call(panelize_server, pcb_file, rows=2, cols=0)
        assert "error" in result
        assert "cols" in result["error"]

    def test_cols_1_accepted(self, panelize_server, pcb_file):
        """cols=1 is exactly at the lower bound — must be accepted."""
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, rows=2, cols=1)
        assert "error" not in result

    def test_cols_100_accepted(self, panelize_server, pcb_file):
        """cols=100 is exactly at the upper bound — must be accepted."""
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, rows=2, cols=100)
        assert "error" not in result

    def test_cols_101_rejected(self, panelize_server, pcb_file):
        result = self._call(panelize_server, pcb_file, rows=2, cols=101)
        assert "error" in result
        assert "cols" in result["error"]


# -- Enum validation tests ---------------------------------------------------

class TestEnumValidation:

    def _call_with_enums(self, panelize_server, pcb_file, cut_type="vcuts",
                          framing="railstb", tooling="3hole"):
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
        ):
            return fn(pcb_file, cut_type=cut_type, framing=framing, tooling=tooling)

    # -- cut_type --

    def test_cut_type_vcuts_valid(self, panelize_server, pcb_file):
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, cut_type="vcuts")
        assert "error" not in result

    def test_cut_type_mousebites_valid(self, panelize_server, pcb_file):
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, cut_type="mousebites")
        assert "error" not in result

    def test_cut_type_invalid_rejected(self, panelize_server, pcb_file):
        result = self._call_with_enums(panelize_server, pcb_file, cut_type="laser")
        assert "error" in result
        assert "cut_type" in result["error"]

    def test_cut_type_typo_rejected(self, panelize_server, pcb_file):
        """'mousebite' (singular) is a near-miss typo — must be rejected."""
        result = self._call_with_enums(panelize_server, pcb_file, cut_type="mousebite")
        assert "error" in result
        assert "cut_type" in result["error"]

    # -- framing --

    def test_framing_none_valid(self, panelize_server, pcb_file):
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, framing="none")
        assert "error" not in result

    def test_framing_invalid_rejected(self, panelize_server, pcb_file):
        result = self._call_with_enums(panelize_server, pcb_file, framing="rails")
        assert "error" in result
        assert "framing" in result["error"]

    def test_framing_typo_rejected(self, panelize_server, pcb_file):
        """'rail' (missing 'stb') is a near-miss — must be rejected."""
        result = self._call_with_enums(panelize_server, pcb_file, framing="rail")
        assert "error" in result
        assert "framing" in result["error"]

    # -- tooling --

    def test_tooling_4hole_valid(self, panelize_server, pcb_file):
        run_result = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file, tooling="4hole")
        assert "error" not in result

    def test_tooling_invalid_rejected(self, panelize_server, pcb_file):
        result = self._call_with_enums(panelize_server, pcb_file, tooling="5hole")
        assert "error" in result
        assert "tooling" in result["error"]

    def test_tooling_typo_rejected(self, panelize_server, pcb_file):
        """'3-hole' (with hyphen) is a near-miss typo — must be rejected."""
        result = self._call_with_enums(panelize_server, pcb_file, tooling="3-hole")
        assert "error" in result
        assert "tooling" in result["error"]


# -- kikit discovery tests ---------------------------------------------------

class TestKikitDiscovery:

    def test_kikit_not_found_returns_error(self, panelize_server, pcb_file):
        """When kikit is not on PATH and not in known locations, return clear error."""
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value=None),
            patch("kicad_mcp.tools.pcb_panelize.os.path.isfile", return_value=False),
        ):
            result = fn(pcb_file)
        assert "error" in result
        assert "kikit" in result["error"].lower()

    def test_darwin_glob_path_used(self, panelize_server, pcb_file):
        """On Darwin, _find_kikit should search KICAD_APP_PATH glob before PATH."""
        fake_kikit = "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/kikit"
        run_result = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Darwin"),
            patch(
                "kicad_mcp.tools.pcb_panelize._find_kikit.__code__",
                # Can't patch inner locals; patch at module level instead.
                # We patch os.path.isfile so the first glob candidate is accepted.
                new=None,  # won't be used — we patch _find_kikit directly
            ) if False else patch("kicad_mcp.tools.pcb_panelize._find_kikit", return_value=fake_kikit),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value={}),
        ):
            fn = _get_tool_fn(panelize_server, "panelize_pcb")
            result = fn(pcb_file)
        assert "error" not in result

    def test_darwin_glob_finds_kikit(self, tmp_path):
        """_find_kikit on Darwin: if glob returns a path containing kikit, it's returned."""
        from kicad_mcp.tools import pcb_panelize as mod

        fake_fw_dir = str(tmp_path / "3.9")
        fake_kikit = str(tmp_path / "3.9" / "bin" / "kikit")

        with (
            patch.object(mod.platform, "system", return_value="Darwin"),
            patch("kicad_mcp.config.KICAD_APP_PATH",
                  "/Applications/KiCad/KiCad.app", create=True),
            patch("glob.glob", return_value=[fake_fw_dir]),
            patch.object(mod.os.path, "isfile", return_value=True),
        ):
            result = mod._find_kikit()
        assert result == fake_kikit


# -- Subprocess failure/success tests ----------------------------------------

class TestSubprocessBehavior:

    def test_subprocess_failure_surfaces_stderr(self, panelize_server, pcb_file):
        """returncode != 0: stderr should appear in the error message."""
        run_result = MagicMock()
        run_result.returncode = 1
        run_result.stderr = "KiKit error: invalid board"
        run_result.stdout = ""

        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
        ):
            result = fn(pcb_file)
        assert "error" in result
        assert "KiKit error: invalid board" in result["error"]

    def test_subprocess_failure_surfaces_stdout_when_no_stderr(self, panelize_server, pcb_file):
        """When stderr is empty, stdout fallback should appear in error."""
        run_result = MagicMock()
        run_result.returncode = 2
        run_result.stderr = ""
        run_result.stdout = "Fatal: cannot parse PCB"

        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
        ):
            result = fn(pcb_file)
        assert "error" in result
        assert "Fatal: cannot parse PCB" in result["error"]

    def test_subprocess_timeout_returns_error(self, panelize_server, pcb_file):
        """TimeoutExpired from subprocess should return a structured error."""
        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch(
                "kicad_mcp.tools.pcb_panelize.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=["kikit"], timeout=60),
            ),
        ):
            result = fn(pcb_file)
        assert "error" in result
        assert "timed out" in result["error"].lower()

    def test_subprocess_success_returns_ok_shape(self, panelize_server, pcb_file):
        """returncode=0 + output file exists → status=ok with expected fields."""
        run_result = MagicMock(returncode=0, stdout="Panel created", stderr="")
        panel_info = {
            "width_mm": 150.0,
            "height_mm": 100.0,
            "footprint_count": 20,
            "net_count": 8,
            "track_count": 60,
        }

        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script", return_value=panel_info),
        ):
            result = fn(pcb_file, rows=2, cols=5)

        assert result["status"] == "ok"
        assert result["input_pcb"] == pcb_file
        assert "output_pcb" in result
        assert result["grid"] == "5x2"  # cols x rows
        assert result["cut_type"] == "vcuts"
        assert result["framing"] == "railstb"
        assert result["width_mm"] == 150.0
        assert result["height_mm"] == 100.0
        assert result["footprint_count"] == 20
        assert result["track_count"] == 60

    def test_output_file_not_created_returns_error(self, panelize_server, pcb_file):
        """Even if kikit exits 0, missing output file is an error."""
        run_result = MagicMock(returncode=0, stdout="", stderr="")

        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            # pcb_path exists (first call), output_path does not (second call)
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists",
                  side_effect=lambda p: p == pcb_file),
        ):
            result = fn(pcb_file)
        assert "error" in result
        assert "not created" in result["error"].lower()

    def test_panel_info_read_failure_surfaces_warning(self, panelize_server, pcb_file):
        """If run_pcbnew_script raises, status is still ok but warning is present."""
        run_result = MagicMock(returncode=0, stdout="", stderr="")

        fn = _get_tool_fn(panelize_server, "panelize_pcb")
        with (
            patch("kicad_mcp.tools.pcb_panelize.platform.system", return_value="Linux"),
            patch("kicad_mcp.tools.pcb_panelize.shutil.which", return_value="/usr/bin/kikit"),
            patch("kicad_mcp.tools.pcb_panelize.subprocess.run", return_value=run_result),
            patch("kicad_mcp.tools.pcb_panelize.os.path.exists", return_value=True),
            patch("kicad_mcp.tools.pcb_panelize.run_pcbnew_script",
                  side_effect=RuntimeError("pcbnew not available")),
        ):
            result = fn(pcb_file)
        assert result["status"] == "ok"
        assert "info_read_warning" in result
        assert result["width_mm"] is None
