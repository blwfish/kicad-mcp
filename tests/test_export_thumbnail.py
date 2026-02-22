"""
Tests for generate_pcb_thumbnail() / _generate_thumbnail_with_cli()

kicad-cli is mocked so tests run without KiCad installed.
"""

import asyncio
import base64
import os
import subprocess

import pytest
from mcp.types import ImageContent

from tests.conftest import get_tool_fn


# Minimal SVG that kicad-cli would produce
FAKE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"></svg>'


@pytest.fixture
def project_dir(tmp_path):
    """Create a minimal KiCad project layout on disk."""
    pro = tmp_path / "test.kicad_pro"
    pro.write_text('{"meta": {"version": 1}}')
    pcb = tmp_path / "test.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20240108) (generator "test")\n'
        "  (general (thickness 1.6))\n"
        ")\n"
    )
    return tmp_path


@pytest.fixture
def project_path(project_dir):
    return str(project_dir / "test.kicad_pro")


def _mock_run_ok(svg_bytes, project_dir):
    """Return a subprocess.run mock that writes a fake SVG output file."""
    def _run(cmd, **kwargs):
        # Find the --output argument and write the SVG there
        out_idx = cmd.index("--output") + 1
        out_path = cmd[out_idx]
        with open(out_path, "wb") as f:
            f.write(svg_bytes)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _run


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

class TestGeneratePcbThumbnailSuccess:
    def test_returns_image_content(self, mcp_server, project_path, monkeypatch):
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_ok(FAKE_SVG, os.path.dirname(project_path)),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.shutil.which", lambda _: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr("kicad_mcp.tools.export.system", "Linux")

        result = asyncio.run(fn(project_path=project_path, ctx=None))
        assert isinstance(result, ImageContent)

    def test_mime_type_is_svg(self, mcp_server, project_path, monkeypatch):
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_ok(FAKE_SVG, os.path.dirname(project_path)),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.shutil.which", lambda _: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr("kicad_mcp.tools.export.system", "Linux")

        result = asyncio.run(fn(project_path=project_path, ctx=None))
        assert result.mimeType == "image/svg+xml"

    def test_image_data_is_valid_base64_svg(self, mcp_server, project_path, monkeypatch):
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_ok(FAKE_SVG, os.path.dirname(project_path)),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.shutil.which", lambda _: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr("kicad_mcp.tools.export.system", "Linux")

        result = asyncio.run(fn(project_path=project_path, ctx=None))
        decoded = base64.b64decode(result.data)
        assert decoded == FAKE_SVG

    def test_generate_project_thumbnail_alias(self, mcp_server, project_path, monkeypatch):
        """generate_project_thumbnail should produce identical output."""
        monkeypatch.setattr(
            "kicad_mcp.tools.export.subprocess.run",
            _mock_run_ok(FAKE_SVG, os.path.dirname(project_path)),
        )
        monkeypatch.setattr(
            "kicad_mcp.tools.export.shutil.which", lambda _: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr("kicad_mcp.tools.export.system", "Linux")

        fn1 = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        fn2 = get_tool_fn(mcp_server, "generate_project_thumbnail")

        r1 = asyncio.run(fn1(project_path=project_path, ctx=None))
        r2 = asyncio.run(fn2(project_path=project_path, ctx=None))

        assert isinstance(r1, ImageContent)
        assert isinstance(r2, ImageContent)
        assert r1.data == r2.data


# ---------------------------------------------------------------------------
# Error paths — tool returns a dict with "error", not ImageContent
# ---------------------------------------------------------------------------

class TestGeneratePcbThumbnailErrors:
    def test_missing_project_file(self, mcp_server, tmp_path):
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        result = asyncio.run(
            fn(project_path=str(tmp_path / "nonexistent.kicad_pro"), ctx=None)
        )
        assert isinstance(result, dict)
        assert "error" in result

    def test_missing_pcb_file(self, mcp_server, tmp_path):
        """Project file exists but has no .kicad_pcb companion."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text('{"meta": {"version": 1}}')
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        result = asyncio.run(fn(project_path=str(pro), ctx=None))
        assert isinstance(result, dict)
        assert "error" in result

    def test_kicad_cli_not_found(self, mcp_server, project_path, monkeypatch):
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        monkeypatch.setattr("kicad_mcp.tools.export.shutil.which", lambda _: None)
        monkeypatch.setattr("kicad_mcp.tools.export.system", "Linux")

        result = asyncio.run(fn(project_path=project_path, ctx=None))
        assert isinstance(result, dict)
        assert "error" in result

    def test_kicad_cli_fails(self, mcp_server, project_path, monkeypatch):
        fn = get_tool_fn(mcp_server, "generate_pcb_thumbnail")
        monkeypatch.setattr(
            "kicad_mcp.tools.export.shutil.which", lambda _: "/usr/bin/kicad-cli"
        )
        monkeypatch.setattr("kicad_mcp.tools.export.system", "Linux")

        def _fail(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="render failed")

        monkeypatch.setattr("kicad_mcp.tools.export.subprocess.run", _fail)

        result = asyncio.run(fn(project_path=project_path, ctx=None))
        assert isinstance(result, dict)
        assert "error" in result
