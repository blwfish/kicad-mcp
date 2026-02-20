"""Unified SQLite FTS5 index for KiCad symbol and footprint libraries.

Single database with separate tables for symbols and footprints.
Both indexes auto-rebuild when their respective library files change.
"""

import logging
import os
import platform
import re
import sqlite3
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Singleton instance
_index: Optional["LibraryIndex"] = None

# Default cache location
_DEFAULT_DB_PATH = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "kicad-mcp",
    "library_index.db",
)


# ---------------------------------------------------------------------------
# Library path discovery
# ---------------------------------------------------------------------------


def _get_kicad_share_path() -> Optional[str]:
    """Find KiCad's SharedSupport/share directory."""
    system = platform.system()
    if system == "Darwin":
        p = "/Applications/KiCad/KiCad.app/Contents/SharedSupport"
        if os.path.isdir(p):
            return p
    elif system == "Linux":
        for p in ["/usr/share/kicad", "/usr/local/share/kicad"]:
            if os.path.isdir(p):
                return p
    elif system == "Windows":
        p = r"C:\Program Files\KiCad\share\kicad"
        if os.path.isdir(p):
            return p
    return None


def _get_footprint_lib_path() -> Optional[str]:
    """Find the KiCad footprint library directory."""
    env = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env and os.path.isdir(env):
        return env
    share = _get_kicad_share_path()
    if share:
        fp = os.path.join(share, "footprints")
        if os.path.isdir(fp):
            return fp
    return None


def _get_symbol_lib_path() -> Optional[str]:
    """Find the KiCad symbol library directory."""
    env = os.environ.get("KICAD_SYMBOL_DIR")
    if env and os.path.isdir(env):
        return env
    share = _get_kicad_share_path()
    if share:
        sp = os.path.join(share, "symbols")
        if os.path.isdir(sp):
            return sp
    return None


# ---------------------------------------------------------------------------
# .kicad_mod parser (footprints)
# ---------------------------------------------------------------------------


def _parse_kicad_mod(filepath: str) -> Dict:
    """Extract metadata from a .kicad_mod file using regex.

    Fast top-of-file parsing — avoids full S-expression parse.
    """
    result = {"name": "", "description": "", "tags": "", "pad_count": 0}
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return result

    m = re.match(r'\(footprint\s+"([^"]+)"', content)
    if m:
        result["name"] = m.group(1)
    m = re.search(r'\(descr\s+"([^"]*)"', content)
    if m:
        result["description"] = m.group(1)
    m = re.search(r'\(tags\s+"([^"]*)"', content)
    if m:
        result["tags"] = m.group(1)
    result["pad_count"] = len(re.findall(r"\(pad\s+", content))
    return result


# ---------------------------------------------------------------------------
# .kicad_sym parser (symbols)
# ---------------------------------------------------------------------------


def _parse_kicad_sym(filepath: str) -> List[Dict]:
    """Extract symbol metadata from a .kicad_sym library file.

    Each .kicad_sym can contain many symbols. Returns a list of dicts.
    Uses regex for speed — good enough for name/description/keywords/pin_count.
    """
    results = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return results

    library = os.path.splitext(os.path.basename(filepath))[0]

    # Find all top-level (symbol "Name" blocks — skip nested (symbol "Name_N_N" pin units)
    # Top-level symbols are at indent level 1 (after the kicad_symbol_lib wrapper)
    # They DON'T start with the library name + underscore + digit pattern used for units
    for m in re.finditer(r'^\t\(symbol\s+"([^"]+)"', content, re.MULTILINE):
        name = m.group(1)

        # Skip sub-units like "LM358_1_1", "LM358_0_1" — they contain pin defs
        # but are children of the main symbol. Heuristic: if name contains _N_N
        # where N are digits, and the prefix matches a symbol we already found, skip.
        if re.search(r"_\d+_\d+$", name):
            continue

        # Extract the block for this symbol (approximate — find next same-indent symbol)
        start = m.start()
        # Find the extent of this symbol block (next top-level symbol or end)
        next_sym = re.search(r'^\t\(symbol\s+"', content[m.end() :], re.MULTILINE)
        end = m.end() + next_sym.start() if next_sym else len(content)
        block = content[start:end]

        sym: Dict = {
            "name": name,
            "library": library,
            "description": "",
            "keywords": "",
            "pin_count": 0,
        }

        dm = re.search(r'\(property\s+"Description"\s+"([^"]*)"', block)
        if dm:
            sym["description"] = dm.group(1)

        km = re.search(r'\(property\s+"ki_keywords"\s+"([^"]*)"', block)
        if km:
            sym["keywords"] = km.group(1)

        # Count pins across all sub-units
        sym["pin_count"] = len(re.findall(r"\(pin\s+", block))

        results.append(sym)

    return results


# ---------------------------------------------------------------------------
# Unified LibraryIndex
# ---------------------------------------------------------------------------


