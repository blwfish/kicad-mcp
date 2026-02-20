"""
KiCad-specific utility functions.
"""
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List

from kicad_mcp import config

logger = logging.getLogger(__name__)


def find_kicad_projects() -> List[Dict[str, Any]]:
    """Find KiCad projects in the user's directory.

    Returns:
        List of dictionaries with project information
    """
    projects = []
    logger.info("Attempting to find KiCad projects...")

    raw_search_dirs = [config.KICAD_USER_DIR] + config.ADDITIONAL_SEARCH_PATHS
    logger.info("Raw search list before expansion: %s", raw_search_dirs)

    expanded_search_dirs: list[str] = []
    for raw_dir in raw_search_dirs:
        expanded_dir = os.path.expanduser(raw_dir)
        if expanded_dir not in expanded_search_dirs:
            expanded_search_dirs.append(expanded_dir)
        else:
            logger.info("Skipping duplicate expanded path: %s", expanded_dir)

    logger.info("Expanded search directories: %s", expanded_search_dirs)

    for search_dir in expanded_search_dirs:
        if not os.path.exists(search_dir):
            logger.warning("Expanded search directory does not exist: %s", search_dir)
            continue

        logger.info("Scanning expanded directory: %s", search_dir)
        for root, _, files in os.walk(search_dir, followlinks=True):
            for file in files:
                if file.endswith(config.KICAD_EXTENSIONS["project"]):
                    project_path = os.path.join(root, file)
                    if not os.path.isfile(project_path):
                        logger.info("Skipping non-file/broken symlink: %s", project_path)
                        continue

                    try:
                        mod_time = os.path.getmtime(project_path)
                        rel_path = os.path.relpath(project_path, search_dir)
                        project_name = get_project_name_from_path(project_path)

                        logger.info("Found accessible KiCad project: %s", project_path)
                        projects.append({
                            "name": project_name,
                            "path": project_path,
                            "relative_path": rel_path,
                            "modified": mod_time,
                        })
                    except OSError as e:
                        logger.error("Error accessing project file %s: %s", project_path, e)
                        continue

    logger.info("Found %d KiCad projects after scanning.", len(projects))
    return projects


def get_project_name_from_path(project_path: str) -> str:
    """Extract the project name from a .kicad_pro file path.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Project name without extension
    """
    basename = os.path.basename(project_path)
    return basename[: -len(config.KICAD_EXTENSIONS["project"])]


def open_kicad_project(project_path: str) -> Dict[str, Any]:
    """Open a KiCad project using the KiCad application.

    Args:
        project_path: Path to the .kicad_pro file

    Returns:
        Dictionary with result information
    """
    if not os.path.exists(project_path):
        return {"success": False, "error": f"Project not found: {project_path}"}

    try:
        cmd: list[str] = []
        if sys.platform == "darwin":
            cmd = ["open", "-a", config.KICAD_APP_PATH, project_path]
        elif sys.platform == "linux":
            cmd = ["xdg-open", project_path]
        else:
            return {"success": False, "error": f"Unsupported operating system: {sys.platform}"}

        result = subprocess.run(cmd, capture_output=True, text=True)

        return {
            "success": result.returncode == 0,
            "command": " ".join(cmd),
            "output": result.stdout,
            "error": result.stderr if result.returncode != 0 else None,
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
