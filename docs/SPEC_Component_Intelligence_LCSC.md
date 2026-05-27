# SPEC — Component Intelligence (LCSC)

**Status:** Approved for implementation
**Author:** Brian + Claude
**Date:** 2026-05-27
**Target:** Spec-based component selection for kicad-mcp with LCSC/JLCPCB data; replaces the manual Chrome-extension kludge with a first-class MCP tool.
**Scope:** LCSC only in v1. DigiKey is a planned future supplier; `ResolvedPart` is designed to accommodate it but the v1 implementation is LCSC-specific.

## Motivation

Selecting components currently requires the human (Brian) to bridge between the LLM's design intent and a supplier catalog. The workflow involves a Chrome extension that scrapes LCSC search results and pipes them back to the conversation. This:

- **Doesn't work for external users.** kicad-mcp is consumed by AI assistants without Brian's Chrome setup; they have no component-selection workflow at all.
- **Puts the human in the loop for every component.** Brian lacks deep component domain knowledge and is functionally an obstruction in the iteration loop.
- **Doesn't integrate KiCad symbol/footprint resolution.** Even after finding a part, the user separately searches for a symbol and footprint, and may pick mismatched ones.

This SPEC defines a tool that accepts a functional specification (`"3.3V LDO, SOT-23, JLCPCB basic tier"`) and returns vetted candidates with their KiCad symbol/footprint resolved, prices, and stock — closing the loop without a human relay.

It is the highest near-term impact item for both Brian and external users.

## Non-goals

- **Not a price tracker** — current price + stock are returned at query time; we don't build a price history.
- **Not a BOM tool** — single-component lookups; no multi-part assembly/cost optimization.
- **Not DigiKey in v1** — the `ResolvedPart` shape accommodates DigiKey but the v1 surface and implementation are LCSC-only.
- **Not a substitute suggester** — given part X, we don't suggest alternates. (Spec a new search to find candidates.)
- **Not a circuit reasoning engine** — we don't infer "you probably need a buck converter, not an LDO." The LLM provides the functional spec.
- **Not bundling the jlcpcb-parts SQLite into the kicad-mcp repo** — see Dependencies and Legal posture below.

## Dependencies

This SPEC depends on:

- **`mcp-events` package** (per [`SPEC_OOB_Events.md`](SPEC_OOB_Events.md), **implemented 2026-05-27** at `/Volumes/Files/claude/mcp-events/`, 47 tests passing) — for surfacing first-use ToS prompts, snapshot staleness warnings, and uncertain-match warnings to the calling LLM.
- **Feedback Infrastructure** (per [`SPEC_Feedback_Infrastructure.md`](SPEC_Feedback_Infrastructure.md)) — for persisting telemetry needed to tune the staleness thresholds and match scoring over time. Not a hard blocker at runtime: if feedback infra is absent, `record_call`/`record_warning` calls degrade to no-ops via `try/except ImportError` stubs. Strongly preferred to land first for the calibration loop, but this tool can ship in advance if needed.
- **`library_index.db`** (already in kicad-mcp) — for resolving manufacturer part numbers to KiCad symbol `lib_id`s.

**PyPI publication blocker.** kicad-mcp's CI uses `uv sync --frozen`, which fails on local-path-only dependencies. This SPEC's PR cannot merge until `mcp-events 0.1.0` is published to PyPI. The implementation can begin with a local-path source for dev, but coordinate the PyPI publication of `mcp-events` BEFORE opening the PR for review.

Optional but recommended:
- **`yaqwsx/jlcparts` upstream availability** — we depend on their CI continuing to publish the SQLite snapshot. If they go down, we degrade to "last cached snapshot" mode and surface a strong warning. Escape hatch (own scraping) documented but not in v1.

## Data sources

The investigation in this session (see memory: `project_component_intelligence.md`) revealed that the originally-assumed `open-apis.lcsc.com` is dead. The real data landscape:

### 1. JLCPCB cart API (unofficial, no auth required)
- `GET https://cart.jlcpcb.com/shoppingCart/smtGood/getComponentDetail?componentCode=Cxxxx` — per-part details
- `POST https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList/v2` — keyword search
- **Critical**: requires a browser User-Agent header (their CDN blocks `python-requests` defaults)
- No documented rate limit; we self-throttle to ~1 req/sec
- Used for **on-demand fresh data** (current stock, current price) when the snapshot would be too stale

