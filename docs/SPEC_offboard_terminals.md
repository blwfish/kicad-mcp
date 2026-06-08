# SPEC: Off-board peripheral → labeled terminal (v2, post-cold-review)

Status: DRAFT, cold-reviewed (3 subagents: external-assumptions / contradictions / scope). **Do not implement in this session** (Spec Review Rule). v2 incorporates the review; the diff from v1 is summarized in §0.

## 0. What the cold review changed (read this first)

- **2 BLOCKERS killed v1's risky path.** (a) board.yaml *cannot* key an un-carded peripheral by firmware name — `_apply_placement` only resolves `periph_by_ref`/`bus_by_name`; an unknown name → `placement_unknown_target` gap, ignored (`sidecar.py:488–511`). (b) Orphan nets carry **no machine-readable peripheral attribution** — the peripheral name lives only in the gap *string*, not on the `Net` object (`intent.py:386–392`). Both blockers hit ONLY the "auto-terminal for un-carded orphans" idea (v1 §4a options B/C). **Both are sidestepped entirely by terminal-only device cards** (a card gives a ref → nets become attributed → flows through the existing `remote_peripherals` path). → **v2 recommends cards as the v1 mechanism.**
- **CRITICAL contradiction resolved.** The auto-fallback would have emitted a terminal for an INMP441 I2S bus that was *not* declared remote, contradicting the honest chip-refusal. Rule added (§4): the terminal path applies to **carded terminal-devices and explicitly `locus:remote` buses only** — never an automatic fallback on un-declared bus peripherals.
- **Facts corrected.** The user's three examples: **ICS-43434 — done** (remote-mic terminal + now on-board); **LD2410 — already done** (UART remote terminal; integration-tested at `test_firmware_pcb_pipeline.py:458`); **TCRT5000 — the genuinely-unhandled case, and it's the *expander-array* flavor** (sensors hang off MCP23017 ports; firmware never `#define`s their pins, so there are NO MCU-side orphan nets — the v1 "linchpin" does NOT apply to TCRT5000). So TCRT5000 is inherently board.yaml-driven.
- **`external_io` confirmed dead; `power:` doesn't exist** and adding it touches a 3-source schema lockstep + `SCHEMA_VERSION`. SPI buses exist and were omitted. Several existing tests *pin the current refusal behavior* and will invert.

## 1. Problem (unchanged)

Off-board breakout peripherals must appear on the main PCB as labeled (screw) terminals carrying signals + power. Today this works for carded-ref devices and the I2S-mic/UART buses (via `locus:remote`), but a recognized-but-uncarded part has no way to become a terminal.

## 2. Current state (corrected)

Locus: board.yaml `placement:` → `Placement` in `intent.placements`; single dispatch `intent.is_remote()`. `_REALIZED_LOCI` (`sidecar.py:472`) = exactly `{(bus,I2S_IN,remote),(bus,UART,remote),(bus,I2S_OUT,on_board_with_remote_io),(ref,None,remote)}`; anything else non-`on_board` → `placement_unsupported` gap.

**Handled (labeled terminal today):** carded ref + `remote` → `remote_peripherals` (uses `ex.joins`); I2S_IN bus + `remote` → `_emit_remote_mic` (uses `ex.new_nets`, has MIC/MIC1 prefix); UART bus + `remote` → `_emit_remote_uart`; I2S_OUT `on_board_with_remote_io` → amp on-board + speaker terminal; manual `extra_connectors`.
**Connector machinery:** `synthesize_connector` builds `Connector:Screw_Terminal_01x{N}` for any N; Phoenix MKDS-1,5 footprint for N∈2..16, pin-header fallback for N=1 or N>16 (`connectors.py:36–82`).

**Gaps (no terminal):**
1. **Un-carded loose part with MCU signals** (piezo `PIEZO_ADC`, track switches `TRACK_SW1/2`): signals survive as orphan nets but with no peripheral attribution on the `Net`.
2. **Expander-port arrays** (TCRT5000 ×N on MCP23017): no MCU-side signals at all → not representable without board.yaml.
3. **I2C bus-level remote** → `placement_unsupported` (no `_emit_remote_i2c`).
4. **SPI bus remote** → not modeled at all (omitted from advisory + registry + `_REALIZED_LOCI`).
5. **I2S_OUT fully remote** (amp on breakout) → only `on_board_with_remote_io` exists.
6. **`external_io`** → dead metadata (no template reads it).

