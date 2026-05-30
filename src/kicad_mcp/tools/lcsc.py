"""LCSC/JLCPCB component intelligence — search, resolve, and assign parts.

Single router tool ``lcsc`` with five operations:
  search         — keyword+filter search, returns ranked ResolvedPart candidates
  resolve        — exact lookup by LCSC part number
  assign         — apply a resolved part to a schematic component
  accept_tos     — write ToS acceptance record
  refresh_snapshot — re-download the jlcparts snapshot
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from mcp_events import emit_event, event_context

from kicad_mcp.utils import lcsc_db
from kicad_mcp.utils.lcsc_db import (
    DB_PATH,
    META_PATH,
    CACHE_DIR,
    build_db_from_jsonl,
    db_exists,
    get_component,
    get_snapshot_age_days,
    get_snapshot_date,
    resolve_footprint,
    search_components,
    search_components_with_total,
    validate_schema,
    _fetch_live_part,
    _live_part_to_row,
)

try:
    from kicad_mcp.utils.telemetry import record_call, record_warning
    _TELEMETRY_AVAILABLE = True
except Exception:
    _TELEMETRY_AVAILABLE = False

logger = logging.getLogger(__name__)

TOS_FILE = CACHE_DIR / "lcsc_tos_acceptance.json"
TOS_BYPASS_ENV = "KICAD_MCP_LCSC_TOS_ACCEPTED"

TOS_NOTICE = (
    "This will download a ~200 MB component-catalog snapshot from the community "
    "yaqwsx/jlcparts project. The snapshot contains data from LCSC's product catalog. "
    "LCSC's API terms of service prohibit redistribution of bulk catalog data; the "
    "snapshot is downloaded directly from the community source, not from this MCP server. "
    "By proceeding, you accept responsibility for compliance with LCSC's terms. "
    "To accept and continue, call `lcsc(operation='accept_tos')`. "
    "To decline, do not call further LCSC operations."
)
_TOS_HASH = hashlib.sha256(TOS_NOTICE.encode()).hexdigest()

DESCRIPTION_MAX_LEN = 256


# ---------------------------------------------------------------------------
# ResolvedPart dataclass
# ---------------------------------------------------------------------------

@dataclass
class ResolvedPart:
    # Identity
    supplier_name: str
    supplier_part_number: str
    mpn: str
    manufacturer: str
    # Description
    description: str
    package: str
    pin_count: int | None
    # Parametric
    attributes: dict[str, str]
    # Commercial
    price_tiers: list[dict]
    stock: int
    # LCSC/JLCPCB
    assembly_tier: str | None
    # KiCad resolution
    kicad_symbol_lib_id: str | None
    kicad_footprint_path: str | None
    # Documentation
    datasheet_url: str | None
    # Match metadata
    match_score: float | None
    match_deviations: list[str] = field(default_factory=list)
    # Provenance
    snapshot_date: str = ""
    fetched_live: bool = False


def _row_to_resolved(
    row: dict[str, Any],
    *,
    match_score: float | None,
    deviations: list[str],
    snapshot_date: str,
    fetched_live: bool,
    kicad_symbol_lib_id: str | None,
) -> ResolvedPart:
    package = row.get("package") or ""
    description = row.get("description") or ""
    footprint = resolve_footprint(package, description)

    price_raw = row.get("price") or "[]"
    try:
        price_tiers = json.loads(price_raw) if isinstance(price_raw, str) else price_raw
    except Exception:
        price_tiers = []

    joints = row.get("joints")
    pin_count = int(joints) if joints is not None else None

    return ResolvedPart(
        supplier_name="lcsc",
        supplier_part_number=row.get("lcsc") or "",
        mpn=row.get("mfr") or "",
        manufacturer=row.get("manufacturer") or "",
        description=description,
        package=package,
        pin_count=pin_count,
        attributes={},
        price_tiers=price_tiers,
        stock=row.get("stock") or 0,
        assembly_tier=row.get("assembly_tier") or "extended",
        kicad_symbol_lib_id=kicad_symbol_lib_id,
        kicad_footprint_path=footprint,
        datasheet_url=row.get("datasheet") or None,
        match_score=match_score,
        match_deviations=deviations,
        snapshot_date=snapshot_date,
        fetched_live=fetched_live,
    )


def _resolved_to_dict(rp: ResolvedPart) -> dict[str, Any]:
    d = asdict(rp)
    return d


# ---------------------------------------------------------------------------
# Symbol lookup
# ---------------------------------------------------------------------------

def _lookup_kicad_symbol(mpn: str) -> str | None:
    """Attempt to resolve MPN → KiCad symbol lib_id via library index."""
    if not mpn:
        return None
    try:
        from kicad_mcp.utils.library_index import get_library_index
        index = get_library_index()
        results = index.search_symbols(mpn, limit=5)
        if not results:
            return None
        # Exact name match first
        for r in results:
            if r.get("name") == mpn:
                return r.get("lib_id")
        # No exact match → top FTS result only if it's clearly the best
        if len(results) == 1:
            return results[0].get("lib_id")
        # Multiple candidates with no exact match → ambiguous, return None
        return None
    except Exception as e:
        logger.debug("Library symbol lookup failed for %r: %s", mpn, e)
        return None


# ---------------------------------------------------------------------------
# ToS helpers
# ---------------------------------------------------------------------------

def _tos_accepted() -> bool:
    if os.environ.get(TOS_BYPASS_ENV):
        return True
    try:
        data = json.loads(TOS_FILE.read_text())
        return bool(data.get("acceptance_hash") == _TOS_HASH)
    except Exception:
        return False


def _write_tos_acceptance() -> str:
    from datetime import datetime, timezone
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    accepted_at = datetime.now(timezone.utc).isoformat()
    TOS_FILE.write_text(json.dumps({
        "accepted_at": accepted_at,
        "acceptance_hash": _TOS_HASH,
        "snapshot_first_used": get_snapshot_date() or "",
    }, indent=2))
    return accepted_at


# ---------------------------------------------------------------------------
# Freshness check helper
# ---------------------------------------------------------------------------

def _check_freshness() -> None:
    """Emit appropriate freshness events based on snapshot age."""
    age = get_snapshot_age_days()
    if age is None:
        return
    if age >= 31:
        emit_event("warn", "lcsc_snapshot_very_stale",
                   f"jlcparts snapshot is {age} days old. "
                   "Run lcsc(operation='refresh_snapshot') to update.",
                   {"age_days": age})
    elif age >= 15:
        emit_event("warn", "lcsc_snapshot_stale",
                   f"jlcparts snapshot is {age} days old. Consider refreshing.",
                   {"age_days": age})
    elif age >= 8:
        emit_event("info", "lcsc_snapshot_aging",
                   f"jlcparts snapshot is {age} days old.",
                   {"age_days": age})


def _ensure_db_ready() -> None:
    """Ensure DB exists and schema is valid. Raises on hard failures."""
    if not db_exists():
        # Trigger download
        build_db_from_jsonl()
        return
    try:
        validate_schema()
    except RuntimeError:
        # Schema mismatch → rebuild
        logger.warning("jlcparts schema mismatch detected; rebuilding DB")
        build_db_from_jsonl()


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_lcsc_tools(mcp: FastMCP) -> None:
    """Register the lcsc router tool."""

    @mcp.tool()
    def lcsc(
        operation: str,
        description: str | None = None,
        package: str | None = None,
        assembly_tier: str = "basic",
        max_results: int = 3,
        include_unresolvable: bool = False,
        part_number: str | None = None,
        reference: str | None = None,
        schematic_path: str | None = None,
    ) -> dict:
        """Search, resolve, and assign LCSC/JLCPCB components.

        Operations:
          search           — keyword search for components
          resolve          — exact lookup by LCSC part number (e.g., 'C6186')
          assign           — apply a resolved part to a schematic component
          accept_tos       — accept the catalog download terms of service
          refresh_snapshot — re-download the jlcparts component catalog

        For 'search':
          description    : functional description (e.g., '3.3V LDO regulator')
          package        : package filter (e.g., 'SOT-23') — optional
          assembly_tier  : 'basic' | 'extended' | 'preferred' | 'any' (default 'basic')
          max_results    : maximum results to return (default 3)
          include_unresolvable : if True, include parts with no KiCad footprint mapping

        For 'resolve':
          part_number    : LCSC part number (e.g., 'C6186')

        For 'assign':
          reference      : schematic reference (e.g., 'U1')
          part_number    : LCSC part number to assign
          schematic_path : optional path override (defaults to currently-loaded schematic)
        """
        t_start = time.monotonic()

        # --- accept_tos (no ToS or DB check needed) ---
        if operation == "accept_tos":
            return _op_accept_tos()

        # --- ToS gate ---
        if not _tos_accepted():
            return {
                "status": "error",
                "code": "lcsc_tos_acceptance_required",
                "message": TOS_NOTICE,
            }

        # --- refresh_snapshot ---
        if operation == "refresh_snapshot":
            return _op_refresh_snapshot()

        # --- DB availability (for search / resolve / assign) ---
        with event_context() as events:
            try:
                _ensure_db_ready()
            except RuntimeError as e:
                msg = str(e)
                if "lcsc_snapshot_unavailable" in msg:
                    return {"status": "error", "code": "lcsc_snapshot_unavailable", "message": msg}
                return {"status": "error", "code": "db_error", "message": msg}

            _check_freshness()

            snap_date = get_snapshot_date() or ""
            age = get_snapshot_age_days()

            if operation == "search":
                result = _op_search(
                    description=description,
                    package=package,
                    assembly_tier=assembly_tier,
                    max_results=max_results,
                    include_unresolvable=include_unresolvable,
                    snap_date=snap_date,
                    t_start=t_start,
                    age_days=age,
                )
            elif operation == "resolve":
                result = _op_resolve(
                    part_number=part_number,
                    snap_date=snap_date,
                    t_start=t_start,
                    age_days=age,
                )
            elif operation == "assign":
                result = _op_assign(
                    reference=reference,
                    part_number=part_number,
                    schematic_path=schematic_path,
                    snap_date=snap_date,
                    t_start=t_start,
                )
            else:
                return {"status": "error", "code": "unknown_operation",
                        "message": f"Unknown operation: {operation!r}. "
                                   "Valid: search, resolve, assign, accept_tos, refresh_snapshot"}

            if events.has_any("warn") or events.has_any("error"):
                result["events"] = events.to_envelope()
            return result


def _op_accept_tos() -> dict:
    accepted_at = _write_tos_acceptance()
    return {"status": "ok", "accepted_at": accepted_at}


def _op_refresh_snapshot() -> dict:
    prev_date = get_snapshot_date()
    try:
        meta = build_db_from_jsonl()
    except RuntimeError as e:
        msg = str(e)
        if db_exists():
            emit_event("warn", "lcsc_snapshot_offline_fallback",
                       f"Download failed; using cached snapshot. Error: {msg}",
                       {"error": msg})
            return {"status": "error", "code": "refresh_failed",
                    "message": msg, "fallback_available": True}
        return {"status": "error", "code": "lcsc_snapshot_unavailable", "message": msg}
    return {
        "status": "ok",
        "previous_snapshot_date": prev_date,
        "new_snapshot_date": meta.get("snapshot_date"),
        "total_components": meta.get("total_components"),
    }


def _op_search(
    *,
    description: str | None,
    package: str | None,
    assembly_tier: str,
    max_results: int,
    include_unresolvable: bool,
    snap_date: str,
    t_start: float,
    age_days: int | None,
) -> dict:
    if not description:
        return {"status": "error", "code": "missing_parameter",
                "message": "description is required for search"}

    # Cap description length
    if len(description) > DESCRIPTION_MAX_LEN:
        emit_event("warn", "lcsc_description_truncated",
                   f"description truncated from {len(description)} to {DESCRIPTION_MAX_LEN} chars")
        description = description[:DESCRIPTION_MAX_LEN]

    valid_tiers = {"basic", "extended", "preferred", "any"}
    if assembly_tier not in valid_tiers:
        return {"status": "error", "code": "invalid_parameter",
                "message": f"assembly_tier must be one of {sorted(valid_tiers)}"}

    rows, total_candidates = search_components_with_total(
        description=description,
        package=package,
        assembly_tier=assembly_tier,
        max_results=max_results,
        include_unresolvable=include_unresolvable,
    )

    if not rows:
        emit_event("warn", "lcsc_no_results",
                   "No components matched the search criteria.",
                   {"description": description, "package": package, "tier": assembly_tier})
        return {"status": "ok", "results": [], "total_candidates": 0,
                "truncated": False, "events": []}

    top_score = rows[0].get("_score", 0.0) if rows else 0.0
    if top_score < 1.0:
        emit_event("warn", "lcsc_no_exact_match",
                   "No exact match found; returning closest candidates.",
                   {"top_match_score": round(top_score, 3)})

    resolved = []
    for row in rows:
        score = row.pop("_score", None)
        deviations = row.pop("_deviations", [])
        row.pop("_footprint", None)
        kicad_sym = _lookup_kicad_symbol(row.get("mfr") or "")
        rp = _row_to_resolved(
            row,
            match_score=round(score, 4) if score is not None else None,
            deviations=deviations,
            snapshot_date=snap_date,
            fetched_live=False,
            kicad_symbol_lib_id=kicad_sym,
        )
        resolved.append(_resolved_to_dict(rp))

    _record_telemetry("lcsc.search", description, {
        "result_count": len(resolved),
        "top_match_score": round(top_score, 3),
        "snapshot_age_days": age_days,
        "low_score_count": sum(1 for r in resolved if (r.get("match_score") or 0) < 0.5),
    }, int((time.monotonic() - t_start) * 1000))

    truncated = total_candidates > len(resolved)
    if truncated:
        emit_event("info", "lcsc_results_truncated",
                   f"Showing top {len(resolved)} of {total_candidates} matching candidates. "
                   "Raise max_results to see more.",
                   {"returned": len(resolved), "total_candidates": total_candidates})

    return {"status": "ok", "results": resolved,
            "total_candidates": total_candidates, "truncated": truncated}


def _op_resolve(
    *,
    part_number: str | None,
    snap_date: str,
    t_start: float,
    age_days: int | None,
) -> dict:
    if not part_number:
        return {"status": "error", "code": "missing_parameter",
                "message": "part_number is required for resolve"}

    # Normalize: ensure "C" prefix
    pn = part_number if part_number.upper().startswith("C") else f"C{part_number}"

    fetched_live = False
    row = get_component(pn)

    if row is None:
        # Cache miss → try live API
        live_data = _fetch_live_part(pn)
        if live_data:
            row = _live_part_to_row(live_data, pn)
            fetched_live = True

    if row is None:
        return {"status": "error", "code": "part_not_found",
                "message": f"Part {pn!r} not found in snapshot or live catalog."}

    kicad_sym = _lookup_kicad_symbol(row.get("mfr") or "")
    rp = _row_to_resolved(
        row,
        match_score=None,
        deviations=[],
        snapshot_date=snap_date,
        fetched_live=fetched_live,
        kicad_symbol_lib_id=kicad_sym,
    )

    _record_telemetry("lcsc.resolve", pn, {
        "fetched_live": fetched_live,
        "snapshot_age_days": age_days,
        "jlc_extra_present": False,
    }, int((time.monotonic() - t_start) * 1000))

    return {"status": "ok", "part": _resolved_to_dict(rp)}


def _op_assign(
    *,
    reference: str | None,
    part_number: str | None,
    schematic_path: str | None,
    snap_date: str,
    t_start: float,
) -> dict:
    if not reference:
        return {"status": "error", "code": "missing_parameter",
                "message": "reference is required for assign"}
    if not part_number:
        return {"status": "error", "code": "missing_parameter",
                "message": "part_number is required for assign"}

    pn = part_number if part_number.upper().startswith("C") else f"C{part_number}"

    # Resolve the part first
    row = get_component(pn)
    fetched_live = False
    if row is None:
        live_data = _fetch_live_part(pn)
        if live_data:
            row = _live_part_to_row(live_data, pn)
            fetched_live = True
    if row is None:
        return {"status": "error", "code": "part_not_found",
                "message": f"Part {pn!r} not found in snapshot or live catalog."}

    mpn = row.get("mfr") or ""
    package = row.get("package") or ""
    description = row.get("description") or ""
    footprint = resolve_footprint(package, description)
    kicad_sym = _lookup_kicad_symbol(mpn)

    # Access the loaded schematic
    try:
        from kicad_mcp.tools.schematic import _require_schematic
        sch = _require_schematic()
    except RuntimeError:
        return {"status": "error", "code": "no_schematic_loaded",
                "message": "No schematic loaded. Call load_schematic first."}

    # Find component by exact reference
    comp = sch.components.get(reference)
    if comp is None:
        return {"status": "error", "code": "reference_not_found",
                "message": f"Reference {reference!r} not found in schematic."}

    applied: dict[str, Any] = {}

    # Set Value
    if mpn:
        comp.value = mpn
        applied["value"] = mpn

    # Set LCSC property
    comp.set_property("LCSC", pn)
    applied["lcsc_property"] = pn

    # Set Footprint
    if footprint:
        comp.footprint = footprint
        applied["footprint"] = footprint
    else:
        emit_event("warn", "lcsc_footprint_unmapped",
                   f"No KiCad footprint mapping for package {package!r}. "
                   "Set footprint manually.",
                   {"package": package, "part_number": pn})
        applied["footprint"] = None

    # Update symbol if currently unset or mismatched
    if kicad_sym:
        current_lib = comp.lib_id or ""
        if not current_lib or current_lib != kicad_sym:
            comp._data.lib_id = kicad_sym  # update underlying data
            applied["symbol"] = kicad_sym
    elif not kicad_sym:
        applied["symbol"] = None

    # Save the schematic
    try:
        if schematic_path:
            sch.save(schematic_path)
        else:
            sch.save()
        applied["saved"] = True
    except Exception as e:
        logger.warning("assign: save failed: %s", e)
        applied["saved"] = False
        applied["save_error"] = str(e)

    _record_telemetry("lcsc.assign", pn, {
        "reference": reference,
        "fetched_live": fetched_live,
        "footprint_mapped": footprint is not None,
        "symbol_resolved": kicad_sym is not None,
    }, int((time.monotonic() - t_start) * 1000))

    return {"status": "ok", "applied": applied}


def _record_telemetry(tool_name: str, subject: str, output_summary: dict, elapsed_ms: int) -> None:
    if not _TELEMETRY_AVAILABLE:
        return
    try:
        record_call(
            tool_name=tool_name,
            schematic_hash="",
            inputs_summary={"subject": subject},
            output_summary=output_summary,
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        pass
