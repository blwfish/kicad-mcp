# Spec: Human-rational autoplacement — the *system*

**Status:** DRAFT, written 2026-06-01 with Brian during PR #58 review. **Requires a
fresh-session cold review before implementation** (CLAUDE.md Spec Review Rule — this
spec and its implementation must not share a session).

**Framing (Brian):** we are designing a *system*; the `mr-esp32/audio-node` board is
merely the worked *example*. Every capability below must be general and data-driven, and
must hold across all golden board shapes (speed-cal, track-geometry, audio on-board, audio
remote-terminal) — the integration harness is the generality gate. Nothing may be tuned to
make one board look good.

## The core lesson that motivates this

The first engine inferred terminal orientation from **pad-centroid geometry** — a syntactic
proxy for the *semantic* question "which way does the wire enter?". It's unrecoverable from
pad positions (J3/J4 came out right by luck; J1/J2/J5/J7 wrong). Terminal placement is
driven by facts geometry cannot see: wire access, RFI distance from the radio, enclosure
orientation, which signals group. **These are decide-up-front facts, not derive-from-geometry
facts.** The system must let a human commit them (in plain language → Claude → board.yaml)
and must fill the rest with constraint-respecting defaults — then show a rough proposal for
approval *before* the expensive placement/routing.

## Separation of concerns (unchanged, reaffirmed)

- **Firmware-derived** (honest-by-construction): MCU-side signal nets only. Never invented.
- **Board-level, firmware-blind** facts: terminal edges/orientation, mounting holes, board
  size, antenna handling. These live in `board.yaml` (the existing sidecar channel). **Brian
  never edits YAML** — he states intent in plain language, Claude translates. board.yaml is
  an internal interchange format, not a human-facing one.

## Capabilities to build (all general)

### 1. Terminal placement plan  `{ref → edge, order, orientation}`
Resolution order per terminal:
1. **board.yaml override** (explicit edge / order / rotation) wins.
2. **antenna-aware heuristic default** otherwise:
   - the MCU antenna keepout **overhangs** one edge (see §2);
   - field-wiring terminals go on the **opposite** edge (RFI — keep wires away from the
     radio), spilling to the side edges as needed, **never** the antenna edge;
   - **wire-entry faces outward** (off the board), oriented **deterministically from the
     synthesized footprint's known wire-entry geometry** — NOT pad centroid. Since locus
     synthesizes these (MKDS family), the wire-entry direction at 0° is known/measurable
     per footprint family; store it as footprint metadata, rotate so it faces the edge's
     outward normal.
   - **single row per edge, flush to the edge**, ordered by function/bus (e.g. speakers in
     bus0-L, bus0-R, bus1-L, bus1-R order). Never stack a second row behind the first
     (that buried J5/J7 behind J1–J4 and made the wires inaccessible).
3. On-board module headers (OLED/I2C breakouts, `edge:"none"`) are interior, near their
   device — not forced to an edge, not dead-centre.

### 2. Antenna overhang (tier-1, general)
Place any MCU whose footprint carries an antenna **keepout** so the keepout **overhangs a
board edge** (Brian confirmed real ESP32-S3 antennas hang off-board). This is the deferred
tier-1 work, now required. The keepout is currently merged into the fit-extents at
footprint-collection time — that merge must be undone for the overhang to be possible
(reserve the keepout as off-board area, like a terminal body overhang). Terminals take the
opposite edge (§1).

### 3. Mounting holes (general board feature)
Firmware-blind, board.yaml-overridable. Default: **4 corner holes, M3 (3.2 mm drill) + a
keepout**, inset a sensible distance from the corners. Configurable count/size/pattern; at
hobby scale 4 corners is sufficient (Brian). Holes reserve corner real-estate in the size
estimate (§4) and exclude copper/components under them.

### 4. Content-aware board sizing
Compute **after** terminal edges are chosen (the reserved perimeter depends on which edges
carry terminals). Size = tight interior cluster (MCU + actives + passives + interior
headers) **+ perimeter** for each terminal-carrying edge **+ corner mounting-hole insets
+ antenna overhang allowance**. Default to auto-size; explicit dims still win. Today's
`estimate_board_size` (footprint-area × 2.5 + keepout) does none of this and must be
replaced/extended. (Worked example, audio-remote: interior ≈ 45×40, → board ≈ 70×60 mm,
vs the 110×90 the test fixture hardcoded.)

### 5. Human approval gate
Before the expensive placement/routing, emit a **rough proposal**: a render + table of the
proposed terminal edges/orientation, mounting holes, and board size. The human approves or
adjusts (adjustments flow back as board.yaml via NL→Claude). Only on approval does the full
placement + routing proceed. This is a workflow step, general to every board.

### 6. Silk legend fixes
Per synthesized terminal, the silk legend must (a) show the **per-pad signal** (e.g. `+`/`−`
for a speaker, `OUTP`/`OUTN`, `SDA`/`SCL`), not just the device name, and (b) be **offset
clear of the terminal body** (readable on-board beside/inboard of the block), never under it.

## Placement order (revised tiers)
The connectors-before-their-IC-partners ordering is the root of the "all piled on top"
degeneracy. Revised commit order:
1. Commit board-level fixtures first: **antenna overhang edge, terminal edges/orientation,
   mounting-hole corners** (from the approved plan).
2. Place the **MCU** at its antenna-overhang edge.
3. Place **interior actives + passives** in the freed interior, near their partners.
4. Terminals are already committed to their edges (step 1), so ICs cluster *toward* them
   rather than connectors scattering to find unplaced partners.
5. Route.

## Generality gate
Re-run the full integration harness (all golden shapes). Add asserts: every terminal's
pads on-board (exists); terminal on its assigned/expected edge; wire-entry orientation
correct (pad/​body geometry on the outward side); mounting holes present at corners with
keepout; board sized within tolerance of the content-aware estimate; no second-row stacking.

## Open questions / risks
- **Wire-entry metadata source**: measure per MKDS footprint family and store as data, vs a
  general "longest courtyard axis faces along the edge" rule. Measurement is safer; verify.
- **Un-merging the antenna keepout** from fit-extents (§2) touches the tier-1 path that
  affects every board's routing — needs careful golden-harness verification.
- **Approval gate UX**: render+table is the MVP; how the human's NL adjustments map back to
  board.yaml deterministically.
- **board.yaml schema growth**: mounting_holes, per-terminal edge/order — keep the sidecar
  validator the single source of truth.
- **Does the antenna-opposite-edge rule ever conflict** with a partner-proximity preference?
  RFI wins per Brian, but document the tradeoff.
