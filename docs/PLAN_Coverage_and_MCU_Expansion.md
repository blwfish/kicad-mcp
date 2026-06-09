# PLAN: Coverage & Substrate Expansion

Status: **DRAFT roadmap** (a program, not a single-change spec). Living document.
**Cold-reviewed once** (3 severity-tier subagents) — material corrections folded in;
see *Provenance* at the end for what changed and what was verified against the code.

## Motivation

The firmware front end is validated on a handful of real boards (speed-cal,
track-geometry, audio nodes), ~a dozen device cards, exactly two MCUs
(`ESP32-WROOM-32E`, `ESP32-S3-WROOM-1`), and one toolchain shape (PlatformIO + a
C `config.h` of pin macros). Everything outside that envelope is unexplored.
Bug-finding to date has been **reactive** — we fix what breaks on boards we build.

You do not close an infinite space by enumeration. The strategy is three moves:

1. **Make the whole space fail safe** — unrecognized / edge inputs produce loud,
   recoverable *gaps*, never a silently-wrong board. (This mechanizes the CLAUDE.md
   data-capture / threshold / syntactic-seam rules as executable assertions.)
2. **Measure coverage empirically** with a corpus, so "unexplored" becomes a
   ranked, prioritized backlog instead of an unknown unknown.
3. **Widen the substrate** — but along *all four* of its axes, cheapest first.

## The chasm has four axes (not one)

The "unexplored space" is a product of four independent axes. They are **not**
equally expensive, and the cheapest are the broadest — a fact the first draft of
this plan missed by treating "add MCUs" as the headline.

