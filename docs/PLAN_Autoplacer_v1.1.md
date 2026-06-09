# Autoplacer v1.1 — Implementation Plan (Phase 5 + Phase 7)

Branch `feat/autoplacer-v1.1` off `main`. Continues the human-rational autoplacer
(v1 merged). See `docs/PLAN_Autoplacer_Human_Rational_System.md` for the parent plan.

## Default decisions taken (override if wrong)
- **count=2 holes** → top-left + bottom-right (diagonal).
- **Hole footprint** → `MountingHole_3.2mm_M3_Pad_TopOnly` (copper pad on F.Cu; allows optional GND tie). Present + name-stable on KiCad 9 AND 10.
- **`approved` default** = `True` on `build_pcb_from_schematic` (gate is opt-in; the real user-facing gate is the `design` op `propose_placement`).
- **Render fidelity** = Level A (real temp PCB + `kicad-cli pcb export svg`), fallback `render_path: null` if cli absent.

---

## Phase 5 — Mounting holes

1. **`sidecar.py`** — add `mounting_holes` to `BoardSidecar` (`count`/`drill_mm`/`inset_mm`/`keepout_mm`, all optional). Add `_KNOWN_SIDECAR_KEYS` frozenset + reject unknown top-level keys in `_validate`. Add `_validate_mounting_holes` (count ∈ {0,2,4}; positive numerics). Populate in `load_sidecar`; thread through `intent.source["mounting_holes"]` in `apply_sidecar`.
2. **`pcb_pipeline.py`** — pure `_resolve_mounting_holes(mh_source)` merging defaults (`_HOLE_DEFAULTS = {count:4, drill_mm:3.2, inset_mm:3.5, keepout_mm:1.5}`); `count=0` disables.
3. **`pcb_pipeline.py`** — new `_step_add_mounting_holes(pcb_path, holes)` (embedded pcbnew script): compute 4 corner positions from `inset_mm`; `FootprintLoad` the MountingHole; refs `H1..H4`; add a circular rule-area keepout per hole (radius = drill/2 + keepout_mm; `SetDoNotAllowTracks/Vias/Pads`; layer set via `GetEnabledLayers() & LSET.AllCuMask()`; KiCad 9/10 zone-fill compat via the `hasattr` pattern in `keepout_helpers.py:74`). **Use layer-ID constants, never name strings** (KiCad 10 F.CrtYd→F.Courtyard).
4. **`pcb_pipeline.py`** — drop `"H"` from `_EDGE_DESIGNATOR_CLASSES` (line ~269) and the tier-2 docstring; holes are fixtures now, placed before the tier system.
5. **`pcb_pipeline.py`** — return hole positions from the step; caller builds `fixed` placement hints for `H1..H4` so the engine registers their keepout boxes and later tiers avoid them.
6. **`pcb_pipeline.py`** — `_content_aware_size` gains `corner_inset_mm=0.0` (added to both dims); default 0 keeps existing sizing tests green. `_estimate_board_size` passes `holes["inset_mm"]`.
7. **`build_pcb_from_schematic`** — resolve holes from `design_intent.source`; run the hole step after outline; merge hole hints first (user hints override); add `add_mounting_holes` param (or infer from `count>0`).

Tests: `TestResolveMountingHoles`, `TestContentAwareSizeWithCornerInset` (boundary at inset=0), sidecar unknown-key + count/drill boundary tests, integration `_refs_at_corners` assertion on `test_audio_remote_to_routed_pcb`, and a `count:0` → no-H-refs case.

---

## Phase 7 — Approval gate (`propose_placement`)

1. **`pcb_pipeline.py`** — extract the terminal-edge-assignment loop (currently embedded-script lines ~1019–1040, pure: math + fp summaries + placement_decisions) into a pure Python `_plan_terminal_edges(...)`. Single source: the embedded script consumes it via params (like `wire_entry_table`); the parent-side propose op calls it directly. **No logic duplication.**
2. **`design.py`** — `_op_propose_placement(*, intent_path, schematic_path, out_dir)`: load intent → extract netlist from the (already-generated) schematic → `_estimate_board_size(holes=...)` → `_plan_terminal_edges(...)` → emit terminal table + render. Workflow: `… generate_schematic → propose_placement → [human approves] → build_pcb_from_schematic`.
3. **Render (Level A)** — build a temp PCB: `_step_create_pcb_and_outline` + `_step_add_mounting_holes` + a trimmed footprint-place-only step (stack J-refs on assigned edges, no nets/no routing), then `kicad-cli pcb export svg --layers F.Cu,B.Cu,F.SilkS,Edge.Cuts` (same path as `export.py:257`).
4. **Response** — `{board_size_mm, antenna_edge, mounting_holes[], terminal_table[], crowded_edges[], render_path, proposed_board_yaml}`. `proposed_board_yaml` round-trips through `load_sidecar`.
5. **`design.py`** — register `propose_placement` in the router dispatch + docstring (`schematic_path` param already exists).
6. **`build_pcb_from_schematic`** — `approved: bool = True`; when `False`, run steps 1–4 only, return `status:"pending_approval"` + `proposal`. Programmer escape hatch, not the primary gate.

Tests: `TestPlanTerminalEdges` (empty / one-edge / overflow boundary / no-antenna / over-wide), `TestProposeBoardYaml` (valid keys, round-trip), integration `test_propose_placement_audio_remote` (antenna_edge=top, no terminal on antenna edge, render non-empty, proposed_board_yaml loads, 4 holes).

## External-system assumptions to verify
- `MountingHole_3.2mm_M3_Pad_TopOnly` present on KiCad 9 & 10 (confirmed via find).
- `SetDoNotAllowZoneFills` vs `SetDoNotAllowCopperPour` (9/10) on a rule-area zone.
- `kicad-cli pcb export svg` valid from outline+footprints, no routed nets.
- `zone.SetLayerSet(... AllCuMask())` on both versions (CI-gate).

## Sequencing
Phase 5 sidecar+pure helpers → `_content_aware_size` inset → Phase 7 `_plan_terminal_edges` extraction → Phase 5 hole step + `_EDGE_DESIGNATOR_CLASSES` → orchestrator wiring → Phase 7 op+render → router+`approved`.

## DEFERRED (RFE — cosmetic sizing quality, correctness complete)
Phase 5 + Phase 7 are DONE and verified (integration 6/6 on KiCad 9 & 10), incl.
two eyeball-driven fixes (holes pinned at corners; terminals cleared of corner
holes). Deferred sizing polish, in priority order:
1. **Axis-aware corner reservation.** `_hole_corner_clear` is reserved on BOTH
   board axes, but only the terminal-EDGE (along) axis needs the full clearance;
   the depth axis needs only the hole-center inset. Result: the board is taller
   than the content needs (dead band between the interior cluster and the terminal
   row). Fix: reserve full clearance only along the terminal edge, hole-center
   inset on the depth axis. Cosmetic — does not affect routing/correctness.
2. **Multi-edge terminal distribution** (the original v1 board-AREA RFE): spread
   terminals across antenna-opposite + side edges → squarer, smaller board vs. the
   current single-edge letterbox. Bigger change (side-edge silk crowding).
