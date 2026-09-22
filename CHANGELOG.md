# Changelog

All notable changes to kicad-mcp are documented here.

## [0.17.0] — 2026-09-22

Makes AGENT-INSTRUCTIONS.md's "no concurrent PCB writes" rule actually
enforced within one server process, instead of caller-enforced discipline
only — plus an honest, documented account of what it still doesn't cover.

### Added

- **`utils/pcb_lock.py`**: a per-resolved-path, in-process, non-blocking
  lock. A second mutating call against a PCB path another call already
  holds returns `{"status": "error", "error": "...already in progress..."}`
  immediately — it never blocks and waits, since a queued autoroute pass
  can legitimately hold the lock for up to 30 minutes and a caller
  silently hanging that long is worse than an immediate, actionable error.
  Wired into every operation AGENT-INSTRUCTIONS.md already documented as
  needing serialization: `pcb.py`'s 21 mutating operations (of 29 —
  read-only operations deliberately never acquire it, so they stay safe
  to run in parallel per that same document), `autoroute(run|start)`
  (including the async background-worker path), `drc(autofix)`,
  `audit(auto_fix_placement)`, `panelize_pcb`, and
  `build_pcb_from_schematic`.
- Documented, not silently assumed away: this is genuinely an in-process
  fix, not a general one. It does not cover two separate kicad-mcp server
  processes (e.g. two agent sessions, each with their own
  stdio-connected server) writing the same file — a lock held in one
  process's memory is invisible to another process, and real
  cross-process file locking would need OS-level primitives
  (flock/fcntl/LockFileEx — three different models) that don't even agree
  with each other over NFS/SMB, and don't apply at all to a board in a
  Dropbox/OneDrive/iCloud-synced folder. Nor does it cover
  `schematic(...)` operations, which mutate an in-memory module-level
  object, not a file, until `save()` — a different concurrency model
  entirely. See `utils/pcb_lock.py`'s module docstring, the updated
  `no-concurrent-pcb-writes` CRITICAL note in `usage_guidance.py` (synced
  into AGENT-INSTRUCTIONS.md/AGENT-INSTALL.md), and a new AGENTS.md
  section for contributors adding future mutating tools.

### Testing

- `tests/test_pcb_lock.py`: the lock primitive itself — uncontended
  acquire, release on both normal exit and exception, non-blocking
  contention (asserted to return in under 0.5s, not wait), realpath-based
  keying (relative vs. absolute path, and a symlink vs. its target, both
  correctly contend for the same lock), and genuine multi-threaded
  contention via a `threading.Barrier`.
- `tests/test_pcb_lock_wiring.py` and additions to `tests/test_new_tools.py`:
  end-to-end proof through the real tool dispatch — a mutating operation
  is rejected while another holds the lock on the same path, a read-only
  operation is NOT (serializing it would defeat the parallel-reads
  design), and the lock is released (not leaked) after a call completes
  regardless of whether that call's own business logic succeeded.

### Fixed

- **`pcb()` router let subprocess failures escape as raw exceptions
  instead of the documented `{"status": "ok"|"error"}` envelope.**
  `run_pcbnew_script` normalizes every KiCad-subprocess failure (missing
  KiCad Python, a script traceback, a timeout) to `RuntimeError` — that's
  its own documented contract — but none of `pcb.py`'s dispatch (nor any
  of the `_op_*` helpers in `pcb_board.py`, `pcb_footprints.py`, etc.) ever
  caught it, so it propagated straight out of the tool call. Caught this
  release's own CI: the new `test_path_validation_wiring.py` test exercises
  `pcb(operation="load")` against a real (if unavailable) KiCad Python and
  hit exactly this path. `_dispatch()` is now wrapped in a single
  `try/except RuntimeError` at the router's two call sites (read-only and
  the `pcb_write_lock`-guarded mutating path), converting it to
  `{"error": str(e)}`. Regression-tested in `test_pcb_board.py` (read path)
  and `test_pcb_footprints.py` (mutating/locked path).

Verified against `scripts/audit_testability.py` (no new violations) and
the full suite (2641 passed).

## [0.16.0] — 2026-09-22