class LibraryIndex:
    """Unified SQLite FTS5 index for KiCad symbol and footprint libraries."""

    def __init__(
        self,
        db_path: str = _DEFAULT_DB_PATH,
        footprint_lib_path: Optional[str] = None,
        symbol_lib_path: Optional[str] = None,
    ):
        self.db_path = db_path
        self.footprint_lib_path = footprint_lib_path or _get_footprint_lib_path()
        self.symbol_lib_path = symbol_lib_path or _get_symbol_lib_path()
        self._ensure_db_dir()
        self._ensure_tables()

    def _ensure_db_dir(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_tables(self):
        """Create tables if they don't exist (non-destructive)."""
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()
        conn.close()

    # -------------------------------------------------------------------
    # Footprint index
    # -------------------------------------------------------------------

    def footprints_stale(self) -> bool:
        """Check if the footprint index needs rebuilding."""
        if not self.footprint_lib_path or not os.path.isdir(self.footprint_lib_path):
            return True
        return self._is_stale("fp_build_time", "fp_lib_path", self.footprint_lib_path, ".pretty")

    def rebuild_footprints(self) -> int:
        """Rebuild the footprint index. Returns count."""
        if not self.footprint_lib_path or not os.path.isdir(self.footprint_lib_path):
            raise RuntimeError(
                f"Footprint library path not found: {self.footprint_lib_path}. "
                "Set KICAD_FOOTPRINT_DIR or install KiCad."
            )

        start = time.monotonic()
        logger.info("Rebuilding footprint index from %s", self.footprint_lib_path)

        conn = self._connect()
        conn.execute("DROP TABLE IF EXISTS footprints")
        conn.execute("DROP TABLE IF EXISTS footprints_fts")

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
                content='footprints', content_rowid='id'
            )
        """)

        count = 0
        for lib_dir in sorted(os.scandir(self.footprint_lib_path), key=lambda e: e.name):
            if not lib_dir.name.endswith(".pretty") or not lib_dir.is_dir():
                continue
            library = lib_dir.name[:-7]  # strip ".pretty"
            for mod_file in sorted(os.scandir(lib_dir.path), key=lambda e: e.name):
                if not mod_file.name.endswith(".kicad_mod"):
                    continue
                meta = _parse_kicad_mod(mod_file.path)
                if not meta["name"]:
                    meta["name"] = mod_file.name[:-10]
                conn.execute(
                    "INSERT INTO footprints (library, name, description, tags, pad_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (library, meta["name"], meta["description"], meta["tags"], meta["pad_count"]),
                )
                count += 1

        conn.execute("""
            INSERT INTO footprints_fts (rowid, library, name, description, tags)
            SELECT id, library, name, description, tags FROM footprints
        """)
        self._set_meta(conn, "fp_build_time", str(time.time()))
        self._set_meta(conn, "fp_lib_path", self.footprint_lib_path)
        self._set_meta(conn, "fp_count", str(count))
        conn.commit()
        conn.close()

        logger.info("Indexed %d footprints in %.2fs", count, time.monotonic() - start)
        return count

    def search_footprints(
        self, query: str, library: Optional[str] = None, limit: int = 20
    ) -> List[Dict]:
        """Search footprints by name, description, tags, or library."""
        if not query or not query.strip():
            return []

        fts_query = self._build_fts_query(query)
        conn = self._connect()

        try:
            if library:
                rows = conn.execute(
                    """
                    SELECT f.library, f.name, f.description, f.tags, f.pad_count
                    FROM footprints_fts fts
                    JOIN footprints f ON f.id = fts.rowid
                    WHERE footprints_fts MATCH ? AND f.library = ?
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_query, library, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT f.library, f.name, f.description, f.tags, f.pad_count
                    FROM footprints_fts fts
                    JOIN footprints f ON f.id = fts.rowid
                    WHERE footprints_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("Footprint FTS query failed for %r: %s", query, e)
            conn.close()
            return []

        results = [
            {
                "library": r["library"],
                "name": r["name"],
                "full_name": f"{r['library']}:{r['name']}",
                "description": r["description"],
                "tags": r["tags"],
                "pad_count": r["pad_count"],
            }
            for r in rows
        ]
        conn.close()
        return results

    # -------------------------------------------------------------------
    # Symbol index
    # -------------------------------------------------------------------

    def symbols_stale(self) -> bool:
        """Check if the symbol index needs rebuilding."""
        if not self.symbol_lib_path or not os.path.isdir(self.symbol_lib_path):
            return True
        return self._is_stale("sym_build_time", "sym_lib_path", self.symbol_lib_path, ".kicad_sym")

    def rebuild_symbols(self) -> int:
        """Rebuild the symbol index. Returns count."""
        if not self.symbol_lib_path or not os.path.isdir(self.symbol_lib_path):
            raise RuntimeError(
                f"Symbol library path not found: {self.symbol_lib_path}. "
                "Set KICAD_SYMBOL_DIR or install KiCad."
            )

        start = time.monotonic()
        logger.info("Rebuilding symbol index from %s", self.symbol_lib_path)

        conn = self._connect()
        conn.execute("DROP TABLE IF EXISTS symbols")
        conn.execute("DROP TABLE IF EXISTS symbols_fts")

        conn.execute("""
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                library TEXT NOT NULL,
                name TEXT NOT NULL,
                lib_id TEXT NOT NULL,
                description TEXT,
                keywords TEXT,
                pin_count INTEGER
            )
        """)
        conn.execute("""
            CREATE VIRTUAL TABLE symbols_fts USING fts5(
                library, name, lib_id, description, keywords,
                content='symbols', content_rowid='id'
            )
        """)

        count = 0
        for entry in sorted(os.scandir(self.symbol_lib_path), key=lambda e: e.name):
            if not entry.name.endswith(".kicad_sym"):
                continue
            for sym in _parse_kicad_sym(entry.path):
                lib_id = f"{sym['library']}:{sym['name']}"
                conn.execute(
                    "INSERT INTO symbols (library, name, lib_id, description, keywords, pin_count) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        sym["library"],
                        sym["name"],
                        lib_id,
                        sym["description"],
                        sym["keywords"],
                        sym["pin_count"],
                    ),
                )
                count += 1

        conn.execute("""
            INSERT INTO symbols_fts (rowid, library, name, lib_id, description, keywords)
            SELECT id, library, name, lib_id, description, keywords FROM symbols
        """)
        self._set_meta(conn, "sym_build_time", str(time.time()))
        self._set_meta(conn, "sym_lib_path", self.symbol_lib_path)
        self._set_meta(conn, "sym_count", str(count))
        conn.commit()
        conn.close()

        logger.info("Indexed %d symbols in %.2fs", count, time.monotonic() - start)
        return count

    def search_symbols(
        self, query: str, library: Optional[str] = None, limit: int = 20
    ) -> List[Dict]:
        """Search symbols by name, description, keywords, or library."""
        if not query or not query.strip():
            return []

        fts_query = self._build_fts_query(query)
        conn = self._connect()

        try:
            if library:
                rows = conn.execute(
                    """
                    SELECT s.library, s.name, s.lib_id, s.description, s.keywords, s.pin_count
                    FROM symbols_fts fts
                    JOIN symbols s ON s.id = fts.rowid
                    WHERE symbols_fts MATCH ? AND s.library = ?
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_query, library, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT s.library, s.name, s.lib_id, s.description, s.keywords, s.pin_count
                    FROM symbols_fts fts
                    JOIN symbols s ON s.id = fts.rowid
                    WHERE symbols_fts MATCH ?
                    ORDER BY rank LIMIT ?
                    """,
                    (fts_query, limit),
                ).fetchall()
        except sqlite3.OperationalError as e:
            logger.warning("Symbol FTS query failed for %r: %s", query, e)
            conn.close()
            return []

        results = [
            {
                "lib_id": r["lib_id"],
                "name": r["name"],
                "library": r["library"],
                "description": r["description"],
                "keywords": r["keywords"],
                "pin_count": r["pin_count"],
            }
            for r in rows
        ]
        conn.close()
        return results

    # -------------------------------------------------------------------
    # Shared helpers
    # -------------------------------------------------------------------

    def _is_stale(
        self, time_key: str, path_key: str, lib_path: str, suffix: str
    ) -> bool:
        """Generic staleness check for a library type."""
        if not os.path.exists(self.db_path):
            return True
        try:
            conn = self._connect()
            # Check table exists
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "metadata" not in tables:
                conn.close()
                return True

            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (time_key,)
            ).fetchone()
            if not row:
                conn.close()
                return True
            build_time = float(row["value"])

            row = conn.execute(
                "SELECT value FROM metadata WHERE key = ?", (path_key,)
            ).fetchone()
            if not row or row["value"] != lib_path:
                conn.close()
                return True

            # Check if any library entry is newer
            for entry in os.scandir(lib_path):
                if suffix == ".pretty":
                    if entry.name.endswith(suffix) and entry.is_dir():
                        if entry.stat().st_mtime > build_time:
                            conn.close()
                            return True
                else:
                    if entry.name.endswith(suffix) and entry.is_file():
                        if entry.stat().st_mtime > build_time:
                            conn.close()
                            return True

            conn.close()
            return False
        except (sqlite3.Error, ValueError):
            return True

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: str):
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)", (key, value)
        )

    @staticmethod
    def _build_fts_query(query: str) -> str:
        """Build FTS5 query with prefix matching on last term."""
        terms = query.strip().split()
        fts_terms = []
        for i, term in enumerate(terms):
            safe = term.replace('"', '""')
            if i == len(terms) - 1:
                fts_terms.append(f'"{safe}"*')
            else:
                fts_terms.append(f'"{safe}"')
        return " ".join(fts_terms)


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------


def get_library_index() -> LibraryIndex:
    """Get or create the singleton LibraryIndex."""
    global _index
    if _index is None:
        _index = LibraryIndex()
    return _index
