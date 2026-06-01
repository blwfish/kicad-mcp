# Implementation Plan: Human-Rational Autoplacement System

Companion to `docs/SPEC_Autoplacer_Human_Rational_System.md`. Produced by a fresh-context
architect pass after the spec's cold review (2026-06-01). Each phase is an independently
shippable, golden-harness-gated increment in the Phase-1–8 style the firmware front end used.
Every phase keeps `tests/integration/test_firmware_pcb_pipeline.py` green (run with
`KICAD_INTEGRATION=1`, against KiCad 9.0 **and** 10.0 at `/Volumes/Files/claude/kicad-versions/{9.0,10.0}/`).

## v1 scope
**Ship phases 1, 2, 3, 4, 6.** Defer phase 5 (mounting holes) and phase 7 (approval gate) to v1.1.
Phases 1→3 are tightly coupled (un-merge enables the overhang → overhang edge drives the
antenna-opposite terminal default → `WIRE_ENTRY` orients the terminals) and must ship together to
be coherent; phase 4 delivers the headline win (≈70×60 vs the hardcoded 110×90); phase 6 is small
and non-fatal. Mounting holes and the approval gate are mechanically independent of placement
*quality* and the gate's hardest part (deterministic NL→board.yaml) is the spec's own unresolved
UX question — both best built after the quality core is proven.

## Ground-truth code anchors (confirmed during planning)
- The four golden shapes all pass **explicit** board dims today (`test_firmware_pcb_pipeline.py:139,188,255,352,400`),
  so `_estimate_board_size` (`pcb_pipeline.py:245`) / the auto-size branch (`:157-176`) are **not
  exercised by the harness at all** — §4 must flip a shape to auto-size or it ships untested.
- Keepout→fit-extent merge: `pcb_pipeline.py:656-660` (the four `max(...)` lines), fed by the
  collection loop `:643-660`; `ext_*` flow into `fits_on_board` (`:759`), the tier-1 edge loop
  (`:828-864`), and `layout_along_edge`/`rotate_extents`.
- Terminal rotation today: `pcb_pipeline.py:938` `rotation_to_face((pad_cx,pad_cy), inward_normal(edge))`
  — pad-centroid driven. Helpers DUPLICATED: importable defs `edge_terminal.py:66,140`; verbatim
  source-string copy in `EDGE_TERMINAL_HELPER` (`edge_terminal.py:324-461`, defs `:342,:381`),
  concatenated into the bridge script at `pcb_pipeline.py:567`. Drift gate:
  `tests/test_edge_terminal_placement.py:303` `TestEdgeTerminalHelperSource`.
- `H` ∈ `EDGE_CLASSES` (`pcb_pipeline.py:704`) → tier-2 edge placement today; `is_screw_terminal_class`
  rotates only `J` (`edge_terminal.py:39-44`).
- Sidecar four-place coupling: `BoardSidecar` (`sidecar.py:85-93`), `_validate` (`:148-178`, silently
  ignores unknown top-level keys), `load_sidecar` (`:181-200`), `apply_sidecar` (`:222-282`).
- Disposition pattern to mirror for the WIRE_ENTRY generator: `prefetch.py:50-111` +
  `scripts/prefetch_cards.py` (`high`/`low`/`skip`).

---

## Phase 1 — Antenna keepout un-merge + immediate golden re-route (closes H1b)
**Goal:** stop merging the antenna keepout into board-containment extents so it can overhang the
edge; prove the four golden boards still route on the real router.

- **Touches:** `pcb_pipeline.py:643-674` collection loop in `_step_smart_placement`. Remove the four
  merge lines `:657-660`. Keep `keepout_rel`/`keepout_side`/`has_keepout` (`:652-655`) — they still
  drive tier-1 edge selection + collision tracking via `keepout_boxes`. Result: `ext_*` become
  courtyard-only (containment); the keepout is reserved off-board and tracked through
  `place_at`/`keepout_boxes` (`:795-798`) and `hits_keepout` (`:776-781`). No `edge_terminal.py` /
  `EDGE_TERMINAL_HELPER` change.
- **Gate:** all four shapes stay within their `_assert_mostly_routed` bounds (this is the
  FreeRouter-tolerance check H1 left open — DRC-clean ≠ router-happy). For the ESP32-bearing shapes
  assert the MCU keepout-zone bbox extends **outside** `Edge.Cuts` while the MCU **pads** stay
  on-board (`_refs_with_pads_off_board:79` must still be empty for the MCU).