**Note:** this version was never tagged or released — `pyproject.toml` was
bumped and this section written, but work continued straight into 0.17.0
before the tag+release step happened. `v0.15.0` is the last version that
actually shipped as a GitHub Release; this section is kept for the
historical record of what changed, folded into the 0.17.0 release below.

Response-envelope standardization (breaking), an autoroute/drc-fix
data-loss fix, tool annotations, pagination, and path-validation wiring —
findings from an mcp-builder audit of this server, addressed in order of
risk.

### Breaking

- **Every tool's success/error envelope now uses `{"status": "ok"|"error",
  ...}`** — the majority convention already used by `library(search)`,
  `lcsc`, and most of the codebase. Twelve functions previously used
  `{"success": true|false, ...}` instead: `analyze(operation="netlist"
  |"connections"|"circuit_patterns"|"project_patterns"|"bom")`,
  `schematic(operation="find_component_connections")`,
  `drc(operation="run"|"history")`, `export(operation="bom_csv")`,
  `project(operation="open"|"validate")`. Any caller checking
  `result["success"]` on these operations needs to check
  `result["status"] == "ok"` instead.
  `project(operation="validate")` also gained a genuinely new field in the
  process: `success` there previously conflated "did the call complete"
  with "did validation find zero issues" — a project missing its PCB file
  was reported as `success: false`, indistinguishable from a real error.
  It's now `{"status": "ok", "valid": false, ...}` — the call succeeded,
  the project just isn't complete.
- **`project(operation="list")`** now returns `{"status", "projects",
  "count", "total", "truncated"}` instead of a bare list (see 0.15.0's
  pagination work below — this was already noted there but is called out
  again here since it's part of the same breaking-change surface).

### Fixed

- **`pcb_autoroute.py`'s `_export_dsn`** removed copper zones and saved
  `pcb_path` to disk in step 1 of the autoroute pipeline, before
  FreeRouter ever ran. If every pass then failed, that save had already
  happened with no rollback and no mention of it in the error response.
  Zone removal is now applied and persisted only by `_import_ses`, the
  sole remaining write point, reached only once a route actually exists
  to justify it — a total FreeRouter failure now leaves `pcb_path`
  completely untouched, and both failure paths say so explicitly.
- **`pcb_drc_fix.py`'s `_op_autofix`** clears all existing tracks/vias
  (required so autoroute has a clean board to route against) and then
  calls `_run_full_autoroute` with no check on the result — a failure
  there was folded into an optimistic-looking `"routing: cleared N
  tracks/vias, re-autorouted (2 passes, ? unconnected)"` action-log entry,
  indistinguishable from a real, if incomplete, success. The board was
  actually left with no routing at all. Now reports the regression
  plainly and returns `status: "warning"` with an explicit
  `routing_regressed` flag instead of `status: "ok"`.
- **`jmri_logs`-style path traversal gap**: `path_validation.py`'s
  `validate_project_path()` existed and was fully unit tested, but was
  only actually called from `project.py` — every PCB-mutating tool
  (`pcb_board`, `pcb_footprints`, `pcb_nets`, `pcb_routing`, `pcb_zones`,
  `pcb_silkscreen`, `pcb_keepout`, `pcb_autoroute`, `pcb_drc_fix`,
  `pcb_panelize`, `pcb_planning`, `export`) gated only on a bare
  `os.path.exists(pcb_path)`. Now wired into all 41 of those call sites.
  Behaviorally safe by default — `KICAD_MCP_STRICT_PATHS` stays warn-only
  unless already opted into strict mode.

### Added

- **MCP tool annotations** (`readOnlyHint`/`destructiveHint`/
  `idempotentHint`/`openWorldHint`) on all 18 registered tools — previously
  none had any, despite most routers mixing read-only and destructive
  operations under one tool name.
- **Pagination** on `project(operation="list")` (`limit`/`total`/
  `truncated`, default 50) and `analyze(operation="netlist")`
  (`components`/`nets` capped at `limit=100`, `component_count`/
  `net_count`/`analysis` always reflect the full, untruncated netlist).
  `analyze(operation="bom")` was flagged by the originating audit as
  needing the same treatment but turned out not to: the actual code only
  returns aggregate counts/categories, no raw per-component list exists to
  cap.

