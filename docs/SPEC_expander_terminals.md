# SPEC: Expander-port terminals (MCP23017 GPA/GPB → labeled terminals) — v2, post-cold-review

Status: DRAFT, cold-reviewed (3 severity tiers). **Do not implement in the writing session.** v2 resolves the review's contradictions/gaps; the diff from v1 is in §0.

## 0. What the cold review changed (read first)

- **Core mechanism CONFIRMED (KiCad 10).** An endpoint `{ref=<MCP>, pin="GPA0"}` resolves: `generate.py:resolve_pin` → `resolve_pin_token` → exact pin-name match on the symbol (GPA0 → pad 21). `synthesize_connector`/`_emit_connector` are **agnostic about the far end** — a terminal whose other endpoint is a chip pin places/legends exactly like one wired to the MCU. So the design is buildable.
- **BLOCKER — KiCad 9.** The MCP23017 card's `lib_id: Interface_Expansion:MCP23017x-x-SO` exists **only in KiCad 10**; KiCad 9 names it `MCP23017_SO`. The CI matrix runs both. On KiCad 9 the MCP23017 chip already fails to place (pre-existing card issue), so tapping its pins yields unwired nets. → §9 makes this an explicit dependency/risk; the expander feature inherits the MCP23017's cross-version symbol resolution and must not be the thing that "discovers" it in the field.
- **7 contradictions resolved** (§4 decisions): `ports:int` kept (it's the MCP **hardware** register order + the user's explicit count, not a firmware guess); `ports>16` is a disclosed **error**, not a silent cap; `single`-over-16 emits a **gap** + pin-header fallback (recommend per_bank); `power` default `3v3` reframed as a declared convenience (carve-out in §3), `none` available; `_emit_connector` reuse **decided yes** (tier-1 confirmed compatible); `group: per_sensor` is the schema default (Q5 closed); duplicate `net_prefix` is a **SidecarError at load**.
- **Schema cascade fully enumerated** (§5) incl. the sites v1 review taught us: `BoardSidecar`, `load_sidecar`, `_KNOWN_SIDECAR_KEYS`, `_validate`, `apply_sidecar`, `test_known_keys_all_accepted`, `SCHEMA_VERSION` bump (6→7), the sidecar **docstring example**, and the `DesignIntent` field shape (now committed).
- **mcp23017.yaml gains a `port_pins` enumeration** as the validation source of truth (avoids needing KiCad at unit-test time).

## 1. Problem
(unchanged) The MCP23017 is placed with its I2C side wired but GPA0-7/GPB0-7 float; the up-to-16 TCRT5000 sensors that wire there are absent. Firmware gives a count + register convention + position table but no per-sensor `#define` (register-addressed at runtime). This case requires an explicit board.yaml declaration.

## 2. Current state (grounded, file:line)
- `mcp23017.yaml`: roles `{SDA, SCL: SCK, INT}`, supply/ground, address straps; **GPA/GPB not modeled**.
- KiCad 10 symbol `Interface_Expansion:MCP23017x-x-SO`: pins `GPA0..GPA7` (pads 21-28), `GPB0..GPB7` (pads 1-8). KiCad 9: `MCP23017_SO` (different lib_id) — see BLOCKER §0/§9.
- `Endpoint.pin` (intent.py:43) is consumed by `generate.resolve_pin` (generate.py:89-107) via `resolve_pin_token` (mcu_pinmap.py:50-66) — exact, case-sensitive name match. Already exercised by passive/USB pads.
- Connector synth: `synthesize_connector` (connectors.py:104-188) far-end-agnostic; `_emit_connector` (templates.py:120-157) sets silk legend + field-wiring placement heuristic.
- Schema guards: `_KNOWN_SIDECAR_KEYS` (sidecar.py:127), `BoardSidecar` (sidecar.py:94), `load_sidecar` (sidecar.py:315), `_validate` (sidecar.py:223), `apply_sidecar` (sidecar.py:349), `test_known_keys_all_accepted` (test_firmware_sidecar.py:208), `SCHEMA_VERSION=6` (intent.py:29). `_REALIZED_LOCI` (sidecar.py:472) — NOT touched (this feature bypasses `placements`).
- Expand: `_REGISTRY` list (templates.py:793); `expand_intent` runs `_inject_terminal_loci` then the registry; `RefAllocator`/`nets_by_name` built once before the loop. `new_nets` append has **no collision guard** (templates.py:840).

