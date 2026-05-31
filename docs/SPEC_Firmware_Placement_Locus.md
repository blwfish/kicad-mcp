# SPEC: Placement Locus

**Status:** DRAFT — written 2026-05-31. **Requires fresh-session cold review before implementation** (Spec Review Rule). **Sequencing: this phase lands FIRST**, before the part-resolution work (`SPEC_Firmware_Part_Resolution.md`, now parked behind it) — locus is a foundational intent-model axis that changes what "place a resolved part" means.

---

## 1. Problem

The front end has silently assumed, since the beginning of the pipeline, that **every firmware-named device is a component placed on the board.** That is wrong for a large and common class of devices that are wired in from off-board:

| Device (audio-node) | Reality | Current (wrong) behavior |
|---|---|---|
| INMP441 mic | **remote** — positioned for acoustics, wired in | placed as a chip (and has no KiCad symbol — see part-resolution spec) |
| LD2410 presence | **remote** — needs line-of-sight | placed as a header module |
| Piezos | **remote** — mounted elsewhere | n/a / would be placed |
| Speakers | **remote** load | partially: a hardcoded speaker header |
| MAX98357A amps | **on-board, remote speaker I/O** | placed (correct), speaker header hardcoded |
| ESP32 / LDO / USB / caps / Rs | on-board | placed (correct) |

For the audio-node, **most of the interesting peripherals are remote** — the board is an MCU + amp + power core ringed by screw terminals. The model cannot express this. What exists are three disconnected fragments that each realize a connector a different way (§4), with no first-class concept.

---

## 2. Verified facts (2026-05-31; re-verify per KiCad version)

- `Screw_Terminal_01x01..01x20` symbols present in `Connector.kicad_sym`. ✓
- `Conn_01x01..01x20` present in `Connector_Generic.kicad_sym`. ✓
- Terminal-block footprint libraries present: `TerminalBlock_{Phoenix,Altech,Degson,Dinkle,CUI,RND,MetzConnect,4Ucon,Philmore,Ningbo-Kagnex}.pretty` (Phoenix: 126 footprints). ✓
- **Implication:** the `remote` realization uses **stock KiCad parts** — no symbol/footprint authoring (unlike the bare INMP441 chip). The specific terminal-block footprint (series/pitch) is a board choice with a default (§6).
- **Silkscreen capability already exists** (`pcb_silkscreen.py`): `_op_add_text` (PCB_TEXT on a silk layer at coords), `_op_check_silkscreen_overlaps` (single source of truth, shared with the audit router), `_op_auto_fix_silkscreen`. So the terminal legend (§7.1) is **reuse** — including silk-over-pad detection/fixing — not net-new.

The net path needs **no change**: `generate_schematic` is label-at-pin (joins by name), `net_injection` is name-based — only the `Endpoint` *target* changes (terminal ref + position number instead of device ref + role-resolved pin). Verified against `generate.py` / `net_injection.py`.

---

## 3. Concept: placement locus (a fourth axis)

Alongside **identity** (firmware) / **footprint** / **availability**, a device has a **placement locus** — *where it physically lives relative to the board*:

