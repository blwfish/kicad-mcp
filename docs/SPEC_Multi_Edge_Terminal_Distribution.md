# SPEC — Multi-Edge Terminal Distribution (autoplacer RFE)

> **STATUS: COLD-REVIEWED 2026-06-02 (3 fresh-context agents) — findings folded in below.**
> Authored, then cold-read by three independent clean-context reviewers (external-system
> assumptions / internal-consistency+references / geometry+heuristic). Their blockers and
> should-fixes are incorporated; the changed sections are tagged `[CR-fixed]`. Remaining items
> the reviewers flagged as needing a real-KiCad render or a human call are listed in §9/§10.
> Implementation may proceed per §12, treating §9's render-gated assumptions as must-verify.
>
> Authored against `claude/recursing-kowalevski-365bfb` @ `b4c8bb6`; line numbers re-confirmed
> accurate at review time (`_content_aware_size:404`, `_estimate_board_size:451`, single-edge
> gate `:491-520`, `terminal_edges` reconstruct `:2091`, sidecar `_KNOWN_SIDECAR_KEYS:116`,
> validation `:211/:217`). Re-confirm before editing.

## 1. Context & motivation

The human-rational autoplacer (shipped via PRs #58/#65) places **all** field-wiring screw
terminals on the single board edge **opposite the MCU antenna** (an RFI rule — keep field wires
away from the radio). On a board with several terminals this yields a wide **letterbox**:
audio-remote auto-sizes to **129×65 mm** with both side (N-S / left-right) edges bare and a large
empty interior band — roughly **45 cm² used of ~84 cm²**.

`RFE #1` (axis-aware corner reservation, shipped this session as commit `4e6a267`) only shaved the
vertical over-reservation; the dominant waste is the **single-edge shape itself**. This RFE
distributes terminals across the antenna-opposite edge **and the two side edges** to produce a
squarer, smaller board (the autoplacer memory estimated ~30–45% area reduction).

The single-edge design was a *deliberate* earlier decision: side-edge terminal legends point
**inboard** (toward the cluster) and crowd it, which is why one edge was chosen. This spec
re-opens that decision and reserves an inboard silk gap to make side edges viable.

**Intended outcome:** an opted-in board (audio-remote) comes out squarer and smaller, terminals
correctly oriented (wire-entry outward) and silk-labelled on up to three edges, still routing
0-unconnected — without changing the other three golden boards.

## 2. Settled decisions (do not re-litigate in review)

- **D1 — Opt-in flag, default `single_edge`.** New `board.yaml` key
  `terminal_distribution: single_edge | multi_edge`, default `single_edge`. Only audio-remote
  opts in. Rationale: the project rule forbids overfitting one board; defaulting on would perturb
  the other three goldens and their gates, and multi-edge only pays off on a wide letterbox.
- **D2 — Spec-then-cold-review.** No implementation in the authoring session (this one).

## 3. Architecture — one source of truth (CLAUDE.md Rule 3)

**Today the data flows placement → parent:** the embedded pcbnew script in `_step_smart_placement`
decides each terminal's edge (`pcb_pipeline.py:1176-1196`), and the parent *reconstructs*
`terminal_edges` from the script's `rotation_chosen` events (`pcb_pipeline.py:2091`) to feed the
silk step. The board is sized **before** placement (`_estimate_board_size`/`_content_aware_size`)
under the assumption that all terminals share one edge.

**Invert it: decide in the pure parent layer, pass edges down as hints.**

- New pure function in `src/kicad_mcp/utils/placement/edge_terminal.py`:

  ```python
  def distribute_terminals(
      cluster_mm: float,
      terminals: list[dict],         # [{"ref","w","h"}, …]  (w,h = body extents)
      antenna_edge: str | None,      # "top"|"bottom"|"left"|"right"|None
      *,
      mode: str = "single_edge",     # "single_edge" | "multi_edge"
      routing_factor, padding, spacing,
      corner_inset_mm, corner_center_inset_mm, side_silk_gap_mm,
  ) -> tuple[dict[str, str], dict]:  # (edge_of {ref:edge},  {"width_mm","height_mm"})
  ```

  It is the **single source** for BOTH (a) board dims and (b) per-terminal edge assignment.
- `_content_aware_size` (`pcb_pipeline.py:404`) / `_estimate_board_size` (`:451`) consume it for
  dims. `_estimate_board_size` already measures terminal `w/h` and derives `antenna_side`, so it
  has all inputs.
- The parent then passes the returned `edge_of` into `_step_smart_placement` as per-ref
  `placement_hints[ref] = {"edge": E}`, **reusing the existing edge-hint branch** at
  `pcb_pipeline.py:1159` (`elif hint.get("edge") in ("top","bottom","left","right")`). No new
  placement code path; the per-edge layout (`natural_ref_key` order, WIRE_ENTRY → outward-normal
  rotation) is unchanged and already handles all four edges.
- **Delete ONLY the assignment loop** (`pcb_pipeline.py:1179-1196` — the `pref_edges` build + the
  greedy capacity `for` loop); terminals now arrive pre-assigned. **KEEP the `antenna_edge`
  derivation at `:1176-1177`** (it reads the tier-1 `keepout_overhang` decision, not the
  distribution loop) — the `antenna_frame_mismatch` guard (§3.1) needs it as the script-side value
  to compare against. `[CR-fixed: deleting the whole block would remove the value the guard
  compares, so the guard could never fire.]` The `t2_terminals` collection (`:1166`) becomes a
  no-op for firmware boards (all carry an edge hint) but is kept as the fallback for any field
  terminal without a hint.

### 3.1 Antenna-edge frame agreement (a real subtlety — flagged for review)
There are **two** independent antenna-edge derivations and they must agree:
- the **script** computes `antenna_edge` from the tier-1 `keepout_overhang` decision, in the
  **post-rotation board frame** (`pcb_pipeline.py:1176`);
- `_estimate_board_size` derives `antenna_side` from the footprint's rule-area keepout in the
  **footprint 0° frame** (`:~530`).

Both name the same physical edge — the one the antenna overhangs. `[CR-fixed: the earlier
justification "MCU placed at 0°" was WRONG — the tier-1 placer ROTATES the MCU so its keepout
faces the chosen edge's outward normal (pcb_pipeline.py:~1078-1110). Agreement holds because both
derivations describe the antenna-overhang edge, NOT because the MCU stays at 0°. It could diverge
if tier-1 falls back to a non-preferred edge.]* The distribution decision uses the parent's value.
**Do not add a third computation.** Emit an `antenna_frame_mismatch` decision/event when the
script's `antenna_edge` (`:1176`) disagrees with the parent-provided one — so the divergence case
is loud rather than silent. See External-System Assumption A4.

## 4. Geometry `[CR-fixed — the first cut did NOT reduce to `_content_aware_size`; two reviewers flagged it]`

**Derive the multi-edge formula by GENERALIZING `_content_aware_size`, not approximating it.**
The current code (single edge, `terminal_edge_horizontal=True`) is, verbatim:
```
along = max(cluster, term_along) + 2·padding + 2·corner_inset_mm          → width
depth = cluster + term_depth     + 2·padding + 2·corner_center_inset_mm   → height
```
Key structural facts the first cut got wrong: padding + corner reservation are added **outside**
the `max(...)` (not inside); the depth axis uses `2·corner_center_inset_mm` (**twice**, both ends);
neither corner term nor the silk gap is folded into a per-edge `depth(E)`.

**Generalized formula (antenna on top → `bottom` primary, `left`/`right` are the side edges):**
Raw per-edge quantities (no padding/corner/silk folded in; `0` when E is unused):
```
along(E) = Σ over terminals on E of (max(w,h) + spacing)          # span along edge E
depth(E) = max over terminals on E of min(w,h)
           + (side_silk_gap_mm if E ∈ {left,right} and E is used else 0)
```
Board (corner reservation is **axis-aware**, per RFE #1 `4e6a267`: an axis that a terminal edge
runs ALONG gets the FULL `corner_inset_mm`; an axis only crossed perpendicularly gets
`corner_center_inset_mm`):
```
sides_used = (left used) or (right used)

width  = max( C + depth(left) + depth(right),  along(bottom) )
         + 2·padding + 2·corner_inset_mm                       # bottom runs along width → FULL

height = max( C + depth(bottom),  along(left),  along(right) )
         + 2·padding + 2·(corner_inset_mm if sides_used else corner_center_inset_mm)
```
**Regression lock (now actually holds):** single-edge ⇒ `left`/`right` empty ⇒ `depth(left)=
depth(right)=0`, `along(left)=along(right)=0`, `sides_used=False` ⇒
`width = max(C, term_along)+2·padding+2·corner_inset`, `height = (C+term_depth)+2·padding+
2·corner_center_inset` — **identical to the code**. A test asserts this exactly (§8.1). Implement
single-edge mode by **calling the existing `_content_aware_size` unchanged**, and have the
multi-edge branch generalize it — don't re-derive a parallel single-edge path.

Antenna on a **vertical** edge → transpose axes (as the existing `terminal_edge_horizontal` flag
already does).

### 4.1 Adjacent-edge shared-corner rule `[CR-fixed — new blocker: terminal BODIES overlap, not just holes]`
R3 (shared mounting hole) is the *minor* corner issue — the reservation above guards the hole
because each axis independently reserves its corner. The **real** corner hazard the reviewers
surfaced: when two used edges meet (e.g. `bottom` + `left`), a `bottom` terminal's body extends
**up** by `depth(bottom)` and a `left` terminal's body extends **right** by `depth(left)`; near
the shared corner those two *bodies* can physically overlap even though each clears the hole.

**Rule:** on an edge `E` that shares a corner with a **used** perpendicular edge `F`, the terminal
run on `E` must start that end inset by `max(corner_inset_mm, depth(F) + spacing)` (not just
`corner_inset_mm`). Both the SIZING (the along-dimension must fit `along(E)` plus these possibly-
enlarged end insets) and the placement layout (`layout_along_edge` start offset) must honor the
same value — single-sourced through `distribute_terminals`. With ≤2 side terminals on audio-remote
the enlargement is small, but the rule must exist or a dense board overlaps at a corner.

## 5. Distribution heuristic (deterministic, N ≤ ~12 terminals)

1. `mode="single_edge"` → all terminals on `bottom` (today's behaviour). Done.
2. `mode="multi_edge"` but the single-edge board is already **near-square** → no-op, stay
   single-edge. `[CR-fixed: the cutoff must be axis-independent — use the aspect ratio
   `max(width,height) / min(width,height) ≤ THRESH` (THRESH ≈ 1.35), NOT `width ≤ height·THRESH`,
   which is wrong when the antenna is on a vertical edge and the board is tall-and-narrow.]`
3. Otherwise: peel a **suffix** (in `natural_ref_key` order) off `bottom`. For `k = 1 … N-1`
   peeled, split the peeled suffix across the two sides with a **fixed deterministic rule**
   `[CR-fixed: was underspecified]`: the **first ⌈k/2⌉ of the peeled refs → `left`, the rest →
   `right`** (so each side's refs stay in `natural_ref_key` order — the layout gate only requires
   per-edge sorted order, which this guarantees; a contiguous range is not required). The odd
   terminal goes to `left` (the `⌈⌉`). Compute `(width, height)`, score `(max(width,height),
   area)`, take the argmin tie-broken toward **smaller k** (fewer wire faces). Reuse
   `natural_ref_key` from `edge_terminal.py`.

**Degenerate cases (must be covered by §8.1):** N=0 → empty assignment; N=1 → never peeled (k≥1
would empty `bottom`, allowed only if scoring wins, but a 1-terminal board is near-square so step 2
no-ops first); when a candidate `k` empties `bottom` entirely, `along(bottom)=0` and the
`max(...)` simply drops that branch — `width`/`height` still well-defined (the `+2·padding+corner`
terms remain). `[CR-fixed: "only if E used" was ambiguous — along(E)=0 for an unused edge and the
padding/corner terms stay on the axis regardless.]`

## 6. Silk on side edges `[CR-fixed — labels render HORIZONTAL; the gap was under-sized]`

`_step_silkscreen_legends` places legends **inboard** for left/right edges (the `_INBOARD`
`ix>0`/`ix<0` branches) — BUT the reviewer found it never calls `SetTextAngle`, so **every label
is drawn horizontal**. On a LEFT/RIGHT edge a horizontal label extends inboard by its **text
width** (`≈ len(label)·char_width`, ~3–5 mm for `+3V3`/`BCLK`), not by the glyph height. So the
naïve `side_silk_gap_mm ≈ 2.5` (sized for a glyph height) is **too small**. Two options, decide at
implementation (this is the §9-A1 render-gated call):
- **(preferred)** rotate side-edge legend text 90° (`SetTextAngle`) so it reads along the edge like
  the terminal — then the inboard reach is the glyph height again and `side_silk_gap_mm ≈ 2.5`
  holds; OR
- keep horizontal text and set `side_silk_gap_mm = max_label_len · char_width + margin` (measure
  it, don't guess).

Either way `side_silk_gap_mm` enters `depth(E)` for side edges (§4) and is **eyeball-verified on a
real render** (A1). The integration gate (§8.2) must assert side-edge legend bboxes don't overlap
pads OR the cluster.

## 7. Opt-in flag plumbing

`src/kicad_mcp/utils/firmware/sidecar.py`:
- Add `terminal_distribution: Optional[str] = None` to `BoardSidecar` (`:94`).
- Add `"terminal_distribution"` to `_KNOWN_SIDECAR_KEYS` (`:116`) — unknown-key rejection already
  exists at `:211`.
- Add literal validation mirroring the `power_source` check (`:217-219`): value must be in
  `{"single_edge","multi_edge"}` or it's a loud error.
- Thread into `DesignIntent.source` (already how `board_size_mm` flows) → read in
  `_estimate_board_size` → pass `mode=` to `distribute_terminals`. Default `single_edge` when
  absent.

Opt audio-remote in via its `board.yaml`: `terminal_distribution: multi_edge`.

## 8. Tests

### 8.1 Pure (`tests/test_board_sizing.py` + new `tests/test_terminal_distribution.py`)
Boundary-focused per CLAUDE.md Threshold rule:
- **0 terminals** → empty `edge_of`, size == no-terminal baseline.
- **1 terminal** → never split (stays on bottom regardless of mode).
- **all-fit / near-square** → `multi_edge` no-ops (heuristic step 2).
- **forced-spill** → a deliberately wide letterbox spills to sides; assert squarer + area ≤
  single-edge.
- **antenna on each of the 4 sides** → opposite + perpendicular pair correct; antenna edge
  **never** in `edge_of.values()`; axis transpose correct.
- **`single_edge` ≡ today** → parametrize the existing `_content_aware_size` tests through the new
  function in single-edge mode; assert identical dims (regression lock for §4).
- determinism (same input → same output); corner insets applied per used edge.

### 8.2 Integration (`tests/integration/test_firmware_pcb_pipeline.py`)
- **KEEP** the existing single-edge gate (`_term_edges == {"bottom"}`, wide-letterbox window,
  per-edge `natural_ref_key` order — currently ~lines 491-520). It now guards the **default**
  path; do not weaken it.
- **ADD** a multi-edge test on an opted-in board:
  - `_term_edges ⊆ {"bottom","left","right"}`, `"top" ∉ _term_edges`, `len(_term_edges) ≥ 2`;
  - per-edge `natural_ref_key` order still holds (the existing `by_edge` loop works unchanged);
  - **squarer**: `abs(bw-bh)` smaller than the single-edge build of the same project;
  - **area not worse** than single-edge;
  - still routes (`_assert_mostly_routed`);
  - side-edge legend bboxes don't overlap any pad (extend the existing silk-vs-pad check);
  - **no `antenna_frame_mismatch`** event.

## 9. External-system assumptions — REVIEW THESE FIRST (highest severity)

A wrong assumption here invalidates the mechanism, not just a constant. None surface from cold
reading unless explicitly checked.

- **A1 — Side-edge silk renders readably `[CR: confirmed UNVERIFIED + worse than thought]`.** The
  reviewer found `_step_silkscreen_legends` draws labels **horizontal** (no `SetTextAngle`), so a
  left/right label reaches inboard by its *text width*, not glyph height → `side_silk_gap_mm` must
  be resized or the text rotated (§6). MUST validate with an actual `kicad-cli` render of an
  opted-in board. (This is precisely the crowding that drove the original single-edge decision.)
- **A2 — WIRE_ENTRY rotation faces outward on left/right edges `[CR: VERIFIED-OK (math)]`.** The
  reviewer confirmed `rotation_to_face(vec, outward_normal(edge))` gives left θ=90 / right θ=270
  with outward dot = 1.0. Still confirm at impl that **no pad lands off-board** on side edges
  (the PR #58 rotation-sign bug class — assert `_refs_with_pads_off_board` empty).
- **A3 — FreeRouter still routes ~complete** with terminals on three edges `[CR: UNVERIFIED, no
  3-edge test exists]`. Spreading terminals changes net topology and FreeRouter's nondeterministic
  tail; the board may need a higher best-of-N or pass count (documented rule: bump passes, never
  loosen the unconnected bound).
- **A4 — Parent vs script antenna edge agree `[CR: justification was WRONG, see §3.1]`.** They are
  in **different frames** and agreement does NOT come from "MCU at 0°" (the tier-1 placer rotates
  the MCU); it comes from both naming the antenna-overhang edge, which can diverge if tier-1 falls
  back to a non-preferred edge. The `antenna_frame_mismatch` event is therefore **essential**, not
  belt-and-braces — confirm it fires on a constructed divergence and is silent on all 4 goldens.

## 10. Open design questions (for reviewer / implementer)

- **R1** — squareness-vs-area objective weighting and the `THRESH ≈ 1.35` near-square cutoff
  (§5). What does the reviewer think the objective should optimize?
- **R2** — `side_silk_gap_mm` starting value (eyeball-tuned at impl; start 2.5).
- **R3 — `[CR-resolved → §4.1]`** the shared mounting *hole* is safe (each axis reserves its
  corner independently; the double-reservation is slightly generous, never unsafe). The reviewer
  promoted the *real* corner hazard — adjacent-edge terminal **body** overlap — to a sizing+layout
  rule in §4.1. Implement and test that rule.

## 11. Critical files (current locations @ `b4c8bb6`)

| File | What changes |
|---|---|
| `src/kicad_mcp/utils/placement/edge_terminal.py` | NEW pure `distribute_terminals`; reuse `natural_ref_key`, `outward_normal`, `rotation_to_face` |
| `src/kicad_mcp/tools/pcb_pipeline.py` | `_content_aware_size` (`:404`) + `_estimate_board_size` (`:451`) consume `distribute_terminals`; delete **only** the assignment loop (`:1179-1196`), KEEP `antenna_edge` at `:1176`; reuse edge-hint path (`:1159`); add `antenna_frame_mismatch`; if rotating side silk, `SetTextAngle` in `_step_silkscreen_legends` (`:~1665`); parent passes `edge_of` as hints; `terminal_edges` reconstruction (`:2091`) unchanged |
| `src/kicad_mcp/utils/firmware/sidecar.py` | `terminal_distribution` field (`:94`), `_KNOWN_SIDECAR_KEYS` (`:116`), literal validation (`:217` pattern) |
| `tests/test_board_sizing.py`, NEW `tests/test_terminal_distribution.py` | regression lock + pure boundary tests (§8.1) |
| `tests/integration/test_firmware_pcb_pipeline.py` | keep single-edge gate, add multi-edge gate (§8.2) |
| audio-remote `board.yaml` — **`[CR-fixed]` it is NOT a fixture file**; it's written **inline** via `(fw/"board.yaml").write_text(...)` at `test_firmware_pcb_pipeline.py:424` (and again `:614` in `test_approval_gate_audio_remote`). Opting in = adding `terminal_distribution: multi_edge` to that inline string (or, better, a NEW dedicated multi-edge test so the existing single-edge gate keeps exercising the default) | add the flag |

## 12. Implementation order (post-review)

1. **Lock** current sizing: parametrize existing `_content_aware_size` tests through
   `distribute_terminals(mode="single_edge")` and assert identical (no behaviour change yet).
2. Extract/refactor: `_content_aware_size` calls `distribute_terminals` internally (single-edge),
   green.
3. Add `multi_edge` heuristic + geometry; pure tests (§8.1).
4. Plumb the flag through sidecar → intent → `_estimate_board_size`.
5. Parent passes `edge_of` as placement hints; delete **only** the assignment loop (`:1179-1196`),
   keep `antenna_edge` (`:1176`); add `antenna_frame_mismatch` (test it fires on a constructed
   divergence); enforce the §4.1 adjacent-corner start-offset in both sizing and `layout_along_edge`.
6. Add the integration multi-edge gate (§8.2) as a NEW test so the existing single-edge gate keeps
   guarding the default; opt that new test's board in via its inline `board.yaml`. Resolve the §6
   horizontal-silk choice (rotate text vs. measured gap) against a real render.
7. **Eyeball gate** (per the human-rational layout norm): build + render the opted-in board, tune
   `side_silk_gap_mm`, confirm squarer/smaller, terminals oriented outward on side edges, silk
   readable, routes 0-unconnected. Verify the other three goldens are byte-identical.
