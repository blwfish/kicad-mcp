# SPEC: Multi-Version Regression Testing for KiCad MCP

## Problem Statement

The existing 479-test suite mocks `run_pcbnew_script`, so it validates MCP logic but not compatibility with the actual KiCad subprocess API. With KiCad 10.0 released and the daily-driver upgrade already done, we have no regression signal for:

1. Breakage in the pcbnew subprocess bridge between KiCad versions (API renames, behavior changes)
2. Library name drift (symbol/footprint lib_id changes between versions)
3. Output format changes in pcbnew Python scripts that the bridge parses as JSON
4. `kicad-cli` invocation differences (DRC, gerber export, BOM, netlist)

The `pcbnew_bridge.py` also hardcodes `/Applications/KiCad/KiCad.app`, so it cannot target a non-default install.

## Goals

- Run the full MCP test surface against multiple KiCad versions in CI on every PR
- Allow developers to install side-by-side KiCad versions for local regression testing without disturbing the daily-driver install at `/Applications/KiCad/KiCad.app`
- Keep the fast mocked suite as the per-commit gate; integration tier runs alongside it but does not replace it

## Non-Goals

- Replacing the existing mocked test suite (it stays as-is)
- Supporting KiCad versions older than 9.x
- Windows/Linux CI matrix in the first iteration (macOS only — add later)
- Testing every one of the 98 tools through real KiCad (smoke-test representatives per category is enough)

## Design

### Phase 1 — Parameterize the KiCad install path

**Files:**
- [src/kicad_mcp/utils/pcbnew_bridge.py](src/kicad_mcp/utils/pcbnew_bridge.py) — 2 hardcoded `/Applications/KiCad/KiCad.app` references (lines ~35, ~65)
- [src/kicad_mcp/utils/kicad_cli.py](src/kicad_mcp/utils/kicad_cli.py) — `_get_common_installation_paths()` has a hardcoded macOS path for `kicad-cli`
- [src/kicad_mcp/config.py](src/kicad_mcp/config.py) — `KICAD_CLI` constant has a hardcoded `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`

The right fix is a single shared resolver in `kicad_cli.py` and `pcbnew_bridge.py` that honors `KICAD_APP_PATH`, rather than patching each caller. `config.py`'s `KICAD_CLI` should delegate to `kicad_cli.py`'s resolver so there is one source of truth.

```python
def _get_kicad_app_path() -> str:
    return os.environ.get("KICAD_APP_PATH", "/Applications/KiCad/KiCad.app")
```

Both `_get_kicad_python()` and `_get_kicad_env()` in `pcbnew_bridge.py` use this resolver. `kicad_cli.py`'s macOS path and `config.py`'s `KICAD_CLI` use the same resolver. No other call sites should reference the path directly — grep for `/Applications/KiCad` after the change to confirm zero remaining hits.

**Acceptance:**
- `KICAD_APP_PATH=/Applications/KiCad/KiCad.app python -c "from kicad_mcp.utils.pcbnew_bridge import _get_kicad_python; print(_get_kicad_python())"` returns the expected python3 path
- `KICAD_APP_PATH=$HOME/kicad-versions/10.0/KiCad.app ...` returns a path under that directory
- `grep -r '/Applications/KiCad' src/` returns zero results
- Existing mocked tests still pass

### Phase 2 — Side-by-side install procedure (documented, manual)

**New file:** `docs/MULTI_VERSION_TESTING.md`

Document the install workflow:

```bash
# Download KiCad 9.0.x and 10.0.x .dmg installers from kicad.org/downloads/
mkdir -p $HOME/kicad-versions/9.0 $HOME/kicad-versions/10.0

# Mount each .dmg, copy KiCad.app to the version dir (do NOT drag to /Applications)
cp -R /Volumes/KiCad/KiCad.app $HOME/kicad-versions/9.0/

# Remove Gatekeeper quarantine (one-time, per install)
xattr -dr com.apple.quarantine $HOME/kicad-versions/9.0/KiCad.app

# Verify
KICAD_APP_PATH=$HOME/kicad-versions/9.0/KiCad.app uv run python -c \
    "from kicad_mcp.utils.pcbnew_bridge import _get_kicad_python; print(_get_kicad_python())"
```

Note explicitly that `/Applications/KiCad/KiCad.app` stays as the daily driver (currently 10.0) and is not touched by integration tests unless `KICAD_APP_PATH` is unset.

### Phase 3 — Integration test tier

**New file:** `tests/integration/test_kicad_versions.py`

Also add to `[tool.pytest.ini_options]` in `pyproject.toml`:

```toml
norecursedirs = ["tests/integration"]
```

This prevents `pytest` (no args) from collecting the integration module and printing skip noise. The CI job passes `tests/integration/` explicitly to override. This resolves the open question about separate pytest config — `norecursedirs` in the existing `pyproject.toml` is sufficient; no separate `pytest.ini` needed.

Gated by `KICAD_INTEGRATION=1` env var; pytest skips the entire module otherwise.

```python
import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real KiCad install",
)
```

**Coverage — one representative per category, no mocks:**

| Category | Tool | What it proves |
|---|---|---|
| Schematic create | `create_schematic` + `add_component` + `save_schematic` | kicad-sch-api compatibility |
| Library search | `search_components` + `search_footprints` | Library DB rebuild + lib_id stability across versions |
| PCB create | `create_pcb` + `add_board_outline` | pcbnew subprocess bridge basics |
| Footprint placement | `place_footprint` + `audit_all` | Pad geometry parsing |
| Schematic→PCB sync | `update_pcb_from_schematic` | Cross-tool integration |
| Routing | `autoroute_pcb` with `passes=1` | FreeRouter handshake + DSN export/import |
| DRC | `run_drc_check` | kicad-cli DRC output format |
| Export | `export_gerbers` + `export_bom_csv` | kicad-cli export output formats |

Each test uses a `tmp_path` workspace, creates real files, asserts on the JSON the tool returns (`status == "ok"` plus a few key fields). Avoid asserting on counts that may shift between versions (e.g. exact number of DRC violations on an empty board) — assert on structural keys instead.

`passes=1` for autoroute in CI keeps FreeRouter's nondeterminism from muddying the per-version signal. Nightly job (if added later) can bump to `passes=2+`.

**Expected runtime:** with FreeRouter v2.2.3's 10-30x speedup, the whole tier should run in <2 min per KiCad version. Cheap enough for every PR.

### Phase 4 — CI matrix

**New file:** `.github/workflows/integration.yml`

macOS runner, matrix over KiCad versions. KiCad installs aren't cached by default GitHub Actions; cache the `.dmg` download or use a self-hosted runner with pre-installed versions.

```yaml
strategy:
  matrix:
    kicad: ["9.0", "10.0"]
```

Steps:
1. Restore cached `~/kicad-versions/${{ matrix.kicad }}/KiCad.app` (or download+install if cache miss)
2. `xattr -dr com.apple.quarantine` the install
3. `uv sync`
4. `KICAD_INTEGRATION=1 KICAD_APP_PATH=~/kicad-versions/${{ matrix.kicad }}/KiCad.app uv run pytest tests/integration/ -v`

Mark the job `continue-on-error: false` for the daily-driver version (10.0) and `continue-on-error: true` for the trailing version (9.0) so a 9.0-only regression doesn't block PRs but is visible.

**Use a self-hosted macOS runner.** Keep both KiCad versions pre-installed at `$HOME/kicad-versions/{9.0,10.0}/KiCad.app` on the runner host. This skips the download/install/quarantine dance on every job and makes the per-PR runtime fast. GitHub-hosted macOS runners are slow, expensive, and require re-installing KiCad on every run — not viable for a per-PR gate.

## Verification

After all phases land:

1. `uv run pytest` (no env vars) — passes, 479 tests, ~9s, identical to today
2. `KICAD_INTEGRATION=1 uv run pytest tests/integration/` against `/Applications/KiCad/KiCad.app` (10.0) — passes
3. `KICAD_INTEGRATION=1 KICAD_APP_PATH=~/kicad-versions/9.0/KiCad.app uv run pytest tests/integration/` — passes (or surfaces real regressions)
4. Open a no-op PR — both matrix cells run, green
5. Open a PR that intentionally breaks the bridge — both cells go red

## Open Questions

- How do we handle KiCad library lib_id changes between 9 and 10? If `search_components(query="op amp")` returns a different top result, the integration test should assert on *structure* (a valid lib_id format) rather than specific lib_id strings. Document this rule in the test module's docstring. Same applies to `search_footprints` — assert the returned list is non-empty and each item has the expected schema fields (`lib_id`, `name`, `description`), not specific values.

## Implementer Notes

- The bridge change in Phase 1 is the only blocking dependency; Phases 2-4 can proceed in parallel once Phase 1 lands and a smoke test confirms the env var works
- Don't add backwards-compat shims for the old hardcoded path — `os.environ.get(..., default)` is the entire compat story
- Keep the integration tier separate from `tests/` — don't intermix with the mocked suite, so `pytest` defaults stay fast
