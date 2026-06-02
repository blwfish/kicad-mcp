# SPEC — Multi-Edge Terminal Distribution (autoplacer RFE)

> **STATUS: DRAFT — needs fresh-session cold review BEFORE implementation.**
> Per the project's Spec Review Rule, this spec was authored in one session and must be
> cold-read by a fresh context (with no memory of the authoring discussion) before any code is
> written. The reviewer's first job is the **External-System Assumptions** section below — that
> class invalidates whole mechanisms, not just numbers, and does not surface from cold reading
> unless asked for explicitly.
>
> Authored against branch `claude/recursing-kowalevski-365bfb` @ `b4c8bb6`. Line numbers are from
> that commit; re-confirm before editing.

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
- **Delete** the script's own distribution loop (`pcb_pipeline.py:1176-1196`); terminals now
  arrive pre-assigned. The `t2_terminals` collection (`:1166`) becomes a no-op for firmware
  boards (they all carry an edge hint) but is kept as the fallback for any field terminal without
  a hint.

### 3.1 Antenna-edge frame agreement (a real subtlety — flagged for review)
There are **two** independent antenna-edge derivations and they must agree:
- the **script** computes `antenna_edge` from the tier-1 `keepout_overhang` decision, in the
  **post-rotation board frame** (`pcb_pipeline.py:1176`);
- `_estimate_board_size` derives `antenna_side` from the footprint's rule-area keepout in the
  **footprint 0° frame** (`:~530`).

They agree on every golden because the MCU is placed at 0° on the antenna-opposite edge. The
distribution decision uses the parent's value. **Do not add a third computation.** Add an
`antenna_frame_mismatch` decision/event if the script's post-rotation edge ever disagrees with the
parent-provided one, so future drift is loud rather than silent. See External-System Assumption A4.

## 4. Geometry

Per terminal: `along = max(w,h) + spacing`, `depth = min(w,h)`.
Cluster `C = max(sqrt(interior_area · routing_factor), max_interior_dim)`.
`bottom := _OPP[antenna_edge]`; `sides :=` the perpendicular pair; the antenna edge is **never**
assigned a terminal.

```
depth(E) = max(depth of terminals on E) + corner_center_inset_mm
           + (side_silk_gap_mm  if E is a non-empty SIDE edge  else 0)
along(E) = sum(along of terminals on E) + 2·padding + 2·corner_inset_mm     # only if E used

width  = max( C + depth(left) + depth(right),   along(bottom) )
height = max( C + depth(bottom),                 along(left),  along(right) )
```

When the antenna sits on a **vertical** edge the axes transpose (terminals march along height),
exactly as today's `terminal_edge_horizontal` flag in `_content_aware_size` handles.

**Regression lock:** `mode="single_edge"` ⇒ every terminal on `bottom` ⇒ `depth(left)=depth(right)
=0`, `along(left)=along(right)=0` ⇒ the formula **reduces byte-for-byte to today's
`_content_aware_size`**. A test must assert this exactly (§7).

## 5. Distribution heuristic (deterministic, N ≤ ~12 terminals)