- **Risk:** highest blast radius — changes sizing/placement on **all four** shapes (keepout is on
  both `ESP32-S3-WROOM-1` and `ESP32-WROOM-32`). If a structurally-hard net drifts over bound, bump
  that board's `max_unrouted` by 1 with a comment (heuristic noise) — not a revert. If FreeRouter
  refuses the overhang, fall back to a thin on-board keepout margin and document. Threshold rule:
  the off-board/on-board tolerance (`tol=0.05`) — test the keepout corner at edge ± epsilon.
- **Effort: M** (tiny diff, whole-harness re-verify on 9+10).

## Phase 2 — WIRE_ENTRY family table + maintainer generator (behind the old oracle)
**Goal:** build the single-source `WIRE_ENTRY` table + maintainer generator; do **not** switch the
engine yet.

- **Touches:** new `src/kicad_mcp/utils/placement/wire_entry.py` — `WIRE_ENTRY = {family: (ux,uy)}`
  keyed by normalized footprint family, value = 0°-frame wire-entry unit vector; plus
  `normalize_family(footprint_name)` (parse the MKDS/`_Horizontal`/`_Vertical` shapes from
  `connectors.py:42-47`). New `scripts/wire_entry_gen.py` (maintainer CLI mirroring
  `scripts/prefetch_cards.py`): pure S-expr parse of `.kicad_mod` (no KiCad launch), rule = *wire-entry
  face is the side where courtyard/fab overhangs the pad row more*, emit `(vector, asymmetry_mm,
  confidence)`, `high`/`low`/`skip` disposition. **Never** key off the silk pin-1 arrow (it points the
  wrong way). New `tests/test_wire_entry.py`.
- **Gate:** no integration behavior change (engine still pad-centroid) → all four stay green. Unit
  gate: generator reproduces the verification-log measurements — MKDS-1,5-{2,3}-5.08 Horizontal →
  −Y at 0°, asymmetry ≈0.6 mm = `high`; vertical pin header → 0.000 mm = `skip`; bit-identical across
  the 9.0 and 10.0 trees.
- **Risk:** low/additive. Threshold rule is central: the ≥0.4 mm asymmetry threshold needs
  at/below/above coverage (0.6 `high`, 0.000 `skip`, synthetic 0.4 and 0.4±1e-9). Data-capture rule:
  mark low-margin families for human audit, never auto-trust.
- **Effort: M.**

## Phase 3 — Switch terminal orientation onto WIRE_ENTRY (cross-bridge, single-source)
**Goal:** replace pad-centroid orientation with the table; pad-centroid demotes to an explicit,
event-emitting fallback.

- **Touches BOTH the importable helper AND the string copy:**
  - `pcb_pipeline.py:567` — interpolate the `WIRE_ENTRY` literal into the bridge script source
    (`... + f"WIRE_ENTRY = {WIRE_ENTRY!r}\n" + EDGE_TERMINAL_HELPER + ...`) so the one dict in
    `wire_entry.py` is the single source across the bridge. **Do NOT hand-copy** (Syntactic-Semantic
    Seam / single-source-of-truth).
  - `pcb_pipeline.py:935-940` — rotation branch: look up
    `WIRE_ENTRY[normalize_family(footprint_name)]`; if found `ang = rotation_to_face(wire_vec,
    outward_normal(edge))` (wire-entry faces off-board); else fall back to pad-centroid **and emit a
    `rotation_fallback` decision event** (new kind in `_emit_placement_decision:33-51`). Capture
    `footprint_name` in the collection loop (`:590`, from `fp.GetFPID()`).
  - **Preferred:** keep `normalize_family` parent-side-only and pass the resolved per-ref wire-vector
    into the script via params (the way `placement_hints` already crosses, `:685,:1092`) to avoid
    growing the drift-prone string. If any logic *does* enter the string, add it in both places and
    extend `TestEdgeTerminalHelperSource` (`tests/test_edge_terminal_placement.py:303`, `HELPER_FUNCS`
    `:307`).
- **Gate (M1 oracle fix):** replace the pad-geometry orientation oracle — assert each synthesized
  terminal's **body/wire-entry side outboard, pads inboard**, using the WIRE_ENTRY table + expected
  outward normal (NOT pad-centroid). audio-remote (`:206`) is the carrier (already checks orthogonal
  rotation + natural order `:276-283` + off-board pads `:289`): add that the MKDS angles match the
  WIRE_ENTRY prediction and at least one `rotation_chosen` is WIRE_ENTRY-sourced (not fallback). All
  four stay within bound.
- **Risk:** `_rotate_vec` sign (`edge_terminal.py:103-113`) is empirically pcbnew-matched only for the
  pad-centroid→inward case; switching to wire-vec→outward must preserve it — if it mirrors, pads fly
  off-board and `_refs_with_pads_off_board` catches it. Syntactic-Semantic Seam is the dominant rule.