### Testing

- Added `tests/test_path_validation_wiring.py` — end-to-end proof that
  `KICAD_MCP_STRICT_PATHS=1` traversal rejection reaches through real tool
  calls (`pcb`/`autoroute`/`export`), not just `validate_project_path`'s
  own unit tests, plus a control test proving the same path is allowed
  when strict mode is off.
- Added boundary-threshold tests for the autoroute zone-removal fix
  (script-content assertions that `_export_dsn` never saves and
  `_import_ses` saves last) and for `_op_autofix`'s three routing
  outcomes (success, failure, FreeRouter unavailable).
- Closed 19 genuine test-coverage gaps found by an audit of this suite's
  177 loose `"error" in result`-style assertions — cases where the only
  assertion in a test wouldn't have caught a `status`/`success` envelope
  regression. The other ~90% of that raw count turned out to be either
  already covered by a stronger assertion nearby, or hitting an error
  branch with no status/success field to check in the first place (most
  of this codebase's error returns are bare `{"error": ...}` with nothing
  else to assert on).

## [0.15.0] — 2026-09-20

Onboarding-knowledge infrastructure, two pcbnew robustness fixes found via
cross-model testing, and a real (not just tool-count-based) client
compatibility claim.

### Added

- **`get_usage_guidance` tool + [mcp-agent-notes](https://github.com/blwfish/mcp-agent-notes) migration:** the
  `SERVER_INSTRUCTIONS` string and a hand-rolled static-JSON guidance tool
  from the previous pass are now both rendered from one authored `NOTES`
  tuple via the real `mcp-agent-notes` package — the generalized version of
  a mechanism this project and freecad-mcp had independently converged on.
  `get_usage_guidance` is now a proper query router
  (`operation="strategy"|"tactics"|"find"`) with topic-scoped detail and
  free-text symptom search, not a single static payload.
- **`docs/BOUNDARY_OPS.md`** — the previously-missing target of three
  existing references (`AGENTS.md`, `CONTRIBUTING.md`, a `pcb_keepout.py`
  comment). Full `*_HELPER`/thin-shell/exec-test pattern, grounded in the
  repo's actual `GEOMETRY_HELPER` exemplar, plus the no-splice-needed
  post-processing variant (`_truncate_violations`).
- **`scripts/lm_studio_probe.py`** — a standalone MCP client for smoke-testing
  this server's tool schemas and instructions against local, non-Claude
  models via LM Studio (see Testing below).
- **`scripts/sync_agent_notes_docs.py`** — generates `AGENT-INSTRUCTIONS.md`'s
  "Mandatory Rules" and `AGENT-INSTALL.md`'s "Critical Rules" sections from
  `NOTES`' `CRITICAL` entries, wired into `docs-check.yml`, so those two
  sections can no longer drift from what the server actually says.

### Fixed

- **`bulk_assign_pad_nets`** now validates assignment dict keys
  (`reference`/`pad`/`net`) before touching pcbnew, instead of surfacing a
  malformed key (e.g. `pad_number` instead of `pad`) as a bare `KeyError`
  wrapped in a confusing `wxApp`/`traits` subprocess assertion.
- **Every `pcbnew.LoadBoard()` call site** (51 across 12 tool modules) now
  checks for `None` — its only documented failure mode — instead of
  crashing downstream with `AttributeError: 'NoneType' object has no
  attribute 'GetFootprints'`. Root cause of the triggering case (a
  hallucinated `"<new_pcb_path>"` path argument) turned out to be the
  missing `.kicad_pcb` extension, not the bracket characters first
  suspected — `pcbnew.LoadBoard()` selects its IO plugin by extension
  internally.
- **`mcp-agent-notes` dependency resolution under plain `pip`:** the
  package isn't on PyPI yet; the original `[tool.uv.sources]` pin only
  worked for `uv sync`, silently breaking `pip install -e .` (which
  `docs-check.yml` and `check-upstream-releases.yml` both use) — caught by
  this release's own PR CI, not before. Switched to a PEP 508 direct
  reference plus the required `tool.hatch.metadata.allow-direct-references`
  opt-in, verified this time with an actual `pip install -e .` in a clean
  venv.
- **`requires_kicad` test marker:** its Linux availability check only
  verified `/usr/bin/python3` *exists* — true on every Linux box, KiCad or
  not — so a `requires_kicad`-marked test with no other guard would run
  (and fail with `ModuleNotFoundError`) on CI instead of skipping. Latent
  bug predating this release; every prior `requires_kicad` test happened to
  also depend on a maintainer-only fixture file that masked it. Caught by
  this release's own PR CI, on the first `requires_kicad` test without that
  incidental mask. Replaced with an actual `import pcbnew` subprocess check.

### Testing

- Smoke-tested critical-rule adherence and tool-schema usability against
  three local, non-Claude models via LM Studio (`qwen2.5-coder-14b`,
  `qwen3-32b`, `gemma-4-e4b`), using a real MCP client (not LM Studio's own
  MCP handling, which turned out to need a GUI-granted permission its API
  refuses by default). Every model, including the smallest (4B), correctly
  reached for `autoroute(operation="run")` over manual routing and
  `library(operation="search")` over guessing a symbol/footprint name.
  `get_usage_guidance` was not spontaneously called by any model — the
  existing in-response escalation notes on `audit(operation="placement")`
  and `pcb(operation="add_trace"/"add_via")` remain the more load-bearing
  channel for a model that hasn't read the docs.

## [0.14.0] — 2026-09-14

Maintenance release — dependency updates and CI hardening, no feature work.

### Fixed

- **CI:** the self-hosted KiCad integration suite had been failing silently
  for over a month (last green run 2026-08-07) — `integration/kicad-10.0`
  isn't a required branch-protection status check, so PRs (including
  Dependabot auto-merges) kept landing through it unnoticed. Added a weekly
  schedule + `workflow_dispatch`, plus a notify-on-failure job that files or
  comments on a tracking issue so drift surfaces even during quiet periods.
  Root cause of the actual break was a missing FreeRouter jar on the
  runners' shared HOME, fixed out-of-band; this change is about making the
  next one visible.