### 2. yaqwsx/jlcparts SQLite snapshot (community-maintained)
- Downloaded from `https://bouni.github.io/jlcparts/data/` (Bouni's GitHub Pages hosting the processed SQLite, ~100-500 MB across split zip chunks)
- Schema: `components(lcsc, category_id, mfr, package, joints, basic, preferred, description, datasheet, stock, price, last_update, extra, jlc_extra)` — see `project_component_intelligence.md` for full field reference
- Updated 3×/day by yaqwsx's CI (which has the LCSC partner-API credentials we don't)
- Used for **bulk search/filter** (parametric queries across the catalog)

**`basic`/`preferred` → `assembly_tier` mapping:** `basic=True` → `"basic"`; `preferred=True` (and `basic=False`) → `"preferred"`; both `False` → `"extended"`. If both are `True` (data error), treat as `"basic"` (more restrictive). This mapping must be a single canonical function; do not inline it at multiple call sites.

**Multi-chunk download:** The snapshot is split across numbered zip files (`.001`, `.002`, ...). The download algorithm must: (1) fetch a manifest or probe for chunks sequentially until a 404, (2) concatenate and extract the SQLite. The exact chunk-discovery strategy must be verified against the live hosting before implementation — check whether a manifest file exists at the base URL or whether sequential probing is the only option.

**Schema drift risk:** jlcparts is a community project and has changed its schema before. On first use after download, validate that all expected column names are present. If a column is missing, return `status: "error"` with `code: "jlcparts_schema_mismatch"` rather than failing silently at query time.

