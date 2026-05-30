"""jlcparts SQLite management — download, build, and query the LCSC/JLCPCB component catalog.

Data source: yaqwsx/jlcparts JSONL shards hosted at https://yaqwsx.github.io/jlcparts/data/
We download the JSONL shards and build a lean local SQLite (no img/url/extra columns).
The local DB is stored at ~/.cache/kicad-mcp/jlcparts.db with a companion metadata file.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

JLCPARTS_BASE_URL = "https://yaqwsx.github.io/jlcparts/data"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "kicad-mcp"
DB_PATH = CACHE_DIR / "jlcparts.db"
META_PATH = CACHE_DIR / "jlcparts_meta.json"

SCHEMA_VERSION = 2  # v2 adds the parametric `attributes` column
# Only the columns a query strictly requires; `attributes` is intentionally
# omitted so a v1 snapshot still validates (it just yields {} parametrics until
# the next refresh) rather than forcing a re-download.
EXPECTED_COLUMNS = {"lcsc", "mfr", "manufacturer", "package", "joints",
                    "assembly_tier", "description", "datasheet", "stock", "price"}

_FOOTPRINT_MAP_PATH = Path(__file__).parent.parent / "data" / "lcsc_footprint_mapping.yaml"
_footprint_map_cache: dict[str, Any] | None = None

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def _load_footprint_map() -> dict[str, Any]:
    global _footprint_map_cache
    if _footprint_map_cache is None:
        with open(_FOOTPRINT_MAP_PATH) as f:
            _footprint_map_cache = yaml.safe_load(f)
    return _footprint_map_cache


def _passive_family(description: str) -> str:
    """Determine passive component family from description text."""
    desc = description.lower()
    if any(w in desc for w in ("capacitor", " cap ", "ceramic", "electrolytic", "tantalum", " uf ", " nf ", " pf ")):
        return "capacitor"
    if any(w in desc for w in ("inductor", "choke", "ferrite", "ferrite bead", " uh ", " nh ")):
        return "inductor"
    return "resistor"


def resolve_footprint(package: str, description: str = "") -> str | None:
    """Map an LCSC package string to a KiCad footprint path."""
    fmap = _load_footprint_map()
    entry = fmap.get(package)
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry
    # Multi-family passive entry
    family = _passive_family(description)
    result = entry.get(family) or entry.get("default")
    return str(result) if result is not None else None


def _normalize_pkg(pkg: str) -> str:
    """Strip spaces, uppercase for comparison."""
    return pkg.strip().upper()


def _score_package(requested: str | None, available: str) -> float:
    """Score package match: 1.0 exact, 0.7 same family, 0.0 different."""
    if requested is None:
        return 1.0
    req = _normalize_pkg(requested)
    avail = _normalize_pkg(available)
    # Exact (including hyphen-normalized)
    if req == avail:
        return 1.0
    req_dehyph = req.replace("-", "").replace(" ", "")
    avail_dehyph = avail.replace("-", "").replace(" ", "")
    if req_dehyph == avail_dehyph:
        return 1.0
    # Family: longer is shorter + "-" + purely-numeric suffix
    short, long_ = (req, avail) if len(req) < len(avail) else (avail, req)
    if long_.startswith(short + "-"):
        suffix = long_[len(short) + 1:]
        if re.match(r"^\d+$", suffix):
            return 0.7
    # Family for dehyphenated forms
    short_d, long_d = (req_dehyph, avail_dehyph) if len(req_dehyph) < len(avail_dehyph) else (avail_dehyph, req_dehyph)
    if long_d.startswith(short_d) and re.match(r"^\d+$", long_d[len(short_d):]):
        return 0.7
    return 0.0


# Assembly tier scoring table  [requested][available]
_TIER_SCORES: dict[str, dict[str, float]] = {
    "basic":    {"basic": 1.0, "preferred": 0.5, "extended": 0.5},
    "extended": {"basic": 0.8, "preferred": 1.0, "extended": 1.0},
    "preferred":{"basic": 0.7, "preferred": 1.0, "extended": 0.7},
    "any":      {"basic": 1.0, "preferred": 1.0, "extended": 1.0},
}


def score_tier(requested: str, available: str | None) -> float:
    """Score assembly tier match."""
    avail = (available or "extended").lower()
    row = _TIER_SCORES.get(requested.lower())
    if row is None:
        return 1.0  # unknown requested tier → no penalty
    return row.get(avail, 0.5)


def score_stock(stock: int) -> float:
    """Score stock level."""
    if stock >= 1000:
        return 1.0
    if stock >= 100:
        return 0.7
    if stock >= 1:
        return 0.3
    return 0.0


def _description_score(query_keywords: list[str], description: str) -> float:
    """Fraction of query keywords present in description (case-insensitive)."""
    if not query_keywords:
        return 1.0
    desc_lower = description.lower()
    matched = sum(1 for kw in query_keywords if kw in desc_lower)
    return matched / len(query_keywords)


def score_candidate(
    row: dict[str, Any],
    query_keywords: list[str],
    requested_package: str | None,
    requested_tier: str,
    footprint_path: str | None,
) -> float:
    """Compute weighted match score (0.0–1.0) for a candidate row."""
    desc_score = _description_score(query_keywords, row.get("description") or "")
    pkg_score = _score_package(requested_package, row.get("package") or "")
    tier_score = score_tier(requested_tier, row.get("assembly_tier"))
    stk_score = score_stock(row.get("stock") or 0)
    fp_score = 1.0 if footprint_path else 0.5

    return (
        0.40 * desc_score
        + 0.25 * pkg_score
        + 0.15 * tier_score
        + 0.10 * stk_score
        + 0.10 * fp_score
    )


def build_deviations(
    row: dict[str, Any],
    requested_package: str | None,
    requested_tier: str,
    footprint_path: str | None,
) -> list[str]:
    """Build human-readable deviation list for a candidate."""
    devs: list[str] = []
    if requested_package:
        pkg_s = _score_package(requested_package, row.get("package") or "")
        if pkg_s < 1.0:
            devs.append(f"package: {row.get('package')!r} (requested {requested_package!r})")
    tier = (row.get("assembly_tier") or "extended").lower()
    t_score = score_tier(requested_tier, tier)
    if requested_tier != "any" and t_score < 1.0:
        note = " — adds JLCPCB assembly fee" if tier in ("extended", "preferred") else ""
        devs.append(f"assembly_tier: {tier!r} (requested {requested_tier!r}){note}")
    stock = row.get("stock") or 0
    s_score = score_stock(stock)
    if s_score < 0.7:
        devs.append(f"stock: {stock} (low; consider alternates)")
    if not footprint_path:
        devs.append(f"kicad_footprint_path: no mapping for {row.get('package')!r}; assign manually")
    return devs


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _fetch_url(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data: bytes = resp.read()
        return data


def _fetch_gzip(url: str) -> bytes:
    """Fetch gzip-compressed resource and return decompressed bytes."""
    data = _fetch_url(url)
    return gzip.decompress(data)


# ---------------------------------------------------------------------------
# Database build
# ---------------------------------------------------------------------------

def _db_connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS components (
            lcsc      TEXT PRIMARY KEY,
            mfr       TEXT,
            manufacturer TEXT,
            package   TEXT,
            joints    INTEGER,
            assembly_tier TEXT CHECK(assembly_tier IN ('basic','preferred','extended')),
            description TEXT,
            datasheet TEXT,
            stock     INTEGER,
            price     TEXT,
            attributes TEXT      -- JSON dict of parametric attributes
        );
        CREATE INDEX IF NOT EXISTS idx_components_package ON components(package);
        CREATE INDEX IF NOT EXISTS idx_components_tier    ON components(assembly_tier);
        CREATE INDEX IF NOT EXISTS idx_components_stock   ON components(stock);
        CREATE VIRTUAL TABLE IF NOT EXISTS components_fts
            USING fts5(lcsc, description, content=components, content_rowid=rowid);
    """)
    conn.commit()


