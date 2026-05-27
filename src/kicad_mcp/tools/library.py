"""Library router — symbol/footprint library search and index management.

See docs/SPEC_Tool_Consolidation.md.
"""
import logging
import sqlite3
from typing import Any, Dict, Optional

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _op_search(
    query: str,
    type: str = "footprint",
    library: Optional[str] = None,
    limit: int = 20,
) -> Dict[str, Any]:
    if type not in ("footprint", "symbol"):
        return {"error": f"type must be 'footprint' or 'symbol', got {type!r}"}

    try:
        from kicad_mcp.utils.library_index import get_library_index

        index = get_library_index()

        if type == "footprint":
            if index.footprints_stale():
                count = index.rebuild_footprints()
                logger.info("Footprint index rebuilt: %d entries", count)
            results = index.search_footprints(query, library=library, limit=limit)
        else:
            if index.symbols_stale():
                index.rebuild_symbols()
            results = index.search_symbols(query, library=library, limit=limit)

        return {
            "status": "ok",
            "count": len(results),
            "results": results,
            # When count == limit there are likely more matches not shown.
            # Without this flag, callers can't distinguish "5 total matches"
            # from "top 5 of 500 matches."
            "truncated": len(results) == limit,
        }
    except (sqlite3.Error, RuntimeError) as e:
        # Library-index database error or other runtime failure.
        # AttributeError/KeyError/ImportError propagate (programming bugs).
        logger.error("Search failed (%s): %s", e.__class__.__name__, e)
        return {"error": f"Search failed: {e.__class__.__name__}: {e}"}


def _op_rebuild_index(kind: str = "both") -> Dict[str, Any]:
    valid = ("symbols", "footprints", "both")
    if kind not in valid:
        return {"error": f"kind must be one of {valid}; got {kind!r}"}

    try:
        from kicad_mcp.utils.library_index import get_library_index

        index = get_library_index()
        result: Dict[str, Any] = {"status": "ok"}

        if kind in ("symbols", "both"):
            result["symbols_indexed"] = index.rebuild_symbols()
            logger.info("Symbol index rebuilt: %d entries", result["symbols_indexed"])

        if kind in ("footprints", "both"):
            result["footprints_indexed"] = index.rebuild_footprints()
            logger.info("Footprint index rebuilt: %d entries", result["footprints_indexed"])

        return result
    except (sqlite3.Error, RuntimeError, OSError) as e:
        logger.error("Library rebuild failed (%s): %s", type(e).__name__, e)
        return {"error": f"Library rebuild failed: {type(e).__name__}: {e}"}


def register_library_tools(mcp: FastMCP) -> None:
    """Register the library domain router."""

    @mcp.tool()
    def library(
        operation: str,
        *,
        query: Optional[str] = None,
        type: str = "footprint",
        library: Optional[str] = None,
        limit: int = 20,
        kind: str = "both",
    ) -> Dict[str, Any]:
        """Symbol/footprint library operations.

        Operations:
          search(query, type="footprint"|"symbol", library=None, limit=20)
              -> {status, count, results, truncated}
              Search KiCad libraries via the SQLite FTS5 index. Results are
              suitable for use with add_component (symbols) or place_footprint
              (footprints). The index auto-rebuilds when stale.

          rebuild_index(kind="symbols"|"footprints"|"both")
              -> {status, symbols_indexed?, footprints_indexed?}
              Force-rebuild the index. Use when the staleness check misses an
              edit (e.g. you edited a .kicad_mod without bumping mtime in a
              way the scanner noticed), or after installing/updating a
              third-party library.
        """
        if operation == "search":
            if query is None:
                return {"error": "operation='search' requires 'query'"}
            return _op_search(query, type=type, library=library, limit=limit)
        if operation == "rebuild_index":
            return _op_rebuild_index(kind=kind)
        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: search|rebuild_index"
            )
        }
