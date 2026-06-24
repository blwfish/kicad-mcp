"""
Tests for the pcbnew bridge utility.

Tests the bridge functions that manage subprocess execution to KiCad's Python.
"""

import os
from unittest.mock import patch, MagicMock

import pytest

from kicad_mcp.utils.pcbnew_bridge import (
    _extract_last_json_object,
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
    @patch("kicad_mcp.utils.pcbnew_bridge.glob.glob", return_value=["/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9"])
    @patch("kicad_mcp.utils.pcbnew_bridge.os.path.isfile")
    def test_finds_macos_python(self, mock_isfile, mock_glob, mock_system):
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


# -- _extract_last_json_object tests ----------------------------------------

class TestExtractLastJsonObject:
    """The JSON extractor handles scripts that produce JSON in various shapes,
    mixed with SWIG/GTK diagnostic chatter on stdout."""

    def test_single_line_json(self):
        result = _extract_last_json_object('{"status": "ok"}')
        assert result == {"status": "ok"}

    def test_multi_line_indented_json(self):
        stdout = '{\n  "status": "ok",\n  "data": [1, 2, 3]\n}'
        result = _extract_last_json_object(stdout)
        assert result == {"status": "ok", "data": [1, 2, 3]}

    def test_json_preceded_by_swig_warning(self):
        stdout = "swig/python detected memory leak\n{\"status\": \"ok\"}"
        assert _extract_last_json_object(stdout) == {"status": "ok"}

    def test_json_followed_by_swig_warning(self):
        stdout = '{"status": "ok"}\nswig/python detected memory leak'
        assert _extract_last_json_object(stdout) == {"status": "ok"}

    def test_multiple_objects_returns_last(self):
        stdout = '{"first": 1}\n{"second": 2}'
        assert _extract_last_json_object(stdout) == {"second": 2}

    def test_brace_inside_string_value(self):
        """Braces inside a JSON string literal must NOT affect depth tracking."""
        stdout = '{"path": "/tmp/{a}/{b}", "valid": true}'
        result = _extract_last_json_object(stdout)
        assert result == {"path": "/tmp/{a}/{b}", "valid": True}

    def test_escaped_quote_inside_string(self):
        """\\" inside a string value must not be treated as string-end."""
        stdout = '{"msg": "He said \\"hi\\" loudly"}'
        result = _extract_last_json_object(stdout)
        assert result == {"msg": 'He said "hi" loudly'}

    def test_escaped_backslash_then_quote(self):
        r"""\\\\ then \" — backslash escapes, then quote ends string."""
        stdout = r'{"path": "C:\\test\\"}'
        result = _extract_last_json_object(stdout)
        assert result == {"path": "C:\\test\\"}

    def test_no_json_returns_none(self):
        assert _extract_last_json_object("no json here") is None
        assert _extract_last_json_object("") is None

    def test_unbalanced_braces_returns_none(self):
        """Unclosed { should not return a partial parse."""
        assert _extract_last_json_object('{"status": "ok"') is None

    def test_invalid_json_in_balanced_braces_returns_none(self):
        """{not valid json} is balanced but doesn't parse."""
        assert _extract_last_json_object("{not valid json}") is None

    def test_invalid_then_valid_returns_valid(self):
        """An earlier invalid object doesn't block a later valid one."""
        stdout = '{not valid}\n{"status": "ok"}'
        assert _extract_last_json_object(stdout) == {"status": "ok"}

    def test_nested_objects(self):
        stdout = '{"outer": {"inner": {"deep": "value"}}, "list": [1, 2]}'
        result = _extract_last_json_object(stdout)
        assert result == {"outer": {"inner": {"deep": "value"}}, "list": [1, 2]}

    def test_pretty_printed_with_strings_containing_braces(self):
        """Realistic case: pretty-printed JSON whose values contain { and }."""
        stdout = (
            'Some warning\n'
            '{\n'
            '  "status": "ok",\n'
            '  "message": "couldn\'t find {symbol} in library",\n'
            '  "data": {"nested": 1}\n'
            '}\n'
            'trailing warning'
        )
        result = _extract_last_json_object(stdout)
        assert result["status"] == "ok"
        assert result["data"] == {"nested": 1}
        assert "{symbol}" in result["message"]