def _validate_schema(conn: sqlite3.Connection) -> None:
    """Raise if expected columns are missing (schema drift guard)."""
    row = conn.execute("PRAGMA table_info(components)").fetchall()
    present = {r["name"] for r in row}
    missing = EXPECTED_COLUMNS - present
    if missing:
        raise RuntimeError(
            f"jlcparts_schema_mismatch: columns missing from local DB: {sorted(missing)}"
        )


def _decode_attributes(attr_indices: list[int], lut: list[list]) -> dict[str, str]:
    """Decode a row's LUT attribute indices into a flat name->value dict.

    Single source of truth for the LUT walk. Takes each attribute's primary
    value's first element (the displayed value). First occurrence wins on a
    duplicate attribute name, matching the prior short-circuit decoders. This
    is the full parametric-attribute capture: nothing the LUT resolves is
    dropped.
    """
    out: dict[str, str] = {}
    for idx in attr_indices:
        if not isinstance(idx, int) or idx < 0 or idx >= len(lut):
            continue
        entry = lut[idx]
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        name, data = entry[0], entry[1]
        if not isinstance(name, str) or not isinstance(data, dict):
            continue
        values = data.get("values", {})
        primary = data.get("primary", "default")
        val_entry = values.get(primary)
        if isinstance(val_entry, list) and val_entry and val_entry[0] is not None:
            out.setdefault(name, str(val_entry[0]))
    return out


