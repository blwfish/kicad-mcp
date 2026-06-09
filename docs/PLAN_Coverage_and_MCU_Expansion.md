# PLAN: Coverage & MCU Expansion

Status: **DRAFT roadmap** (a program, not a single-change spec). Living document.

## Motivation

The firmware front end is validated on a handful of real boards (speed-cal,
track-geometry, audio nodes) using ~a dozen device cards and exactly two MCUs
(`ESP32-WROOM-32E`, `ESP32-S3-WROOM-1`). That leaves a vast unexplored space:
thousands of peripheral chips, other MCUs, near-infinite firmware idioms, and
KiCad symbol-library drift across versions. Bug-finding to date has been
**reactive** — we fix what breaks on boards we happen to build.

You do not close an infinite space by enumeration. The strategy is three moves:

1. **Make the whole space fail safe** — unrecognized / edge inputs produce loud,
   recoverable *gaps*, never a silently-wrong board. (This is exactly what the
   CLAUDE.md data-capture / threshold-boundary / syntactic-seam rules are about,
   mechanized as executable assertions.)
2. **Measure coverage empirically** with a corpus, so "unexplored" becomes a
   ranked, prioritized backlog instead of an unknown unknown.
3. **Widen the substrate** (MCUs) so the corpus measures *real* gaps rather than
   re-measuring the already-covered ESP32 path.

Through-line: **widen substrate → measure → fill, on top of a safety net.**

## Principle: modules only

MCUs are supported as **modules** (Pi Pico board, Arduino Nano/Uno, ESP32
modules), never chip-down. A module carries its own regulator, flash, crystal,
and USB; the tool places a header footprint and wires GPIOs. Chip-down (bare
silicon + its support circuit) is **explicitly out of scope** — the modules are
cheap to source and not worth rebuilding.

## What this closes — and what it does not

Distinguish **seal** (eliminate the gap) from **close** (narrow it appreciably).

- **Fail-safe axis — SEALED by invariants.** No crash, no silent data loss,
  honest `ok`/`partial`/`error` status, no shorts / no ref-collisions — for all
  inputs.
- **Declared-connectivity correctness — SEALED by firmware-as-spec.** The
  firmware *is* the spec: `#define I2C_SDA_PIN 21` declares GPIO21 ↔ the
  peripheral's SDA. So "did the netlist faithfully realize every firmware-declared
  pin relationship" is **mechanically checkable with no human labels**. (The
  hand-written `members("I2C_SDA") == {...}` assertions are instances; the *class*
  version derives the expected connectivity from the firmware automatically and
  asserts it for any firmware.) This assumes the card's role→pin map is correct.
- **Semantic correctness at scale — CLOSED by the corpus.** A correctness bug in a
  chip's mapping affects *every* board using that chip, so it surfaces as repeated
  signal: inspect the top-N chip mappings once each and you've validated the bulk
  of real usage. Needle-in-haystack becomes a frequency-ranked, finite list.
- **The residual (genuinely unsealed — needs human / golden):**
  - **Card-vs-datasheet semantics** — does `SDA → pin 13` actually match the
    chip? One confirmation per card; the corpus ranks which cards are worth it;
    auto-draft proposes, a human confirms.
  - **Tool-injected design knowledge** — power-tree topology, decoupling / pull-up
    values, regulator choice, programming block, terminal pad assignment,
    placement. Firmware declares none of it, so it needs golden files + eyeball.

Net: the program **seals** declared connectivity + fail-safe, **closes** the bulk
of semantic correctness, and leaves a small, well-named residual. Effort
estimates below are in claude+blw **pairing** units.

## Phases

### Phase 1 — Probe + foundation (cheap, parallelizable, first)

Two independent low-cost tracks that de-risk everything after.

**1a. Pico module probe (discovery).** Add RP2040 / Pi Pico as a module
`McuInfo` (symbol + castellated footprint + `board_match: [pico, ...]`,
`needs_3v3: false`, `native_usb: true`). Run a real Pico firmware through
import→expand→generate. The deliverable is *not the Pico* — it is a written
**leak map** of every place ESP32 assumptions surface (`power_tree`,
`usb_programming`, `mcu_straps`, the ESP32-centric `McuInfo` strap fields). Pico
is the right probe because it is "S3-shaped" (3.3V + native USB) — the closest fit
to a path that already exists.
- *Exit:* a Pico board generates **and** the leak map is written.
- *Effort:* ~½–1 day. Mostly delegable; the "where did it leak" judgment is worth
  doing together.

**1b. Safety net (insurance for all later work).**
- Harden the **card × version × pin** class gate, and add the **meta-gate**:
  *every card field whose values are pin names is in the pin-collector* — so the
  `port_pins`-style omission (a gate that silently excluded 10 of 16 pins) cannot
  recur.