### Changed

- Dependency bumps: `fastmcp` 3.4.7 → 4.0.3 (major), `authlib` 1.7.2 →
  1.8.0, `filelock`, `ruff`, `hypothesis`, `mypy`.

## [0.13.0] — 2026-08-07

### Fixed

- **CI:** `check-upstream-releases.yml` hardcoded per-tool version baselines
  in the workflow itself, so they silently drifted from what was actually
  installed on the dev machine — either filing "update available" forever
  or going quiet by coincidence. Baseline moved to
  `.github/upstream-versions.env`, auto-synced by the local upstream-update
  launchd job whenever KiCad/FreeRouter gets upgraded (closes #117).
- **CI:** loading `upstream-versions.env` straight into `GITHUB_ENV` broke
  the file-command parser on the header comment block, failing the workflow
  outright before it could read `KICAD_VERSION`/`FREEROUTER_VERSION`.
- **library-index:** two KiCad installs (the integration matrix's
  kicad-9.0/kicad-10.0 jobs, running concurrently on one self-hosted
  runner) shared a single `library_index.db` with no lock and no
  per-install partitioning — one job's rebuild could be read mid-rebuild by
  the other, surfacing as empty search results for a query that had worked
  seconds earlier. Cache path is now keyed by a hash of the resolved
  library paths; rebuilds hold a `filelock.FileLock` for their duration.
- **test:** `add_label_to_pin`/`connect_pins_with_labels` draw a wire stub
  from pin to label, but the existing tests only checked the returned
  UUID/text, not that the wire actually exists — a label placed directly at
  a bare pin coordinate looked fully wired in the schematic view but
  produced zero connections under real KiCad ERC (surfaced by
  circuit-synth/mcp-kicad-sch-api#3).

### Note

Version jumps straight from 0.11.0 to 0.13.0 in this file: the `v0.12.0`
tag was cut against a commit where `pyproject.toml` still read `0.11.0` (a
mislabeling bug), so its entry below reflects the intended content of that
release rather than what the tag itself points at.

## [0.12.0] — 2026-07-26

A large release: the firmware front-end arc (Phases 1–3), MCU expansion,
device cards, and CI/dependency hygiene — roughly 90 commits since v0.11.0.

### Added — Firmware front end, Phases 1–3

- `.ino`/`const`/`constexpr` pin-declaration parsing, multi-tab Arduino
  sketch discovery, `board.yaml` `board_id` escape hatch (Phase 1a).
- `import_intent` + `intent_template` + gap-parity, and `validate_intent` as
  a trust gate for AI- or hand-authored intents (Phase 1b).
- Generalized MCU wiring core, proven on RP2040/Pico (Phase 2); first 5V
  MCU support — Arduino Nano v3 / ATmega328P — with full supply-rail
  generalization (Phase 2b); Arduino analog pins A0–A7 resolved by symbol
  pin name.
- Cross-version symbol resolution for KiCad 9/10 renames;
  `arduino_nano_esp32` no longer mis-resolves to classic WROOM-32E.
- Phase 3 corpus harness: a nightly fail-safe sweep over real firmware
  sketches (1,319 examples sampled — zero crashes, everything degrades to
  an honest gap) plus a dependency-metadata-driven device-card backlog
  ranker.

### Added — MCU + device cards

- 4 MCUs total: ESP32 family, RP2040 (Pico/Pico 2), Arduino Nano v3.
- New cards: rotary encoder, DHT22, servo, ICS-43434 (buildable I2S mic),
  MCP23017 `expander_terminals` (v2), and terminal-only off-board devices
  (v1).

### Fixed

- **Release gate:** `release.yml`'s `release` job only depended on the
  ubuntu unit-test matrix, not the self-hosted KiCad integration suite —
  v0.11.0 had published over a red integration run. `release` now needs
  both jobs.
- A cluster of cold-review-driven fixes across the firmware pipeline:
  silent-wrong-output holes across Phases 0/1a/1b/2/2b, analog-pin
  silent-drop/conflict, chip-identity guard + bus-family ambiguity,
  `mcu_pin_refs`' symmetric falsy-skip, and the config.h ambiguity warning.
- `generate_schematic` now reports `partial`/`error` instead of always
  `ok`; the generated schematic's paper size auto-fits the layout height
  and reports overflow instead of silently clipping.
- 5 weeks of dep-audit CVE fixes consolidated into one PR; default branch
  switched `dev` → `main`; `test_track_geometry_to_routed_pcb`'s routing
  bound re-baselined (2 → 6) to match the `audio_s3` precedent, fixing a
  CI flake.

### Docs

- Examples gallery added (audio-node, audio-remote schematics + PCB
  images); internal design/planning docs moved off `main` to the orphan
  `specs` branch.

## [0.11.0] — 2026-06-03

A large release: the tool-surface consolidation (below), an entire firmware
front end, a human-rational autoplacer, component intelligence, autoroute
hardening, and a release-readiness review pass. Net tool count is **17** — the
13 consolidated core routers/standalones plus the firmware/LCSC/placement domain
tools added this release.

### Added — Firmware front end (`design` router)

Turn an ESP32-style `config.h` pin map into a partial, routed board:
`import_firmware → expand_templates → generate_schematic`, consumed by
`build_pcb_from_schematic`. MCU auto-detected from `platformio.ini`. Everything
firmware can't know (power tree, decoupling, pull-ups, address straps,
connectors, parts) is emitted as an explicit **gap manifest** — never invented.

- **Part resolution (C1–C9):** binds each bus to the SPECIFIC part the firmware
  names (corpus + sibling source/docs), never silently substitutes. An ambiguous
  bus (>1 candidate) is disclosed and held unrealized; a `board.yaml`
  `bus_part_overrides` entry lets the user declare the part.
- **Placement locus (L1–L8):** `board.yaml` `placement:` declares per-bus locus
  (`remote` → field-wired screw terminal, `on_board_with_remote_io`, …) with
  per-pad silk legends so terminals are wireable by hand.
- `board.yaml` sidecar for firmware-blind facts (connectors, power source, board
  size, mounting holes); refuses silent part substitution (INMP441 EOL case).

### Added — Human-rational autoplacer

Topology-aware placement that lays out a board the way a person would, verified
by eyeballing real boards (`suggest_placement` / `schematic_layout` / the build
pipeline):

- Antenna-keepout overhang (module RF keepout hangs off the board edge, copper
  stays on-board); content-aware board sizing; WIRE_ENTRY-oriented terminals.
- Corner M3 mounting holes + per-hole no-copper keepouts; an approval gate
  (`build(approved=False)` returns a placement proposal + render before routing).
- Opt-in board re-fit, terminal centering, and **multi-edge terminal
  distribution** (spreads field terminals across 2–3 edges; ~−24% area on the
  audio-remote board).

### Added — Component intelligence (`lcsc` router)

LCSC/JLCPCB part search + footprint resolution backed by a local indexed DB,
with assembly-tier awareness.

### Changed — Autoroute hardening

- Rank passes by KiCad's measured ratsnest, not FreeRouter's log; report the
  measured unconnected count so the routing gate is honest.
- Run FreeRouter as a macOS background app (no Dock/focus steal); disable its
  analytics phone-home (air-gap).

### Fixed — Release-readiness pass (cold code + test review, board-regen gate)

- **Critical:** `schematic move_component` used `filter(reference=…)`, which
  silently returns ALL components → moved the wrong one and reported `ok`.
- **High:** touching-courtyard detection in keepout auto-fix; pre-route pad-gap
  drift → single-source `PAD_GAP_HELPER`; `set_design_rules` now actually writes
  through-hole/edge-clearance and stops clobbering the copper layer count;
  footprint-load failure made fatal (was silently building incomplete boards);
  zones/gerber failures can't discard a routed board; `drc autofix` checks
  FreeRouter availability BEFORE clearing routing; async autoroute runs the
  shared preflight; I2S mic net-name collision on a 2nd bus; netlist
  incompleteness forwarding; lcsc tier-attribute guards.
- **Medium:** oscillator double-classification; silk-vs-routing keyword order;
  fiducials validation; premature keepout event; mutable default; event-context
  early return; `from_dict` null coercion (+ top-level null-list TypeError);
  label unescaping; sqlite connection leaks; `suggest_cards` skipped list;
  config.h ambiguity warning; connector-edge cascade.
- **#7 LM2596** misclassified as a linear LDO (prefix-shadow: loose `LM\d{3}`
  matched inside `LM2596`) → anchored the pattern.