def _tier_from_attributes(attr_indices: list[int], lut: list[list]) -> str:
    """Derive assembly_tier from resolved LUT attributes."""
    for idx in attr_indices:
        if idx >= len(lut):
            continue
        entry = lut[idx]
        if not isinstance(entry, list) or len(entry) < 2:
            continue
        name = entry[0]
        data = entry[1]
        if not isinstance(data, dict):
            continue
        values = data.get("values", {})
        primary = data.get("primary", "default")
        val_entry = values.get(primary, [None])[0] if isinstance(values.get(primary), list) else None
        if name == "Basic/Extended":
            if isinstance(val_entry, str):
                v = val_entry.lower()
                if v == "basic":
                    return "basic"
                if v == "preferred":
                    return "preferred"
        if name in ("JLCPCB Best Choice Part", "Preferred Part") and isinstance(val_entry, str):
            if val_entry.lower() in ("yes", "true", "preferred"):
                return "preferred"
    return "extended"


def _package_from_attributes(attr_indices: list[int], lut: list[list]) -> str:
    """Extract package string from LUT attributes (consumes _decode_attributes)."""
    return _decode_attributes(attr_indices, lut).get("Package", "")


def _manufacturer_from_attributes(attr_indices: list[int], lut: list[list]) -> str:
    """Extract manufacturer name from LUT attributes (consumes _decode_attributes)."""
    return _decode_attributes(attr_indices, lut).get("Manufacturer", "")


def _parse_price(price_raw: Any) -> list[dict]:
    """Normalize price to [{qFrom, qTo, price}] list."""
    if not price_raw:
        return []
    if isinstance(price_raw, list):
        return list(price_raw)
    if isinstance(price_raw, str):
        try:
            parsed = json.loads(price_raw)
            return list(parsed) if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _decode_shard_rows(
    data_lines: list[str],
    col_map: dict[str, int],
    lut: list[list],
) -> tuple[list[tuple], dict[str, int]]:
    """Decode one shard's data lines into INSERT tuples + a drop counter.

    Pure (no I/O) so the row-level drop accounting is unit-testable. Every
    dropped row is counted by reason; none are silently skipped. Reasons:
      bad_json — line is not valid JSON
      not_list — row is JSON but not the expected positional list
      no_lcsc  — row lacks the LCSC part number (the primary key)
    """
    batch: list[tuple] = []
    drops = {"bad_json": 0, "not_list": 0, "no_lcsc": 0}
    for line in data_lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            drops["bad_json"] += 1
            continue
        if not isinstance(row, list):
            drops["not_list"] += 1
            continue

        def get(col: str) -> Any:
            idx = col_map.get(col)
            return row[idx] if idx is not None and idx < len(row) else None

        attr_indices = get("attributes") or []
        if not isinstance(attr_indices, list):
            attr_indices = []

        attributes = _decode_attributes(attr_indices, lut)
        package = attributes.get("Package", "")
        manufacturer = attributes.get("Manufacturer", "")
        assembly_tier = _tier_from_attributes(attr_indices, lut)
        price_list = _parse_price(get("price"))

        lcsc_raw = get("lcsc")
        if lcsc_raw is None:
            drops["no_lcsc"] += 1
            continue
        lcsc = str(lcsc_raw) if str(lcsc_raw).startswith("C") else f"C{lcsc_raw}"

        batch.append((
            lcsc,
            get("mfr") or "",
            manufacturer,
            package,
            get("joints"),
            assembly_tier,
            get("description") or "",
            get("datasheet") or "",
            get("stock") or 0,
            json.dumps(price_list),
            json.dumps(attributes),
        ))
    return batch, drops


