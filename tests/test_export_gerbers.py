"""Tests for export(operation="gerbers") / _op_gerbers().

kicad-cli is mocked so tests run without KiCad installed. Focused on the
CRITICAL regression: kicad-cli exiting 0 but writing a 0-byte gerber/drill
file (a truncated write, or an empty layer) used to pass the
existence-only check in _op_gerbers and get silently zipped into the fab
package as a "successful" export.
"""
import asyncio
import os
import subprocess

import pytest

from tests.conftest import get_tool_fn


@pytest.fixture
def pcb_path(tmp_path):
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text('(kicad_pcb (version 20240108) (generator "test"))\n')
    return str(pcb)


def _mock_run_writing(files_by_ext, output_dir):
    """subprocess.run mock: on the gerbers call writes one file per
    (name, content) pair; the drill call writes nothing extra (real
    kicad-cli runs are separate gerbers/drill subprocess calls, but for this
    test both write into output_dir before _op_gerbers globs it)."""
    written = {"done": False}

    def _run(cmd, **kwargs):
        if not written["done"]:
            for name, content in files_by_ext.items():
                with open(os.path.join(output_dir, name), "wb") as f:
                    f.write(content)
            written["done"] = True
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _run


class TestGerbersZeroByteOutput:

    def test_zero_byte_gerber_file_is_an_error_not_success(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b""}, output_dir),  # 0 bytes
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=False,
        ))
        assert "error" in result
        assert result.get("status") != "ok"
        assert "test-F_Cu.gbr" in result.get("empty_files", [])

    def test_nonempty_gerber_file_still_succeeds(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b"G04 fake gerber content*\n"}, output_dir),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=False,
        ))
        assert result["status"] == "ok"
        assert result["gerber_count"] == 1

    def test_one_empty_among_several_files_flags_only_the_empty_one(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({
                "test-F_Cu.gbr": b"G04 real content*\n",
                "test-B_Cu.gbr": b"",  # this one is empty
            }, output_dir),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=False,
        ))
        assert "error" in result
        assert result["empty_files"] == ["test-B_Cu.gbr"]