| Axis | What varies | Cost to widen | Reach per unit effort |
|------|-------------|---------------|-----------------------|
| **Peripheral chips** | which sensor/display/driver chips have cards | low (data: a yaml card) | medium — the long tail, one chip at a time |
| **Toolchain / project layout** | where the board id & pins live (PlatformIO `.ini` vs Arduino-IDE sketch vs pico-sdk CMake vs none) | **low** (an importer/sidecar change) | **high** — unlocks whole user populations for chips already supported |
| **Firmware language / pin paradigm** | how pin→role is declared: C `#define ..._PIN n` vs C runtime `gpio_init(n)` vs Python `machine.Pin(n)` vs Rust HAL | medium (architectural, see Two intent producers) | **high** — unlocks whole ecosystems (e.g. MicroPython, the #1 hobbyist Pico path) |
| **MCU family** | the chip wiring the firmware targets | **high** (the wiring core is ESP32-shaped — see Phase 2) | low–medium — one MCU family at a time, and it's real surgery |

**Key re-rank (post cold-review):** the toolchain and language axes are *cheaper
and broader* than the MCU axis. An ESP32 driven from the Arduino IDE, or a Pico
running MicroPython, fails today not because we lack the *chip* but because we
can't *import* the firmware. Fixing import serves more real users per unit effort
than MCU-module surgery does. So import generalization is sequenced **ahead** of
MCU expansion.

## Principles

### Modules only
MCUs are supported as **modules** (Pi Pico board, Arduino Nano/Uno, ESP32
modules) — place a header footprint, wire GPIOs. Chip-down (bare silicon + its
support circuit) is **out of scope**: the modules are cheap to source. (Caveat the
cold review surfaced: even *module* support is not free — the wiring/power layer is
ESP32-shaped. See Phase 2.)

### Two intent producers
`DesignIntent`'s own docstring calls it "the canonical, **human/LLM-editable**
seam between the firmware importer and the schematic generator." Lean on that: the
intent doc has **two producers**, not one.
- **A deterministic C-macro parser** — the fast, reproducible path for the
  high-frequency case (C firmware with `#define ..._PIN n`, the parser's
  load-bearing assumption today).
- **The AI assistant as a universal firmware reader** — for the long tail of
  languages / paradigms the parser cannot read (pico-sdk runtime calls,
  MicroPython/CircuitPython Python, Rust). Claude reads the firmware in any
  language and writes / fills the intent directly; the *same* intent→expand→
  generate→PCB pipeline takes over.
- **`board.yaml` as the declared-facts escape hatch** — anything neither producer
  can determine (board identity on a layout that doesn't declare it; a pin the
  AI can't infer) is supplied as a firmware-blind fact, with provenance.

This reframes the tool from "a PlatformIO-ESP32-C importer" to **"an intent→PCB
pipeline with two intent producers."** It makes the *language* axis tractable
**without an N-language parser explosion** — at the honest cost that AI-extracted
intents are not reproducible like a macro parse and land in the correctness
residual (they need the verification/eyeball pass).

## What this closes — and what it does not

Distinguish **seal** (eliminate the gap) from **close** (narrow it appreciably).

- **Fail-safe axis — SEALED by invariants.** No crash, no silent data loss, honest
  `ok`/`partial`/`error` status, no shorts / ref-collisions — for all inputs.
  (Verified consistent with the code: unresolved pins are *recorded, not crashed*.)
- **Declared-connectivity faithfulness — CLOSED by firmware-as-spec (not sealed).**
  The firmware *is* the spec: `#define I2C_SDA_PIN 21` declares GPIO21 ↔ SDA, so
  "did the netlist realize what the firmware declared" is checkable. **Important
  correction from the cold review:** this only proves the tool faithfully realized
  *what the card says* — it re-runs the tool's own derivation, so it catches
  mis-realization *regressions and crashes*, **not** whether the card's role→pin
  map matches the datasheet. It depends on card correctness, which is the unsealed
  residual below. So: a strong regression net, **not** a correctness seal.
- **Semantic correctness at scale — CLOSED by the corpus.** A correctness bug in a
  chip's mapping affects *every* board using that chip, so it surfaces as repeated
  signal: inspect the top-N chip mappings once each and you've validated the bulk
  of real usage. Frequency-ranked, finite.
- **The residual (genuinely unsealed — needs human / golden):**
  - **Card-vs-datasheet semantics** — does `SDA → pin 13` match the chip? One
    confirmation per card; the corpus ranks which cards are worth it. (Note:
    `autodraft.py`'s `draft_card()` proposes *peripheral* cards to bootstrap this —
    it does **not** bootstrap MCU cards.)
  - **Tool-injected design knowledge** — power-tree topology, decoupling / pull-up
    values, regulator choice, programming block, placement. Firmware declares none
    of it → golden files + eyeball (Phase 5).
  - **AI-extracted intents** — non-reproducible by construction; verify on a
    sample.

Effort estimates below are in claude+blw **pairing** units.

## Phases (ordered by leverage, cheapest/broadest first)

### Phase 0 — Safety net (foundation; protects everything after)

- Harden the **card × version × pin** class gate + a **meta-gate**: *every card
  field whose values are pin names is in the pin-collector* (so the `port_pins`-
  style omission — a gate that silently excluded 10 of 16 pins — cannot recur).
- **Status-honesty invariant:** for all intents, `ok ⟺ clean` — across
  `import_firmware` / `expand_templates` / `generate_schematic`, not one path.
- **No-crash + no-silent-loss fuzz** (add **Hypothesis** as a dev dependency — it
  is not one today): import/expand never raise; `macros == modeled ∪ gapped ∪
  unparsed`.
- **Round-trip property** over *generated* intent shapes.
- **Firmware-as-spec connectivity invariant:** every declared pin macro produces a
  correctly-resolved net joining the right two pins. (Catches regressions/crashes;
  not a datasheet-correctness oracle — see above.)
- *Exit (two separable criteria, per cold review):* (a) all run per-PR
  (mechanical), AND (b) a human signs off that the fuzzer's generators cover the
  stated cases — **not** "the fuzzer found ≥1 bug," which is gameable.
- *Effort:* ~2–3 days, highly delegable. (Would have caught the status-honesty,
  port-pin-gate, and duplicate-pin bugs from the off-board-terminals session before
  they were written — the three structural ones, not the two semantic ones.)

### Phase 1 — Import-path generalization (cheap, broad — the highest-leverage win)

Unlock whole user populations for chips already supported, with **zero new MCU
surgery**. Two sub-tracks:

**1a. Toolchain / board-identity.** Today `find_board_id` only reads a
`platformio.ini board=`; an Arduino-IDE sketch keeps the board in GUI state (no
project file), pico-sdk keeps it in a CMake `PICO_BOARD` var. So:
- Add **`board_id` (and/or `fqbn`) to `BoardSidecar`** — the board becomes a
  firmware-blind fact (it genuinely is, for these layouts), supplied like
  `power_source`. `resolve_mcu`'s `board_match` learns to match FQBNs too.
- Read an Arduino `sketch.yaml` `default_fqbn` when present (covers the arduino-cli
  subset for free).
- Treat the **`.ino` (and tab files) as a first-class config source** — pins live
  there, not in a `config.h`; `_find_config_header` is `config.h`-only today.
- *Reach:* unlocks **ESP32-via-Arduino-IDE** — probably the single largest
  hobbyist population — for no new chip work.

**1b. Formalize the AI intent-producer path.** Make "AI reads non-C-macro firmware
→ writes/fills the intent" a documented, supported path (not an accident), with a
verification step. Unlocks MicroPython/CircuitPython/pico-sdk/Rust *for the chips
already supported*.
- *Exit:* an ESP32 Arduino-IDE sketch (no `platformio.ini`) and a MicroPython
  Pico script both reach a generated schematic via their respective producers.
- *Effort:* 1a ~2–3 days (importer + sidecar field, mostly mechanical); 1b is
  partly process/docs + a verification harness, ~1–2 days. Mostly delegable.

### Phase 2 — MCU-module generalization (expensive; the real ESP32-shaped surgery)

**Cold-review correction:** the first draft framed the Pico as a cheap "S3-shaped"
probe. It is **not** — the MCU *wiring core* is ESP32-only, and the Pico trips it
before Arduino is even in play. This phase generalizes that core, proven on the
Pico, then extended to a 5V Arduino. Known leaks to fix (all verified in code):

- **GPIO pin-name resolution** — `gpio_to_pin_number` (`mcu_pinmap.py`) searches
  for token `IO{n}`; the Pico symbol names pins `GP{n}`/`GPIO{n}`, so *every* GPIO
  resolves to nothing. Needs a per-MCU GPIO-naming convention. **This is the core
  wiring step, not glue.**
- **Native power** — `power_tree` fires only for `needs_3v3`, `usb_programming`
  only for `not native_usb`. A Pico (`needs_3v3:false`, `native_usb:true`) gets
  **no +5V and no +3V3 source** — supply pins float. Needs a `native_power`
  concept (the module self-regulates). (The 5V-Arduino "what sources 3.3V" problem
  is the *same* problem — it is already live on the Pico, not Arduino-only.)
- **Native-USB programming** — `usb_programming` returns an *empty* Expansion for
  `native_usb` → no USB/programming hardware placed at all. Needs a native-USB
  connector path.
- **Required strap fields** — `_MCU_REQUIRED` (`cards.py`) requires
  `en_pin`/`boot_pin`; `mcu_straps` accesses them unguarded. Make them optional +
  guard the template (AVR/Pico have no such straps). **This gates even loading a
  Pico card** — so part of it is a prerequisite, not a follow-on.
- **Placement** — the antenna/keepout placement heuristic assumes an antenna side;
  the Pico footprint has no keepout zone → the `antenna_side=None` path is
  under-tested with a real non-ESP32 footprint.

Then add **Arduino Uno/Nano (AVR, 5V)** — within the modules-only principle; the
only open question is whether the 5V power work is worth it.
- *Exit:* a Pico board *and* an Arduino board generate with GPIOs wired and power
  either sourced or honestly gapped — never floating.
- *Effort:* **larger than the first draft claimed.** The Pico core work is real
  (GPIO naming + native power + native USB + optional straps), ~1 week; the AVR 5V
  generalization on top is ~2 days–2 weeks depending on how pervasive the 3.3V
  assumption proves. **Not delegable** — it is the architectural seam.
- *Decision point:* after the Pico core lands, decide whether 5V Arduino is worth
  it or whether to stop at **3.3V modules** (Pico + ESP32-C3/C6/S2).

### Phase 3 — Corpus harness (the measurement engine)

Run import→expand over a diverse corpus; emit a **chip-frequency-ranked carding
backlog** + a **fail-safe report** + firmware-as-spec results. Route only a sample.
- **Prerequisites the cold review surfaced** (the corpus is *not* "almost no
  scraping"): library example sketches are `.ino` with **no `platformio.ini`**
  (→ needs Phase 1a's `board_id`) and pins often passed as **constructor
  arguments**, not `#define`s (→ sparse firmware-as-spec signal; the corpus skews
  to bus-default parts). Plan a sampling spike before committing.
- **Can start ESP32-only in parallel with Phase 2** — peripheral-chip coverage is
  independent of MCU breadth, so an ESP32 corpus already produces a carding backlog.
- *Mechanics:* multi-agent fan-out; nightly once stood up. Decide corpus storage /
  licensing (third-party sketches) and CI cost (separate from per-PR runners).
- *Effort:* ~2–3 days for the harness (after 1a), fully delegable; then continuous.

### Phase 4 — Steady state (the loop)

Card the top-N peripherals the corpus surfaces → re-measure → repeat. Corpus +
invariants become a permanent coverage machine. `draft_card()` bootstraps
peripheral cards; a human confirms card-vs-datasheet. Continuous, mostly delegable.

### Phase 5 — Golden-file harness (the residual)

Scope golden files specifically to the **injected-design-knowledge** residual
(power, passives, programming, placement) and to **AI-extracted intents** — not
"correctness" broadly, since declared connectivity is already covered by Phase 0.
Sequence after the substrate is stable.

## Open decisions

1. **MCU scope ceiling:** 3.3V modules only (Pico, ESP32-C3/C6/S2), or 5V Arduino
   (pulls in the Phase 2 power work)? The Pico core work informs it.
2. **Relative priority:** import-path (Phase 1) vs MCU (Phase 2) first. This plan
   ranks Phase 1 ahead on leverage; confirm that matches appetite.
3. **Phase 0/1 sequencing:** Phase 0 protects later work; run it alongside Phase 1
   or fully before it? Default: alongside, with Phase 0's status/fuzz pieces landed
   before Phase 2's seam work.

## What this plan deliberately does NOT do

- Chip-down anything (modules are cheap to source).
- Write a parser per firmware language (the AI-producer path covers the tail).
- Treat the corpus as ground truth for injected design knowledge (Phase 5's job).

## Provenance (cold review, 2026-06-08)

Reviewed by three clean-context subagents (external-system/codebase assumptions;
internal contradictions; scope gaps). Material corrections folded in:
- **Pico is not a cheap near-fit** — the MCU wiring core (`gpio_to_pin_number`
  `IO{n}`), native power, native-USB programming, and required strap fields are all
  ESP32-shaped. Phase 2 re-scoped from "probe" to real surgery.
- **firmware-as-spec is CLOSE, not SEAL** — it proves self-consistency, not
  datasheet correctness (depends on card correctness, the unsealed residual).
- **Corpus blockers** — `.ino`/no-`platformio.ini`/constructor-arg pins; added the
  Phase 1a prerequisites and a sampling spike.
- **Smaller fixes** — split the gameable fuzzer exit criterion; Hypothesis added as
  a dependency; `draft_card` is peripheral-only; Phase 3 can run ESP32-only in
  parallel; antenna/keepout `None` path noted.
- **Two new axes** (toolchain, language) and the **two-intent-producer model**
  added from the Arduino-IDE / RP2040-on-other-IDEs discussion — and the phases
  re-ranked so the cheap/broad import work leads the expensive MCU work.

Verified solid: modules-only thesis; the measure→fill loop; fail-safe genuinely
holds (gap-not-crash); the 2-MCU baseline + IDF guard; `en_pin`/`boot_pin` really
required; the Pico module symbol (`MCU_Module:RaspberryPi_Pico`) + castellated
footprint (`Module:RaspberryPi_Pico_SMD`) exist on both KiCad versions.
