"""
Tests for the pcbnew bridge utility.

Tests the bridge functions that manage subprocess execution to KiCad's Python.
"""

import json
import os
from unittest.mock import patch, MagicMock

import pytest

from kicad_mcp.utils.pcbnew_bridge import (
    _filter_stderr,
    _get_kicad_python,
    run_pcbnew_script,
)


# -- _filter_stderr tests ---------------------------------------------------

class TestFilterStderr:

    def test_removes_wxapp_assertion(self):
        stderr = "assert IsOk wxApp blah blah"
        assert _filter_stderr(stderr) == ""

    def test_removes_gtk_warning(self):
        stderr = "(kicad:1234): Gtk-WARNING **: some warning"
        assert _filter_stderr(stderr) == ""

    def test_removes_empty_lines(self):
        stderr = "\n\n  \n"
        assert _filter_stderr(stderr) == ""

    def test_keeps_real_errors(self):
        stderr = "Traceback (most recent call last):\n  File foo.py, line 1\nNameError: x"
        filtered = _filter_stderr(stderr)
        assert "Traceback" in filtered
        assert "NameError" in filtered

    def test_mixed_content(self):
        stderr = (
            "assert IsOk wxApp created\n"
            "ImportError: No module named foo\n"
            "  \n"
        )
        filtered = _filter_stderr(stderr)
        assert "ImportError" in filtered
        assert "wxApp" not in filtered


# -- _get_kicad_python tests ------------------------------------------------

class TestGetKicadPython:

    @patch("kicad_mcp.utils.pcbnew_bridge.platform.system", return_value="Darwin")
    @patch("kicad_mcp.utils.pcbnew_bridge.os.path.isfile")
    def test_finds_macos_python(self, mock_isfile, mock_system):
        mock_isfile.return_value = True
        result = _get_kicad_python()
        assert result is not None
        assert "python3" in result.lower()

    @patch("kicad_mcp.utils.pcbnew_bridge.platform.system", return_value="Darwin")
    @patch("kicad_mcp.utils.pcbnew_bridge.os.path.isfile", return_value=False)
    def test_returns_none_when_not_found(self, mock_isfile, mock_system):
        result = _get_kicad_python()
        assert result is None

    @patch("kicad_mcp.utils.pcbnew_bridge.platform.system", return_value="Linux")
    @patch("kicad_mcp.utils.pcbnew_bridge.os.path.isfile", return_value=True)
    def test_linux_path(self, mock_isfile, mock_system):
        result = _get_kicad_python()
        assert result == "/usr/bin/python3"

    @patch("kicad_mcp.utils.pcbnew_bridge.platform.system", return_value="UnknownOS")
    def test_unknown_os(self, mock_system):
        result = _get_kicad_python()
        assert result is None


# -- run_pcbnew_script tests ------------------------------------------------

class TestRunPcbnewScript:

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python", return_value=None)
    def test_raises_when_no_kicad_python(self, mock_get_python):
        with pytest.raises(RuntimeError, match="KiCad Python interpreter not found"):
            run_pcbnew_script('print("hello")')

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_successful_execution(self, mock_run, mock_python):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"status": "ok", "value": 42}\n',
            stderr="",
        )
        result = run_pcbnew_script('print(json.dumps({"status": "ok", "value": 42}))')
        assert result["status"] == "ok"
        assert result["value"] == 42

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_nonzero_exit_raises(self, mock_run, mock_python):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="ImportError: No module named pcbnew",
        )
        with pytest.raises(RuntimeError, match="pcbnew script failed"):
            run_pcbnew_script('import pcbnew')

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_empty_output_raises(self, mock_run, mock_python):
        mock_run.return_value = MagicMock(
            returncode=0, stdout="", stderr=""
        )
        with pytest.raises(RuntimeError, match="no output"):
            run_pcbnew_script('pass')

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_json_after_swig_warning(self, mock_run, mock_python):
        """JSON is found even with SWIG memory leak warnings on stdout."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                '{"status": "ok"}\n'
                "swig/python detected a memory leak of type 'ZONE *'\n"
            ),
            stderr="",
        )
        result = run_pcbnew_script('pass')
        assert result["status"] == "ok"

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_no_json_in_output(self, mock_run, mock_python):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="just some text with no json\n",
            stderr="",
        )
        with pytest.raises(RuntimeError, match="no valid JSON"):
            run_pcbnew_script('print("not json")')

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_timeout_raises(self, mock_run, mock_python):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="python", timeout=30)
        with pytest.raises(RuntimeError, match="timed out"):
            run_pcbnew_script('import time; time.sleep(60)', timeout=30)

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_params_written_to_temp_file(self, mock_run, mock_python):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"status": "ok"}\n',
            stderr="",
        )
        run_pcbnew_script('pass', params={"pcb_path": "/tmp/test.kicad_pcb"})
        cmd = mock_run.call_args[0][0]
        # Should have python, script_path, params_path
        assert len(cmd) == 3

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_no_params_no_extra_arg(self, mock_run, mock_python):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"status": "ok"}\n',
            stderr="",
        )
        run_pcbnew_script('pass')
        cmd = mock_run.call_args[0][0]
        assert len(cmd) == 2  # python + script only

    @patch("kicad_mcp.utils.pcbnew_bridge._get_kicad_python",
           return_value="/usr/bin/python3")
    @patch("kicad_mcp.utils.pcbnew_bridge.subprocess.run")
    def test_temp_files_cleaned_up(self, mock_run, mock_python):
        """Temp script and param files are deleted after execution."""
        created_files = []

        def capture_cmd(*args, **kwargs):
            cmd = args[0]
            created_files.extend(cmd[1:])  # script and optionally params path
            return MagicMock(returncode=0, stdout='{"status": "ok"}\n', stderr="")

        mock_run.side_effect = capture_cmd
        run_pcbnew_script('pass', params={"key": "value"})
        for f in created_files:
            assert not os.path.exists(f), f"Temp file not cleaned up: {f}"