- **Effort: L** (conceptual core).

## Phase 4 — Content-aware board sizing (§4) + M3 resolution
**Goal:** replace area×2.5+keepout with cluster-interior + per-edge perimeter + corner-inset +
overhang allowance, computed **after** edges are chosen.

- **Touches:** `pcb_pipeline.py:245` `_estimate_board_size` and `pcb_planning.py:108-135` — extract the
  new sizing math into one importable pure helper both call (also fixes the min_dim-padding divergence
  at `pcb_pipeline.py:283-284`). Auto-size branch `:157-176` + orchestrator size resolution `:1479-1502`.
- **M3 resolution (no circular dependency, no second expensive run):**
  1. **Pre-placement estimate:** edge assignment needs only keepout side + board.yaml hints + the
     antenna-opposite default — none require packing. Size = interior-cluster area-pack estimate
     (courtyard-area sum of MCU + actives + passives + interior `edge:"none"` headers × routing_factor,
     sqrt-to-aspect) + per-terminal-edge perimeter bands + corner insets + overhang allowance.
  2. **Post-approval fit-check:** the approved size becomes the outline; `_step_smart_placement`'s
     existing strict checks (`fits_on_board:759`) pack within it. If any part can't seat
     (`failed_placements:1064`), v1 widens by the deficit and emits a `size_underestimate` event
     (re-trigger-the-gate variant ships with §5 in v1.1).
- **Gate:** flip **audio-remote** to `board_width_mm=0, board_height_mm=0`; assert the estimate lands
  within tolerance of the content-aware target (≈70±8 × 60±8), still routes within bound, no
  `failed_placements`. Keep the other three on explicit dims to isolate. Unit test the extracted helper.
- **Risk:** under-size → unplaceable → routing collapse (caught by `_assert_mostly_routed` + new
  `failed_placements == []`). Threshold rule for all sizing arithmetic. **Sequence after the rest** so
  the corner-inset term is wired even though mounting holes ship in v1.1 (reserve 0 if no holes).
- **Effort: L.**

## Phase 6 — Silk legend per-pad signal + clear-of-body offset (§6)
**Goal:** each synthesized terminal's silk shows the per-pad signal (`+`/`−`, `OUTP`/`OUTN`,
`SDA`/`SCL`), offset clear of the body, never under it.

- **Touches:** `pcb_pipeline.py:1215-1289` `_step_silkscreen_legends`. The per-pad loop (`:1252-1271`)
  already labels by pad with `positions[idx]` and content from `connectors.py:191-218` `_build_legend`
  (already per-pad). Gap: offset direction — today fixed `ty = bb.GetTop() - margin` (`:1263`); make it
  face the **inboard** side per the placed orientation (wire-entry is outboard → labels go inboard),
  using Phase 3's `rotation_chosen` decisions.
- **Gate:** audio-remote already asserts no label overlaps a pad (`:307-318`) + labels added
  (`:262-265`). Extend: each label sits inboard of its terminal body bbox, and text equals the per-pad
  signal role (not the device name). Keep overlap-clean green.
- **Risk:** low — silk is strictly non-fatal (`:1583-1593`). Offset must not push labels off-board.
- **Effort: S.**

---

## Deferred to v1.1
- **Phase 5 — mounting holes as corner fixtures (§3):** four-place sidecar coupling (`sidecar.py`
  dataclass + `_validate` + `load_sidecar` + `apply_sidecar`) + **reject/warn on unknown top-level
  keys** (data-capture fix) + a fixture-commit step before tier-1 MCU placement that registers corner
  keepouts. Interacts with phase 4 sizing. Grep `tests/fixtures/` for stray board.yaml keys before the
  unknown-key rejection lands. **Effort: M.**
- **Phase 7 — approval gate (§5):** new `design.py` op `propose_placement` (runs the planning of
  phases 1/3/4/5 without `_step_smart_placement`/`_step_autoroute`, returns table + render);
  `build_pcb_from_schematic` gains an `approved` gate; NL→board.yaml stays a human/Claude workflow.
  **Effort: M.**

## Residual risk to settle before phase 1
1. **FreeRouter tolerance of the off-board keepout overhang** — H1 proved only `kicad-cli pcb drc`
   clean. Phase 1's gate surfaces it immediately; or de-risk first with a throwaway un-merged audio_s3
   route on both KiCad trees. If FreeRouter chokes, §2 needs a thin on-board keepout-margin compromise.
2. **`max_unrouted` bump policy** — phase 1 re-perturbs all four boards; agree a +1 bump with a comment
   is acceptable heuristic-noise, not a regression.
3. **Stray board.yaml key** — one grep of `tests/fixtures/` before phase 5's unknown-key rejection.
