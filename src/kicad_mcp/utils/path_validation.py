"""Path-traversal defense for tool entry points.

Tools accept paths from the caller (an LLM, an agent, a network client) and
hand them to ``open()``, ``subprocess.run``, ``shutil.copy*``. Without
validation, ``../../../etc/passwd`` and similar inputs can escape the
intended workspace.

This module provides one canonical validator. The default policy is
permissive (warn-only) so legitimate workflows aren't broken; strict mode
is opt-in via ``KICAD_MCP_STRICT_PATHS=1``.

A path is *allowed* under strict mode when its real path (symlinks
resolved, ``..`` collapsed) is contained in one of:

  - any of ``ADDITIONAL_SEARCH_PATHS`` (configured project roots)
  - ``KICAD_USER_DIR`` (user's KiCad projects directory)
  - ``tempfile.gettempdir()`` (so tests and scratch work keep functioning)
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)


def _strict_mode() -> bool:
    return os.environ.get("KICAD_MCP_STRICT_PATHS", "").lower() in {"1", "true", "yes"}


def _allowed_roots() -> list[str]:
    from kicad_mcp.config import ADDITIONAL_SEARCH_PATHS, KICAD_USER_DIR

    roots = [os.path.realpath(KICAD_USER_DIR), os.path.realpath(tempfile.gettempdir())]
    roots.extend(os.path.realpath(p) for p in ADDITIONAL_SEARCH_PATHS)
    return [r for r in roots if r]


def validate_project_path(path: str, must_exist: bool = True) -> Optional[str]:
    """Validate a caller-supplied path.

    Returns ``None`` if the path is acceptable. Returns an error string
    suitable for direct return as a tool response otherwise.

    The check normalizes ``..`` and resolves symlinks via ``os.path.realpath``.
    In strict mode (``KICAD_MCP_STRICT_PATHS=1``), the resolved path must
    sit under one of the configured roots; otherwise an out-of-root path
    only logs a warning.
    """
    if not isinstance(path, str) or not path:
        return "Invalid path: must be a non-empty string"

    real = os.path.realpath(os.path.expanduser(path))

    if must_exist and not os.path.exists(real):
        return f"Path not found: {path}"

    roots = _allowed_roots()
    in_root = any(real == r or real.startswith(r + os.sep) for r in roots)

    if not in_root:
        if _strict_mode():
            return (
                f"Path {path!r} resolves to {real!r}, which is outside the "
                "configured KICAD_SEARCH_PATHS. Set KICAD_MCP_STRICT_PATHS=0 "
                "to allow, or add the parent directory to KICAD_SEARCH_PATHS."
            )
        logger.warning(
            "path %r resolves outside configured roots (%s); allowing "
            "(set KICAD_MCP_STRICT_PATHS=1 to reject)",
            path, real,
        )

    return None
