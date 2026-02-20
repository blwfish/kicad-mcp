"""SQLite FTS5 index for KiCad footprint libraries.

Scans all .kicad_mod files under the KiCad footprint library path,
extracts metadata (name, library, description, tags, pad count),
and stores it in a SQLite database with full-text search.

The index auto-rebuilds when any .pretty directory has been modified
(e.g. after a KiCad upgrade).
"""

import logging
import os
import platform
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Singleton index instance
_index: Optional["FootprintIndex"] = None

# Default cache location
_DEFAULT_DB_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "kicad-mcp",
    "footprint_index.db",
)


def _get_footprint_lib_path() -> Optional[str]:
    """Find the KiCad footprint library directory."""
    system = platform.system()

    if system == "Darwin":
        candidates = [
            "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        ]
    elif system == "Linux":
        candidates = [
            "/usr/share/kicad/footprints",
            "/usr/local/share/kicad/footprints",
        ]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\KiCad\share\kicad\footprints",
        ]
    else:
        candidates = []

    # Also check KICAD_FOOTPRINT_DIR env var
    env_path = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env_path:
        candidates.insert(0, env_path)

    for path in candidates:
        if os.path.isdir(path):
            return path

    return None


def _parse_kicad_mod(filepath: str) -> Dict:
    """Extract metadata from a .kicad_mod file using simple regex parsing.

    We avoid a full S-expression parser for speed — the fields we need
    (footprint name, descr, tags, pad count) are always near the top of the file.
    """
    result = {
        "name": "",
        "description": "",
        "tags": "",
        "pad_count": 0,
    }

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return result

    # Footprint name: first line (footprint "NAME")
    m = re.match(r'\(footprint\s+"([^"]+)"', content)
    if m:
        result["name"] = m.group(1)

    # Description: (descr "...")
    m = re.search(r'\(descr\s+"([^"]*)"', content)
    if m:
        result["description"] = m.group(1)

    # Tags: (tags "...")
    m = re.search(r'\(tags\s+"([^"]*)"', content)
    if m:
        result["tags"] = m.group(1)

    # Pad count: count occurrences of (pad ...)
    result["pad_count"] = len(re.findall(r'\(pad\s+', content))

    return result


