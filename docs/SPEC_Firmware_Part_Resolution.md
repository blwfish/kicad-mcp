# SPEC: Firmware-Driven Part Resolution

**Status:** DRAFT — written 2026-05-31. **BLOCKED behind the placement-locus phase** (see `SPEC_Firmware_Placement_Locus.md`): locus is a foundational intent-model axis that changes what "place a resolved part" means (a remote device is realized as a terminal, not a placed symbol — e.g. the INMP441 on the audio-node), so it must land first. **Also has pending cold-review must-fixes** to fold in before implementation: I1 (canonicalize-vs-raw-regex mismatch breaks hyphenated parts like ICS-43434 — match on raw strings, canonical only as lookup key), I3/G2 (resolved-but-unplaceable + guardrail need a generate-time change; "generate_schematic no changes" is wrong), G1 (type-only binding does nothing for multi same-type buses — needs the bind-all-with-disclosure + bus-section-association tiers), I2 (`serves` validates against the finer `Bus.type` vocab), E5 (dead-branch guarantee covers config.h only; source/doc evidence is best-effort). **Requires fresh-session cold review before implementation** (per the project's Spec Review Rule: do not implement in the session that wrote the spec).

**Scope:** Workstreams **A** (INMP441 library content) and **C** (read-from-firmware part-resolution layer). Workstream **B** (standalone disclosure/override patch) is **dropped** — it is a strict subset of C and would build the same scaffolding twice.

---

## 1. Problem

The firmware front end translates ESP32 firmware into a schematic/PCB. Today it parses **only `#define` GPIO numbers**, then `expand_intent` runs templates that **hardcode specific application parts**:

- `i2s_mic` unconditionally places a Knowles **SPH0645** for any `I2S_IN` bus.
- `i2s_output_amps` unconditionally places a **MAX98357A** for any `I2S_OUT` bus.

The user's real firmware (`mr-esp32/audio-node`) **names these parts explicitly**: `INMP441` in `config.h` comments (lines 43, 190) and a dedicated `amp_gain.cpp`/`.h` written around the MAX98357A's GAIN_SLOT pin. The pipeline discards all of it and guesses — and guessed **wrong** for the mic (SPH0645 ≠ INMP441; **not firmware-compatible** — different silicon, footprint, and the SPH0645's known data-alignment quirk).

This violates the front end's core contract — *"honest-by-construction, never invent parts."* The defect is not that a default was chosen; it is that a **specific user-choice part was invented from a generic bus and presented silently as fact.**

### Blast radius (audit result)

| Template | Invented part | Trigger | Disclosed? | Verdict |
|---|---|---|---|---|
| `i2s_mic` | SPH0645 | `I2S_IN` bus | No | **Bug — wrong part** |
| `i2s_output_amps` | MAX98357A | `I2S_OUT` bus | No | Silent, but *correct* default |
| `usb_programming` | CP2102N | non-native-USB MCU | No | Silent; commodity glue |
| `power_tree` | AMS1117-3.3 | 3V3 MCU | Partially (docstring only) | Silent; commodity glue |

The MAX98357A case proves the principle: the **mechanism** (silent invention) is wrong even when the **part** happens to be right. There is no part-recovery path anywhere in the pipeline and no guardrail preventing silent invention.

---

## 2. Verified facts (external-system checks, 2026-05-31)

These were confirmed against the installed KiCad libraries and the local filesystem this session. **Re-verify on the target machine / KiCad version before shipping.**

| Part | KiCad symbol | KiCad footprint | Note |
|---|---|---|---|
| **INMP441** | **NONE** | **NONE** | Not in std libs, not in any 3rd-party/user lib on the machine. **This is the central constraint for workstream A.** |
| MAX98357A | `Audio:MAX98357A` ✓ | `Package_DFN_QFN:HVQFN-16-1EP_3x3mm_P0.5mm_EP1.5x1.5mm` (in use today) | Resolution + placement both work. |
| SPH0645 | `Sensor_Audio:SPH0645LM4H` ✓ | `Sensor_Audio:Knowles_SPH0645LM4H-6_3.5x2.65mm` ✓ | Usable as a **disclosed** fallback stand-in. |
| ICS-43434 | `Sensor_Audio:ICS-43434` ✓ | (check) | Candidate **disclosed substitute** for INMP441 (24-bit I2S MEMS mic that ships with KiCad). Firmware-compatibility not yet verified. |

**Implication:** resolving "INMP441" from firmware is necessary but **not sufficient** — there is nothing to instantiate. This forces the new **identity vs footprint vs availability** distinction below, and the **resolved-but-unplaceable** path (§5.4).

`parse.py` captures *trailing inline* comments on `#define` lines (`Macro.comment`) but **drops standalone comment lines** — the `// --- INMP441 ... ---` callout at config.h:190 is a standalone line, so it is currently invisible. The extractor must read raw/preprocessed source text, **not** `ParsedFirmware`.

---

## 3. Architecture

Three **separate axes**, previously conflated:

1. **Identity** — *which part* (INMP441). Determinable from firmware (code > adjacent comments > docs > platformio).
2. **Footprint / form-factor** — *bare chip vs module-on-header*. A board-level decision the firmware does **not** make; defaulted by the card, overridable per board. Never inferred from how the reference was prototyped.
3. **Availability** — *does a KiCad symbol/footprint exist*. Independent of identity; can fail (INMP441) even when identity is certain.

**Resolution order:** read the part from firmware **first** (authoritative, with provenance). Templates become a **disclosed fallback** consulted only when resolution fails. Glue/passives (LDO, decoupling, pull-ups, straps, gain/isolator resistors) **stay template-owned** — firmware does not name them and they are not user-choice parts.

**Honest-by-construction restored:** every placed specific part carries *either* firmware provenance *or* a disclosed-assumption gap. Enforced by a guardrail test (§5.6).

---

## 4. Workstream A — INMP441 library content

Author the INMP441 as a real, placeable part with **two footprints** (the form-factor axis):

- **A1 — bare LGA**: author a KiCad symbol + footprint for the INMP441 bottom-port LGA. Pinout **verified against InvenSense datasheet DS-INMP441-00 Rev 1.0, Table 6** (9-pad LGA, not 6 — the breakout module hides pads 5/6/8/9):

  | Pad | Name | Card role / tie |
  |----|------|-----------------|
  | 1 | SCK | role `BCLK` |
  | 2 | SD | role `DATA`; **100 kΩ pulldown** (bus tri-state discharge) |
  | 3 | WS | role `WS` |
  | 4 | L/R | static_tie → GND (left channel default) |
  | 5 | GND | ground (center pad) |
  | 6 | GND | ground |
  | 7 | VDD | supply; **0.1 µF decouple to pad 6** |
  | 8 | CHIPEN | **static_tie → VDD — floating = mic disabled** (landmine; assert in tests) |
  | 9 | GND | ground |

  Support glue the template must add (firmware-blind): CHIPEN→VDD tie, L/R→GND tie, SD 100 kΩ pulldown, VDD 0.1 µF decoupling. The three GND pads (5/6/9) wire by number. **Note:** this is a fine-pitch bottom-port part — choosing it on a PCB reopens the fine-pitch fanout question. Real but bounded work.
- **A2 — module header**: `Connector_Generic:Conn_01x06` (or the actual breakout pinout) — trivial, no custom symbol; matches the breakout modules in use on the bench.

Default footprint = author's choice (recommend bare LGA to match the "PCB ≠ breadboard" intent); the alternative is selectable via `board.yaml` (§5.5). Ship both as committed project library content (air-gap: no synthesis at user time).

**Open:** decide the canonical symbol pin names; verify against any reputable third-party INMP441 symbol before drawing from scratch.

---

## 5. Workstream C — resolution layer

New stage inserted between `partition()` and `build_intent()`:

```
config.h text
  → parse_defines + partition                       (unchanged)
  → collect_corpus(config_path)                      (NEW; I/O shell)
  → extract_part_names(corpus, recognized_names)     (NEW; pure)
  → resolve_bus_parts(parsed, evidences, registry)   (NEW; pure)
  → build_intent(parsed, bus_part_resolutions=...)   (Bus gains resolved_part + provenance)
  → expand_intent                                    (templates read resolved_part; fallback discloses)
  → apply_sidecar (board.yaml part/footprint override)
  → generate_schematic                               (unchanged)
```

`Peripheral` dataclass and `generate_schematic` need **no** changes — a resolved part manifests as `lib_id`/`value`/`footprint` from a card.

### 5.1 Part-name extractor (`part_extractor.py`, new)

- `collect_corpus(config_path) -> FirmwareCorpus` — I/O shell. Reads: preprocessed config.h text (post-`select_active_branches`, for comments); `*.cpp`/`*.h` within a bounded depth of config.h; docs (`WIRING*.md`, `README*.md`, `CLAUDE.md`). Returns plain data tagged by kind (`config_comment` | `source` | `doc`).
- `extract_part_names(corpus, recognized_names: frozenset[str]) -> list[PartEvidence]` — **pure**. Whole-word regex (`\bINMP441\b`, case-insensitive) so `INMP441_VDD` does not match. Must NOT import the card layer; `recognized_names` is injected.
- `PartEvidence{part_name, file, line, kind, raw_text}` + a `category` (mic/amp/...) looked up from the registry downstream.

**Tests** (`tests/test_part_extractor.py`, no KiCad): regex boundary at/below/above (`INMP441` matches, `INMP4411` does not, lowercase matches), part in block comment, line comment, C string literal; `collect_corpus` over a `tmp_path` tree.

### 5.2 Recognition registry = cards (single source of truth)

Cards gain an optional **`aliases`** list and a **`serves`** field (the bus type the part attaches to — `I2S_IN`, `I2S_OUT`, `I2C`, `UART`). `cards.recognized_part_names()` returns the union of all card `type` values + aliases, each run through the existing `canonical_type()` normalizer (no second source; no drift). Validator extends to check `aliases` (list of str) and `serves` (known bus type).

**Tests** extend `test_device_cards.py`: alias validates; `recognized_part_names()` includes canonicalized type + aliases; parity check guards drift.

### 5.3 Bus-part resolution (`bus_part_resolver.py`, new) — **binding by semantics, not proximity**

`resolve_bus_parts(parsed, corpus, recognized_names) -> dict[bus_stem, ResolvedBusPart]`.

**Binding rule (decided):** bind by **card-declared category ↔ bus type**, NOT by text proximity. A recognized part declares `serves: I2S_IN`; a bus has `type: I2S_IN`. For each bus, find recognized parts whose `serves` matches the bus type **and** which appear in the corpus:

- exactly one → bind, `confidence="high"`, record provenance (file:line, kind);
- zero → no resolution (→ fallback, §5.4);
- more than one (e.g., two `I2S_IN` buses, one mic name) → `confidence="low"`, **do not guess** — surface a gap inviting `board.yaml` resolution.

This explicitly **rejects** proximity/section-header heuristics (Open Decision 1 in the architect plan) as a syntactic-semantic seam: text position is not bus membership. Priority among evidence *kinds* for the same binding: source code > config comment > doc.

`Bus` gains: `resolved_part: Optional[str]`, `part_provenance: Optional[dict]`, `part_is_assumption: bool`. `SCHEMA_VERSION` → 4 (back-compat via existing `_only_fields`). `build_intent` gains `bus_part_resolutions=...`; `design.py::_op_import` wires corpus collection + resolution in.

**Tests** (`tests/test_bus_part_resolver.py`, no KiCad): source beats comment beats doc; single-match high; multi-match low (no silent pick); unknown name ignored; missing stem absent (no KeyError).

### 5.4 Resolved-but-unplaceable path (**NEW — the INMP441 lesson**)

When a part is resolved (identity known) but **no KiCad symbol/footprint is available** for its card's `lib_id`/`footprint`, the system must **not** silently fall back to a different chip. It emits a gap:

> `Gap(kind="part_unavailable", detail="firmware specifies INMP441; no KiCad symbol/footprint shipped — options: (1) add project library content, (2) use module-header footprint, (3) board.yaml disclosed substitute", resolved=False)`

and either places the user's chosen alternative (if `board.yaml` supplies one) or leaves a clearly-flagged placeholder. For INMP441 specifically, workstream A removes this gap by shipping the library content; the path exists for *future* resolved-but-absent parts.

### 5.5 Footprint / form-factor override (`board.yaml`)

`BoardSidecar` gains `bus_part_overrides: {bus_stem: {part?: str, footprint?: str}}`, applied in `apply_sidecar` after `build_intent` (existing pattern). `part` and `footprint` are independent: overriding `footprint` swaps only `Peripheral.footprint` (identity/`lib_id` unchanged) — this is how the user selects bare-LGA vs module-header for the INMP441, or forces a disclosed substitute. `part` validated against `recognized_part_names()`; unknown keys rejected (existing error discipline). User override → `part_provenance.kind="user"`, `part_is_assumption=False`, highest priority.

**Tests** extend `test_firmware_sidecar.py`.

### 5.6 Guardrail (`tests/test_part_identity_guardrail.py`, new)

Fails if any placed **non-generic** part (not C/R/SW/HDR/CONN/USB_C/CP2102/AMS1117) lacks **either** `origin="imported"`/firmware provenance **or** a disclosed `assumed_part`/`part_unavailable` gap naming it. The test *is* the contract — silent invention becomes structurally impossible. Test-first (write before changing templates).

### 5.7 Template split (`templates.py`)

`i2s_mic` / `i2s_output_amps`: if `bus.resolved_part` set → load its card, place with `origin="imported"`; else → place the disclosed fallback default (SPH0645 / MAX98357A) **and** emit `assumed_part` gap, `origin="template"`. **Support glue stays template-owned** (decoupling, DIN isolator, SEL strap, speaker headers). MAX98357A/SPH0645 Python dicts in `knowledge.py` remain as fallback constants; deprecate once cards exist and the split is proven.

### 5.8 Golden harness (`tests/integration/`)

- Existing `test_audio_s3_to_routed_pcb` unchanged (fixture names no part → fallback SPH0645/MAX98357A + gaps; existing count assertions hold).
- **New fixture** `tests/fixtures/firmware/audio_inmp441/` — config.h with the `// INMP441` callout (+ optional `amp_gain.cpp` stub naming MAX98357A). New test asserts: bus `resolved_part=="INMP441"`, provenance kind, placed mic `lib_id` = INMP441 card (not SPH0645), no `assumed_part` gap for the mic, and (once A ships) no `part_unavailable` gap.

---

## 6. Open decisions (for the cold reviewer)

1. **INMP441 default footprint** — bare LGA (matches "PCB ≠ breadboard") vs module-header (matches bench reality, avoids fine-pitch). Spec recommends bare LGA as default with header overridable; confirm.
2. **Disclosed substitute identity** — if a user wants a stand-in rather than authoring library content, is ICS-43434 the sanctioned choice? Requires a firmware-compatibility check against the INMP441 the firmware expects.
3. **Tier-2 glue (CP2102N, AMS1117) disclosure** — fold into this work, or defer? They are silent-but-correct; lower priority than the mic.
4. **Branch base** — build on `agitated-curie` (keeps the now-mooted fanout commits, which touch `i2s_mic`) or branch from `main` and shelve fanout. Recommend: **branch from main**, cherry-pick only the autoroute fixes, shelve fanout (it is subsumed/mooted; the bare-LGA INMP441 path can revive it later if chosen).

---

## 7. External-system assumptions to verify before implementation

- INMP441 symbol pin names — **VERIFIED 2026-05-31** against datasheet DS-INMP441-00 Rev 1.0 Table 6 (see §4, A1). Still TODO at A-impl: the **land pattern / outline dimensions** for the footprint (same datasheet, pages 17–19).
- `Audio:MAX98357A` and `Sensor_Audio:SPH0645LM4H` / `ICS-43434` pin names vs the card `roles` map, on KiCad 9 **and** 10 (integration `test_device_cards.py` is the catch; the `~{SD_MODE}` negation markup is version-sensitive).
- `select_active_branches` strips inactive `#else`/`#if` blocks **before** the extractor reads comments (so a part named only in an excluded legacy block is correctly ignored) — confirm `collect_corpus` receives preprocessed config text.
- `kicad_sch_api` behavior when a `lib_id` is absent (today: `component_errors`, not a crash) — the resolved-but-unplaceable path depends on detecting absence cleanly.

---

## 8. Out of scope / parked

- **Amp GAIN pin** — firmware drives GAIN via GPIO (`CMCA_AMP_GAIN_BUS0/1`, GPIO 11/12, `amp_gain.cpp`); the template currently leaves GAIN floating (9 dB Hi-Z). So even "support glue" is partly firmware-determined. The resolution layer should eventually read this; **not** in the A+C MVP. Flag explicitly so it is not mistaken for covered.
- **Pluggable firmware readers** for non-config.h ecosystems (STM32 .ioc, Zephyr DT).
- **Fanout / fine-pitch plane-pad** work (the `agitated-curie` Stage-1/2 commits) — mooted for the module-header form; revived only if bare-LGA INMP441 is chosen.

---

## 9. Effort (focused pairing sessions)

| | |
|---|---|
| A — INMP441 library content (2 footprints) | ~1 session (+ symbol/footprint authoring care) |
| C1 extractor + corpus | 1–2 |
| C2 registry (aliases/serves) | 0.5–1 |
| C3 INMP441/MAX98357A cards + integration pin tests | 1 |
| C4 resolver + Bus fields + `_op_import` wiring | 1–2 |
| C5 resolved-but-unplaceable path | 0.5 |
| C6 template split | 1 |
| C7 board.yaml override | 1 |
| C8 guardrail (test-first) | 0.5 |
| C9 golden harness + INMP441 fixture | 0.5–1 |

**MVP to a correct, honest board for the audio-node:** A + C3 + C6 (+ a one-line default-swap stopgap) ≈ 3 sessions. **Full layer:** ~8–10. Dependency order: C2 → C3 → C1 → C4 → (C5, C6) → C7 → C8 → C9; A in parallel.