- **Board-regen gate (verified on real boards, KiCad 9 + 10):** mounting-hole
  keepout self-flagged its own NPTH pad and never kept the copper pour off the
  screw annulus → fixed; module thermal vias (0.2mm) tripped the default 0.3mm
  min-hole → create step now sets a 0.2mm min through-hole. All five golden
  boards regenerate, route fully (DRC `unconnected=0`), and carry zero must-fix
  DRC violations.
- Cross-agent operating instructions (`AGENT-INSTRUCTIONS.md` + server
  `instructions`) so non-Claude agents get the operating guide; corrected stale
  tool references/counts. Unit suite grown to **2143** with regression coverage
  pinning every fix above.

### Breaking — Tool Surface Consolidation (97 → 13 tools)

This release completes the domain-router consolidation described in
`docs/SPEC_Tool_Consolidation.md`. The 97 individual tools that existed in
v0.9.0 are replaced by 9 domain routers + 4 standalone tools = **13 tools
total**. There are no backwards-compatibility aliases — every call site must
be updated to the router form.

**Why:** MCP clients impose hard tool-count limits (Cursor: 40, Gemini: ~100).
System-prompt overhead also scales with tool count (~200–500 tokens per tool).
At 13 tools the server is well within every known client limit and the
per-turn token overhead is negligible.

