# SPEC — Board Re-Fit + Terminal Centering (autoplacer RFE)

> **TWO independent, composing features** that together tighten + balance the auto-placed board:
> **Part A — Board Re-Fit** (size the board to the *measured* cluster, not the over-estimate) and
> **Part B — Terminal Centering** (center each edge's terminal group instead of packing it at one
> end). Audio-remote opts into both. **BOTH COLD-REVIEWED** (Part A: 3 agents, 4 blockers; Part B:
> 2 agents, 2 blockers — extend the drift test to cover `anchor="center"`, and don't claim to fix
> the pre-existing §4.1 layout gap). All `[CR-fixed]` inline.
>
> Authored against `claude/recursing-kowalevski-365bfb` @ `e27a3b6` (multi-edge, just shipped).
> Re-confirm line numbers before editing.
>
> **Why both — neither alone closes the gap.** *Cluster-centering is NOT viable*: the MCU antenna
> keepout overhangs the top edge by design, pinning the cluster there — moving it down un-overhangs
> the antenna. So the gap is attacked two other ways: **Re-fit** recovers the *sizing over-estimate*
> (≈ `C − measured_cluster` − routing slack ≈ ~8–12 mm height on audio-remote, 72 → ~62–64 mm,
> ~10–15 % area). **Terminal-centering** does NOT shrink the board — it fixes the *look*: today
> terminals pack at one end of their edge (bottom row left-packed, side terminals top-packed),
> leaving the far ends empty; centering fills the edges symmetrically. Together: a tighter board
> that also reads as balanced. The residual ~9 mm reserve gap (routing margin + corner-hole inset
> between cluster and terminal band) is structural and not recoverable without routing risk.

## 1. Context & motivation (measured)

The auto-sizer over-estimates the board, leaving an internal empty band. On the audio-remote
**multi_edge** board (88×72 mm, the best we get today):

- interior cluster (U/R/C parts) actually occupies **y 0–39 mm** (plus the MCU antenna overhanging
  *above* the top edge);
- the bottom field-wiring terminal row sits at the bottom edge, **y ~60–72**;
- → a **~21 mm empty band (y 39–60)** between them.

The placed-content bounding box already fills 0–88 × 0–72 (terminals touch every edge, the cluster
touches the top), so a **naive crop does nothing** — the waste is *internal*, between the cluster
(top) and the terminal rows (anchored to the far edges). Single-edge boards have the same vertical
gap.

**Root cause:** the sizer models the cluster as a **square** `C = max(√(interior_area ·
routing_factor), max_interior_dim)`, `routing_factor = 2.5` (`_content_aware_size`,
`pcb_pipeline.py:~419`; mirrored in `_size_from_assignment`, `edge_terminal.py:~325`). For
audio-remote `C ≈ 50 mm`, but the cluster's *actual placed* height is ~39 mm. The height formula
reserves the full `C`; edge terminals + corner holes anchor to the over-reserved edges → the gap.
`routing_factor = 2.5` is deliberately generous for routability, so it cannot be lowered globally.

**Outcome:** measure the cluster *after* placement and re-size+re-place against a board that hugs
it. `[CR-fixed: realistic estimate]` The recoverable height is bounded by `C − measured_cluster_h`
(≈ 50 − 39 ≈ 11 mm) MINUS the routing slack added back — so audio-remote ≈ **88×72 → ~88×62–64 mm**
(~10–15 % area), not the ~55 mm the draft over-claimed. The residual gap is structural
(padding + corner reserve between cluster and terminal band) plus the cluster being placed high
(the separate cluster-centering nit). The exact figure is **measured at the eyeball gate** (§12).
Helps single_edge boards equally.

## 2. Settled decisions

- **D1 — Opt-in flag, default OFF.** New `board.yaml board_refit: true|false`, default `false`
  (resolved exactly like `terminal_distribution`). Re-fit moves *every auto-sized board's*
  dimensions, which would force re-baselining every integration dimension-window at once; a flag
  lets the audio-remote tests opt in and pin the new tighter windows while the other three goldens
  stay **byte-identical** (flag off → no second pass → zero diff). Flipping the default ON later is
  a *separate, deliberate* change with its own window re-baseline (the no-op guard, §3, already
  makes a future flip safe for already-tight boards). Matches the multi-edge precedent.