def build_db_from_jsonl(
    db_path: Path = DB_PATH,
    meta_path: Path = META_PATH,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Download jlcparts JSONL shards and build local SQLite.

    Returns metadata dict with snapshot_date, total_components, etc.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Fetch manifest
    logger.info("Fetching jlcparts manifest...")
    try:
        manifest_data = _fetch_url(f"{JLCPARTS_BASE_URL}/manifest.json")
    except urllib.error.URLError as e:
        raise RuntimeError(f"lcsc_snapshot_unavailable: cannot fetch manifest: {e}") from e
    manifest = json.loads(manifest_data)
    snapshot_date = manifest.get("created", "")[:10]  # ISO-8601 date prefix
    files_dict: dict[str, Any] = manifest.get("files", {})

    # 2. Fetch attributes LUT
    logger.info("Fetching attributes LUT...")
    try:
        lut_data = _fetch_gzip(f"{JLCPARTS_BASE_URL}/attributes-lut.json.gz")
    except Exception as e:
        raise RuntimeError(f"lcsc_snapshot_unavailable: cannot fetch attributes LUT: {e}") from e
    lut: list[list] = json.loads(lut_data)
    logger.info("Loaded attributes LUT with %d entries", len(lut))

    # 3. Build DB
    conn = _db_connect(db_path)
    _create_schema(conn)

    # Clear existing component data for a fresh build
    conn.execute("DELETE FROM components")
    conn.execute("DELETE FROM components_fts")
    conn.execute("DELETE FROM metadata")
    conn.commit()

    total_inserted = 0
    drop_counts: dict[str, int] = {"bad_json": 0, "not_list": 0, "no_lcsc": 0}
    shard_files = [fname for fname, info in files_dict.items()
                   if isinstance(info, dict) and info.get("kind") in ("components", None)]

    logger.info("Processing %d shard files...", len(shard_files))
    for i, fname in enumerate(shard_files):
        if progress_callback:
            progress_callback(i, len(shard_files), fname)
        url = f"{JLCPARTS_BASE_URL}/{fname}"
        try:
            shard_bytes = _fetch_gzip(url)
        except Exception as e:
            logger.warning("Failed to fetch shard %s: %s — skipping", fname, e)
            continue

        lines = shard_bytes.decode("utf-8").splitlines()
        if not lines:
            continue

        # First line is the column map
        try:
            col_map: dict[str, int] = json.loads(lines[0])
        except Exception:
            logger.warning("Shard %s: bad header line — skipping", fname)
            continue

        required = {"lcsc", "mfr", "joints", "description", "price", "attributes", "stock"}
        if not required.issubset(col_map.keys()):
            logger.warning("Shard %s: missing required columns %s — skipping",
                           fname, required - col_map.keys())
            continue

        batch, shard_drops = _decode_shard_rows(lines[1:], col_map, lut)
        shard_dropped = sum(shard_drops.values())
        if shard_dropped:
            logger.warning("Shard %s: dropped %d malformed row(s): %s",
                           fname, shard_dropped, shard_drops)
        for reason, count in shard_drops.items():
            drop_counts[reason] += count

        if batch:
            conn.executemany(
                """INSERT OR REPLACE INTO components
                   (lcsc, mfr, manufacturer, package, joints, assembly_tier,
                    description, datasheet, stock, price, attributes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                batch,
            )
            conn.commit()
            total_inserted += len(batch)

        time.sleep(0.05)  # polite throttle

    # Rebuild FTS
    conn.execute("INSERT INTO components_fts(components_fts) VALUES('rebuild')")
    conn.commit()

    # Write metadata
    rows_dropped = sum(drop_counts.values())
    if rows_dropped:
        logger.warning("jlcparts ingestion dropped %d malformed row(s) total: %s",
                       rows_dropped, drop_counts)
    downloaded_at = datetime.now(timezone.utc).isoformat()
    meta = {
        "downloaded_at": downloaded_at,
        "source_url": f"{JLCPARTS_BASE_URL}/manifest.json",
        "snapshot_date": snapshot_date,
        "total_components": total_inserted,
        "rows_dropped": rows_dropped,
        "rows_dropped_by_reason": drop_counts,
        "schema_version": SCHEMA_VERSION,
    }
    conn.execute("INSERT OR REPLACE INTO metadata VALUES ('downloaded_at', ?)", (downloaded_at,))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot_date', ?)", (snapshot_date,))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES ('total_components', ?)", (str(total_inserted),))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES ('rows_dropped', ?)", (str(rows_dropped),))
    conn.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    conn.close()

    meta_path.write_text(json.dumps(meta, indent=2))
    logger.info("jlcparts DB built: %d components (%d dropped), snapshot_date=%s",
                total_inserted, rows_dropped, snapshot_date)
    return meta


# ---------------------------------------------------------------------------
# Query API
# ---------------------------------------------------------------------------

def _read_meta(meta_path: Path = META_PATH) -> dict[str, Any]:
    try:
        result: dict[str, Any] = json.loads(meta_path.read_text())
        return result
    except Exception:
        return {}


def get_snapshot_age_days(meta_path: Path = META_PATH) -> int | None:
    """Return age of local snapshot in days, or None if no snapshot."""
    meta = _read_meta(meta_path)
    downloaded_at = meta.get("downloaded_at")
    if not downloaded_at:
        return None
    try:
        ts = datetime.fromisoformat(downloaded_at)
        delta = datetime.now(timezone.utc) - ts
        return delta.days
    except Exception:
        return None


def get_snapshot_date(meta_path: Path = META_PATH) -> str | None:
    return _read_meta(meta_path).get("snapshot_date")


def db_exists(db_path: Path = DB_PATH) -> bool:
    return db_path.exists() and db_path.stat().st_size > 0


def validate_schema(db_path: Path = DB_PATH) -> None:
    """Check that the local DB has the expected columns."""
    conn = _db_connect(db_path)
    try:
        _validate_schema(conn)
    finally:
        conn.close()


def _ranked_candidates(
    description: str,
    package: str | None,
    assembly_tier: str,
    include_unresolvable: bool,
    db_path: Path,
) -> list[dict[str, Any]]:
    """Full scored+ranked candidate list (no truncation).

    Single source of truth for both ``search_components`` (sliced) and
    ``search_components_with_total`` (sliced + pre-slice count). Neither
    re-encodes the SQL filter / scoring / ranking.
    """
    conn = _db_connect(db_path)
    try:
        # SQL pre-filter (indexed columns)
        params: list[Any] = []
        where_clauses: list[str] = ["stock > 0"]

        if assembly_tier == "basic":
            where_clauses.append("assembly_tier = 'basic'")
        elif assembly_tier == "preferred":
            where_clauses.append("assembly_tier IN ('basic', 'preferred')")
        elif assembly_tier != "any":
            # "extended" means any tier is acceptable
            pass

        if package:
            where_clauses.append("package = ?")
            params.append(package)

        sql = f"SELECT * FROM components WHERE {' AND '.join(where_clauses)}"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    # Score and rank in Python
    query_keywords = [kw.lower() for kw in description.strip().split() if kw]
    results: list[tuple[float, dict]] = []

    for row in rows:
        # Require at least one keyword to appear in description
        if query_keywords:
            desc_lower = (row.get("description") or "").lower()
            if not any(kw in desc_lower for kw in query_keywords):
                continue
        fp = resolve_footprint(row.get("package") or "", row.get("description") or "")
        if not include_unresolvable and fp is None:
            continue
        score = score_candidate(row, query_keywords, package, assembly_tier, fp)
        deviations = build_deviations(row, package, assembly_tier, fp)
        results.append((score, {**row, "_score": score, "_deviations": deviations, "_footprint": fp}))

    results.sort(key=lambda x: x[0], reverse=True)
    return [r[1] for r in results]


def search_components(
    description: str,
    package: str | None = None,
    assembly_tier: str = "basic",
    max_results: int = 3,
    include_unresolvable: bool = False,
    db_path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Search the local jlcparts DB and return scored, ranked candidates."""
    return _ranked_candidates(
        description, package, assembly_tier, include_unresolvable, db_path
    )[:max_results]


def search_components_with_total(
    description: str,
    package: str | None = None,
    assembly_tier: str = "basic",
    max_results: int = 3,
    include_unresolvable: bool = False,
    db_path: Path = DB_PATH,
) -> tuple[list[dict[str, Any]], int]:
    """Like ``search_components`` but also return the pre-slice candidate count.

    The count lets callers distinguish "only N matched" from "top N of many"
    so they can emit a truncation flag instead of silently capping.
    """
    ranked = _ranked_candidates(
        description, package, assembly_tier, include_unresolvable, db_path
    )
    return ranked[:max_results], len(ranked)


def get_component(lcsc: str, db_path: Path = DB_PATH) -> dict[str, Any] | None:
    """Exact lookup by LCSC part number (e.g., 'C6186')."""
    conn = _db_connect(db_path)
    try:
        row = conn.execute("SELECT * FROM components WHERE lcsc = ?", (lcsc,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Live JLCPCB API fallback (for resolve cache-miss)
# ---------------------------------------------------------------------------

def _fetch_live_part(part_number: str) -> dict[str, Any] | None:
    """Fetch part details from live JLCPCB API (cache-miss fallback only)."""
    url = (f"https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail"
           f"?componentCode={part_number}")
    try:
        data = _fetch_url(url, timeout=15)
        payload = json.loads(data)
        if payload.get("code") != 200:
            return None
        data_val: dict[str, Any] | None = payload.get("data")
        return data_val
    except Exception as e:
        logger.debug("Live API fetch failed for %s: %s", part_number, e)
        return None


def _live_part_to_row(data: dict[str, Any], part_number: str) -> dict[str, Any] | None:
    """Normalize live API response to local row format."""
    if not data:
        return None
    price_raw = data.get("prices") or data.get("price") or []
    if isinstance(price_raw, list):
        price_list = [
            {"qFrom": p.get("startQuantity", p.get("qFrom", 1)),
             "qTo": p.get("endQuantity", p.get("qTo")),
             "price": float(p.get("productPrice", p.get("price", 0)))}
            for p in price_raw
        ]
    else:
        price_list = []
    tier = "basic" if data.get("componentLibraryType") == "base" else "extended"
    return {
        "lcsc": part_number,
        "mfr": data.get("componentModelEn") or data.get("erpMpn") or "",
        "manufacturer": data.get("brandNameEn") or "",
        "package": data.get("componentSpecificationEn") or "",
        "joints": data.get("solderJoint"),
        "assembly_tier": tier,
        "description": data.get("describe") or data.get("description") or "",
        "datasheet": data.get("dataManualUrl") or "",
        "stock": data.get("stockCount") or 0,
        "price": json.dumps(price_list),
        "attributes": _live_attributes(data),
    }


def _live_attributes(data: dict[str, Any]) -> dict[str, str]:
    """Decode the live JLCPCB API's parametric attributes into a name->value dict.

    The API returns them as a list of {attribute_name_en, attribute_value_name}
    objects (key casing varies); fall back to a plain dict if already keyed.
    """
    raw = data.get("attributes")
    out: dict[str, str] = {}
    if isinstance(raw, list):
        for a in raw:
            if not isinstance(a, dict):
                continue
            name = (a.get("attribute_name_en") or a.get("attributeNameEn")
                    or a.get("name"))
            value = (a.get("attribute_value_name") or a.get("attributeValueName")
                     or a.get("value"))
            if isinstance(name, str) and value is not None:
                out.setdefault(name, str(value))
    elif isinstance(raw, dict):
        for k, v in raw.items():
            if v is not None:
                out[str(k)] = str(v)
    return out