#### Rename mapping — all 84 removed tools

Each old tool name maps to `router(operation="op_name", ...)`.

**Phase 1 — `library`, `analyze`, `export` routers**

| Old tool name | New call |
|---|---|
| `search` | `library(operation="search", ...)` |
| `rebuild_library_index` | `library(operation="rebuild_index")` |
| `analyze_schematic_connections` | `analyze(operation="connections", ...)` |
| `identify_circuit_patterns` | `analyze(operation="circuit_patterns", ...)` |
| `analyze_project_circuit_patterns` | `analyze(operation="project_patterns", ...)` |
| `analyze_bom` | `analyze(operation="bom", ...)` |
| `extract_netlist` | `analyze(operation="netlist", ...)` |
| `export_gerbers` | `export(operation="gerbers", ...)` |
| `export_bom_csv` | `export(operation="bom_csv", ...)` |
| `generate_pcb_thumbnail` | `export(operation="thumbnail", ...)` |

**Phase 2 — `project`, `drc`, `autoroute` routers**

| Old tool name | New call |
|---|---|
| `list_projects` | `project(operation="list")` |
| `open_project` | `project(operation="open", ...)` |
| `get_project_structure` | `project(operation="get_structure", ...)` |
| `validate_project` | `project(operation="validate", ...)` |
| `run_drc_check` | `drc(operation="run", ...)` |
| `drc_autofix` | `drc(operation="autofix", ...)` |
| `get_drc_history_tool` | `drc(operation="history", ...)` |
| `autoroute_pcb` | `autoroute(operation="run", ...)` |
| `autoroute_pcb_async` | `autoroute(operation="start", ...)` |
| `poll_autoroute` | `autoroute(operation="poll", ...)` |
| `cancel_autoroute` | `autoroute(operation="cancel", ...)` |
| `list_autoroute_jobs` | `autoroute(operation="list_jobs")` |