- **D2 — Re-fit only when `auto_sized`.** Never re-fit a board the user/intent dimensioned (they
  may have sized it for an enclosure). `_resolve_board_size` already says when dims came from the
  user vs auto.
- **D3 — Spec-then-cold-review**, no implementation in the authoring session.

## 3. Architecture — two-pass (chosen over crop-and-translate)

**Why not crop-and-translate.** Every downstream position is *derived from* the board dims, not
stored independently: edge terminals via `layout_along_edge` anchored to
`GetBoardEdgesBoundingBox()`; corner holes at the bbox corners; the antenna overhang relative to
the board edge; silk offsets keyed off `terminal_edges` + board size. Translating would have to
re-derive all four by hand (and re-run per-edge layout anyway, since a shrunk along-axis may
overflow a side) — i.e. re-implement the placement engine's invariants in a second, untested
location. Two-pass instead reuses `_step_smart_placement` **verbatim** for pass 2, so every
invariant the integration gates assert (pads-on-board, antenna overhang, holes-at-corners, natural
order, wire-entry-outward, silk-clear) is re-established by the *same* engine on the *final*
outline; routing then runs once, on that outline.

**Sequence** (in `build_pcb_from_schematic`, between smart_placement and the `terminal_edges`
extraction):

1. **Pass 1** — create+outline → holes → load → nets → merge hints → smart_placement, exactly as
   today. **Capture `auto_sized = step.get("auto_sized")`** into a local var (today it's only read
   for a warning and dropped — `[CR-fixed]`); re-fit needs it for D2. Also have pass-1's
   `_estimate_board_size` **return `comps`** (the measured footprint list) so pass 2 can reuse it
   without re-running the bridge script — `[CR-fixed: the function doesn't expose comps today; add
   it to the return dict and thread it through `_step_create_pcb_and_outline`'s result]`.
2. **Measure** — new `_step_measure_cluster(pcb_path, edge_placed_refs)`: union the body bboxes
   (reuse `BODY_EXTENT_HELPER`/`body_bbox`, antenna keepout excluded) of the footprints that are
   the **interior cluster** → `cluster_w × cluster_h`. `[CR-fixed: define "cluster" by EXCLUSION,
   not by is_terminal]` Exclude (a) the **edge-placed terminals** — pass the set of refs that got a
   `rotation_chosen` edge from pass-1's `placement_decisions`, NOT a designator-class test; and
   (b) **mounting holes** — `_ref_class(ref) == "H"` explicitly (H is NOT in
   `_EDGE_DESIGNATOR_CLASSES`, so an `is_terminal` filter would wrongly KEEP the corner holes and
   inflate the bbox to the full board → the no-op guard fires → feature silently does nothing).
   Everything else is cluster — crucially this **INCLUDES interior module headers** (J6 the OLED,
   `edge:"none"`, spiral-placed inside the cluster): they're `is_terminal=True` for sizing but
   physically sit in the cluster, so the measure must count them or it under-sizes.
3. **Recompute dims** — call `_estimate_board_size(cluster_wh=(cluster_w, cluster_h), comps=<pass-1
   comps>)` (§4); with `comps` supplied it SKIPS the bridge script and reuses the cached terminal
   `w/h` + `antenna_side`. **No-op guard:** if the new dims aren't meaningfully smaller (within
   ~1 mm on both axes), **skip re-fit** — board unchanged, no second pass (declines on already-tight
   boards, protecting the other goldens even with the flag on).
4. **Pass 2** (only if smaller) — `[CR-fixed: DO NOT reuse `_step_create_pcb_and_outline` — its
   first sub-script calls `CreateEmptyBoard()` and WIPES all footprints+nets]`. First
   **`shutil.copy2(pcb_path, pcb_path + ".pass1")`** (the fallback backup, §5). Then a NEW
   **`_step_redraw_outline(pcb_path, w, h)`** that runs ONLY the Edge.Cuts removal+redraw sub-script
   (the second half of `_step_create_pcb_and_outline`, via `LoadBoard` — never `CreateEmptyBoard`),
   so placed footprints/nets survive. Then **re-place holes** (§6). Then re-run
   `_step_smart_placement` against the tighter board with fresh `edge_of` hints from the pass-2
   `distribute_terminals`. Footprints persist (placement only `SetPosition`s them), so
   load/nets need NOT repeat.