### 3. kicad-mcp `library_index.db` (existing)
- Full-text search over KiCad's bundled symbol libraries
- Used to resolve manufacturer part number → KiCad `lib_id` (e.g., `"AMS1117-3.3"` → `"Regulator_Linear:AMS1117-3.3"`)
- **Lookup algorithm (open question — see Open Questions #6):** The query strategy (exact-match vs. FTS, column searched, disambiguation on multiple hits) must be decided before implementation. First-match-wins on duplicates is a Rule 3 violation if not pinned by a test.

### 4. Package → KiCad footprint mapping table (new, hand-curated)
- Static lookup: LCSC's `componentSpecificationEn` strings ("SOT-223", "SOIC-8", "0603") → KiCad footprint paths (`"Package_TO_SOT_SMD:SOT-223-3"`, `"Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"`, `"Resistor_SMD:R_0603_1608Metric"`)
- ~200 entries covers ~95% of JLCPCB's catalog
- Lives in the repo as a YAML or Python dict; updated by hand as new packages are observed
- Unmapped packages return `kicad_footprint_path=None` (caller can still proceed; manual footprint assignment needed)

**Pre-implementation step (required):** Before writing the YAML, query a live snapshot for the 50 most-common `package` column values and verify the strings match the YAML keys. jlcparts may normalize differently from LCSC's API (`componentSpecificationEn` used in the live API vs. the `package` column in the snapshot). Case-sensitivity and trailing variant letters (e.g., `"SOT-23-5L"` vs. `"SOT-23-5"`) have caused 5–10% mapping failures in similar projects. The 95% coverage claim depends on this verification.

### Legal posture (must read before implementation)

LCSC's API terms of service explicitly prohibit redistribution of catalog data. The jlcparts upstream operates under a private arrangement with LCSC that does NOT extend to downstream consumers. The ecosystem (Bouni, tscircuit, etc.) universally downloads at runtime rather than bundles.

This SPEC inherits that posture: **download at runtime with explicit user opt-in**, never bundle. See "First-use ToS acceptance flow" below.

## The `ResolvedPart` record

The shared data type returned by `lcsc(operation="resolve", ...)` and the items in `lcsc(operation="search", ...)` results. Designed to work for LCSC v1 and DigiKey v2 (and other future suppliers) without changes — fields that don't apply to a given supplier are `None`.

```python
@dataclass
class ResolvedPart:
    # Identity
    supplier_name: str              # "lcsc" | "digikey" (future)
    supplier_part_number: str       # "C6186" or "P5555-ND" (DigiKey future)
    mpn: str                        # manufacturer part number
    manufacturer: str               # "Advanced Monolithic Systems"

    # Description
    description: str                # short description; the supplier's text
    package: str                    # normalized package string, e.g. "SOT-223"
    pin_count: int | None           # solder pad count (from jlcparts.joints) if available

    # Parametric attributes
    attributes: dict[str, str]      # {"Output Voltage": "3.3V", "Output Current": "1A", ...}
                                    # Values are strings (not parsed/typed); caller interprets
                                    # NOTE: populated by parsing the jlcparts `extra` JSON blob.
                                    # Verify the actual JSON structure in a live snapshot before
                                    # coding the parser — this affects description-scoring quality.

    # Commercial
    price_tiers: list[dict]         # [{"qty_min": 1, "qty_max": 49, "unit_price_usd": 0.2022}, ...]
                                    # qty_max=None means "and above"
                                    # NOTE: the jlcparts `price` column raw format is unknown —
                                    # likely a JSON string or bracket-delimited list. Verify
                                    # in a live snapshot before writing the parser.
    stock: int                      # global stock at time of snapshot
    lifecycle: str                  # "active" | "nrnd" | "obsolete" | "unknown"
                                    # NOTE: jlcparts has no lifecycle column; source is TBD —
                                    # likely parsed from jlc_extra JSON or the description field.
                                    # Verify before coding. Default to "unknown" if not resolvable.

    # LCSC/JLCPCB-specific
    assembly_tier: str | None       # "basic" | "extended" | "preferred" — None for non-JLCPCB

    # KiCad resolution
    kicad_symbol_lib_id: str | None      # "Regulator_Linear:AMS1117-3.3" if resolvable, else None
    kicad_footprint_path: str | None     # "Package_TO_SOT_SMD:SOT-223-3" if mapped, else None

    # Documentation
    datasheet_url: str | None       # if available

    # Match metadata (when returned from a search)
    match_score: float | None       # 0.0-1.0; None when returned from `resolve` (exact lookup)
    match_deviations: list[str]     # human-readable explanations of how this differs from query
                                    # Empty list = exact match. Example items:
                                    # "package: SOT-223 differs from requested SOT-23"
                                    # "assembly_tier: 'extended' differs from requested 'basic'"

    # Provenance
    snapshot_date: str              # ISO-8601 date of the jlcparts snapshot used
    fetched_live: bool              # True if any field was fetched from the live JLCPCB API
                                    # (rather than from the snapshot); affects freshness
```

JSON-serializable shape mirrors the dataclass. All fields present in every response (None where not applicable), so callers can rely on the shape.

## Tool surface

A single router `lcsc` with five operations: three primary (`search`, `resolve`, `assign`) and two administrative (`accept_tos`, `refresh_snapshot`).

### `lcsc(operation="search", ...)`

```python
lcsc(
    operation="search",
    description: str,                      # "3.3V LDO regulator"
    package: str | None = None,            # "SOT-23", "0402", etc.
    assembly_tier: str = "basic",          # "basic" | "extended" | "preferred" | "any"
    max_results: int = 3,                  # cap; default 3
    include_unresolvable: bool = False,    # if False (default), filter out results with no kicad_footprint_path mapping
    extra_filters: dict | None = None,     # optional parametric filters, e.g. {"voltage": "3.3V"}
)
-> {
    "status": "ok",
    "results": [ResolvedPart, ResolvedPart, ResolvedPart],  # ranked best-match first
    "events": [...],                       # populated when uncertain (per OOB envelope)
}
```

**Returns up to `max_results` ranked best-match-first.** If no exact matches exist, the closest matches are returned with `match_score < 1.0` AND a `warn`-level event in `events` saying "no exact match; returning closest candidates". Caller decides whether to accept or refine.

### `lcsc(operation="resolve", ...)`

```python
lcsc(
    operation="resolve",
    part_number: str,                      # "C6186"
)
-> {
    "status": "ok",
    "part": ResolvedPart,
    "events": [...],
}
```

Exact lookup by LCSC number. `match_score` is None (not applicable). Use this when the LLM has already chosen a specific candidate or knows the LCSC number from another source.

### `lcsc(operation="assign", ...)`

```python
lcsc(
    operation="assign",
    reference: str,                        # "U1"
    part_number: str,                      # "C6186"
    schematic_path: str | None = None,     # defaults to currently-loaded schematic
)
-> {
    "status": "ok",
    "applied": {
        "value": "AMS1117-3.3",            # set on the schematic component
        "lcsc_property": "C6186",
        "footprint": "Package_TO_SOT_SMD:SOT-223-3",  # or None if unmapped
        "symbol": "Regulator_Linear:AMS1117-3.3",     # or unchanged if symbol was already set
    },
    "events": [...],
}
```

Applies a resolved part to a schematic component:
- Sets the `Value` field to the MPN
- Sets a `LCSC` property to the part number (used by JLCPCB assembly BOM tooling)
- Sets the `Footprint` field if mapping exists
- Updates the symbol if currently unset or mismatched
- Emits a `warn` event if any field had to be skipped (e.g., footprint unmappable)

This is the **explicit confirmation step** — search/resolve are read-only; assign mutates the schematic.

**Integration note:** `assign` must mutate the KiCad schematic. The implementation should use the existing `kicad_sch_api` path (the same mechanism as `edit_label`, `bulk_update_components`). Specifically: load the schematic via the currently-loaded schematic handle, find the component by `reference`, then set its `Value` field, `Footprint` field (if mapped), and add/overwrite the `LCSC` custom property. Do not write a separate file-editing path — reuse the existing API.

### `lcsc(operation="accept_tos")`

```python
lcsc(
    operation="accept_tos",
)
-> {
    "status": "ok",
    "accepted_at": "2026-05-27T15:32:11Z",   # ISO-8601 timestamp written to acceptance file
}
```

Writes `~/.cache/kicad-mcp/lcsc_tos_acceptance.json`. Subsequent calls to `search`, `resolve`, `assign` proceed without the ToS error. Idempotent — calling again just overwrites the file with a new timestamp.

### `lcsc(operation="refresh_snapshot")`

```python
lcsc(
    operation="refresh_snapshot",
)
-> {
    "status": "ok",
    "previous_snapshot_date": "2026-05-10",  # ISO-8601 date from prior metadata, or None
    "new_snapshot_date": "2026-05-26",        # ISO-8601 date from freshly downloaded snapshot
    "size_bytes": 327145728,
    "events": [...],
}
```

Downloads and replaces the cached snapshot. Emits `lcsc_snapshot_unavailable` error if the download fails with no existing cache. If the download fails but a cache exists, that case is not applicable here — `refresh_snapshot` fails hard rather than silently falling back, because the user explicitly requested a refresh.

## Match scoring and ranking

The search algorithm scores each candidate against the query and returns the top N.

### Scoring criteria (with weights)

The score is computed as a weighted average of per-criterion match scores:

| Criterion | Weight | Match scoring |
|---|---|---|
| Description / functional match | 0.40 | Full-text relevance against `description` and `attributes`; 1.0 = all query keywords match, 0.0 = none |
| Package | 0.25 | Exact package string match = 1.0; close family (e.g., SOT-23 vs SOT-23-5) = 0.7; different = 0.0 |
| Assembly tier | 0.15 | Exact = 1.0; "extended" when "basic" requested = 0.5; "obsolete"/"unknown" = 0.0 |
| Stock availability | 0.10 | stock ≥ 1000 = 1.0; stock 100-999 = 0.7; stock 1-99 = 0.3; stock 0 = 0.0 |
| KiCad footprint resolvable | 0.10 | Has mapping = 1.0; no mapping = 0.5 (reduced; user can still proceed) |

Overall score = sum of (weight × per-criterion score). Score in [0, 1]. The configurable parameter is whether unresolvable-footprint candidates are returned at all (`include_unresolvable` arg, default False).

### Deviation reporting

For each returned candidate, `match_deviations` lists every criterion where the score was less than 1.0, in human-readable form. Examples:

- `"package: SOT-223 (requested SOT-23)"`
- `"assembly_tier: 'extended' (requested 'basic') — adds JLCPCB assembly fee"`
- `"stock: 47 (low; consider alternates)"`
- `"kicad_footprint_path: no mapping for 'XQFN-24'; assign manually"`

The LLM consumes these to decide whether to accept the top result, propose a different one to the user, or re-query with looser constraints.

### Search implementation strategy (open question — see Open Questions #7)

The `description` keyword scoring runs against a potentially million-row snapshot. Python-side scoring of every row will time out. The strategy is one of:

- **SQLite FTS5 virtual table** on `description` (pre-built at snapshot load time): fast, but requires an index-build step on first use.
- **Pre-filter then score**: use SQL `WHERE description LIKE '%keyword%' AND basic=1` (or similar) to narrow to hundreds of rows, then score in Python. Simpler, but LIKE scans are linear and the approach scales poorly with OR-queries.
- **Hybrid**: pre-filter by `assembly_tier`, `stock > 0`, and `package` (all indexed columns) before any text matching; score only the survivors.

`extra_filters` against the `extra` JSON blob column is a particular risk — `json_extract()` requires SQLite ≥ 3.38 and is not guaranteed on all platforms, and Python-side JSON parsing of every row is O(n). This must be resolved before implementation (see Open Questions #7).

### Why a list, not a single recommendation

A single recommendation makes the tool feel decisive but obscures the trade-offs the LLM should be reasoning about. With three candidates and their deviations, the LLM can articulate "I picked C6186 over C70569 because stock is 100x higher even though current rating is overspec'd" — that's the right level of judgment for the LLM to exercise.

## First-use ToS acceptance flow

The first time `lcsc(operation=...)` is called in an installation, the user must accept that downloading the JLCPCB component catalog uses LCSC's catalog data under terms they're responsible for. Specifics:

### Acceptance record

A small file at `~/.cache/kicad-mcp/lcsc_tos_acceptance.json`:

```json
{
  "accepted_at": "2026-05-27T15:32:11Z",
  "acceptance_hash": "<sha256 of the ToS notice text shown>",
  "snapshot_first_used": "2026-05-26"
}
```

### Acceptance prompt

On first call with no acceptance record, the tool:
- Does NOT download anything
- Returns `status: "error"` with `code: "lcsc_tos_acceptance_required"` and a `message` containing the full notice text:
  > "This will download a ~300 MB component-catalog snapshot from the community yaqwsx/jlcparts project. The snapshot contains data from LCSC's product catalog. LCSC's API terms of service prohibit redistribution of bulk catalog data; the snapshot is downloaded directly from the community source, not from this MCP server. By proceeding, you accept responsibility for compliance with LCSC's terms. To accept and continue, call `lcsc(operation='accept_tos')`. To decline, do not call further LCSC operations."
- The LLM relays this to the user and waits for explicit affirmation.
- The user (via the LLM) then calls `lcsc(operation="accept_tos")` which writes the acceptance file.

### Re-prompting

If the ToS notice text ever changes (different `acceptance_hash`), the next call surfaces the new notice and requires re-acceptance. Acceptance is per-install, not per-session.

### Bypassing acceptance (for tests / CI)

`KICAD_MCP_LCSC_TOS_ACCEPTED=1` environment variable skips the prompt (for CI / automated testing). Documented; not the recommended path for users.

## Snapshot management

The jlcparts SQLite is downloaded on first ToS-accepted call and cached at `~/.cache/kicad-mcp/jlcparts.db`. A small metadata file alongside tracks its provenance:

```json
{
  "downloaded_at": "2026-05-27T15:35:22Z",
  "source_url": "https://bouni.github.io/jlcparts/data/cache.sqlite3.zip.001",
  "snapshot_date": "2026-05-26",   // last_update column max from the data itself
  "size_bytes": 327145728
}
```

### Freshness thresholds

| Age | Behavior |
|---|---|
| 0-7 days | Quietly use snapshot; no event emitted |
| 8-14 days | Use snapshot; emit `info` event `lcsc_snapshot_aging` (not surfaced to LLM by default) |
| 15-30 days | Use snapshot; emit `warn` event `lcsc_snapshot_stale`; LLM sees in response `events` |
| 31+ days | Use snapshot; emit `warn` event `lcsc_snapshot_very_stale`; suggest refresh |

These thresholds are **initial guesses**. Telemetry records snapshot age at every call (in `calls.output_summary`). Once we have data on actual LCSC catalog churn rate, we tune.

### Refresh

The user (via the LLM) explicitly refreshes by calling `lcsc(operation="refresh_snapshot")`. This:
- Re-downloads the latest snapshot from the same source URL
- Replaces the cached file
- Updates the metadata file
- Emits an `info` event with the old and new snapshot dates

We do NOT auto-refresh on each call (network cost) or on each session (user surprise). The LLM can decide to refresh based on the staleness warning + user preference.

### Upstream unavailable

If the download fails (jlcparts CI offline, GitHub Pages down, network error):
- If a cached snapshot exists (any age): use it; emit a `warn` event `lcsc_snapshot_offline_fallback` with the age.
- If no cached snapshot exists: return `status: "error"` with `code: "lcsc_snapshot_unavailable"`; tool is unusable until network/upstream restored.

## Package → KiCad footprint mapping

A static lookup file at `src/kicad_mcp/data/lcsc_footprint_mapping.yaml`:

```yaml
"SOT-23":        "Package_TO_SOT_SMD:SOT-23"
"SOT-23-3":      "Package_TO_SOT_SMD:SOT-23"
"SOT-23-5":      "Package_TO_SOT_SMD:SOT-23-5"
"SOT-23-6":      "Package_TO_SOT_SMD:SOT-23-6"
"SOT-223":       "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
"SOT-223-3":     "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
"SOIC-8":        "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
"SOIC-14":       "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm"
"0402":          "Resistor_SMD:R_0402_1005Metric"     # also used for caps; package alone doesn't disambiguate
"0603":          "Resistor_SMD:R_0603_1608Metric"
# ... ~200 entries total ...
```

Caveats:
- **Resistor vs capacitor at the same package size**: the file maps to the resistor variant by default. The `lcsc` tool inspects the part's category (resistor / capacitor / inductor) before picking the appropriate footprint family.
- **Hand-soldering vs reflow variants**: KiCad libraries often have multiple footprints for the same package (e.g., `SOIC-8_3.9x4.9mm_P1.27mm` vs `SOIC-8_3.9x4.9mm_P1.27mm_HandSoldering`). The default is the reflow variant; assembly-aware projects can override.

This file is hand-maintained. When a part comes back with an unmapped package, the tool emits a `warn` event noting the unmapped package; over time the maintenance pattern is "see the warnings, add entries."

## Telemetry & event surfacing

This tool is a heavy producer of both telemetry data (for tuning) and OOB events (for surfacing to the LLM). The integration:

### Telemetry calls per operation

For each `lcsc(operation="search"|"resolve"|"assign", ...)` call:
- `record_call` with `tool_name="lcsc.{operation}"`, `output_summary` including `result_count`, `top_match_score`, `snapshot_age_days`, `fetched_live` flag
- `record_warning` for any uncertain-match (low score) or unresolvable-footprint cases

### OOB events surfaced to the calling LLM

| Event code | Severity | When |
|---|---|---|
| `lcsc_tos_acceptance_required` | `error` | First call, no acceptance record |
| `lcsc_snapshot_aging` | `info` | Snapshot 8-14 days old (not surfaced by default) |
| `lcsc_snapshot_stale` | `warn` | Snapshot 15-30 days old |
| `lcsc_snapshot_very_stale` | `warn` | Snapshot 31+ days old |
| `lcsc_snapshot_offline_fallback` | `warn` | Network/upstream unavailable; using cached |
| `lcsc_snapshot_unavailable` | `error` | No cache + offline; tool unusable |
| `lcsc_no_exact_match` | `warn` | Search returned only sub-1.0 match_score candidates |
| `lcsc_footprint_unmapped` | `warn` | Result has no kicad_footprint_path; assign-time |
| `lcsc_assembly_tier_downgrade` | `info` | Best match is in a more expensive tier than requested |

### Future analyze queries (Feedback Infrastructure)

Not in v1, but documented as targets for the calibration loop:
- Distribution of `snapshot_age_days` across calls — tunes the freshness thresholds
- Distribution of `top_match_score` for successful workflows vs. abandoned ones — tunes scoring weights
- Most-frequently-emitted unmapped-package codes — drives footprint-mapping additions

## v1 scope

Ship:
- `lcsc` router with operations `search`, `resolve`, `assign`, `accept_tos`, `refresh_snapshot`
- `ResolvedPart` dataclass + JSON serialization
- First-use ToS acceptance flow (file at `~/.cache/kicad-mcp/lcsc_tos_acceptance.json`)
- jlcparts snapshot download + metadata tracking
- Freshness thresholds (four tiers: quiet ≤7d, info 8–14d, warn 15–30d, strong warn 31+d) with `mcp-events` emissions
- Static package → footprint mapping (~200 entries; see `lcsc_footprint_mapping.yaml`)
- Match scoring per the criteria/weights table above
- Telemetry integration: every call records to `calls`; events persist via OOB
- Tests: unit tests for ranking, scoring, package mapping; integration tests with a small synthetic jlcparts SQLite fixture
- `KICAD_MCP_LCSC_TOS_ACCEPTED=1` env var for CI/testing

### Tool count after this PR

13 → 14 (`lcsc` router).

### Test count target

Approximately +50 tests. The match-scoring algorithm needs thorough boundary coverage per project's threshold-testing rule.

### Estimated wall-clock to ship

5-7 days of pairing-mode work (per the original memory estimate of "~1 week" for component intelligence). Slightly longer than the original estimate because the ToS flow and snapshot management were not in the original scope.

## Deferred from v1

- **DigiKey supplier integration.** Separate router (`digikey`) sharing only the `ResolvedPart` type. Planned for soon-after; spec to be written separately when prioritized.
- **Auto-best-pick mode.** A `lcsc(operation="search_one", ...)` that returns just the top result without the list. Aggressive default, can be added once base usage patterns are observed.
- **Reverse MPN → LCSC lookup.** Given a manufacturer part number, find its LCSC equivalent. Useful for "I designed against this part, now find a JLCPCB-stocked equivalent" — a real workflow but not v1.
- **Substitution suggestions.** Given a part, suggest functionally-equivalent alternates. Requires deeper attribute analysis than v1 can support.
- **BOM-level operations.** Multi-part workflows (cost optimization, basic-tier maximization across a BOM, etc.). Each component selected one at a time in v1.
- **Smart cache eviction.** v1 keeps the cached snapshot indefinitely; manual refresh only. Auto-eviction at age 90+ days is a v2 nice-to-have.
- **Multi-snapshot history.** v1 holds one snapshot; v2 could keep history for "compare prices across snapshots" workflows.
- **`refresh_snapshot` progress streaming.** v1 downloads silently and reports success/failure at end. v2 could stream progress via `mcp-events` for the user experience.

## Open questions (proposed answers — confirm during implementation)

1. **Should `lcsc(operation="assign", ...)` automatically resolve part_number, or require the caller to have already called `resolve` first?** Proposal: it resolves internally (single round-trip from the LLM's view). The downside is the LLM might be surprised by what `resolve` would have returned. Surface a `Resolved` summary in the `assign` response.
2. **What happens if `assign` is called for a reference that doesn't exist in the schematic?** Proposal: `status: "error"` with `code: "reference_not_found"`. Don't silently no-op.
3. **What happens if `assign` is called for a part whose `kicad_symbol_lib_id` is None?** Proposal: still proceed with `Value` and `LCSC` property; skip the symbol; emit a `warn` event. Assign is a "do as much as you can" operation.
4. **Should `search` honor a `min_score` threshold to filter out very-poor matches?** Proposal: not in v1. Return up to `max_results` regardless of score; let the caller filter. The `match_score` is in the response for caller decisions.
5. **What's the maximum `description` length the search engine handles cleanly?** Proposal: 256 chars; truncate longer queries with a `warn` event. Forces concise spec, avoids confusion when an LLM accidentally pastes a paragraph.
6. **`library_index.db` symbol lookup — exact match or FTS, and disambiguation?** The existing `library_index.db` was designed for interactive symbol search (`library(operation="search", ...)`). Using it for MPN → `lib_id` resolution may require a different query path (exact-match on the symbol name column rather than FTS keyword search, which would return too many false positives). If multiple library entries match the same MPN (e.g., `AMS1117-3.3` appears in both `Regulator_Linear` and a vendor-specific library), the disambiguation rule must be pinned. Proposal: exact-match the FTS index on the symbol `name` field; if multiple hits, prefer the canonical KiCad library (non-vendor-specific). Requires verification against the actual `library_index.db` schema.
7. **Search performance strategy — SQLite FTS5 or hybrid pre-filter?** Options: (a) build an FTS5 virtual table over `description` at snapshot load time and use it for keyword matching; (b) pre-filter by `assembly_tier`, `package`, and `stock > 0` using indexed columns, then score in Python; (c) hybrid. `extra_filters` against the JSON `extra` blob is the hard case — propose either: ban it in v1 (return `status: "error"` with `code: "extra_filters_not_supported"`), or restrict to the small set of keys that jlcparts exposes as top-level columns. Proposal: defer `extra_filters` to v2; remove the parameter from v1 to keep the search contract clean.
8. **When does the live JLCPCB API get called?** The spec mentions the live API for "on-demand fresh data when the snapshot would be too stale" but gives no trigger rule. Proposal: the live API is called ONLY for `resolve(part_number=...)` when the LCSC number is not found in the snapshot (i.e., a very new part). It is NOT used for search or to refresh stock/price for snapshot-found parts. `fetched_live=True` signals this to callers. This avoids rate-limit risk during search operations.

## Testing strategy

Tests live in `tests/test_lcsc.py`. No network access required for tests; no real jlcparts download.

### Synthetic snapshot fixture

A small (~100-row) SQLite file at `tests/fixtures/jlcparts_synthetic.sqlite3` matching the production schema. Covers:
- Multiple parts at the same package (for ranking-by-stock tests)
- Multiple parts at adjacent assembly tiers (for tier-downgrade test)
- Parts with and without resolvable footprints
- Parts with and without datasheet URLs
- Parts in each lifecycle state

### Test cases

**Search ranking and scoring:**
- Exact match (all criteria match) → match_score = 1.0
- Close match (one criterion off) → 0.5 < match_score < 1.0
- Far match (multiple criteria off) → match_score < 0.5
- Package family fallback (SOT-23 → SOT-23-5) → 0.7 score on package criterion
- Unmapped footprint → 0.5 score on footprint criterion when `include_unresolvable=True`
- Default `include_unresolvable=False` filters out unmapped-footprint candidates
- `max_results` honored
- Ranking is stable (same query → same order)

**ToS flow:**
- First call without acceptance file → returns `lcsc_tos_acceptance_required` error
- `accept_tos` creates the file
- After acceptance, subsequent calls succeed
- Changing the embedded ToS text changes the acceptance_hash; old acceptance becomes invalid
- `KICAD_MCP_LCSC_TOS_ACCEPTED=1` bypasses prompt

**Freshness:**
- Snapshot ≤7 days: no event emitted
- Snapshot 8-14 days: `info` event (not in default response envelope)
- Snapshot 15-30 days: `warn` event in response envelope
- Snapshot 31+ days: `warn` (different code) in response envelope
- Boundary: exactly 14 days → still `info`; exactly 15 days → `warn`
- Boundary: exactly 30 days → still `warn` (`lcsc_snapshot_stale`); exactly 31 days → `warn` (`lcsc_snapshot_very_stale`)

**Snapshot download:**
- Refresh fetches; metadata updated
- Download failure with cached snapshot → fallback + `warn`
- Download failure without cached snapshot → `error`

**Match deviations:**
- Returned candidate with wrong package has `"package: ..."` in deviations
- Tier downgrade adds appropriate deviation
- Exact match has empty deviations list

**Assign operation:**
- Sets Value, LCSC property, Footprint when all available
- Skips Footprint when unmapped; emits `warn`
- Errors when reference doesn't exist in schematic

**Boundary tests (per project CLAUDE.md threshold-testing rule):**
- `match_score=1.0` is achievable; `match_score=0.0` is achievable
- `stock=999` vs `stock=1000` boundary in stock scoring criterion
- `assembly_tier="any"` matches all tiers; `"basic"` matches only basic
- Empty result list when no candidates pass `include_unresolvable=False` filter (returns ok status, empty results, `warn` event)

### Integration tests (KiCad required, marked appropriately)

- `lcsc(operation="assign", ...)` on a real schematic: file gets the expected property updates; no DRC violations introduced
- Round-trip: search → resolve → assign → re-load schematic → fields match

## Hand-off summary

For an implementer picking this up cold:

1. Read this SPEC end-to-end.
2. Read the design-philosophy feedback memories: `feedback_context_frugality.md`, `feedback_synchronous_at_call_boundary.md`, `feedback_prefer_packaging_over_vendoring.md`.
3. **Verify the `mcp-events` package exists** at `/Volumes/Files/claude/mcp-events/` and is installed. If not, this PR is blocked — `mcp-events` must be on PyPI before the PR can merge (CI uses `uv sync --frozen`).
4. **Verify the Feedback Infrastructure is merged to main.** If not merged: `record_call` and `record_warning` are unavailable. Use no-op stubs — `def record_call(*args, **kwargs): pass` — guarded by a `try/except ImportError`. Do not add a runtime `if feedback_available:` flag; just let the import fail gracefully.
5. **Before writing any data-layer code:** download a live jlcparts snapshot and verify: (a) column names match the schema above, (b) `price` column raw format, (c) `extra` JSON structure, (d) top-50 `package` values for mapping YAML seeding, (e) whether `lifecycle` data is in `jlc_extra` or absent. Budget 30 minutes for this — it will save days of debugging.
6. **Resolve Open Questions 6–8** (library lookup, search performance, live API trigger) before writing implementation code. See the section above.
7. Implement against the v1 scope above. Defer everything in "Deferred from v1."
8. The test suite is the acceptance criterion. Unit tests run in the existing `uv run pytest` invocation with no KiCad installation required (using the synthetic SQLite fixture). Integration tests require KiCad and are marked accordingly.
9. Hand-curate the initial `lcsc_footprint_mapping.yaml` from the top-50 package values found in step 5, plus common additions. Consult `project_component_intelligence.md` for context.
10. **Tool count 4-file lockstep:** adding this tool requires updating `tests/test_server.py`, `README.md`, `AGENT-INSTALL.md`, and `TOOLS.md` to reflect 14 tools. The `check-docs` CI job validates all four match. Do all four or the CI will fail.
11. Update `MEMORY.md` and `project_component_intelligence.md` to mark this as **implemented** when the PR merges; note any deviations from this spec.

Tool count after this PR: 13 → 14. Test count target: ~+50 tests.

Implementation order context:

```
✅ OOB Events Subsystem       (per SPEC_OOB_Events.md — landed first)
✅ Feedback Infrastructure    (per SPEC_Feedback_Infrastructure.md — landed second)
→  Component Intelligence     (this SPEC — landed third)
✗  Schematic Auto-Placement   (spec still in design; lands fourth, uses all of the above)
```