**Phase 3 — `audit` router**

| Old tool name | New call |
|---|---|
| `audit_all` | `audit(operation="all", ...)` |
| `audit_pcb_placement` | `audit(operation="placement", ...)` |
| `audit_footprint_overlaps` | `audit(operation="footprint_overlaps", ...)` |
| `check_pad_clearances` | `audit(operation="pad_clearances", ...)` |
| `validate_placement` | `audit(operation="validate_one", ...)` |
| `auto_fix_placement` | `audit(operation="auto_fix_placement", ...)` |
| `get_keepout_zones` | `audit(operation="keepouts", ...)` |
| `pre_route_check` | `audit(operation="pre_route_check", ...)` |

Note: `audit(operation="all", detail="full")` replaces the higher-detail output
that was previously only available from the individual standalone audit tools.

**Phase 4 — `pcb` router**

| Old tool name | New call |
|---|---|
| `create_pcb` | `pcb(operation="create", ...)` |
| `load_pcb` | `pcb(operation="load", ...)` |
| `finalize_pcb` | `pcb(operation="finalize", ...)` |
| `add_board_outline` | `pcb(operation="set_outline", ...)` |
| `set_design_rules` | `pcb(operation="set_design_rules", ...)` |
| `get_board_constraints` | `pcb(operation="get_constraints", ...)` |
| `place_footprint` | `pcb(operation="place_footprint", ...)` |
| `move_footprint` | `pcb(operation="move_footprint", ...)` |
| `list_pcb_footprints` | `pcb(operation="list_footprints", ...)` |
| `get_pad_positions` | `pcb(operation="get_pad_positions", ...)` |
| `get_footprint_dimensions` | `pcb(operation="get_footprint_dimensions", ...)` |
| `add_net` | `pcb(operation="add_net", ...)` |
| `rename_net` | `pcb(operation="rename_net", ...)` |
| `list_pcb_nets` | `pcb(operation="list_nets", ...)` |
| `set_net_class` | `pcb(operation="set_net_class", ...)` |
| `assign_pad_net` | `pcb(operation="assign_pad_net", ...)` |
| `bulk_assign_pad_nets` | `pcb(operation="bulk_assign_pad_nets", ...)` |
| `add_trace` | `pcb(operation="add_trace", ...)` |
| `add_via` | `pcb(operation="add_via", ...)` |
| `clear_routing` | `pcb(operation="clear_routing", ...)` |
| `edit_trace_width` | `pcb(operation="edit_trace_width", ...)` |
| `add_copper_zone` | `pcb(operation="add_zone", ...)` |
| `fill_zones` | `pcb(operation="fill_zones", ...)` |
| `add_text_to_pcb` | `pcb(operation="add_text", ...)` |
| `list_silkscreen_items` | `pcb(operation="list_silkscreen", ...)` |
| `update_silkscreen_item` | `pcb(operation="update_silkscreen", ...)` |
| `auto_fix_silkscreen` | `pcb(operation="auto_fix_silkscreen", ...)` |
| `check_silkscreen_overlaps` | `pcb(operation="check_silkscreen_overlaps", ...)` |