## 3. Goals / non-goals (G1 qualified)

**G1.** Every firmware-named off-board peripheral **whose signals the tool can see** becomes a labeled terminal. **G2.** No *silent* orphan for such a peripheral — terminal or disclosed gap. **G3.** Honest-by-construction: a realized terminal comes from an explicit declaration (a card, or a `locus:remote` directive) — never an un-declared heuristic. **G4.** Adding a new remote bus type is cheap.
**Non-goals:** synthesizing sensor silicon on-board; inferring expander-port wiring without board.yaml; auto-guessing 3V3-vs-5V without evidence; auto-terminalizing un-declared bus peripherals (would break the chip-refusal invariant).

## 4. Design — staged, lowest-risk first

### v1 — Terminal-only device cards (resolves both blockers + the honesty contradiction)

A **terminal-only card**: a device card whose realization IS a labeled screw terminal (no chip). Add `tcrt5000.yaml` (single sensor) + generic `switch.yaml` / `analog_sensor.yaml`, marked e.g. `realize: terminal` (new card field) with a screw-terminal footprint and role→signal map. Effect:
- The card gives the peripheral a **ref** → it enters `periph_by_ref` (blocker (a) gone) and its nets become attributed `"peripheral"` nets (blocker (b) gone).
- A terminal-only card can **default to a terminal** with NO honesty cost — the card *is* the explicit declaration that this part is a wire-out (G3 satisfied). No `locus:remote` needed, though `on_board` could be disallowed/ignored for these.
- Reuses `remote_peripherals` / the unified emitter; gets silk legend, ref alloc, multi-instance prefixing for free.

This is the **ICS-43434 pattern applied to terminal-only devices.** It covers gap #1 (loose parts) for any part we card. It does NOT cover gap #2 (expander arrays) — that's board.yaml.

