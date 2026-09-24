"""
Utilities for tracking DRC history for KiCad projects.

This will allow users to compare DRC results over time.
"""
import hashlib
import json
import logging
import os
import platform
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# No schema_version marker existed anywhere in the persisted history JSON --
# format drift (a future field rename/removal) would be undetectable from
# the file alone. Bump this if the persisted shape of a history entry or the
# top-level history dict ever changes incompatibly.
SCHEMA_VERSION = 1

MAX_HISTORY_ENTRIES = 10

# Directory for storing DRC history
if platform.system() == "Windows":
    DRC_HISTORY_DIR = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        "kicad_mcp",
        "drc_history",
    )
else:
    DRC_HISTORY_DIR = os.path.expanduser("~/.kicad_mcp/drc_history")


def ensure_history_dir() -> None:
    """Ensure the DRC history directory exists."""
    os.makedirs(DRC_HISTORY_DIR, exist_ok=True)


def get_project_history_path(project_path: str) -> str:
    """Get the path to the DRC history file for a project.

    Args:
        project_path: Path to the KiCad project file

    Returns:
        Path to the project's DRC history file
    """
    # Stable across processes: built-in hash() is per-process randomized for
    # strings (PYTHONHASHSEED), so it gave a different filename every run and
    # lost all history on restart. sha1 of the path is deterministic.
    project_hash = hashlib.sha1(project_path.encode("utf-8")).hexdigest()[:8]
    basename = os.path.basename(project_path)
    history_filename = f"{basename}_{project_hash}_drc_history.json"

    return os.path.join(DRC_HISTORY_DIR, history_filename)


def save_drc_result(project_path: str, drc_result: Dict[str, Any]) -> None:
    """Save a DRC result to the project's history.

    Args:
        project_path: Path to the KiCad project file
        drc_result: DRC result dictionary
    """
    ensure_history_dir()
    history_path = get_project_history_path(project_path)

    timestamp = time.time()
    formatted_time = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    history_entry = {
        "timestamp": timestamp,
        "datetime": formatted_time,
        "status": drc_result.get("status"),
        "method": drc_result.get("method"),
        "pcb_file": drc_result.get("pcb_file"),
        "total_violations": drc_result.get("total_violations", 0),
        "violation_categories": drc_result.get("violation_categories", {}),
        "raw_violations": drc_result.get("violations", []),
        "unconnected_items": drc_result.get("unconnected_items", []),
        "unconnected_count": drc_result.get("unconnected_count", 0),
        "schematic_parity": drc_result.get("schematic_parity", []),
        "parity_count": drc_result.get("parity_count", 0),
    }

    if os.path.exists(history_path):
        try:
            with open(history_path, "r") as f:
                history = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(
                "DRC history file %s is corrupted (%s) — prior history for "
                "this project is being discarded and a fresh history started",
                history_path, e,
            )
            history = {"project_path": project_path, "entries": []}
    else:
        history = {"project_path": project_path, "entries": []}

    history["schema_version"] = SCHEMA_VERSION
    history["entries"].append(history_entry)

    # Keep only the last MAX_HISTORY_ENTRIES entries. A hardcoded cap with no
    # `truncated` flag meant a caller reading only the returned entries list
    # (get_drc_history) had no way to tell "this project has 3 DRC runs ever"
    # from "this project has 50 DRC runs, you're seeing the newest 10" --
    # persisted here so it round-trips through the file, not just computed
    # transiently at read time.
    history["entries_truncated"] = len(history["entries"]) > MAX_HISTORY_ENTRIES
    if history["entries_truncated"]:
        history["entries"] = sorted(
            history["entries"],
            key=lambda x: x["timestamp"],
            reverse=True,
        )[:MAX_HISTORY_ENTRIES]

    try:
        with open(history_path, "w") as f:
            json.dump(history, f, indent=2)
        logger.debug("Saved DRC history entry to %s", history_path)
    except IOError as e:
        logger.warning("Error saving DRC history to %s: %s", history_path, e)


def get_drc_history(project_path: str) -> List[Dict[str, Any]]:
    """Get the DRC history for a project.

    Args:
        project_path: Path to the KiCad project file

    Returns:
        List of DRC history entries, sorted by timestamp (newest first)
    """
    entries: List[Dict[str, Any]] = get_drc_history_info(project_path)["entries"]
    return entries


def get_drc_history_info(project_path: str) -> Dict[str, Any]:
    """Like :func:`get_drc_history` but also surfaces the truncation flag
    save_drc_result persists -- a caller that only sees the (capped-at-10)
    entries list has no way to tell "this project has 3 DRC runs ever" from
    "this project has 50, you're seeing the newest 10". Kept as a separate
    function (rather than changing get_drc_history's return shape) so the
    existing plain-list contract for that function is undisturbed.

    Returns:
        ``{"entries": [...], "truncated": bool, "schema_version": int | None}``
    """
    history_path = get_project_history_path(project_path)

    if not os.path.exists(history_path):
        logger.debug("No DRC history found for %s", project_path)
        return {"entries": [], "truncated": False, "schema_version": None}

    try:
        with open(history_path, "r") as f:
            history = json.load(f)

        entries = sorted(
            history.get("entries", []),
            key=lambda x: x.get("timestamp", 0),
            reverse=True,
        )

        schema_version = history.get("schema_version")
        if schema_version is not None and schema_version != SCHEMA_VERSION:
            # Not fatal -- older history files (schema_version absent
            # entirely, i.e. None) predate this field and are still read
            # normally -- but a version that IS present and doesn't match
            # is worth knowing about if a future format change ever needs
            # to distinguish "old file, never migrated" from "corrupt".
            logger.info(
                "DRC history file %s has schema_version=%r (current is %d)",
                history_path, schema_version, SCHEMA_VERSION,
            )

        return {
            "entries": entries,
            "truncated": bool(history.get("entries_truncated", False)),
            "schema_version": schema_version,
        }
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("DRC history file %s is corrupted or unreadable (%s) — "
                        "treating as no history", history_path, e)
        return {"entries": [], "truncated": False, "schema_version": None}


def compare_with_previous(
    project_path: str, current_result: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Compare current DRC result with the previous one.

    Args:
        project_path: Path to the KiCad project file
        current_result: Current DRC result dictionary

    Returns:
        Comparison dictionary or None if no history exists
    """
    history = get_drc_history(project_path)

    # Caller must pass history that does NOT already include current_result
    # (i.e. call this before save_drc_result) -- one prior entry is enough
    # for a real comparison.
    if not history:
        return None

    previous = history[0]
    current_violations = current_result.get("total_violations", 0)
    previous_violations = previous.get("total_violations", 0)

    current_categories = current_result.get("violation_categories", {})
    previous_categories = previous.get("violation_categories", {})

    new_categories = {}
    for category, count in current_categories.items():
        if category not in previous_categories:
            new_categories[category] = count

    resolved_categories = {}
    for category, count in previous_categories.items():
        if category not in current_categories:
            resolved_categories[category] = count

    changed_categories = {}
    for category, count in current_categories.items():
        if category in previous_categories and count != previous_categories[category]:
            changed_categories[category] = {
                "current": count,
                "previous": previous_categories[category],
                "change": count - previous_categories[category],
            }

    comparison = {
        "current_violations": current_violations,
        "previous_violations": previous_violations,
        "change": current_violations - previous_violations,
        "previous_datetime": previous.get("datetime", "unknown"),
        "new_categories": new_categories,
        "resolved_categories": resolved_categories,
        "changed_categories": changed_categories,
    }

    return comparison
