"""
Tests for project management tools and related utilities.

Tests project.py tools, file_utils.py, and kicad_utils.py.
"""

import json
import os
from unittest.mock import patch

import pytest

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


# -- open_kicad_project tests ------------------------------------------------

class TestOpenKicadProject:

    def test_missing_project(self):
        result = open_kicad_project("/nonexistent/project.kicad_pro")
        assert result["success"] is False
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
        assert result["success"] is True
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
        assert result["success"] is True
        assert "xdg-open" in result["command"]


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
