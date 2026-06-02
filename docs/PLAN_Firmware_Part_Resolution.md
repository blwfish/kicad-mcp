# Firmware Part-Resolution — Implementation Plan

Branch `feat/firmware-part-resolution` off `main`. See `docs/SPEC_Firmware_Part_Resolution.md`
(carried over from the unmerged `jovial-kilby` branch, commit `ba6c2da`).

**Goal:** stop the firmware front end from INVENTING parts (it substitutes `SPH0645` for
the user's actual `INMP441`). Resolve to the part the user declared; emit a disclosed gap
when a resolved part has no shipped symbol/footprint, rather than silently substituting.

## I1 BLOCKER — fix first
> **I1: canonicalize-vs-raw-regex mismatch breaks hyphenated parts like ICS-43434 — match on raw strings, canonical only as lookup key**

`canonical_type()` strips non-alphanumerics (`ICS-43434` → `ICS43434`), so a regex built from
canonical names never matches the raw source text. **Fix:** the extractor matches on RAW names
(`\bICS-43434\b`); `canonical_type` is used only as the card-lookup key. `cards.recognized_part_names()`
returns `dict[str, str]` mapping `raw_name → canonical_key` (covers `type` + each `alias`).

## Default decisions taken
- **Branch base = `main`** (spec recommendation; cherry-pick only autoroute fixes from `agitated-curie` if needed). The prior `agitated-curie` INMP441 fanout work is NOT pulled in unless Brian says so.
- **Resolution machinery proceeds without the INMP441 symbol** — until workstream A ships it, an INMP441 resolves and emits a `part_unavailable` gap (the honest behavior the spec wants). This is the bulk of the value and is unblocked.

## BLOCKED on Brian (needed only for full completion, not the core)
- Workstream A: author the INMP441 KiCad symbol + footprint. Needs datasheet pin-name decisions (SCK/SD/WS/L-R/GND×3/VDD/CHIPEN — CHIPEN must not float), and the bare-LGA-vs-module-header footprint default.
- ICS-43434 as a sanctioned substitute → needs I2S firmware-compat check.

## Phases (software core — all unblocked)
- **C8 (test-first):** `tests/test_part_identity_guardrail.py` — assert every placed template peripheral with a non-generic type either has imported provenance or an `assumed_part`/`part_unavailable` gap names it. Mark `xfail` until C6 lands so the suite stays green; flip to strict after.
- **C2 registry:** `cards.py` — validate optional `aliases` (list[str]) + `serves` (member of new `_SERVES_BUS_TYPES = {I2C,SPI,I2S_IN,I2S_OUT,UART}` — FINER than `_BUSES`, the I2 fix). Add `recognized_part_names() -> dict[str,str]` (I1 fix). New cards: `inmp441.yaml` (serves I2S_IN), `max98357a.yaml` (serves I2S_OUT, formalizes the `knowledge.py` constant), `sph0645.yaml` (serves I2S_IN, alias SPH0645LM4H). Keep `K.SPH0645`/`K.MAX98357A` Python dicts as disclosed fallbacks until the card path is proven.
- **C1 extractor:** `part_extractor.py` (new, pure) — `collect_corpus(config_path)` reads PREPROCESSED config text (post-`select_active_branches`) + bounded `.cpp/.h` + `README/WIRING/CLAUDE.md`; `extract_part_names(corpus, recognized_names)` matches RAW names with `\b…\b` (case-insensitive), returns `PartEvidence` ordered source > config_comment > doc. The `// INMP441 …` standalone comment in the real config.h is visible only in raw text (parse_defines strips comments).
- **C4 resolver:** extend `Bus` (intent.py) with `resolved_part`/`part_provenance`/`part_is_assumption`; bump `SCHEMA_VERSION`. `bus_part_resolver.py` (new, pure): for each bus, match cards whose `serves` == `bus.type`; if exactly one such part appears in corpus → high confidence; >1 → low (no silent pick, gap-worthy); 0 → none. NO proximity heuristics. Wire into `design.py:_op_import` after `build_intent`.
- **C5 gap:** `generate.py` — when a resolved part's symbol lookup fails, emit `part_unavailable` gap into the intent (spec §5.4 exact text). Confirm the generate-mutates-intent pattern with Brian (I3/G2).
- **C6 template split:** `templates.py` `i2s_mic`/`i2s_output_amps` — if `bus.resolved_part` & not assumption & card found → place card with `origin="imported"`; else disclosed fallback + `assumed_part` gap. INMP441 support glue (CHIPEN→VDD, L/R→GND, SD pulldown, decoupling) via card `config`/`static_ties` → handled by the generic `device_config` template, no new template code.
- **C7 board.yaml override:** `sidecar.py` — `bus_part_overrides` (validated against `recognized_part_names()`; footprint-only swap leaves `lib_id`/identity). Stamps `bus.resolved_part`, `part_is_assumption=False`, provenance `user`.
- **C9 golden:** new fixture `tests/fixtures/firmware/audio_inmp441/` (adds the `// INMP441` comment) → integration test asserts `resolved_part=="INMP441"`, not SPH0645. Existing `audio_s3` (no comment) keeps the fallback path → assert `assumed_part` gap now present.

## External-system assumptions to verify
- INMP441 footprint land pattern (datasheet) — before authoring.
- KiCad symbol pin names for MAX98357A / SPH0645 cards (`~{SD_MODE}` negation is version-sensitive) — re-verify on 9 & 10.
- `select_active_branches` runs BEFORE `collect_corpus` (dead `#else` parts ignored).
- `kicad_sch_api` stable on absent lib_id (append to component_errors, no crash).
- Project-library path discoverable by `get_symbol_cache()` for the INMP441 lib_id.

## Build order
C8 (xfail) → C2 registry (+I1 fix) → C1 extractor → C4 resolver → C5 gap → C6 templates → C7 sidecar → C9 golden. All but A are no-symbol-needed.

## STATUS (this session) — C1–C9 software arc DONE, gated on real KiCad
Branch `feat/firmware-part-resolution-2`. Done + green (no-KiCad 2042, integration
23/23 on KiCad 9 & 10):
- **C1** part-name extractor, **C2** registry (serves/aliases/recognized_part_names),
  **C2b** MAX98357A + SPH0645 cards — on `main` (PR #66) / this branch.
- **C4** Bus schema + pure resolver + part_serves_map; **C4-wire** runs it in
  import_firmware (reports `resolved_parts`).
- **C6** templates consume resolved_part (imported / assumed+gap / refuse-with-
  part_unavailable) — resolution is now LOAD-BEARING.
- **C7** board.yaml `bus_part_overrides` (user declares a bus's part, provenance
  "user", wins over corpus).
- **C8** part-identity guardrail (no silent substitution).
- **C9** golden gate on audio_s3 (MAX98357A imported via corpus; unnamed mic =
  disclosed assumed_part); expand_templates surfaces open gaps.

Demonstrated: the audio firmware NAMES MAX98357A → both amp buses bind it from the
corpus (not invented); the unnamed mic is a disclosed SPH0645 assumption.

### DEFERRED (blocked or future)
- **INMP441 symbol + card (workstream A)** — BLOCKED on datasheet (pin names incl.
  CHIPEN-must-not-float; bare-LGA vs module footprint). Until then an INMP441
  mention resolves to a `part_unavailable` gap (refuse-to-substitute, unit-covered)
  rather than a placed part. The `audio_inmp441` C9 fixture lands with it.
- **C5 generate-level part_unavailable** (symbol-absent-at-generate gap) — not
  needed by any current path: shipped cards all have symbols, and a declared part
  with no realizing template is already refused by C6's _decide_part BEFORE
  placement. It becomes a useful safety net when a card exists whose symbol is
  absent in a given KiCad install (e.g. the INMP441 card pre-symbol). Add then.
- **C7 footprint-only override** parsed/validated but not applied (the INMP441
  bare-LGA-vs-module case, blocked on the symbol).