- **Status-honesty invariant:** for all intents, `ok ⟺ clean`.
- **No-crash + no-silent-loss fuzz** (Hypothesis `config.h` generator): import /
  expand never raise; `macros == modeled ∪ gapped ∪ unparsed`.
- **Round-trip property** over *generated* intent shapes (not just one fixture).
- **Firmware-as-spec connectivity invariant:** for all firmware, every declared
  pin macro produces a correctly-resolved net joining the right two pins — the
  highest-value correctness lever, mechanizable with no labels.
- *Exit:* all run per-PR; the fuzzer has found ≥1 real edge case (if it finds
  nothing first try, it is too weak — per the CLAUDE.md "did any test surprise
  you" check).
- *Effort:* ~2–3 days, highly delegable. **Would have caught 3 of the 5 bugs from
  the off-board-terminals session before they were written.**

Phase 1 gates Phase 2: **1a says what to generalize; 1b protects while we do it.**

### Phase 2 — Generalize the MCU layer for a 5V module (scoped by 1a)

Add Arduino Uno / Nano (AVR, **5V**) — the real assumption-breaker. 5V means
`power_tree` does not fire and "what sources 3.3V for 3.3V peripherals on a 5V
board" is genuinely unhandled today. This is the only phase with real unknowns,
which is why it follows the Pico probe.
- *Work:* generalize the power layer from "ESP32 assumes a 5V→3.3V regulator" to
  "the MCU declares which rails it provides / needs; peripheral rails are sourced
  or honestly gapped." Make `en_pin` / `boot_pin` optional (AVR has neither).
- *Exit:* an Arduino board with a 3.3V I2C peripheral generates with that
  peripheral's power either correctly sourced or honestly gapped — never silently
  floating.
- *Effort:* **UNKNOWN until 1a** — ~2 days (assumptions well-isolated) to ~1–2
  weeks (power topology pervasive). Pairing job, **not delegable** — it is the
  architectural seam.
- *Decision point:* after 1a, re-scope honestly and decide whether 5V Arduino is
  worth the cost, or stop at **3.3V modules** (Pico + ESP32-C3/C6/S2 — all cheap,
  reuse existing paths).

### Phase 3 — Corpus harness (the measurement engine)

With the substrate wider, build the measurement so it finds *real* gaps.
- *Work:* harvest a diverse corpus (ESP32 + Pico + Arduino **library example
  sketches**, organized by chip, + cross-domain GitHub projects for idiom
  diversity). Run import→expand over all of it (**no routing — cheap**). Emit
  three reports: a **chip-frequency-ranked carding backlog**, a **fail-safe
  report** (any crash / silent-drop / `ok`-with-gaps), and **firmware-as-spec
  connectivity** results. Route only a small sample.
- *Mechanics:* natural multi-agent fan-out (one agent per firmware → metrics →
  synthesis); nightly once stood up.
- *Exit:* a ranked backlog to act on + a green fail-safe report.
- *Effort:* ~2–3 days for the harness, fully delegable; then continuous.

> Why library examples: each ESP32/Arduino sensor/display/driver library targets
> one chip and ships example sketches with the canonical pin `#define`s — broad,
> chip-labeled peripheral coverage with almost no scraping. ESP32 firmware stays
> the substrate (the tool's domain); the **peripherals and idioms** are what we
> diversify, deliberately *unlike* speed-cal / track-geometry.

### Phase 4 — Steady state (the loop)

Card the top-N peripherals the corpus surfaces → re-measure → repeat. Corpus +
invariants become a permanent coverage machine: coverage stops being a guess and
becomes a number that goes up. Auto-draft bootstraps cards; a human confirms
card-vs-datasheet. Continuous, mostly delegable.

### Phase 5 — Golden-file harness (the residual)

Scope golden files specifically to the **injected-design-knowledge** residual
(power, passives, programming, placement) — **not** "correctness" broadly, since
declared connectivity is already sealed by Phase 1b. Sequence after the substrate
is stable. (Already tracked as the external-interface verification debt.)

## Open decisions (to settle as we go)

1. **Scope ceiling:** 3.3V modules only (Pico, ESP32-C3/C6/S2 — cheap, reuse
   existing paths), or 5V Arduino (pulls in the Phase 2 power work)? 1a informs;
   appetite sets the ceiling.
2. **Sequencing:** safety net (1b) in parallel with the Pico probe (1a), or
   1a-findings-first to size 1b? Default: parallel.

## What this plan deliberately does NOT do

- Chip-down anything (modules are cheap to source).
- Treat the corpus as ground truth for injected design knowledge — it cannot be,
  because firmware never declared that knowledge (Phase 5's job).