class FootprintIndex:
    """SQLite FTS5 index for KiCad footprint libraries."""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH, lib_path: Optional[str] = None):
        self.db_path = db_path
        self.lib_path = lib_path or _get_footprint_lib_path()
        self._ensure_db_dir()

    def _ensure_db_dir(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def is_stale(self) -> bool:
        """Check if the index needs rebuilding.

        Returns True if:
        - The database doesn't exist
        - The footprint library path has changed
        - Any .pretty directory has a newer mtime than the last build
        """
        if not os.path.exists(self.db_path):
            return True

        if not self.lib_path or not os.path.isdir(self.lib_path):
            return True

        try:
            conn = self._connect()
            cur = conn.execute("SELECT value FROM metadata WHERE key = 'build_time'")
            row = cur.fetchone()
            if not row:
                conn.close()
                return True

            build_time = float(row["value"])

            # Check if lib_path changed
            cur = conn.execute("SELECT value FROM metadata WHERE key = 'lib_path'")
            row = cur.fetchone()
            if not row or row["value"] != self.lib_path:
                conn.close()
                return True

            # Check if any .pretty directory is newer than the build
            for entry in os.scandir(self.lib_path):
                if entry.name.endswith(".pretty") and entry.is_dir():
                    if entry.stat().st_mtime > build_time:
                        conn.close()
                        return True

            conn.close()
            return False

        except (sqlite3.Error, ValueError):
            return True

    def rebuild_index(self) -> int:
        """Rebuild the footprint index from scratch.

        Returns the number of footprints indexed.
        """
        if not self.lib_path or not os.path.isdir(self.lib_path):
            raise RuntimeError(
                f"Footprint library path not found: {self.lib_path}. "
                "Set KICAD_FOOTPRINT_DIR or install KiCad."
            )

        start = time.monotonic()
        logger.info("Rebuilding footprint index from %s", self.lib_path)

        conn = self._connect()
        conn.execute("DROP TABLE IF EXISTS footprints")
        conn.execute("DROP TABLE IF EXISTS footprints_fts")
        conn.execute("DROP TABLE IF EXISTS metadata")

        conn.execute("""
            CREATE TABLE footprints (
                id INTEGER PRIMARY KEY,
                library TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                tags TEXT,
                pad_count INTEGER
            )
        """)

        conn.execute("""
            CREATE VIRTUAL TABLE footprints_fts USING fts5(
                library, name, description, tags,
                content='footprints',
                content_rowid='id'
            )
        """)

        conn.execute("""
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        count = 0
        for lib_dir in sorted(os.scandir(self.lib_path), key=lambda e: e.name):
            if not lib_dir.name.endswith(".pretty") or not lib_dir.is_dir():
                continue

            library = lib_dir.name[:-7]  # strip ".pretty"

            for mod_file in sorted(os.scandir(lib_dir.path), key=lambda e: e.name):
                if not mod_file.name.endswith(".kicad_mod"):
                    continue

                meta = _parse_kicad_mod(mod_file.path)
                if not meta["name"]:
                    meta["name"] = mod_file.name[:-10]  # strip ".kicad_mod"

                conn.execute(
                    "INSERT INTO footprints (library, name, description, tags, pad_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (library, meta["name"], meta["description"], meta["tags"], meta["pad_count"]),
                )
                count += 1

        # Populate FTS index
        conn.execute("""
            INSERT INTO footprints_fts (rowid, library, name, description, tags)
            SELECT id, library, name, description, tags FROM footprints
        """)

        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('build_time', ?)",
            (str(time.time()),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('lib_path', ?)",
            (self.lib_path,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('footprint_count', ?)",
            (str(count),),
        )

        conn.commit()
        conn.close()

        elapsed = time.monotonic() - start
        logger.info("Indexed %d footprints in %.2fs", count, elapsed)
        return count

    def search(
        self,
        query: str,
        library: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict]:
        """Search for footprints matching a query.

        Args:
            query: Search terms (e.g., "SOT-23", "0603 resistor", "SOIC-8").
            library: Optional library name to restrict search (e.g., "Resistor_SMD").
            limit: Maximum results to return.

        Returns:
            List of dicts with library, name, description, tags, pad_count.
        """
        if not query or not query.strip():
            return []

        conn = self._connect()

        # Tokenize query for FTS5 — quote terms with hyphens/dots
        # FTS5 treats hyphens as separators, so "SOT-23" becomes "SOT" AND "23"
        # which is actually what we want for most cases.
        # But also do a prefix match on the last term for partial queries.
        terms = query.strip().split()
        fts_terms = []
        for i, term in enumerate(terms):
            # Escape double quotes
            safe = term.replace('"', '""')
            if i == len(terms) - 1:
                # Last term gets prefix match
                fts_terms.append(f'"{safe}"*')
            else:
                fts_terms.append(f'"{safe}"')

        fts_query = " ".join(fts_terms)

        try:
            if library:
                rows = conn.execute(
                    """
                    SELECT f.library, f.name, f.description, f.tags, f.pad_count,
                           rank
                    FROM footprints_fts fts
                    JOIN footprints f ON f.id = fts.rowid
                    WHERE footprints_fts MATCH ?
                      AND f.library = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, library, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT f.library, f.name, f.description, f.tags, f.pad_count,
                           rank
                    FROM footprints_fts fts
                    JOIN footprints f ON f.id = fts.rowid
                    WHERE footprints_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("FTS query failed for %r: %s", query, e)
            conn.close()
            return []

        results = []
        for row in rows:
            results.append({
                "library": row["library"],
                "name": row["name"],
                "full_name": f"{row['library']}:{row['name']}",
                "description": row["description"],
                "tags": row["tags"],
                "pad_count": row["pad_count"],
            })

        conn.close()
        return results


def get_footprint_index() -> FootprintIndex:
    """Get or create the singleton FootprintIndex."""
    global _index
    if _index is None:
        _index = FootprintIndex()
    return _index
