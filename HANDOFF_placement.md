# Handoff: PCB autoplacer — human-rational layout (rotation/ordering/hints/board-size)

**Branch:** `claude/cranky-rubin-b4da77` (worktree
`/Volumes/Files/claude/kicad-mcp/.claude/worktrees/cranky-rubin-b4da77`).
**Foundation committed:** `f7f0274` (tree clean). **Plan:**
`/Users/blw/.claude/plans/piped-stirring-goblet.md` (approved; reflects full scope incl.
the per-ref hint channel, edge overhang, ESP32 antenna rotation, telemetry, eyeball gate).

## ⚠️ Session caveat — read first

The authoring session's tool-output channel degraded badly partway through: it **replayed
and fabricated tool results** (a fake "WIP commit", a fake integration-test run with a
"failing test", a fake handoff file — none real) and truncated multi-line reads. Every
claim below was re-verified against `git` + `grep` with single atomic commands.
**Next session: trust `git diff`/`grep`/Python introspection, never a Bash echo. Use one
command per call; batched calls cascade-cancelled. If output garbles, write to a file and
Read it, or print a single boolean.**

## State: DONE and verified (committed in f7f0274)

116+ unit tests green (`pytest tests/test_edge_terminal_placement.py
tests/test_firmware_intent.py`), 81 of them new.

- `src/kicad_mcp/utils/placement/edge_terminal.py` (NEW) — pure math + `EDGE_TERMINAL_HELPER`
  (the injectable source string, type-annotation-free for the py3.9 pcbnew subprocess).
  Functions: `is_screw_terminal_class`, `pad_centroid_offset`, `pad_extent`,
  `rotation_to_face(vec, target_normal)`, `rotate_extents(ext, theta)`, `natural_ref_key`,
  `nearest_edge`, `inward_normal`/`outward_normal`, `layout_along_edge`, `normalize_hint`.
  `rotate_extents` + `rotation_to_face` share ONE rotation convention by construction
  (math-CCW in y-down coords). `normalize_hint` = single validation source (drops invalid
  edge/rotation/fixed with a warning; rejects bool traps; never silently substitutes).
  Drift test `TestEdgeTerminalHelperSource` execs the helper string == imported funcs.
- `intent.py`: `placement_hints: dict[str,dict]` field on DesignIntent; SCHEMA_VERSION 4→5
  (round-trips; v4 loads forward-compat via `_only_fields`).
- `sidecar.py`: top-level `placement_hints:` parse → `intent.placement_hints` in
  `apply_sidecar` (`_validate_hints` structural check; value validation at build time).
- `templates.py`: `Expansion.placement_hints`; auto-emit `{edge:"none"}` for on-board
  module headers (pin_header in `_emit_connector` keyed on `ctype`; `i2c_device_header`
  J*). `expand_intent` merges with `setdefault` (user/sidecar hint wins). VERIFIED:
  track-geometry OLED header → `{edge:"none"}`.

## State: NOT STARTED — the core engine rewrite

`grep -c EDGE_TERMINAL_HELPER src/kicad_mcp/tools/pcb_pipeline.py` → **0**. None of the
engine work is on disk. There is NO half-broken state and NO failing test (the "failing
S3 test" earlier was fabricated). This is the bulk of the remaining work and the only
risky part. Build it via small verified edits, NOT a single 390-line splice.

A draft of the rewritten `_step_smart_placement` was composed during the session at
`/tmp/func_new.py` — **treat as untrusted** (may be gone; never verified end-to-end).
Re-derive from the plan rather than trusting it.

### What the engine rewrite must do (per plan §1, §1b, §2, §3)
In `_step_smart_placement` (currently a single embedded pcbnew-script string,
`tools/pcb_pipeline.py` ~L443–831; ends at `}, timeout=60.0)`):
1. Add param `placement_hints: Optional[Dict[str, Dict[str, Any]]] = None`; pass it into
   the script's `params` dict; read `placement_hints = params.get("placement_hints", {})`.
2. Inject the helper: concatenate `EDGE_TERMINAL_HELPER` next to `KEEPOUT_HELPER +
   POWER_NET_HELPER` (~L472). Import it at module top
   (`from kicad_mcp.utils.placement.edge_terminal import EDGE_TERMINAL_HELPER, normalize_hint`).
3. In the fp-info loop, also collect pad bboxes → `pad_centroid_offset` + `pad_extent`.
4. **Tier-2 connectors**: partition by hint — `fixed:[x,y]`→absolute;
   `edge:"none"`→interior spiral (no rotation); else edge from hint or `nearest_edge`.
   Per edge: sort by `natural_ref_key`, compute rotation for J terminals via
   `rotation_to_face(pad_centroid, inward_normal(edge))` (pads face INWARD so wire side
   overhangs), `rotate_extents`, then `layout_along_edge` with **pad-anchored overhang**
   (relax outward board-fit so the body crosses the edge but pads stay on-board —
   `pads_on_board`). Apply rotation via `SetOrientationDegrees` in the apply loop;
   `placements[ref]` must carry the angle.
5. **Tier-1 keepout**: rotate so the keepout (ESP32 antenna) faces OUTWARD via
   `rotation_to_face(keepout_vec, outward_normal(edge))`; rotate `keepout_rel` too.
6. Emit a `placement_decisions` list in the script's JSON (`rotation_chosen`,
   `rotation_ambiguous`, `placement_hint_applied`).