- **`on_board`** — place the component's symbol/footprint (current behavior; the default).
- **`remote`** — do **not** place the device. Synthesize a **terminal/connector** with one position per boundary-crossing signal, redirect all the device's nets to it, document the device off-board, and **suppress its on-board support glue** (a remote mic's bypass cap and CHIPEN strap live at the device, not on this board).
- **`on_board_with_remote_io`** — place the chip normally, but redirect its **external-load** nets (card-declared, e.g. the MAX98357A's `outp`/`outn`) to a terminal.

Locus is a **board-level decision** — firmware cannot know whether a mic is reflowed or hangs on 18″ of wire — so it is **not firmware-derived**. It is set in `board.yaml` (§6). A card MAY carry a suggested default (an LD2410 is almost always remote), but the suggestion is surfaced as a **disclosed assumption**, never silently applied (consistent with the honest-by-construction contract). **Open Decision 1** settles how aggressive defaults may be.

---

## 4. The model gap, and unifying the three fragments

There is **no `Connector` dataclass**. A connector is just a `Peripheral` (`type="HDR"`, `origin="template"` for templates; `type="CONN"`, `origin="user"` for sidecar). Three morally-identical "place a connector + route nets to it" operations exist via **three different code paths** — exactly the single-source-of-truth violation the project's Syntactic-Semantic Seam rule warns about:

| Fragment | Identity source | Materialized at | `origin` | Pin→net | Ref allocation |
|---|---|---|---|---|---|
| (a) `module:true` cards (MPU6050/OLED) | device-card YAML | import (`build_intent`) | `imported` | role-map by number | n/a (from card) |
| (b) `i2s_output_amps` speaker headers | `HDR_1X2` Python const | expand (template) | `template` | hardcoded `"1"`/`"2"` | `RefAllocator.next("J")` |
| (c) `board.yaml extra_connectors` | board.yaml | sidecar (`apply_sidecar`) | `user` | explicit dict | `_normalize_ref` |

**This spec introduces ONE terminal-synthesis path** that all three (and the new locus realizations) consume — a single helper `synthesize_connector(device_or_nets, crossing_signals, connector_type) -> (Peripheral, [Net edits])` that: picks the symbol (`Screw_Terminal_01x{N}` / `Conn_01x{N}`) sized to N signals, allocates a collision-free ref through **one** allocator, redirects the named nets to numbered positions, labels each position from the signal/role name, and **validates N against the symbol pin count** (a check none of the three fragments do today — a latent bug). Fragments (a)/(b)/(c) are reimplemented on top of it. This is a Rule-3 consolidation, not just an addition.

---

## 5. Data model

```python
@dataclass
class Peripheral:
    ...
    locus: str = "on_board"     # NEW: on_board | remote | on_board_with_remote_io
```

- For `on_board_with_remote_io`, the card declares which roles are external load: **card field `external_io: [role,...]`** (e.g. MAX98357A → `[outp, outn]`). For `remote`, *all* of the device's signal nets cross (including VDD/GND — power is delivered over the wire).
- No new `Net` field is required: crossing is realized by moving/adding `Endpoint`s onto the synthesized terminal (the net name is unchanged). A synthesized terminal carries `origin` per **Open Decision 2** (template-regenerated vs user-preserved — affects `merge()` on re-import).
- `SCHEMA_VERSION` bump; `from_dict`/`_only_fields` back-compat (old docs default `locus="on_board"`, preserving current behavior).

---

## 6. `board.yaml` — the locus channel

```yaml
placement:
  INMP441:                 # keyed by device type (Open Decision 3: type vs ref)
    locus: remote
    connector: screw_terminal      # default for remote; or pin_header | pluggable
    footprint: TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-5-5.08_1x05_P5.08mm_Horizontal  # optional; else series default
  LD2410:
    locus: remote
  MAX98357A:
    locus: on_board_with_remote_io   # speaker leads to a terminal; chip stays on board
```

- Applied in `apply_sidecar` (after `build_intent`, before expand) — same insertion point as `extra_connectors`, now routing through the unified §4 helper.
- `connector` default for `remote` = `screw_terminal` (field wiring). The **footprint** (terminal series/pitch) defaults to one concrete stock part (**Open Decision 4** — recommend a 5.08 mm Phoenix MKDS or a generic; must be a verified-present footprint), overridable per device.
- Validation: `locus` ∈ the three values; `connector`/`footprint` against known sets; device key against recognized types (reuse the part-resolution registry once that lands; until then, against placed peripheral types).

---

## 7. Realization in `generate.py`

The placement loop (`generate.py:44–68`) and wiring loop (`100–130`) gain a locus branch **before** `to_place` is built:

- **on_board** → unchanged.
- **remote** → device excluded from `to_place`; synthesize terminal via §4 helper; the device's net endpoints are retargeted to the terminal; emit a `Gap(kind="remote_device", detail="INMP441 (mic): remote — wired to J3 (5-pos screw terminal); sourced off-board", resolved=True)`; **suppress the device's support-glue** (templates check `locus` before emitting decoupling/straps for that device).
- **on_board_with_remote_io** → device placed normally; the card's `external_io` nets retargeted to a synthesized terminal (generalizes the hardcoded speaker header).

`Peripheral` "needs no changes" beyond the `locus` field; the wiring loop's existing `if ep.ref not in placed` path is where retargeting hooks.

### 7.1 Silkscreen legend (functional wiring documentation — NOT cosmetic)

A field-wired terminal with no per-position labels is a wiring hazard: the silk legend **is** the wiring documentation for every remote device. So a synthesized connector is **not complete without its legend** — this is a hard requirement of the realization, emitted at PCB-generation time (the silk is a PCB concern; `generate_schematic` only sets schematic labels) via the existing `pcb_silkscreen` machinery.

Per synthesized connector, the unified helper emits legend metadata that the PCB step realizes:
- **Per-position label** — text = the position's signal/role name (e.g. `SCK WS SD +3V3 GND`; speakers `L+ L- R+ R-`), placed at a fixed offset from that pad, **derived from the footprint's actual pad coordinates** (reuse the pad-geometry access in `pcb_keepout`/`pcb_autoroute`) — never free-floating coordinates.
- **Device identity** — the terminal's value/text set to the off-board device it serves (e.g. `INMP441`), so the board reads which remote device each terminal is for.
- **Validation** — after placement, run `_op_check_silkscreen_overlaps` + `_op_auto_fix_silkscreen` so no label lands on a pad or another label (the fab hazard is handled by existing tooling).

Because the legend hangs off the **unified** `synthesize_connector` helper (§4), every connector — remote terminals, speaker terminals, `extra_connectors` — gets a consistent legend; the labeling is one mechanism, not three. Label text is the human-meaningful signal name (Open Decision 6).

---

## 8. BOM / manifest

`_summary()` (`design.py:74–86`) `peripherals` list gains a `locus` annotation per entry; remote devices are reported as **off-board, with the terminal they land on**. The `remote_device` gap (§7) makes the manifest honest: placed parts vs documented-off-board devices vs terminals are distinguishable. (There is no separate BOM doc today; this is the manifest.)

---

## 9. Interaction with the part-resolution phase (A+C)

Locus runs **upstream** of part placement, and it changes A+C materially:

- A **remote** device still resolves its **identity** (INMP441 — for terminal labels + BOM doc) but **availability is moot**: no chip is placed, so **workstream A (authoring the INMP441 chip symbol/footprint) is NOT needed** for the audio-node. A shrinks to "only if a device is ever `on_board`."
- Locus **dissolves the resolved-but-unplaceable problem** (cold-review I3/G2) for remote devices: a device with no KiCad symbol is realized as a terminal, never an unplaceable chip.
- The part-resolution "place a resolved part" logic must consult `locus` first: `on_board` → place symbol (needs availability); `remote` → terminal; `on_board_with_remote_io` → both.

---

## 10. Tests / golden harness

- Unit: the §4 unified synthesizer (pin-count validation boundary, ref-collision, role→position labeling); `Peripheral.locus` back-compat default; `board.yaml placement` validation (unknown locus/connector rejected).
- Integration: the `audio_s3` fixture gains a `board.yaml` declaring loci (mic/presence/piezos/speakers `remote`; amps `on_board_with_remote_io`). New expected output: terminals placed and net-routed; **no INMP441/LD2410 chip placed**; routes 0-unconnected. The existing component-count assertions in `test_audio_s3_to_routed_pcb` **change** (chips → terminals) — update deliberately.

---

## 11. Open decisions (for the cold reviewer)

1. **Locus default aggressiveness** — card may *suggest* a default locus (LD2410 → remote) with a disclosed assumption, OR locus is always board.yaml-explicit and an un-specified device defaults `on_board` (current behavior, safe but means every remote device needs a board.yaml line). Given the project's "never silently assume" stance, recommend: **no silent remote default; `on_board` default + a `placement_unspecified` advisory gap listing devices that are *commonly* remote**, prompting the user to set them.
2. **Synthesized-terminal `origin`** — `template` (re-generated each expand) vs `user` (preserved across re-import by `merge()`). A locus-derived terminal is conceptually template-generated but encodes a user decision (the board.yaml locus). Recommend `template` with the decision living in board.yaml (re-applied each run), so re-import stays deterministic.
3. **board.yaml `placement` key: device type vs device ref** — type is reusable and matches how loci actually generalize (all INMP441s are remote); ref is needed only to split multi-instance same-type devices to different loci. Recommend type-keyed with optional ref override.
4. **Default terminal-block footprint** — pick one concrete verified-present stock footprint as the screw-terminal default (pitch/series). Recommend a 5.08 mm part; confirm the exact lib:footprint exists on the target KiCad.
5. **`external_io` source for `on_board_with_remote_io`** — card-declared role list (recommended; data-driven, e.g. MAX98357A `[outp,outn]`) vs board.yaml per-net.
6. **Silk label text convention (§7.1)** — short signal/role name (`SCK`, `+3V3`, `L+`) vs full net name (`CMCA_MIC_SCK`). Recommend short + legible at terminal pitch, uniquified per connector, with the full net implied by the device-identity value on the terminal. Pin the dedup/uniquification rule (two `GND` positions on one terminal → `GND`/`GND` is fine; two different nets must not collide to the same label).

---

## 12. External-system assumptions to verify before implementation

- The chosen default terminal-block **footprint** (Open Decision 4) exists on KiCad 9 **and** 10 (symbols verified for both implicitly via std lib; footprint series presence spot-checked on 10 only — re-verify 9).
- `kicad_sch_api` places `Screw_Terminal_01xN` symbols and labels nets at numbered positions the same way it does `Conn_01x0N` (the module-card path already does this for `Conn_01x0N`, so high confidence — but confirm for the screw-terminal symbol specifically).
- `merge()` semantics for a `template`-origin synthesized terminal across re-import (Open Decision 2) — confirm no duplication/orphaning.

---

## 13. Effort (focused pairing sessions) & branch

| Stage | |
|---|---|
| L1 — `Peripheral.locus` + schema bump + back-compat | 0.5 |
| L2 — unified `synthesize_connector` helper (+ pin-count validation) | 1–1.5 |
| L3 — reimplement fragments (a)/(b)/(c) on L2 (Rule-3 consolidation) | 1–1.5 |
| L4 — `generate.py` locus branch (remote / on_board_with_remote_io) + glue suppression | 1 |
| L5 — `board.yaml placement` channel + validation | 1 |
| L6 — BOM/manifest annotations + `remote_device` gap | 0.5 |
| L7 — silkscreen legend on `synthesize_connector` (label off pad coords + overlap auto-fix; reuse `pcb_silkscreen`) | 1 |
| L8 — golden harness: audio-node board.yaml + updated expectations (assert legend present, no silk-over-pad) | 0.5–1 |

~7–8 sessions. **Branch from `main`** (per the part-resolution spec's recommendation); locus lands first, then the part-resolution work rebases on top with `locus` consulted at placement. Dependency order: L1 → L2 → (L3, L4) → (L5, L7) → (L6, L8). (L7 silk depends on the connector synthesis L2 + placement L4.)
