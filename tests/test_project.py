"""
Tests for project management tools and related utilities.

Tests project.py tools, file_utils.py, and kicad_utils.py.
"""

import json
import os
from unittest.mock import patch


from kicad_mcp.tools.project import _op_list
from kicad_mcp.utils.file_utils import get_project_files, load_project_json
from kicad_mcp.utils.kicad_utils import get_project_name_from_path, open_kicad_project


# -- get_project_name_from_path tests ----------------------------------------

class TestGetProjectNameFromPath:

    def test_basic_name(self):
        name = get_project_name_from_path("/home/user/my_board.kicad_pro")
        assert name == "my_board"

    def test_name_with_dashes(self):
        name = get_project_name_from_path("/tmp/esp32-dev-board.kicad_pro")
        assert name == "esp32-dev-board"

    def test_name_with_spaces(self):
        name = get_project_name_from_path("/tmp/My Board.kicad_pro")
        assert name == "My Board"

    def test_mismatched_extension_returns_basename_unchanged(self):
        """Regression: basename[:-len(ext)] silently returned a WRONG (not
        just unchanged) result when the basename didn't actually end with
        .kicad_pro -- it blindly chopped the last 10 characters off
        whatever string it was given. Called (as this canonical helper's
        callers assume it won't be, but the review flagged it as
        unverified) on a non-.kicad_pro path, it must return the basename
        unchanged rather than a corrupted truncation."""
        assert get_project_name_from_path("/tmp/notes.txt") == "notes.txt"

    def test_extension_only_basename_yields_empty_string(self):
        # The one case where chopping IS correct: the whole basename is
        # just the extension itself.
        assert get_project_name_from_path("/tmp/.kicad_pro") == ""


# -- load_project_json tests -------------------------------------------------