7. `build_pcb_from_schematic`: add `placement_hints` param; load intent once before
   Step 2 (reuse for board-size + hints + the Step-7.5 silk pass); resolve board size
   from `intent.source["board_size_mm"]` (precedence explicit args > intent > auto,
   validate `[w,h]` 2 positive numbers); merge hint sources + `normalize_hint` each
   (precedence param > intent); surface `placement_events` (decisions +
   `placement_hint_unmatched` for refs not on the board); wrap in `event_context` +
   `record_warning` for telemetry-DB persistence (pattern: `schematic_layout.py:278-290`).
8. `_estimate_board_size`: add `total_keepout` term (reconcile with the public
   `estimate_board_size` in pcb_planning.py, which already adds it).
9. `design.py` ~L189: update the stale "build_pcb does not yet apply board_size" comment.

### RISK #1 (the thing that will bite): rotation sign vs pcbnew
`rotate_extents`/`rotation_to_face` are self-consistent, but whether they match pcbnew's
`SetOrientationDegrees` (CCW+? y-down) is **unverified**. If wrong, terminal pads face
OFF-board → router can't reach them → unconnected nets. **Verify early** on real KiCad:
build the S3 audio board, `kicad-cli pcb export svg --layers F.Cu,F.SilkS,Edge.Cuts`,
eyeball that J-terminal pads point inward. To localize a routing regression: force every
tier-2 `ang=0` → if it routes 0-unconnected, the bug is the rotation sign.

## Remaining TODO after the engine (per plan)
- Integration asserts in `tests/integration/test_firmware_pcb_pipeline.py`: J orientation
  ∈{0,90,180,270} + pad-centroid-inboard (pins the sign permanently); multi-J monotonic
  order; overhang (pad-bbox inside, courtyard beyond edge); ESP32 keepout off-board;
  board-size-from-intent; `placement_events` envelope. Run with
  `PATH="/Applications/KiCad/KiCad.app/Contents/MacOS:/opt/homebrew/bin:$PATH"
  KICAD_INTEGRATION=1 uv run --no-sync pytest tests/integration/test_firmware_pcb_pipeline.py`.
  **Watch the existing `max_unrouted` bounds** — better edge access should not regress them;
  re-baseline only with an explained reason.
- Sidecar/intent `placement_hints` unit tests (parse + round-trip + auto-emit — proven
  manually this session, not yet committed as tests).
- **Eyeball gate** (the whole reason for this PR — `feedback_human_rational_layout`):
  render the routed audio-node + speaker-terminal boards as SVG and show Brian. Visual
  quality is not test-catchable.

## Useful facts confirmed this session
- Intent build path: `partition(parse_defines(select_active_branches(text, idf_target_defines(board))))`
  then `build_intent(parsed, firmware_path=..., board_id=...)`. The design tool does this
  in `_op_import` (design.py ~L162-168). `build_intent` takes a ParsedFirmware, not a path.
- `_emit_connector` distinguishes connector kind via `ctype` (`pin_header` vs
  `screw_terminal`/`pluggable`); `_TYPE_TAG` → "HDR"/"TERM". `_header()` (templates.py:370)
  makes raw HDR headers; `i2c_device_header` (L518) is where the OLED J* header is born.
- Three golden integration board shapes: speed-cal, audio-node S3, track-geometry, + sidecar.