**Phase 5 — `schematic` router**

| Old tool name | New call |
|---|---|
| `create_schematic` | `schematic(operation="create", ...)` |
| `load_schematic` | `schematic(operation="load", ...)` |
| `save_schematic` | `schematic(operation="save", ...)` |
| `validate_schematic` | `schematic(operation="validate", ...)` |
| `get_schematic_info` | `schematic(operation="info", ...)` |
| `clone_schematic` | `schematic(operation="clone", ...)` |
| `backup_schematic` | `schematic(operation="backup", ...)` |
| `check_pin_collisions` | `schematic(operation="check_pin_collisions", ...)` |
| `add_component` | `schematic(operation="add_component", ...)` |
| `remove_component` | `schematic(operation="remove_component", ...)` |
| `move_component` | `schematic(operation="move_component", ...)` |
| `list_components` | `schematic(operation="list_components", ...)` |
| `filter_components` | `schematic(operation="filter_components", ...)` |
| `components_in_area` | `schematic(operation="components_in_area", ...)` |
| `bulk_update_components` | `schematic(operation="bulk_update_components", ...)` |
| `add_multi_unit_component` | `schematic(operation="add_multi_unit_component", ...)` |
| `get_component_pin_position` | `schematic(operation="get_component_pin_position", ...)` |
| `list_component_pins` | `schematic(operation="list_component_pins", ...)` |
| `find_component_connections` | `schematic(operation="find_component_connections", ...)` |
| `add_wire` | `schematic(operation="add_wire", ...)` |
| `remove_wire` | `schematic(operation="remove_wire", ...)` |
| `add_wire_between_pins` | `schematic(operation="add_wire_between_pins", ...)` |
| `add_junction` | `schematic(operation="add_junction", ...)` |
| `add_label` | `schematic(operation="add_label", ...)` |
| `remove_label` | `schematic(operation="remove_label", ...)` |
| `edit_label` | `schematic(operation="edit_label", ...)` |
| `add_label_to_pin` | `schematic(operation="add_label_to_pin", ...)` |
| `add_hierarchical_label` | `schematic(operation="add_hierarchical_label", ...)` |
| `connect_pins_with_labels` | `schematic(operation="connect_pins_with_labels", ...)` |
| `add_text` | `schematic(operation="add_text", ...)` |
| `add_text_box` | `schematic(operation="add_text_box", ...)` |
| `edit_text` | `schematic(operation="edit_text", ...)` |
| `add_sheet` | `schematic(operation="add_sheet", ...)` |
| `add_sheet_pin` | `schematic(operation="add_sheet_pin", ...)` |
| `add_net` (schematic context) | `schematic(operation="add_net", ...)` |

**Phase 6 — Cleanup**

- Deleted no-op stubs `register_pcb_drc_fix_tools` and `register_netlist_tools`.
- Fixed internal docstring references to the non-existent `update_pcb_from_schematic`
  (this tool was planned but never shipped; `build_pcb_from_schematic` covers the
  pipeline use case).
- Updated all workflow docs to router-style calls.

#### Standalone tools (unchanged names, still available)

| Tool | Notes |
|---|---|
| `build_pcb_from_schematic` | Top-level schematic → PCB pipeline |
| `panelize_pcb` | Manufacturing panelization |
| `estimate_board_size` | Pre-PCB planning aid |
| `suggest_placement` | Connectivity-based placement suggestions |

---

## [0.9.0] — 2026-05-26

First tagged release. Baseline for the consolidation that follows in 0.11.0.
97 tools covering the full KiCad design workflow.