**Rule (resolves CRITICAL contradiction #1):** the terminal-only-card default applies ONLY to cards explicitly marked terminal-only. Bus-typed peripherals (I2S_IN/OUT, UART, I2C, SPI) are NEVER auto-terminalized — they require explicit `locus:remote`. So an INMP441 I2S bus with no `locus:remote` still hits the honest chip-refusal; it does not silently become a terminal.

### v1.1 — Generalize the emitter (refactor, behavior-preserving)

Collapse `_emit_remote_mic` / `_emit_remote_uart` / `remote_peripherals` into one `emit_terminal(signals, power_rail, label, connector_type, net_mode)`. **Watch:** mic/uart create nets (`ex.new_nets`) while `remote_peripherals` joins existing nets (`ex.joins`); the unified emitter needs a `net_mode` param. Preserve the MIC/MIC1 prefix and per-emitter power-pin ordering via tests (snapshot current output first).

### v2 — Bus-remote completeness (each additive)

- **I2C remote:** add `_emit_remote_i2c` (SCL/SDA/+3V3/GND; decide: omit the on-board pull-ups, analogous to the mic's bypass-cap suppression). Add `(bus,I2C,remote)` to `_REALIZED_LOCI`; add I2C to the field-wired advisory; flip `test_apply_flags_unrealized_locus_on_i2c_bus`.
- **SPI remote:** model it (MOSI/MISO/SCK/CS/+3V3/GND); add to advisory + `_REALIZED_LOCI` + registry.
- **I2S_OUT fully remote:** new branch (no amp, terminal carries BCLK/LRC/DIN/+3V3/GND); add `(bus,I2S_OUT,remote)`.

### v2 — Expander-port arrays + board.yaml surface

- `expander_terminals:` (or extend `placement:`) declaring expander ref + port ranges → labeled terminal(s). Firmware-blind (like `extra_connectors`); the ONLY way to express TCRT5000-on-MCP23017. Must be added to `_KNOWN_SIDECAR_KEYS` the moment it's documented (else hard "unknown key" error).
- `power: 3v3|5v` per-remote (breakout supply); default 3V3. **Schema cascade (do all or CI breaks):** field on `Placement` (`intent.py:91`), `_validate_placement` (`sidecar.py:140`), `load_sidecar` construction (`sidecar.py:491`), `from_dict`/`_only_fields` (`intent.py:479`), `_KNOWN_SIDECAR_KEYS` 3-source set + `test_known_keys_all_accepted` (`test_firmware_sidecar.py:208`), `SCHEMA_VERSION` bump (`intent.py:29`).
- **`external_io`:** revive as the partial-remote signal-selector for `on_board_with_remote_io`, OR formally deprecate. Pick one; today it's load-bearing in §7 prose but dead in code.

## 5. Implementation checklist (touchpoints the review surfaced — don't miss these)

- [ ] `_REALIZED_LOCI` + `_apply_placement`: terminal-only cards are `ref`-keyed (already realized as `(ref,None,remote)`), so **v1 may need NO `_REALIZED_LOCI` change** if the card defaults to terminal without a locus directive — confirm the dispatch. Each v2 bus-remote adds one tuple.
- [ ] New card field (`realize: terminal`): `cards.py` validator + the realization dispatch in `templates.py`/`intent.py`.
- [ ] `generate.py:53` schematic-exclusion: terminal-only carded devices have a ref → `is_remote`/terminal check must exclude them from schematic chip placement appropriately.
- [ ] Tests that PIN current behavior and will change: `test_firmware_placement_locus.py:114–149` (the I2C-remote-unsupported assertion inverts in v2); add the uncarded/terminal-card cases.
- [ ] **Integration test:** `test_firmware_pcb_pipeline.py` has remote mic+UART coverage but NONE for a terminal-only-card part routed to a board — add one (piezo/switch/TCRT5000 → terminal, DRC-clean).
- [ ] Tool-count 4-file lockstep ONLY if a new `design` operation is exposed (the emitter is internal, so likely N/A — confirm).
- [ ] `advise_unspecified_placement` (`sidecar.py:444`) covers only I2S/UART buses; G2 implies loose orphan parts should be nudged too — extend or accept they're silent until carded.

## 6. Test plan (per threshold/seam rule)

- Terminal-only card → labeled screw terminal with correct signals + power + silk; no orphan remains; multi-instance non-colliding (MIC/MIC1 precedent).
- **Invariant pin:** an I2S/UART/I2C bus naming an unrealizable chip WITHOUT `locus:remote` still REFUSES (no auto-terminal) — guards contradiction #1.
- Carded `remote_peripherals` + `_emit_remote_mic`/`_emit_remote_uart` outputs unchanged after the §v1.1 unification (snapshot before/after).
- at/below/above on the realization predicate: terminal-only card / chip card / bus+remote / bus-no-remote / uncarded-no-card.
- v2: I2C/SPI/I2S_OUT remote terminals; `power:` rail selection; `expander_terminals`; flip the inverted `placement_unsupported` tests.

## 7. Resolved questions (from the review)

1. Orphan-net survival: TRUE for loose MCU-signal parts (piezo/switch) but the attribution is only in gap text → **avoid relying on it; use cards** (v1). FALSE for TCRT5000 (behind expander, no MCU nets) → board.yaml only.
2. board.yaml keying an uncarded name: **not supported today; cards avoid needing it.**
3. `synthesize_connector` arbitrary-N: OK (Phoenix 2–16; mind the >16 fallback for big expander terminals).
4. Honesty: terminal-only **cards** are explicit declarations → no G3 tension. The un-declared auto-fallback is dropped from the plan.
5. LD2410/ICS-43434 already handled; TCRT5000 is the expander case.

## 8. Open questions remaining for the implementer

- Should a terminal-only card default to terminal with no locus directive, or still require `locus:remote`? (v1 recommends default-terminal; confirm it doesn't surprise anyone wanting an on-board variant.)
- I2C/SPI remote terminal: include or omit the pull-ups/CS-strap that the on-board header adds?
- `external_io`: revive (preferred) or deprecate?
- Is the loose-part **auto** terminal (without a card, via a new `Net.peripheral_hint` field) worth a v3, or do cards + `extra_connectors` cover the real need? (speed-cal's piezo/switches could just be cards.)

## 9. Process note

Cold-reviewed via 3 severity-tier subagents (this session). Implementation is a SEPARATE session/turn. v1 (terminal-only cards) is the recommended first slice — small, blocker-free, mirrors the shipped ICS-43434 work.