## 3. Goals / non-goals (carve-out added)
**Goals.** Declare an expander's sensor ports → labeled terminal(s) tapping the floating GPAn/GPBn pins + power. Honest-by-construction: explicit board.yaml.
**Non-goals.** Auto-read `NUM_SENSORS`; synthesize the TCRT5000 silicon (LED resistor/pull-up live on the breakout); non-MCP23017 expanders in v2.0.
**Carve-out (review #4):** the power rail at the terminal is **user-declared topology**, not an inference about the sensor. `power: 3v3` is a convenience default (most sensor breakouts want board power on a 3-wire terminal); `power: none` for self-powered breakouts. This is distinct from synthesizing the sensor's internal parts.

## 4. Design (decisions baked in)
### board.yaml surface
```yaml
expander_terminals:
  U3:                    # expander ref (a placed peripheral). Bypasses placement:.
    device: TCRT5000     # silk label + connector value
    ports: 6             # int N => first N ports in MCP hardware order GPA0..GPA7,GPB0..GPB7
                         #   (HARDWARE register order, not a firmware guess);
                         # OR explicit list: [GPA0, GPA1, GPB0]
    group: per_sensor    # per_sensor (DEFAULT) | per_bank | single
    power: 3v3           # 3v3 (DEFAULT, convenience) | 5v | none
    net_prefix: SENSOR   # default = device value; MUST be unique across entries
```
**Decisions:**
- `ports: int` — allowed. N maps to the first N pins in the MCP23017 **hardware register order** (GPA0-7 then GPB0-7). This is a hardware fact + the user's explicit count, not a firmware-convention guess (resolves review #1). `> 16` → **disclosed error, no synthesis** (delete any "cap"; resolves review #5).
- `group` — `per_sensor` default (N × `[signal, +power, GND]`); `per_bank` (one terminal per used GP bank); `single` (all + power — if >16 positions, emit a **gap** and fall back to a pin header; recommend per_bank instead). Disclosure is always a **Gap** (non-fatal, user-visible), never a bare log (resolves review #3).
- `power` — `3v3` convenience default / `5v` / `none`. **`5v` requires the `+5V` rail to exist** (the usb_programming/CP2102 block ran); if absent, emit a gap and treat as `none` (resolves review-scope #10).
- `net_prefix` — default = `device`; **must be unique across all `expander_terminals` entries** → `SidecarError` at load on duplicate (resolves review #7/#14). Nets `{prefix}_{i}`.

### Net synthesis
New template `expander_terminals(intent, alloc)` appended to `_REGISTRY` (after the device templates; the MCP is already placed and is NOT remote, so no `remote_peripherals` interaction — §7 asserts this). For each declared port pin: create a **new net** `{prefix}_{i}` = `[Endpoint(ref=expander_ref, pin="GPAn"), <terminal position>]`; **guard against name collision** with `nets_by_name` (rename `{prefix}_{i}_{ref}` + gap, or refuse — see §6). Terminal(s) + `+power`/`GND` via `_emit_connector` (decided reuse; resolves review #2), grouped per `group`.

### DesignIntent shape (committed; resolves scope #3)
Add `DesignIntent.expander_terminals: dict[str, ExpanderSpec]` keyed by expander ref, where `ExpanderSpec` is a small dataclass `{device, ports: list[str], group, power, net_prefix}` (the `ports:int` shorthand is expanded to a pin-name list at sidecar-apply time, so the template sees only resolved pin names). `to_dict`/`from_dict` get an explicit branch; `SCHEMA_VERSION` 6 → 7.

### Port-pin validation source (resolves scope #7)
Add a `port_pins: [GPA0..GPB7]` stanza to `mcp23017.yaml`. `_validate_expander_terminals` checks each requested pin against the card's `port_pins` (data-only; no KiCad needed at test time). Unknown pin (`GPC0`) or `ports` exceeding `len(port_pins)` → SidecarError/gap.

## 5. Implementation touchpoints (complete)
- **intent.py**: `ExpanderSpec` dataclass; `DesignIntent.expander_terminals` field; `to_dict`/`from_dict` branch; `SCHEMA_VERSION` 6→7.
- **sidecar.py**: `BoardSidecar.expander_terminals` field (+ field comment); `load_sidecar` constructor line; `"expander_terminals"` in `_KNOWN_SIDECAR_KEYS`; `_validate_expander_terminals` called from `_validate` (ref present? pins valid vs card `port_pins`? group/power valid? net_prefix unique?); `apply_sidecar` block populating `intent.expander_terminals` (resolve `ports:int`→pin list here; defer "ref not yet placed" to template-time, see §6); **module docstring example** gains the block.
- **templates.py**: `expander_terminals` template + register in `_REGISTRY`; reuse `_emit_connector`; net-collision guard.
- **devices/peripherals/mcp23017.yaml**: add `port_pins` enumeration.
- **generate.py**: no change expected (MCP stays placed; new nets wire by pin name) — confirm via the integration test.
- **tests**: see §7. **`test_known_keys_all_accepted` (test_firmware_sidecar.py:208) MUST add the key + assert the round-trip** (the 3-source CI guard; scope #2).

## 6. Edge cases / seams
- `ports>16` or list longer than `port_pins` → SidecarError/gap, no synthesis.
- `single` >16 positions → gap + pin-header fallback (recommend per_bank).
- bad pin name / unknown expander ref / ref not a placed peripheral → gap, no crash. (Ref-existence checked at template-expand time, since placement happens during expand.)
- multiple expanders → distinct refs; **unique net_prefix enforced at load**.
- net-name collision with an existing net → guard in the template (namespace by ref + gap); do NOT silently append a duplicate-named net (templates.py:840 has no guard today).
- expander I2C/INT side already wired — only tap GPA/GPB; never touch existing nets.
- `power: 5v` with no `+5V` rail → gap, treat as `none`.
- `power: none` → `[signal, GND]` per sensor (2-pos) — confirm 2-pos screw terminal is in the Phoenix range (yes, 2..16).
- `per_bank` when only GPA used → one terminal (no empty GPB terminal).
- `ports: 0`/empty → no-op + note.
- KiCad 9 (BLOCKER §0): MCP not placed → endpoints unresolved. Gate or accept as known limitation (§9).

## 7. Test plan
- per_sensor: N ports → N terminals, each tapping the correct `GPAn` pad + power + GND; nets named/labeled; each net carries the expander-pin endpoint.
- per_bank / single variants: expected terminal count/positions; `single`>16 → gap + pin-header.
- `ports:int` and the equivalent explicit list → identical result.
- validation: bad pin / over-16 / unknown ref / duplicate net_prefix → SidecarError or gap (accept/reject matrix), no crash.
- `power: 5v` with no +5V rail → gap + downgrade.
- **Regression gate (scope #8):** `test_generate_speedcal_end_to_end` (test_firmware_generate.py:75) still passes unchanged WITHOUT an expander_terminals block (MCP I2C side untouched, `components_placed` unchanged).
- **No double-emit (scope #11):** after `expander_terminals` runs, the MCP23017 is NOT in `offboard_refs` and `remote_peripherals` emits no terminal for it.
- **`test_known_keys_all_accepted` (scope #2):** add `expander_terminals` + assert round-trip.
- **Integration (scope #9):** a `test_expander_terminals_to_routed_pcb` (pattern: test_firmware_pcb_pipeline.py:1010) — board.yaml with `expander_terminals: {U3: {device: TCRT5000, ports: 6}}` on the speed-cal (or a minimal MCP) fixture → full import→expand→generate→build_pcb → 6 terminals route DRC-clean, GPA0-5 connected. **This is where KiCad-9 vs 10 surfaces — run on the matrix.**
- at/below/above on ports count (0, 16, 17).

## 8. Decisions still open for the implementer (small)
- Net-collision behavior: rename-with-gap vs refuse. (Recommend rename `{prefix}_{i}_{ref}` + gap, since the user's intent is clear.)
- `per_bank` terminal labeling (GPA bank vs GPB bank silk text).

## 9. Dependency / risk: KiCad 9 MCP23017 symbol
The MCP23017 card's `lib_id` resolves only on KiCad 10. This is **pre-existing** (not introduced here) but the expander feature is the first to depend on the MCP's *port* pins, so a KiCad-9 build would silently produce unwired sensor nets. Options, in order: (a) fix `mcp23017.yaml` to a lib_id that resolves on BOTH 9 and 10 (verify whether the library's symbol-aliasing resolves `MCP23017_SO`↔`MCP23017x-x-SO`, or add a per-version card) — **a separate prerequisite fix**, ideally done first; (b) gate `expander_terminals` on KiCad 10 with a clear gap on 9; (c) accept as a documented limitation. Recommend (a) as a standalone fix before/with implementation, and an integration test on BOTH matrix versions to prove it.

## 10. Process note
Cold-reviewed via 3 severity-tier subagents. Implementation is a SEPARATE session/turn. Companion to SPEC_offboard_terminals (v1 shipped). The KiCad-9 MCP23017 symbol fix (§9) is a recommended prerequisite.
