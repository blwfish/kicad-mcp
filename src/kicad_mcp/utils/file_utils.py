"""
File handling utilities for KiCad MCP Server.
"""

import json
import logging
import os
from typing import Any

from kicad_mcp.utils.kicad_utils import get_project_name_from_path

logger = logging.getLogger(__name__)


def get_project_files(project_path: str) -> dict[str, str]:
    """Get all files related to a KiCad project.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Dictionary mapping file types to file paths
    """
    from kicad_mcp.config import DATA_EXTENSIONS, KICAD_EXTENSIONS

    project_dir = os.path.dirname(project_path)
    project_name = get_project_name_from_path(project_path)

    files = {}

    # Check for standard KiCad files
    for file_type, extension in KICAD_EXTENSIONS.items():
        if file_type == "project":
            files[file_type] = project_path
            continue

        file_path = os.path.join(project_dir, f"{project_name}{extension}")
        if os.path.exists(file_path):
            files[file_type] = file_path

    # Check for data files
    try:
        for ext in DATA_EXTENSIONS:
            for file in os.listdir(project_dir):
                # A bare startswith() matches an unintended partial prefix:
                # project_name="proj" would match an unrelated
                # "project-data.csv" in the same directory just because
                # "project" happens to start with "proj". Require the
                # character right after project_name to be a real separator
                # (or nothing, an exact-name match) -- the same boundary
                # the KICAD_EXTENSIONS loop above already gets for free by
                # constructing f"{project_name}{extension}" directly.
                next_char = file[len(project_name):len(project_name) + 1]
                name_boundary_ok = file.startswith(project_name) and next_char in ("", "-", "_", ".")
                if name_boundary_ok and file.endswith(ext):
                    file_type = file[len(project_name) :].strip("-_")
                    file_type = file_type.split(".")[0]
                    if not file_type:
                        file_type = ext[1:]

                    files[file_type] = os.path.join(project_dir, file)
    except OSError as e:
        # Was a bare `pass` with no signal at all -- "directory has no data
        # files" was indistinguishable from "the listdir/scan failed
        # partway through" (permission denied, a stale network mount).
        logger.warning("Could not scan %s for data files: %s", project_dir, e)

    return files


def load_project_json(project_path: str) -> dict[str, Any] | None:
    """Load and parse a KiCad project file.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Parsed JSON data or None if parsing failed
    """
    try:
        with open(project_path) as f:
            data: dict[str, Any] = json.load(f)
            return data
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        # Was a bare `except Exception`, collapsing FileNotFoundError/
        # PermissionError/JSONDecodeError/UnicodeDecodeError into one
        # indistinguishable None with no logging -- narrowed to the specific
        # failure modes open()+json.load() can actually raise (a genuine
        # MemoryError, for instance, should propagate rather than be
        # swallowed as "couldn't load").
        logger.warning("Could not load project file %s: %s", project_path, e)
        return None