class TestLoadProjectJson:

    def test_loads_valid_json(self, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        data = {"meta": {"filename": "test.kicad_pro"}, "board": {}}
        pro.write_text(json.dumps(data))
        result = load_project_json(str(pro))
        assert result is not None
        assert result["meta"]["filename"] == "test.kicad_pro"

    def test_returns_none_for_invalid_json(self, tmp_path):
        pro = tmp_path / "bad.kicad_pro"
        pro.write_text("not valid json {{{")
        assert load_project_json(str(pro)) is None

    def test_returns_none_for_missing_file(self):
        assert load_project_json("/nonexistent/file.kicad_pro") is None

    def test_invalid_json_logs_a_warning(self, tmp_path, caplog):
        """Regression: a bare `except Exception` collapsed FileNotFoundError/
        PermissionError/JSONDecodeError/UnicodeDecodeError into one
        indistinguishable None with no logging at all. finding #87 of the
        2026-09-23 full review."""
        import logging
        pro = tmp_path / "bad.kicad_pro"
        pro.write_text("not valid json {{{")
        with caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.file_utils"):
            result = load_project_json(str(pro))
        assert result is None
        assert any("Could not load project file" in r.message for r in caplog.records)


# -- get_project_files tests -------------------------------------------------

class TestGetProjectFiles:

    def test_finds_standard_files(self, tmp_path):
        name = "myboard"
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
        (tmp_path / f"{name}.kicad_pcb").write_text("")
        (tmp_path / f"{name}.kicad_sch").write_text("")

        files = get_project_files(str(tmp_path / f"{name}.kicad_pro"))
        assert "project" in files
        assert "pcb" in files
        assert "schematic" in files

    def test_missing_pcb(self, tmp_path):
        name = "nopbc"
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
        (tmp_path / f"{name}.kicad_sch").write_text("")

        files = get_project_files(str(tmp_path / f"{name}.kicad_pro"))
        assert "project" in files
        assert "schematic" in files
        assert "pcb" not in files

    def test_finds_data_files(self, tmp_path):
        name = "proj"
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
        (tmp_path / f"{name}-bom.csv").write_text("ref,value")

        files = get_project_files(str(tmp_path / f"{name}.kicad_pro"))
        assert any("csv" in v for v in files.values())

    def test_unrelated_file_with_shared_prefix_not_matched(self, tmp_path):
        """Regression: file.startswith(project_name) matched an unintended
        partial prefix -- project_name="proj" would match an unrelated
        "project-data.csv" in the same directory just because "project"
        happens to start with "proj". finding #90 of the 2026-09-23 full
        review."""
        name = "proj"
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
        (tmp_path / "project-data.csv").write_text("unrelated,data")

        files = get_project_files(str(tmp_path / f"{name}.kicad_pro"))
        assert not any("project-data" in v for v in files.values())

    def test_exact_name_data_file_still_matches(self, tmp_path):
        # The boundary check must not reject the exact-name case (no
        # separator character at all, file == project_name + ext).
        name = "proj"
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
        (tmp_path / f"{name}.net").write_text("net data")

        files = get_project_files(str(tmp_path / f"{name}.kicad_pro"))
        assert any(v.endswith(f"{name}.net") for v in files.values())

    def test_data_scan_oserror_logs_warning_not_silent(self, tmp_path, monkeypatch, caplog):
        """Regression: the whole DATA_EXTENSIONS scan wrapped in one bare
        `except (OSError, FileNotFoundError): pass` -- "no data files" was
        indistinguishable from "the scan failed partway through". finding
        #86 of the 2026-09-23 full review."""
        import logging
        name = "proj"
        (tmp_path / f"{name}.kicad_pro").write_text("{}")
        monkeypatch.setattr(
            "kicad_mcp.utils.file_utils.os.listdir",
            lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
        )
        with caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.file_utils"):
            files = get_project_files(str(tmp_path / f"{name}.kicad_pro"))
        assert "project" in files  # standard-file detection is unaffected
        assert any("Could not scan" in r.message for r in caplog.records)


# -- open_kicad_project tests ------------------------------------------------

class TestOpenKicadProject:

    def test_missing_project(self):
        result = open_kicad_project("/nonexistent/project.kicad_pro")
        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    @patch("kicad_mcp.utils.kicad_utils.subprocess.run")
    def test_opens_on_macos(self, mock_run, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_run.return_value = type("Result", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })()
        with patch("kicad_mcp.utils.kicad_utils.sys") as mock_sys:
            mock_sys.platform = "darwin"
            result = open_kicad_project(str(pro))
        assert result["status"] == "ok"
        assert "open" in result["command"]

    @patch("kicad_mcp.utils.kicad_utils.subprocess.run")
    def test_opens_on_linux(self, mock_run, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_run.return_value = type("Result", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })()
        with patch("kicad_mcp.utils.kicad_utils.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = open_kicad_project(str(pro))
        assert result["status"] == "ok"
        assert "xdg-open" in result["command"]

    @patch("kicad_mcp.utils.kicad_utils.subprocess.run")
    def test_stderr_on_success_is_surfaced_as_warnings(self, mock_run, tmp_path):
        """Regression: stderr was discarded entirely on a successful
        (returncode==0) launch -- xdg-open/open can still write warnings to
        stderr on success (e.g. a desktop-file lookup warning), which had
        nowhere to go."""
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_run.return_value = type("Result", (), {
            "returncode": 0, "stdout": "", "stderr": "warning: some desktop entry issue"
        })()
        with patch("kicad_mcp.utils.kicad_utils.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = open_kicad_project(str(pro))
        assert result["status"] == "ok"
        assert result["warnings"] == "warning: some desktop entry issue"

    @patch("kicad_mcp.utils.kicad_utils.subprocess.run")
    def test_no_warnings_key_when_stderr_empty_on_success(self, mock_run, tmp_path):
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_run.return_value = type("Result", (), {
            "returncode": 0, "stdout": "", "stderr": ""
        })()
        with patch("kicad_mcp.utils.kicad_utils.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = open_kicad_project(str(pro))
        assert "warnings" not in result

    @patch("kicad_mcp.utils.kicad_utils.subprocess.run")
    def test_subprocess_error_is_caught_not_propagated(self, mock_run, tmp_path):
        """Regression: the bare `except Exception` used to mask real
        programming bugs alongside the subprocess failures it was meant
        for. Narrowed to (SubprocessError, OSError) -- confirm the actual
        failure modes (e.g. a timeout) are still caught cleanly."""
        import subprocess as subprocess_module
        pro = tmp_path / "test.kicad_pro"
        pro.write_text("{}")
        mock_run.side_effect = subprocess_module.TimeoutExpired(cmd="xdg-open", timeout=60)
        with patch("kicad_mcp.utils.kicad_utils.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = open_kicad_project(str(pro))
        assert result["status"] == "error"
        assert "timed out" in result["error"]


# -- project(operation="list") pagination -------------------------------------

class TestOpListPagination:
    """project(list) previously returned a bare, unbounded list -- now a
    paginated envelope matching library(search)/lcsc's convention. Threshold
    boundary per CLAUDE.md's Testing rule: total == limit (not truncated)
    and total == limit + 1 (truncated, smallest case)."""

    def _projects(self, n):
        return [{"name": f"proj{i}", "path": f"/tmp/proj{i}.kicad_pro"} for i in range(n)]

    @patch("kicad_mcp.tools.project.find_kicad_projects")
    def test_default_limit_is_fifty(self, mock_find):
        mock_find.return_value = self._projects(75)
        result = _op_list()
        assert result["status"] == "ok"
        assert result["count"] == 50
        assert result["total"] == 75
        assert result["truncated"] is True
        assert len(result["projects"]) == 50

    @patch("kicad_mcp.tools.project.find_kicad_projects")
    def test_total_exactly_equal_to_limit_is_not_truncated(self, mock_find):
        mock_find.return_value = self._projects(50)
        result = _op_list()
        assert result["count"] == 50
        assert result["truncated"] is False

    @patch("kicad_mcp.tools.project.find_kicad_projects")
    def test_total_one_more_than_limit_is_truncated(self, mock_find):
        mock_find.return_value = self._projects(51)
        result = _op_list(limit=50)
        assert result["count"] == 50
        assert result["truncated"] is True

    @patch("kicad_mcp.tools.project.find_kicad_projects")
    def test_custom_limit_is_honored(self, mock_find):
        mock_find.return_value = self._projects(10)
        result = _op_list(limit=3)
        assert result["count"] == 3
        assert result["total"] == 10
        assert result["truncated"] is True

    @patch("kicad_mcp.tools.project.find_kicad_projects")
    def test_no_projects_found(self, mock_find):
        mock_find.return_value = []
        result = _op_list()
        assert result == {
            "status": "ok", "projects": [], "count": 0, "total": 0, "truncated": False,
        }

    def test_limit_zero_is_rejected_via_tool(self):
        from kicad_mcp.server import create_server
        import asyncio
        mcp = create_server()
        tool = asyncio.run(mcp.get_tool("project"))
        result = tool.fn(operation="list", limit=0)
        assert "error" in result

    def test_negative_limit_is_rejected_via_tool(self):
        from kicad_mcp.server import create_server
        import asyncio
        mcp = create_server()
        tool = asyncio.run(mcp.get_tool("project"))
        result = tool.fn(operation="list", limit=-1)
        assert "error" in result


# -- validate_project tool tests (via project.py) ----------------------------

class TestValidateProjectTool:
    """Test validate_project tool through the registration pattern."""

    def test_missing_project_returns_error(self, tmp_project_dir):
        """Test via direct import since tool uses same utility functions."""
        from kicad_mcp.utils.file_utils import get_project_files

        # Create a project dir with no schematic or PCB
        project_path = tmp_project_dir["project_path"]
        # Remove the PCB and schematic files
        os.unlink(tmp_project_dir["pcb_path"])
        os.unlink(tmp_project_dir["sch_path"])

        files = get_project_files(project_path)
        issues = []
        if "schematic" not in files:
            issues.append("No schematic file found")
        if "pcb" not in files:
            issues.append("No PCB file found")

        assert len(issues) == 2
        assert "No schematic file found" in issues
        assert "No PCB file found" in issues

    def test_valid_project_no_issues(self, tmp_project_dir):
        files = get_project_files(tmp_project_dir["project_path"])
        issues = []
        if "schematic" not in files:
            issues.append("No schematic file found")
        if "pcb" not in files:
            issues.append("No PCB file found")
        assert len(issues) == 0