1. `mode="single_edge"` → all terminals on `bottom` (today's behaviour). Done.
2. `mode="multi_edge"` but the single-edge board is already **near-square**
   (`width ≤ height · THRESH`, `THRESH ≈ 1.35`) → no-op, stay single-edge. (Protects boards that
   don't need it even when the flag is on.)
3. Otherwise: peel a contiguous **suffix** of terminals (in `natural_ref_key` order) off `bottom`
   and split it **evenly across the two side edges**. For `k = 1 … N-1` peeled, compute
   `(width, height)` and score `(max(width,height), area)`; take the argmin, tie-broken toward
   **smaller k** (fewer wire faces). **Suffix-peeling preserves per-edge natural order**
   (e.g. J1–J3 stay on bottom, J4/J5 go to sides) — required by the layout gate's ordering
   assertion. Reuse `natural_ref_key` from `edge_terminal.py`.

## 6. Silk on side edges

`_step_silkscreen_legends` already places legends **inboard** for left/right edges (the
`_INBOARD` `ix>0` / `ix<0` branches, plus this session's clearance-aware interior-header logic).
The `side_silk_gap_mm` term in `depth()` (§4) reserves the inboard band so side-edge legends don't
crowd the interior cluster. The exact value is **eyeball-tuned at implementation** (start ~2.5 mm)
and is an External-System Assumption (A1) — it can only be validated by a real-KiCad render.

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

- **A1 — Side-edge silk renders readably on real KiCad.** The inboard left/right legend placement
  plus `side_silk_gap_mm` must be validated by an actual `kicad-cli` render of an opted-in board,
  not by geometry alone. (This is precisely the crowding that drove the original single-edge
  decision.)
- **A2 — WIRE_ENTRY rotation faces outward on left/right edges.** `rotation_to_face(vec,
  outward_normal(edge))` must aim the MKDS wire entry **off-board** on side edges, not only on
  top/bottom. Confirm `outward_normal` + the rotation snap behave for left/right, and that no pad
  lands off-board (the rotation-sign bug class from PR #58 history —
  `_refs_with_pads_off_board`).
- **A3 — FreeRouter still routes ~complete** with terminals on three edges. Spreading terminals
  changes net topology and FreeRouter's nondeterministic tail; the board may need a higher
  best-of-N or pass count (per the documented rule: bump passes, never loosen the unconnected
  bound).
- **A4 — Parent (0° frame) vs script (post-rotation frame) antenna edge agree** on all four
  goldens. Guarded by the `antenna_frame_mismatch` event, but confirm the assumption holds so the
  guard never fires in normal use.

## 10. Open design questions (for reviewer / implementer)

- **R1** — squareness-vs-area objective weighting and the `THRESH ≈ 1.35` near-square cutoff
  (§5). What does the reviewer think the objective should optimize?
- **R2** — `side_silk_gap_mm` starting value (eyeball-tuned at impl; start 2.5).
- **R3** — corner-hole clearance when terminals occupy **adjacent** edges: a corner hole is shared
  by two edges (e.g. bottom-left by `bottom` and `left`). Verify the per-edge `along`/`depth`
  reservations in §4 don't under- or double-count the shared corner. This is the subtlest part of
  the geometry.

## 11. Critical files (current locations @ `b4c8bb6`)

| File | What changes |
|---|---|
| `src/kicad_mcp/utils/placement/edge_terminal.py` | NEW pure `distribute_terminals`; reuse `natural_ref_key`, `outward_normal`, `rotation_to_face` |
| `src/kicad_mcp/tools/pcb_pipeline.py` | `_content_aware_size` (`:404`) + `_estimate_board_size` (`:451`) consume `distribute_terminals`; delete script distribution loop (`:1176-1196`); reuse edge-hint path (`:1159`); add `antenna_frame_mismatch`; parent passes `edge_of` as hints; `terminal_edges` reconstruction (`:2091`) unchanged |
| `src/kicad_mcp/utils/firmware/sidecar.py` | `terminal_distribution` field (`:94`), `_KNOWN_SIDECAR_KEYS` (`:116`), literal validation (`:217` pattern) |
| `tests/test_board_sizing.py`, NEW `tests/test_terminal_distribution.py` | regression lock + pure boundary tests (§8.1) |
| `tests/integration/test_firmware_pcb_pipeline.py` | keep single-edge gate, add multi-edge gate (§8.2) |
| audio-remote `board.yaml` (test fixture under `tests/fixtures/firmware/audio_s3/` + the inline `board.yaml` in `test_audio_remote_to_routed_pcb`) | `terminal_distribution: multi_edge` |

## 12. Implementation order (post-review)

1. **Lock** current sizing: parametrize existing `_content_aware_size` tests through
   `distribute_terminals(mode="single_edge")` and assert identical (no behaviour change yet).
2. Extract/refactor: `_content_aware_size` calls `distribute_terminals` internally (single-edge),
   green.
3. Add `multi_edge` heuristic + geometry; pure tests (§8.1).
4. Plumb the flag through sidecar → intent → `_estimate_board_size`.
5. Parent passes `edge_of` as placement hints; delete the script distribution loop; add
   `antenna_frame_mismatch`.
6. Add the integration multi-edge gate; opt audio-remote in.
7. **Eyeball gate** (per the human-rational layout norm): build + render the opted-in board, tune
   `side_silk_gap_mm`, confirm squarer/smaller, terminals oriented outward on side edges, silk
   readable, routes 0-unconnected. Verify the other three goldens are byte-identical.
