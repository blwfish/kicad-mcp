"""Project router — KiCad project file management.

See docs/SPEC_Tool_Consolidation.md.
"""
import logging
import os
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.file_utils import get_project_files, load_project_json
from kicad_mcp.utils.kicad_utils import find_kicad_projects, open_kicad_project
from kicad_mcp.utils.path_validation import validate_project_path

logger = logging.getLogger(__name__)


def _op_list(limit: int = 50) -> Dict[str, Any]:
    logger.info("Executing project list...")
    projects = find_kicad_projects()
    total = len(projects)
    page = projects[:limit]
    logger.info("project list returning %d of %d projects.", len(page), total)
    return {
        "status": "ok",
        "projects": page,
        "count": len(page),
        "total": total,
        "truncated": total > limit,
    }


def _op_get_structure(project_path: str) -> Dict[str, Any]:
    err = validate_project_path(project_path)
    if err:
        return {"error": err}

    project_dir = os.path.dirname(project_path)
    project_name = os.path.basename(project_path)[:-10]  # Remove .kicad_pro

    files = get_project_files(project_path)

    metadata = {}
    project_data = load_project_json(project_path)
    if project_data and "metadata" in project_data:
        metadata = project_data["metadata"]

    return {
        "name": project_name,
        "path": project_path,
        "directory": project_dir,
        "files": files,
        "metadata": metadata,
    }


def _op_open(project_path: str) -> Dict[str, Any]:
    err = validate_project_path(project_path)
    if err:
        return {"error": err}
    return open_kicad_project(project_path)


def _op_validate(project_path: str) -> Dict[str, Any]:
    err = validate_project_path(project_path)
    if err:
        return {"success": False, "error": err}

    files = get_project_files(project_path)
    issues: list[str] = []

    if "schematic" not in files:
        issues.append("No schematic file found")
    if "pcb" not in files:
        issues.append("No PCB file found")

    return {
        "success": len(issues) == 0,
        "project_path": project_path,
        "files_found": list(files.keys()),
        "issues": issues,
    }


def register_project_tools(mcp: FastMCP) -> None:
    """Register the project domain router."""

    @mcp.tool(
        annotations={
            # `open` launches KiCad's GUI as a subprocess -- a real side
            # effect, but writes no file and isn't confirmed idempotent
            # (repeated calls may focus an existing window or spawn a new
            # one; not verified either way).
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def project(
        operation: str,
        *,
        project_path: Optional[str] = None,
        limit: int = 50,
    ) -> Any:
        """KiCad project management operations.

        Operations:
          list(limit=50)
              -> {status, projects: [{name, path, ...}, ...], count, total,
                  truncated}
              Find and list all KiCad projects on this system.
              BREAKING CHANGE (was a bare list): now returns a paginated
              envelope, matching library(search)/lcsc's convention --
              `total` is always the true count, `projects` is capped at
              `limit`, `truncated` says whether more exist.

          get_structure(project_path)
              -> {name, path, directory, files, metadata}
              Get the structure and files of a KiCad project.

          open(project_path)
              -> {success, command, ...}
              Open a KiCad project in KiCad.

          validate(project_path)
              -> {success, project_path, files_found, issues}
              Basic validation of a KiCad project — checks that schematic
              and PCB files are present.
        """
        if operation == "list":
            if limit <= 0:
                return {"error": f"limit must be > 0, got {limit}"}
            return _op_list(limit=limit)
        if operation == "get_structure":
            if project_path is None:
                return {"error": "operation='get_structure' requires 'project_path'"}
            return _op_get_structure(project_path)
        if operation == "open":
            if project_path is None:
                return {"error": "operation='open' requires 'project_path'"}
            return _op_open(project_path)
        if operation == "validate":
            if project_path is None:
                return {"error": "operation='validate' requires 'project_path'"}
            return _op_validate(project_path)
        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: list|get_structure|open|validate"
            )
        }