5. **Fit-fallback guard** — restore pass-1 (copy `pcb_path + ".pass1"` back) and record an event if
   pass-2 reports ANY of: non-empty `failed_placements`, a `terminal_edge_crowded` event, or a
   `keepout_fallback_interior` event (the MCU dropped to the interior → antenna no longer overhangs)
   — `[CR-fixed: failed_placements alone misses these soft regressions]`. This makes re-fit
   **monotone FOR PLACEMENT** — it never ships a board that doesn't seat. `[CR-fixed: it does NOT
   guarantee routing]` — pass-2 routing completeness is gated only by the integration test (§9-A3),
   so the "monotone" claim is placement-only.

The existing `terminal_edges` extraction, `antenna_frame_mismatch` guard, silk, and autoroute then
run on whichever board won (they read from `step["placement_decisions"]`). **GND copper zones must
not be added before pass 2** (they'd be silently dropped by the autoroute DSN export anyway; zones
already run after autoroute, so this holds — `[CR-noted]`).

## 4. The `cluster_wh` override (size-formula feedback)

The cluster term lives in two places that must stay consistent: `_content_aware_size`
(`pcb_pipeline.py:~444`) and `_size_from_assignment` (`edge_terminal.py:~337`) — both model the
cluster as a square `C`. The measured cluster is a **rectangle**.

- **`_size_from_assignment`** (`edge_terminal.py:~325`): add `cluster_wh: Optional[tuple] = None`.
  When set, replace the square with axis-specific values in the canonical ALONG/CROSS frame:
  ```
  along_cluster, cross_cluster = (cluster_w, cluster_h) if horizontal else (cluster_h, cluster_w)
  along_dim = max(along_cluster, _along(p_ts)) + d_a + d_b + 2·padding + 2·corner_inset
  cross_dim = max(cross_cluster, _along(a_ts), _along(b_ts)) + d_p + 2·padding + 2·cross_corner
  ```
  Keep the `max(cluster_dim, max_interior)` floor. **`cluster_wh is None` ⇒ the square-`C` path is
  byte-identical to today** (the regression lock — assert it).
- **`distribute_terminals`** (`edge_terminal.py:~368`): add `cluster_wh=None`, thread into the
  `_size` closure.
- **`_estimate_board_size`** (`pcb_pipeline.py:~466`): add `cluster_wh=None` AND `comps=None`. When
  `comps` is supplied (pass 2), **skip the bridge measurement script entirely** and use those
  cached comps; pass `cluster_wh` into `distribute_terminals`. Pass 1 calls it with neither
  (byte-identical to today) and now **returns `comps` in its result dict** so the orchestration can
  hand them to pass 2. `[CR-fixed: the function didn't expose comps; this is the plumbing that makes
  "don't re-run the bridge script" actually possible]`.

**Routing slack — the key tunable.** Do NOT pass the raw measured `cluster_h`: routing still happens
*inside* the cluster. Inflate the measured dimensions by a modest `cluster_routing_margin_mm` per
side (new tunable, start generous) — NOT the full 2.5× area factor, since the measured bbox already
reflects the spacing the placer actually used. The right value is **empirical against the golden
routing counts** (§9-A3, §10-R1).

## 5. Multi-edge interaction

The sizer is already unified through `distribute_terminals`/`_size_from_assignment`, so the
`cluster_wh` override feeds **both** modes for free:
- **single_edge** audio-remote: the gap is the CROSS-axis `cluster` term; measured `cluster_h≈39`
  replaces `C≈50` → shrinks vertically. Primary target.
- **multi_edge**: the cluster square over-reserves both axes; measured `cluster_w×cluster_h` tightens
  both. The peel search re-runs in pass 2 with the measured cluster, so pass-2 `edge_of` may differ
  (tighter) — consistent end-to-end since pass 2 re-places with the pass-2 hints. **Flag:** the
  multi-edge dimension-window gate (`max/min < 1.6`, `area < 129·65`) shifts *strictly better*
  (squarer+smaller) but must be re-validated, not assumed.

## 6. Mounting holes in pass 2

Holes are added *before* placement in pass 1, and their keepout **ZONE** objects are standalone
`board.Add(z)` rule-areas (NOT footprint children, so they don't move with `SetPosition`). On pass 2,
naively re-adding holes would duplicate them (H5–H8) and leave stale keepout zones. **Decision:**
add a small `_step_remove_mounting_holes` (delete `H*` footprints + their rule-area zones) and
**remove + re-add** via the existing `_step_add_mounting_holes` on the new outline. This keeps holes
flowing through the single tested code path and avoids stale keepouts. (Do NOT "optimize" this into
a translate — that reintroduces the stale-zone bug; see §9-A5.)

`[CR-fixed: the keepout zones have NO ref linkage to their H* footprint — they're anonymous
rule-area ZONEs.]` `_step_remove_mounting_holes` must therefore identify them by type+geometry:
delete every footprint whose `_ref_class == "H"`, and every **rule-area** zone (`GetIsRuleArea()`)
whose centre is near a current hole position (or, simpler and robust: delete ALL rule-area zones
that are NOT the MCU antenna keepout — but the antenna keepout lives on the MCU footprint, not as a
board-level zone, so "delete all board-level rule-area zones" is in fact safe here; verify no other
board-level rule-area zones exist before relying on that).

## 7. Opt-in flag plumbing

`board.yaml board_refit: true|false` → `BoardSidecar` field + `_KNOWN_SIDECAR_KEYS` + bool
validation + `intent.source["board_refit"]` (mirror `terminal_distribution` exactly,
`sidecar.py`). Read in the orchestration: `_refit = design_intent.source.get("board_refit", False)`;
attempt re-fit only when `_refit and auto_sized`.

## 8. Tests

### 8.1 Pure (`tests/test_terminal_distribution.py`, `tests/test_board_sizing.py`)
- **Regression lock (critical):** `_size_from_assignment(cluster_wh=None)` ≡ today's square path,
  across the existing parametrization — no drift.
- `cluster_wh=(w,h)` with an oblong cluster shrinks the CROSS dim to a hand-computed value;
  width within tolerance.
- Reduction property: audio-remote interior set, `cluster_wh=(measured)` → height strictly below the
  square-`C` height.
- Both modes consume `cluster_wh`; multi-edge peel still yields valid per-edge assignments + a
  smaller board.
- **No-op guard:** measured cluster ≈ square `C` → recomputed dims within skip tolerance → re-fit
  declines.
- The embedded-script **drift test** still passes (`cluster_wh` is parent-side only; the placement
  helper string is untouched).

### 8.2 Integration (`tests/integration/test_firmware_pcb_pipeline.py`)
New `board_refit: true` audio-remote variant (mirror the multi-edge test):
- board shrinks (new `bh` strictly below the pass-1 height; area below pass-1);
- **still routes** within `_assert_mostly_routed(max_unrouted=…)` — the key empirical check that
  slack tuning didn't starve routing;
- terminals still on their assigned edges, natural order, wire-entry **outward**;
- `_refs_with_pads_off_board` empty (rotation-sign invariant on the new outline);
- the MCU still in `_refs_with_keepout_overhang` (antenna overhangs the *new* edge);
- exactly **4** holes, at the *new* corners (no H5–H8 — pins §6);
- silk legends added, `_op_check_silkscreen_overlaps` clear of pads;
- `failed_placements` empty (fit guard held);
- a synthetic already-tight board → re-fit declines (dims == flag-off).
- **Regression:** the three non-audio-remote goldens with the flag absent are byte-identical.

## 9. External-system assumptions — REVIEW FIRST (highest severity)

- **A1 — Re-drawing Edge.Cuts mid-pipeline `[CR-fixed: the draft said "reuse
  _step_create_pcb_and_outline" — that calls `CreateEmptyBoard()` FIRST and WIPES the board]`.**
  Pass 2 must use the NEW `_step_redraw_outline` (the Edge.Cuts removal+redraw via `LoadBoard`
  only). Verified-OK: that sub-script touches only drawings (footprint positions/nets intact), and
  each pcbnew sub-script `LoadBoard`s fresh so the pass-2 placement script sees the NEW outline via
  `GetBoardEdgesBoundingBox()`. Confirm `_step_redraw_outline` never calls `CreateEmptyBoard`.
- **A2 — Moving placed footprints in pass 2.** Assumes `SetPosition`/`SetOrientationDegrees` on
  net-assigned footprints preserves pad-net connectivity (nets live on pads, independent of
  position) — pass 2 runs after nets are assigned, so routing sees the same netlist.
- **A3 — FreeRouter on the smaller outline (HIGHEST RISK).** Routing runs once, on the final
  (smaller) board. A too-aggressive shrink starves routing. The slack margin + `failed_placements`
  fallback guard *placement* fit, NOT routing completeness — only the integration gate catches a
  routing regression. **Run the audio-remote build several times (FreeRouter is stochastic) when
  tuning the slack**; bump passes, never loosen the unconnected bound.
- **A4 — Antenna keepout DRC-clean overhanging the NEW edge.** The MCU keepout must overhang the
  closer edge cleanly with no new collision on the tighter board. Gated by
  `_refs_with_keepout_overhang` + `failed_placements`, but **DRC isn't in the gate** — manual DRC
  spot-check on the re-fit audio-remote during bring-up.
- **A5 — Hole keepout zones are ZONEs, not moved by `SetPosition`.** The §6 remove+re-add avoids
  stale zones; flag so a reviewer doesn't refactor it into a translate.
- **A6 — Autoroute DSN export preserves rule-area zones `[CR-added]`.** `_export_dsn`
  (`pcb_autoroute.py:~178`) removes only non-rule-area (copper pour) zones; the mounting-hole
  keepouts (rule-area) survive into the DSN so FreeRouter routes around them. The refit relies on
  this (re-added hole keepouts must still be honored). Currently true but unnamed/un-tested — add a
  test that rule-area zones survive the DSN export, so a future "remove all zones" change can't
  silently let routing cross the hole keepouts.

## 10. Open design questions

- **R1 — `cluster_routing_margin_mm`** (the routing slack on the measured cluster): empirical,
  per-golden, gates on routing completeness the unit tests can't cover. Start generous, tighten.
- **R2 — Default off now; flip to default-on later?** Recommend off; flipping is a separate
  window-rebaseline change.
- **R3 — Pass-2 placement non-determinism.** Placement is connectivity-driven and not guaranteed
  identical between passes; the fit-fallback makes it *safe* (never ships under-fit) but means
  re-fit occasionally *declines*. Confirm decline-silently is the desired UX vs. erroring.

## 11. Critical files (@ `e27a3b6`)

| File | Change |
|---|---|
| `src/kicad_mcp/tools/pcb_pipeline.py` | new `_step_measure_cluster(pcb_path, edge_placed_refs)`, `_step_redraw_outline(pcb_path,w,h)` (Edge.Cuts-only, NO `CreateEmptyBoard`), `_step_remove_mounting_holes`; `_estimate_board_size` gains `cluster_wh`+`comps` AND returns `comps` (:~466); the pass-2 block + backup/restore + fit-fallback in `build_pcb_from_schematic` (~2125→2150); capture `auto_sized`; `board_refit` resolve (next to `terminal_distribution` :~2048) |
| `src/kicad_mcp/utils/placement/edge_terminal.py` | `cluster_wh` override in `_size_from_assignment` (:~325) + `distribute_terminals` (:~368) |
| `src/kicad_mcp/utils/firmware/sidecar.py` | `board_refit` bool field + `_KNOWN_SIDECAR_KEYS` + validation + `intent.source` |
| `tests/test_terminal_distribution.py`, `tests/test_board_sizing.py` | `cluster_wh` math + regression-lock no-drift (§8.1) |
| `tests/integration/test_firmware_pcb_pipeline.py` | `board_refit: true` gate + window re-baseline (§8.2) |
| audio-remote inline `board.yaml` (test, `:424`) | `board_refit: true` (a new test variant; keep the existing ones for the no-refit default) |

## 12. Implementation order (post-review)

1. `cluster_wh` override in `_size_from_assignment`/`distribute_terminals` + pure tests incl. the
   `cluster_wh=None` regression lock (no behaviour change yet).
2. `_estimate_board_size(cluster_wh=…, comps=…)` consuming it (skip-measure when comps supplied) AND
   returning `comps`; thread comps out through `_step_create_pcb_and_outline`'s result.
3. `_step_redraw_outline` (Edge.Cuts-only), `_step_measure_cluster(edge_placed_refs, exclude H*,
   include interior J6)`, `_step_remove_mounting_holes`.
4. Pass-2 block in the orchestration: capture `auto_sized`; backup (`shutil.copy2 …".pass1"`);
   no-op guard; fit-fallback on `failed_placements ∪ terminal_edge_crowded ∪
   keepout_fallback_interior`; the `board_refit` flag through sidecar→intent.
5. Integration gate + opt audio-remote variant in; re-baseline the multi-edge/refit dimension
   windows; add the rule-area-survives-DSN test (A6).
6. **Eyeball + DRC gate:** build+render the re-fit audio-remote, tune `cluster_routing_margin_mm`,
   confirm the gap closes, routes 0–few unconnected across several FreeRouter runs, antenna still
   overhangs, holes at new corners, silk clear, DRC clean. Verify the other three goldens
   byte-identical.

---

# PART B — Terminal Centering (NEW — needs cold review)

## 13. Terminal centering

**Problem.** `layout_along_edge` (`edge_terminal.py:218`) anchors the cursor at `edge_start +
margin` and packs terminals from the START of each edge, so a group shorter than the edge leaves
the FAR end empty: on audio-remote the bottom row (J1–J3) is left-packed and the side terminals
(J4/J5 left, J7 right) are top-packed. The board reads as lopsided even when correctly sized. This
is a **layout-quality / balance** change — it does NOT shrink the board (the edge length is fixed by
sizing; centering only repositions the group within it).

### 13.1 Design — `anchor="center"` in `layout_along_edge`
Add an `anchor: str = "start"` parameter (values `"start"` | `"center"`). `"start"` is today's
behaviour byte-for-byte (the regression lock). `"center"` works ENTIRELY within the `board_box`
`layout_along_edge` already receives — `[CR-fixed: the hole corner-clear is ALREADY baked into that
box by the caller (`lay_box = board ± corner_clear`, pcb_pipeline.py:~1293); centring must NOT
re-apply a corner offset — it uses the span as handed in]`:
```
edge_lo = board_box edge-start + margin        # exactly today's start cursor
edge_hi = board_box edge-end   − margin        # exactly today's `fits` limit
usable  = edge_hi − edge_lo                     # SCALAR span (CR-fixed: not len() of a 2-list)
total   = Σ items' along-extent + spacing·(n−1)
cursor0 = edge_lo + max(0, (usable − total) / 2)    # centre the group within usable
```
then advance exactly as today (`cursor += along-extent + spacing`). **Only the along-axis START
moves**; cross-axis anchoring (overhang pad-inside / body-outside), rotation, order, and the `fits`
overflow check are untouched. If `total > usable` (won't fit centred), `max(0,…)` keeps `cursor0 =
edge_lo` → falls back to start (never a negative offset, never past a corner).

`[CR-noted — PRE-EXISTING gap, NOT introduced by centring, NOT in this spec's scope]:` the multi-edge
**§4.1 adjacent-edge band** (a side terminal must clear the bottom band's depth at a shared corner)
is reserved in SIZING but is NOT yet enforced in `layout_along_edge` — `lay_box` insets only by the
hole `corner_clear` (~6.6 mm), not `max(corner_clear, depth(F)+spacing)`. This affects START anchoring
identically (both start at `edge_lo`), so centring neither causes nor cures it; on audio-remote the
side group has ~4.9 mm of slack so no overlap manifests. Fixing it (pre-adjust `lay_box` per shared
corner, or pass adjacent-band depths into `layout_along_edge`) is a separate follow-up — see
`SPEC_Multi_Edge_Terminal_Distribution.md` step 5. **Centring composes with whatever `lay_box` it is
handed**, so it picks up that fix for free once landed.

### 13.2 Composition with re-fit + multi-edge
Orthogonal. Centering is a pure repositioning inside `_step_smart_placement`'s edge layout; it runs
in BOTH passes of a re-fit (pass 2 centres on the tighter edges) and on every used edge of a
multi-edge board (each of bottom/left/right is centred independently within its `lay_box`). No
interaction with `cluster_wh` sizing. The only data dependency is the `lay_box` corner inset, which
the caller computes per-edge (and, once the §4.1 follow-up lands, per pass-2 `edge_of`).

### 13.3 Flag
`board.yaml terminal_centering: true|false`, default `false` (mirror `terminal_distribution` /
`board_refit`: field + `_KNOWN_SIDECAR_KEYS` + bool validation + `intent.source`). Read in the
orchestration and threaded to `_step_smart_placement` → `layout_along_edge(anchor=…)`. **Default-off
rationale:** centering repositions terminals on EVERY board, which changes FreeRouter's net topology
(stochastic) and could nudge a golden over its unrouted bound — so opt-in keeps the other three
goldens' routing untouched, even though the position checks (edge membership, natural order,
wire-entry outward, pads-on-board, silk-clear) would all still pass (they don't assert exact x/y).
Audio-remote opts into `board_refit` AND `terminal_centering`. *(Open: could be merged with
`board_refit` into one "tidy board" flag — see §15-R4.)*

### 13.4 Tests
- **Pure** (`tests/test_edge_terminal_placement.py`): `anchor="start"` ≡ today (regression lock,
  parametrize existing `layout_along_edge` tests). `anchor="center"`: a short group on a long edge
  gets equal margins both ends; a group that exactly fills the edge is unchanged; an over-long group
  falls back to start (no negative offset, no corner overlap).
- **Drift test MUST be EXTENDED to exercise `anchor="center"`** `[CR-fixed: BLOCKER — the existing
  drift test (`test_edge_terminal_placement.py:~367`) calls `layout_along_edge` with NO `anchor`, so
  it runs only the default `"start"` path and would NOT catch a centring bug present in only ONE
  copy. Add an `anchor="center"` case to the exec-compare so a helper-string-only divergence is
  caught at unit time, not just at the integration test.]`
- **Integration** (`tests/integration/test_firmware_pcb_pipeline.py`, the `terminal_centering: true`
  variant): each used edge's terminal group is centred (group centre within ~margin of the edge
  centre, between the corner reserves); terminals still on their edges, natural order, wire-entry
  outward, pads-on-board, silk clear; still routes within `_assert_mostly_routed`. The three other
  goldens with the flag absent are byte-identical.

### 13.5 External-system assumptions (Part B)
- **B1 — `layout_along_edge` is in `EDGE_TERMINAL_HELPER`** (the injected pcbnew-script string), so
  the `anchor` param + centring logic must be added to BOTH copies (Python `:218` + helper string
  `:549`). `[CR-fixed: the drift test does NOT currently guard this — it must be extended to pass
  `anchor="center"` (§13.4); without that, a half-updated helper surfaces only as a `TypeError` at
  integration time.]` HIGHEST Part-B risk. (Re-fit's `cluster_wh` was parent-side only; centering
  runs INSIDE the placement script, hence the two-copy exposure.)
- **B2 — Repositioning changes FreeRouter topology** (stochastic) — only the integration routing
  gate catches a regression; run the build several times when validating. Same class as re-fit A3.

## 14. Critical files (Part B, @ `e27a3b6`)

| File | Change |
|---|---|
| `src/kicad_mcp/utils/placement/edge_terminal.py` | `anchor` param + centring in `layout_along_edge` (`:218`) AND in the `EDGE_TERMINAL_HELPER` string copy (`:549`, the helper assignment is at `:444`) — keep them drift-identical |
| `src/kicad_mcp/tools/pcb_pipeline.py` | `[CR-fixed: name the plumbing]` add `anchor` param to `_step_smart_placement` (`:~790`) → put it in the `run_pcbnew_script(params={…})` dict (`:~1444`, exactly how `corner_clear_mm` flows) → embedded script reads `params.get("anchor","start")` and passes it to the `layout_along_edge` call (`:~1298`); orchestration resolves `terminal_centering` from `design_intent.source` next to `board_refit` and passes `anchor="center" if _tc else "start"` |
| `src/kicad_mcp/utils/firmware/sidecar.py` | `terminal_centering` bool field + `_KNOWN_SIDECAR_KEYS` + validation + `intent.source` |
| `tests/test_edge_terminal_placement.py` | `anchor="center"` math + `anchor="start"` regression lock + drift test |
| `tests/integration/test_firmware_pcb_pipeline.py` | `terminal_centering: true` gate |

## 15. Combined open questions

- (Part A) R1 `cluster_routing_margin_mm`; R2 default-on later; R3 pass-2 non-determinism (above).
- **R4 — One flag or two?** `board_refit` + `terminal_centering` are orthogonal and independently
  testable, but a user wanting a "tidy compact board" must set both. Consider a single
  `compact_layout: true` umbrella, or keep them separate. Cold reviewer / Brian to decide.
- **R5 — Order of implementation.** Part B (centering) is smaller, lower-risk, and independently
  shippable — recommend doing it FIRST (it's a contained `layout_along_edge` change with no
  two-pass/routing-shrink risk), then Part A (re-fit). They compose regardless of order.
