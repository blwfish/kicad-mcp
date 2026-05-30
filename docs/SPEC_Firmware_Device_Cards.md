# SPEC — Firmware Front End: Data-Driven Device Cards (Phase 6)

**Status:** Phase 6 (refactor) + Phase 7 (offline auto-draft) IMPLEMENTED; 6b (board.yaml sidecar) and Phase 8 (multi-user / pre-fetch) remain planned. Cold-reviewed pre-implementation (findings folded in below).
**Author context:** written in the Phase-5 session (track-geometry I2C sensor-hub, PR #50), with full context on `kicad_mcp/utils/firmware/`.
**Review note:** per the CLAUDE.md spec-review rule, the highest-risk item is the *card → symbol-pin* external-system assumption. It is verified in-session (§9) rather than deferred. If this spec is picked up in a later session, re-verify §9 against the then-current KiCad before implementing.

---

## 1. Problem

Across Phases 1–5, every new board shape required a code change. Sorting those diffs by *kind*:

| Kind | Examples | Really data or logic? |
|---|---|---|
| A. `knowledge.py` table edits | MPU6050/OLED/CP2102/MAX98357A/SPH0645 entries, S3 MCU, footprints, pin/role maps | **Data** in Python clothing |
| B. per-device config template | `mcp23017_config`, `mpu6050_config` (strap pin→rail per address) | **Mostly data** (a strap rule) wearing a function |
| C. classifier/seam tables | `_BUS_ROLES`, `BARE_PIN_ALIASES`, `address_base` shape | **Data** allow-lists + occasional logic |
| D. cross-cutting parse/generate logic | `_name_or_number`, IO{n} mapping, preprocessor, typed-bus grouping | **Genuine logic** |

A, B, and most of C are **data**, and they were the bulk of the Phase 3a/4/5 diffs. D is real code but **saturating** — each D fix is a one-time generalization that retires a whole class of future changes (Phase 4 retired "`_PIN`-only"; Phase 5's `_name_or_number` retired "roles must be pin names").

**Thesis:** push the *data* out of Python so a new board is, in the common case, a *data* edit (new device card) — not a code change, not a test edit. Keep the honesty guarantees by moving the project's "verify pins / boundary-test straps" discipline to **load-time validation** plus the existing per-shape golden harness.

## 2. Goals / Non-goals

**Goals**
- A new *recognized* peripheral or MCU = adding/editing a YAML **device card**. No Python edit, no unit-test edit.
- Collapse `mcp23017_config` + `mpu6050_config` (and any future `*_config`) into **one** data-driven `device_config` template.
- Preserve every current behavior exactly (golden harness proves equivalence; speed-cal/audio-S3/track-geometry must stay byte-stable where asserted).
- Preserve honesty-by-construction: an unknown device still becomes a gap, never an invention.

**Non-goals (this phase)**
- Auto-drafting cards from LCSC/datasheets (perception-bound; separate, fuzzy effort — §11).
- Replacing the *cross-cutting logic* in D. That stays code.
- The `board.yaml` non-firmware sidecar — specified at §7 but may ship as Phase 6b.

## 3. Device-card schema

Cards live in a packaged directory `src/kicad_mcp/utils/firmware/devices/` plus optional override dirs (§6.3). One file per device/MCU. The on-disk card is the **source**; the in-memory shape stays the existing `PeripheralInfo` / `McuInfo` TypedDicts (so `templates.py` and `generate.py` consumers do **not** change — only the data's origin does).

**TypedDict extension (cold-review fix).** `PeripheralInfo` must gain two *optional* fields the cards add — `config` (strap/tie spec, §4) and `decoupling` (override list). Python floor is **3.10**, so `typing.NotRequired` (3.11+) is unavailable and `typing_extensions` is only transitive. Use the stdlib **inheritance pattern**: a required base `class _PeripheralInfoBase(TypedDict)` + `class PeripheralInfo(_PeripheralInfoBase, total=False): config: dict; decoupling: list[dict]`. Existing literals/cards that omit these stay valid and mypy-clean on all of 3.10/3.12/3.13.

**Consumer audit (cold-review).** Every reader of the externalized data keeps working *unchanged* because return types are preserved — confirmed callers: `resolve_peripheral` (intent.py `build_intent`, templates.py `_ic_power_pins`/`device_config`), `resolve_mcu` + `resolve_mcu_by_part` (templates.py, 4 call sites), `role_to_pin_name` (generate.py:97, delegates to `resolve_peripheral`). The template-owned constants `MAX98357A`, `SPH0645`, `AMS1117`, `USB_C_*`, `CP2102_*`, `HDR_1X*`, `SW_PUSH_*` **stay as Python in knowledge.py** — they are not firmware-addressable peripherals (no `*_ADDR`/pin-hint names them); they are design knowledge owned by their templates. Out of card scope, by design.

### 3.1 Peripheral card

```yaml
# devices/peripherals/mpu6050.yaml
type: MPU6050                 # UPPER; matches address_base()/peripheral_hint
lib_id: Connector_Generic:Conn_01x05
value: GY-521 (MPU-6050)
footprint: Connector_PinHeader_2.54mm:PinHeader_1x05_P2.54mm_Vertical
bus: I2C                      # null for non-bus devices
module: true                 # breakout-module-header representation (carrier-board)
roles:                        # firmware signal-role (UPPER) -> symbol pin NAME or NUMBER
  SDA: "4"
  SCL: "3"
supply_pins: ["2"]
ground_pins: ["1"]
decoupling:                   # optional; replaces the implicit "1x 100nF per IC"
  - {value: 100nF}
config:                       # optional; replaces the *_config TEMPLATE (see §4)
  address_strap:
    pin_bits: ["5"]           # LSB-first; one symbol pin per strappable address bit
    base: 0x68                # address when all bits clear
    rail_set: "+3V3"          # bit = 1
    rail_clear: "GND"         # bit = 0
  static_ties: []             # e.g. MCP23017: [{pin: "~{RESET}", rail: "+3V3"}]
```

`mcp23017.yaml` is the same schema with `pin_bits: ["A0","A1","A2"]`, `base: 0x20`, and `static_ties: [{pin: "~{RESET}", rail: "+3V3"}]`. **This single schema reproduces both existing strap functions exactly** (§8 equivalence).

### 3.2 MCU card

```yaml
# devices/mcus/esp32-wroom-32e.yaml
part: ESP32-WROOM-32E
lib_id: RF_Module:ESP32-WROOM-32E
value: ESP32-WROOM-32E
footprint: RF_Module:ESP32-WROOM-32E
board_match: ["esp32dev", "esp32"]   # board-id substrings (see §3.3)
needs_3v3: true
supply_pin: VDD
ground_pin: GND
en_pin: EN
boot_pin: IO0
uart_rx_pin: RXD0/IO3
uart_tx_pin: TXD0/IO1
native_usb: false
```

### 3.3 MCU resolution — removing a Rule-3 seam

`resolve_mcu` today is an **ordered dict** where "more specific first" is enforced only by a comment (`esp32-s3` must precede `esp32`). That ordering is a silent-failure seam. Replace with explicit, order-independent resolution:

1. Exact match on a card's `part`/`board_match` (lowercased).
2. Else the card whose **longest** `board_match` substring occurs in the board-id (deterministic; ties broken by `part` for stability).

This makes adding an MCU card order-independent — a logic improvement that *removes* a footgun, not just relocates data. **Equivalence caveat (cold-review):** this is behavior-identical to the current ordered-dict resolution for all 5 board IDs any test exercises (`esp32dev`, `esp32-s3-devkitc-1`, `esp32-s3`, and the substrings `esp32`/`esp32s3`); it differs from insertion-order tie-breaking *only* for a hypothetical board-id with two equal-length matching substrings, which nothing exercises. The §8 "zero assertion edits" claim holds.

## 4. The generic `device_config` template

Replace `mcp23017_config` and `mpu6050_config` with one template that reads each placed peripheral's card `config`:

- For `address_strap`: `offset = address - base`; for each bit *i*, tie `pin_bits[i]` to `rail_set` if `(offset >> i) & 1` else `rail_clear`. If `offset` is outside `[0, 2**len(pin_bits) - 1]`, emit an `invalid_address` gap (never strap).
- For `static_ties`: tie each `{pin, rail}` directly.

The bit→rail math lives in **one** boundary-tested helper, `compute_address_straps(card_config, address) -> list[(pin, rail)] | None`, which is the single source of truth replacing both `mcp23017_address_straps` and `mpu6050_ad0_strap`.

**No-op without a `config` (cold-review):** `device_config` produces nothing for a peripheral whose card lacks `config.address_strap`/`static_ties` (e.g. the OLED 4-pin header has no strappable pin) — identical to today's behavior where no `*_config` template matched. The template iterates placed peripherals and skips any with no `config`.

A new addressable device then needs **no template at all** — just a `config.address_strap` stanza in its card.

## 5. Loader

`knowledge.py` gains:

- `load_device_cards()` — discover + parse + validate all cards once (module-level cache). Returns `{type: PeripheralInfo}` and `{part: McuInfo}`.
- `resolve_peripheral` / `resolve_mcu` / `resolve_mcu_by_part` read the cache instead of literals. Public signatures and return types are **unchanged**.
- The strap helpers become thin wrappers over `compute_address_straps` (or are removed in favor of it; callers updated).

Discovery precedence (later overrides earlier, like `ADDITIONAL_SEARCH_PATHS`):
1. packaged `devices/` (shipped),
2. `$KICAD_MCP_DEVICE_DIRS` (colon-list, optional),
3. project-local `./firmware-devices/` if present.

A malformed card **fails loudly** at load (see §6) — never silently ignored (data-capture rule: no `continue`-on-failure without surfacing).

## 6. Validation — two tiers (the honesty backstop)

Moving data out of Python forfeits mypy + unit coverage *of that data*. Recover it with validation, split by what each tier can see:

### 6.1 Structural (no KiCad — runs in the 1465-test unit matrix)
- Required fields present; types correct; `bus` ∈ known set or null.
- `lib_id` well-formed (`Lib:Symbol`).
- `config.address_strap`: `base` an int, `pin_bits` non-empty, rails ∈ `{+3V3,+5V,GND,VBUS,...}`.
- **Strap math boundary-checked**: for a card with N bits, addresses `base-1`, `base`, `base+2**N-1`, `base+2**N` produce the expected strap/`None` (at/below/above — threshold rule).
- No duplicate `type` / `part` across cards.

### 6.2 Pin-existence (needs KiCad — runs in the integration / self-hosted matrix)
**This is the constraint surfaced during spec review:** the unit matrix has no KiCad symbol libraries, so pin-existence can't be checked there.
- For each card, load its symbol via the symbol cache and assert every referenced pin (`roles` values, `supply_pins`, `ground_pins`, `config` pins) resolves — **reusing `generate._name_or_number`** (name lookup OR number-in-valid-set), so the validator and the generator share one resolution rule (Rule 3).
- Lives in a new `tests/integration/test_device_cards.py`, gated by `KICAD_INTEGRATION=1`, run on the self-hosted KiCad 9/10 matrix. A wrong pin in a card fails CI there.

### 6.3 Why two tiers is correct, not a compromise
The structural tier catches the *common* card mistake (typo, bad strap range) fast and KiCad-free, keeping the 17-second unit loop intact. The pin tier catches the *dangerous* mistake (pin doesn't exist on the symbol) where symbols are actually available. The per-shape **golden harness** (config.h → routed PCB, 0-unconnected) remains the end-to-end backstop that proved its worth catching the KiCad-10 dead-board bug 1300 unit tests missed.

## 7. Board sidecar `board.yaml` (Phase 6b — optional)

Firmware is structurally blind to connectors, power source, board dimensions, mechanical. Today those are perpetual gaps. A per-board sidecar (next to `config.h` or passed to `import_firmware`) supplies them as **data**, never invented in code:

```yaml
# board.yaml
power_source: usb_c            # or barrel, header, battery — sources the +5V rail
board_size_mm: [90, 75]
extra_connectors:
  - {ref: J_PWR, lib_id: Connector:Barrel_Jack, nets: {"1": "+5V", "2": "GND"}}
```

`build_intent` merges sidecar facts and marks the matching gaps `resolved_by: "board.yaml"`. Honest-by-construction extends cleanly: what firmware can't know comes from a data file, with provenance. **Deferred from the core phase** to keep the equivalence-preserving refactor separable from new behavior.

## 8. Migration & equivalence

1. Extract the 6 existing entries (2 MCUs in `_MCUS`, 4 peripherals in `_PERIPHERALS`) into cards. Byte-for-byte the same field values. **The S3 MCU card must carry all three board-id variants** `board_match: ["esp32-s3-devkitc-1", "esp32-s3", "esp32s3"]` and the WROOM-32 card `["esp32dev", "esp32"]` — omitting any is a regression (cold-review).
2. Extract the 2 strap functions into card `config` stanzas + `compute_address_straps`.
3. Replace `mcp23017_config`/`mpu6050_config` registry entries with one `device_config`.
4. **Equivalence proof:** the full unit suite (1465) + all 3 integration shapes must pass unchanged. Because the in-memory `PeripheralInfo`/`McuInfo` shape and all public signatures are preserved, downstream code is untouched; only the data's *source* moves. Any diff in generated schematic/PCB is a migration bug.
5. Keep `mcp23017_address_straps` / `mpu6050_ad0_strap` as thin wrappers over `compute_address_straps`, **preserving their exact return types** (cold-review): `mcp23017_address_straps` returns the `list[(pin,rail)]` directly; **`mpu6050_ad0_strap` must unpack the single-element list back to a bare `tuple`** (`r[0] if r else None`) so the 3 Phase-5 assertions (`== ("5","GND")`, etc.) stay green with no edit. "Zero assertion edits" means exactly this — all 18 existing references in `test_firmware_templates.py` keep working through the transparent wrappers; the only *registry* change is the two `*_config` entries → one `device_config`.

## 9. External-system assumptions (verified in this session)

| Assumption | Verified? | Evidence |
|---|---|---|
| Symbol pins expose `.number` and `.name` so card pins can be validated | **Yes** | `get_symbol_cache().get_symbol(lib).pins[i].number/.name` — `Conn_01x05` → `("1","Pin_1")…`; `MCP23017` → `("1","GPB0")…` |
| A role may map to a pin NUMBER (header `"4"`) or NAME (`"SDA"`); validator must accept both | **Yes** | `generate._name_or_number` already does exactly this; validator reuses it |
| Pin-existence validation cannot run in the no-KiCad unit matrix | **Yes** | the 1465-test matrix runs with no KiCad install; symbol cache can't resolve there → §6.2 lives in the integration tier |
| One strap schema reproduces both existing functions | **Yes (by construction)** | MCP = `pin_bits:[A0,A1,A2], base:0x20`; MPU = `pin_bits:[AD0], base:0x68`; same `offset` math + range check |
| Bare-IC symbols (e.g. `Sensor_Motion:MPU-6050`) often have many NC/support pins | **Yes** | MPU-6050 symbol = 24 pins (CLKIN, NC×4, AUX_DA…); reinforces that `module: true` header representation is a per-card choice the schema must support |

## 10. What stays code (and why that's fine)

The cross-cutting D logic — `_name_or_number`, `gpio_to_pin_number`/IO{n} mapping, `select_active_branches` preprocessor, `_build_buses` typed-bus grouping, the role-token classifier. These are *algorithms*, not tables, and they're **converging**: the set of board shapes that need a *new* one is shrinking each phase. Data-driving the tables is what removes the recurring effort; the algorithm surface doesn't need externalizing.

## 11. Phase 7 — card auto-draft (drive Floor-1 effort toward zero)

Once cards are the unit, the residual is "no card exists yet for this part." Auto-drafting closes most of it. On an unknown peripheral, synthesize a **draft card** from signals already present:

- **Symbol-pin-name match.** For well-named parts the firmware role token *is* the symbol pin name (`SDA`→`SDA`). Reuse the extracted `resolve_pin_token` (§14 Step 0) to score role→pin matches against the candidate symbol. High score → auto-draft; the mismatches that needed hand-authoring before (MCP23017 `SCL`→`SCK`, HX711 `PD_SCK`, numbered module headers) score low → flagged for confirm.
- **I2C address → identity table** (pure data): `0x68/0x69`→MPU-6xxx/ICM, `0x3C/0x3D`→SSD1306, `0x76/0x77`→BME280, `0x40`→INA219, `0x48`→ADS1115… The address space is a strong identity signal currently ignored. (track-geometry even carries a commented `// #define INA219_ADDR 0x40`.)
- **Identity hints already parsed and dropped:** `*_WHO_AM_I_EXPECTED` sits in `provenance.unparsed` today — a free confirmation signal.

**Confidence-tiered output, never a silent guess:** `draft` (auto, unconfirmed) < `confirmed` (human/agent approved) < `verified` (passed pin-existence + a golden-harness route). An unconfirmed draft is *offered as a resolved-pending gap*, not silently placed. **Confirm-once, reuse-forever:** a confirmed card is committed and never re-drafted — so per-design effort amortizes to zero as the card library saturates the user's recurring parts vocabulary.

**"Clean bus" — a strict definition (cold-review).** Name-existence is not function-correctness: a symbol can have a pin literally named `SCL` that isn't an I2C clock, and `resolve_pin_token` would still resolve it. So `draft` confidence is **high only when ALL of**: (a) the firmware bus roles (e.g. SDA+SCL) resolve to symbol pins; (b) **every** remaining symbol I/O pin is *either* a recognized role token *or* in the supply/ground sets — i.e. nothing unexplained; (c) the symbol is single-unit (`pin_number_by_name` is ambiguous on multi-unit parts — a Phase-8 bulk-gen hazard, flagged not auto-shipped). Anything failing (a)–(c) → `draft-low` → surfaced for confirm, never auto-placed. For numbered-pin module headers there are no names to match, so auto-draft yields *no* output (safe), and they stay in the small hand-card set.

## 12. Phase 8 — generalize beyond one user (local-first, NO phone-home)

Phases 6–7 make *my* boards approach zero because my parts vocabulary is finite and saturates. A different user's vocabulary differs but is *also* finite and recurring. The naive way to share the saturating assets is a runtime registry — **rejected.** A large slice of this audience (privacy-conscious makers, industrial/defense, self-hosters — and the project's own local-first/AI-first ethos) will not run anything that "sends" at runtime; even a part-number lookup leaks what you're building. **Hard constraint: the tool MUST be fully functional air-gapped. No telemetry, no runtime network, no phone-home — ever, by default.**

This is not a real cost, because the *dominant* amortization terms are already local:

- **Single-user reuse** (your boards reuse your own confirmed cards) — the biggest term, 100% on-machine.
- **A bundled card library** shipped *with* the tool — exactly the KiCad symbol/footprint-library model (also PlatformIO platforms, Arduino board packages). It covers the common parts on install; it's curated in the open-source repo and delivered by ordinary `git pull` / package update / re-running AGENT-INSTALL. **Contribution is out-of-band** (a maintainer merges a PR); the user's runtime never reaches out. People who *want* to share do so via normal OSS; everyone else just consumes the curated set on update.

The only term that needed a service — a stranger's confirmation reaching you *automatically at runtime* — is the *smallest*, and is replaced by "it's in the next bundled-library update, if a maintainer curated it." Nobody is bothered by that; it is how KiCad libraries already work.

Three asset types, all local, all versioned-in-repo, none requiring a runtime connection:

1. **Device cards (parts) — a bundled, repo-curated library.** Cards ship with the tool and are discoverable offline by part / lib_id / I2C address / fuzzy name against the *local* set. The library maintains the one layer no existing source provides — the firmware-role → symbol-pin semantic map. Verification tiers + CI-gating apply at the **repo/PR** level (a maintainer's CI runs structural + pin-existence + golden route before merge), not at any user's runtime. **Sharing is file-copy, opt-in:** a card is a YAML file; drop it in a dir read via `KICAD_MCP_DEVICE_DIRS`, commit it to your own repo, paste it in a forum. Never automatic.

   **Populating it — maintainer-time pre-fetch (so PRs reduce to "new or weird").** The bundled library is not seeded by hand one part at a time; it is *bulk-generated at build/curation time* (network + compute are fine for the maintainer — only the *user* must be air-gapped). The richest source is already on disk: the **KiCad symbol libraries** (thousands of symbols, each with named pins). A build-time job:
   - **Enumerates** symbols filtered to device categories (`Sensor_*`, `Interface_Expansion`, `Display_*`, `Driver_*`, `Analog_ADC`, …) with a **clean bus signature** (pins naming SDA/SCL, MOSI/MISO, BCLK/LRCK) — *sensible*, not exhaustive (skip the passive/connector bulk).
   - **Auto-drafts** a card per symbol via the Phase-7 logic (role→pin is identity wherever names match — the well-named majority), and **crosses it with the bundled I2C-address→part table** so address-only firmware declarations (`BME280_ADDR 0x76`) resolve to a pre-carded part with no PR.
   - **Gates hard:** only the **high-confidence tier** (clean bus + identity name match, passes pin-existence, sample golden-route) auto-ships; medium/low are queued for human review. Mass generation without this gate ships subtly-wrong cards at scale.

   This flips the model from "ship empty, PR as you go" to **"ship the auto-derivable universe, PR only the residual."** The residual that still needs a human is exactly the *interesting* set: **name-mismatched pins** (MCP23017 `SCL`→`SCK`, HX711 `PD_SCK` — flagged, not auto-shipped wrong), **numbered-pin module headers** (GY-521, OLED — no names to match; small finite hand-set), **parts with no KiCad symbol**, and **novel topologies** (a template, not a card). A fully-local, no-telemetry complement: the tool keeps a *local* "parts I hit that weren't carded" log — the user's own prioritized PR list; the maintainer prioritizes the pre-fetch from symbol-lib + popular-parts priors instead.

2. **Firmware readers (ecosystems) — a pluggable front.** The `config.h` `#define` scanner encodes one ecosystem's dialect. STM32/CubeMX `.ioc`, Zephyr devicetree, Arduino `variant`, nRF SDK config are different inputs — and **richer ones**: a devicetree literally declares bus topology + device nodes, i.e. most of the DesignIntent already. The `#define` scan is the *worst-case* (least-structured) input; everything else is downhill. DesignIntent is already "the canonical editable seam," so this is purely *additive*: a reader plugin produces the intent's raw inputs (pins/addresses/buses/MCU); intent/templates/generate/cards downstream stay ecosystem-agnostic. All local.

3. **MCU pin-naming scheme as card data.** `mcu_pinmap`'s `IO{n}` mapping is ESP32-specific; STM32 is `PA0`/`PB3`, nRF is `P0.04`. Make the firmware-GPIO → symbol-pin scheme a field on the MCU card, consumed by a generic mapper — so a new MCU family is a card + (occasionally) a scheme, not a code edit.

**Auto-draft (§11) is already offline by construction** — local KiCad symbol-name matching + the *bundled* I2C-address table + `WHO_AM_I` hints from the firmware you already have. A user with zero internet and zero community coverage is still unblocked. Any network feature (e.g. an *optional* LCSC draft) must be **opt-in, explicitly flagged, and never a silent fallback**; the offline path is the complete default.

**End state — effort ∝ *global* novelty, with zero runtime sharing:** the first user to hit a new part cards it (offline auto-draft); if they choose, they PR it to the bundled library and the next *release* carries it. New ecosystem → a reader plugin; new topology → a template plugin. Everyone inherits via ordinary updates, never via a live service. The honest-by-construction gap manifest matters *more* for strangers, not less: a non-you user can't be assumed to know the tool guessed, so unconfirmed drafts always surface as flagged, never silently placed.

**Honest cost:** the bundled library still needs curation + maintainer-side CI (validate submitted cards before they ship). That is real but ordinary open-source maintenance — *not* a hosted service, *not* user-facing infrastructure, and it preserves the air-gapped guarantee.

## 13. Test plan

- **Unit (no KiCad):** `tests/test_device_cards.py` — schema validation, strap-math boundaries per card, duplicate-type detection, loader precedence, resolution parity (`resolve_peripheral`/`resolve_mcu` return the same dicts the literals did — a frozen snapshot of the 6 entries).
- **Integration (KiCad):** `tests/integration/test_device_cards.py` — every card's pins exist on its symbol. Plus the 3 existing golden shapes unchanged.
- **Equivalence gate:** full suite + 3 integration shapes green with zero assertion edits (except the 2 Phase-5 strap-fn imports → generic helper).
- **Auto-draft (Phase 7):** address-table identity boundaries, role→pin match scoring (a known mismatch like MCP `SCL`→`SCK` must score *low* → flagged, never auto-placed), and that an unconfirmed draft surfaces as a pending gap rather than a placed part.

## 14. Sequencing & effort (pairing units)

0. **Precondition (cold-review):** extract `generate._name_or_number` (currently a nested closure inside `generate_schematic`) to a module-level `mcu_pinmap.resolve_pin_token(sym, token) -> Optional[str]`; update `generate.py` to call it. Both the §6.2 validator and the §11/§12 auto-draft *reuse* this — a closure can't be imported, so this must land first. Behavior-preserving; pin it with a unit test. **~20 min.**
1. Card schema + loader + **structural validator** + migrate 6 entries + resolution-parity test — **~1 day**.
2. `compute_address_straps` + generic `device_config` template, retire the two `*_config` — **~half a day**.
3. Integration pin-existence validator — **~half a day**.
4. (6b) `board.yaml` sidecar — **~half a day**, separable.
5. (Phase 7) card auto-draft: address table + role→pin scoring + draft/confirm tiers — **~1–2 days**.
6. (Phase 8) bundled, repo-curated card library + reader-plugin interface + MCU pin-scheme cards — **local-first, no runtime network**. The library is ordinary OSS curation + maintainer-side CI (validate cards before they ship), the KiCad-library model — *not* a hosted service. Reader plugins and MCU-scheme cards are additive code/data. Scope separately from the refactor.

Steps 1–3 are a behavior-preserving refactor gated by the golden harness — low risk, high recurring payoff. 6b/7 add new behavior, shipped separately. Phase 8 generalizes across users **without any phone-home** — the tool stays fully functional air-gapped; sharing is bundled-library updates + opt-in file-copy, never a runtime connection.

## 15. Open questions

- Card format: YAML (consistent with intent docs) vs TOML. **Recommend YAML** — the project already uses it for intent.
- Do we keep `PeripheralInfo`/`McuInfo` TypedDicts as the in-memory type (recommended, zero blast radius) or introduce a dataclass? Recommend TypedDict for this phase.
- Override-dir precedence for a *project* that wants to pin a different footprint — confirm the `KICAD_MCP_DEVICE_DIRS` env name vs reusing an existing search-path mechanism.
