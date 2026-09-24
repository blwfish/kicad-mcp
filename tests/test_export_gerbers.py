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


class TestGerbersStaleLeftoverFiles:
    """Regression: output_dir is reused across runs (os.makedirs(...,
    exist_ok=True)), and the file-globbing comment assumed the directory
    "is freshly created per export and contains only kicad-cli output" --
    a stale file left over from a PREVIOUS export (e.g. a different
    board's gerber that doesn't share a filename with anything this run
    writes) was silently swept into the fab-package ZIP alongside this
    run's real output."""

    def test_stale_leftover_file_excluded_from_output(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        import time
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        # A leftover file from a previous, unrelated export run.
        stale_path = os.path.join(output_dir, "old-board-F_Cu.gbr")
        with open(stale_path, "wb") as f:
            f.write(b"G04 stale content from a previous run*\n")
        old_time = time.time() - 3600  # 1 hour old
        os.utime(stale_path, (old_time, old_time))

        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b"G04 fresh content*\n"}, output_dir),
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
        assert result["gerber_files"] == ["test-F_Cu.gbr"]
        assert result["gerber_count"] == 1
        assert result["ignored_stale_files"] == ["old-board-F_Cu.gbr"]

    def test_no_stale_files_no_warning_key(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b"G04 fresh content*\n"}, output_dir),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=False,
        ))
        assert "ignored_stale_files" not in result


class TestGerbersFileClassification:
    """Regression: the prior binary split ("not drill = gerber") put ANY
    unrecognized extension into the gerber bucket silently -- a stray
    .log/.rpt/.txt file kicad-cli or a future version might emit would be
    reported as a "gerber" with no way to tell it apart from a real one."""

    def test_unrecognized_extension_is_reported_separately(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({
                "test-F_Cu.gbr": b"G04 real content*\n",
                "test.log": b"kicad-cli diagnostic output",
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
        assert result["status"] == "ok"
        assert result["gerber_files"] == ["test-F_Cu.gbr"]
        assert result["gerber_count"] == 1
        assert result["other_files"] == ["test.log"]
        assert result["total_files"] == 2

    def test_no_other_files_key_when_all_recognized(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b"G04 real content*\n"}, output_dir),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=False,
        ))
        assert "other_files" not in result

    def test_drill_extensions_never_land_in_gerber_bucket(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({
                "test.drl": b"M48\nT1C0.3\n%\n",
                "test-F_Cu.gbr": b"G04 real content*\n",
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
        assert result["drill_files"] == ["test.drl"]
        assert result["gerber_files"] == ["test-F_Cu.gbr"]
        assert "other_files" not in result


class TestGerbersFilesystemErrors:
    """Regression: os.makedirs/os.listdir/zipfile.ZipFile were unguarded --
    an OSError (permission denied, disk full) would propagate uncaught
    through the MCP tool call instead of the clean {"error": ...} dict
    every other failure mode in this function returns."""

    def test_makedirs_failure_returns_clean_error(self, mcp_server, pcb_path, monkeypatch):
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.os.makedirs",
            lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path,
            output_dir="/some/dir", create_zip=False,
        ))
        assert "error" in result
        assert "permission denied" in result["error"]

    def test_listdir_failure_returns_clean_error(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b"G04 real content*\n"}, output_dir),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.os.listdir",
            lambda *a, **k: (_ for _ in ()).throw(OSError("stale mount")),
        )
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=False,
        ))
        assert "error" in result
        assert "stale mount" in result["error"]

    def test_zip_failure_reports_error_but_keeps_export_data(self, mcp_server, pcb_path, tmp_path, monkeypatch):
        output_dir = str(tmp_path / "gerbers")
        os.makedirs(output_dir, exist_ok=True)
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_writing({"test-F_Cu.gbr": b"G04 real content*\n"}, output_dir),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.get_kicad_cli_path", lambda required=True: "/usr/bin/kicad-cli"
        )

        class _FailingZipFile:
            def __init__(self, *a, **k):
                raise OSError("disk full")

        monkeypatch.setattr("kicad_mcp.tools.export.zipfile.ZipFile", _FailingZipFile)
        fn = get_tool_fn(mcp_server, "export")
        result = asyncio.run(fn(
            operation="gerbers", ctx=None, pcb_path=pcb_path, output_dir=output_dir,
            create_zip=True,
        ))
        assert result["status"] == "error"
        assert "disk full" in result["error"]
        # The gerber files themselves were already written and reported --
        # a zip failure must not hide that real export data exists on disk.
        assert result["gerber_files"] == ["test-F_Cu.gbr"]
