"""PlacementState — the structured object that ``schematic_layout`` returns.

The schema is documented in ``docs/SPEC_Schematic_Placement.md`` under
"PlacementState schema". This module is dict-shaped (rather than nested
dataclasses) to make verbosity-mode trimming and JSON round-tripping
trivial; the public construction surface is the helpers below.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ALGORITHM_VERSION = "0.1.0-slice1"


# ---------------------------------------------------------------------------
# Public construction
# ---------------------------------------------------------------------------

def new_state(
    *,
    schematic_path: str,
    schematic_hash: str,
) -> dict[str, Any]:
    """Return an empty PlacementState skeleton.

    Layers / phases populate the ``components``, ``clusters``, ``tiers``
    dicts in place as they run.
    """
    return {
        "schematic_path": schematic_path,
        "algorithm_version": ALGORITHM_VERSION,
        "computed_at": _now_iso(),
        "schematic_hash": schematic_hash,
        "components": {},
        "clusters": {},
        "tiers": {},
        "events": [],
        "inputs_honored": {
            "hints_applied": [],
            "fixed_positions": [],
            "redo_scope": None,
        },
        "state_id": _new_state_id(),
    }


def schematic_hash(path: str | Path) -> str:
    """SHA-256 of the schematic file contents. Used for drift detection."""
    p = Path(path)
    if not p.exists():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Verbosity trimming
# ---------------------------------------------------------------------------

# Fields kept in "minimal" mode. Per spec: coordinates + cluster_id + anchor
# + label + label_confidence. Everything else collapses or is dropped.
_MINIMAL_COMPONENT_FIELDS = {"x_mm", "y_mm", "rotation", "mirror_x", "cluster_id", "fixed_by"}
_MINIMAL_CLUSTER_FIELDS = {"members", "anchor", "label", "label_confidence", "tier"}

# Default values that get omitted under sparse encoding (both modes).
_COMPONENT_DEFAULTS: dict[str, Any] = {
    "rotation": 0,
    "mirror_x": False,
    "fixed_by": None,
}


def trim_for_verbosity(state: dict[str, Any], verbosity: str) -> dict[str, Any]:
    """Return a copy of ``state`` adjusted for the requested verbosity.

    ``minimal``: keep only the spec-defined minimal fields per component
    and cluster; drop ``label_source``, ``bbox_mm``, ``louvain_modularity``,
    etc. Sparse-encode default values.

    ``full``: keep everything; still sparse-encode default values.
    """
    if verbosity not in ("minimal", "full"):
        raise ValueError(f"verbosity must be 'minimal' or 'full', got {verbosity!r}")

    out = {k: v for k, v in state.items() if k not in ("components", "clusters")}

    components = {}
    for ref, comp in state.get("components", {}).items():
        if verbosity == "minimal":
            picked = {k: v for k, v in comp.items() if k in _MINIMAL_COMPONENT_FIELDS}
        else:
            picked = dict(comp)
        components[ref] = _sparse_encode(picked, _COMPONENT_DEFAULTS)
    out["components"] = components

    clusters = {}
    for cid, cluster in state.get("clusters", {}).items():
        if verbosity == "minimal":
            picked = {k: v for k, v in cluster.items() if k in _MINIMAL_CLUSTER_FIELDS}
        else:
            picked = dict(cluster)
        clusters[cid] = picked
    out["clusters"] = clusters

    return out


def _sparse_encode(d: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose values equal the listed defaults."""
    return {k: v for k, v in d.items() if not (k in defaults and v == defaults[k])}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_state_id() -> str:
    """16 hex chars of entropy — short enough for prompts, plenty for cache keys."""
    return secrets.token_hex(8)
