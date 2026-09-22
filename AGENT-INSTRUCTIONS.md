# kicad-mcp — Operating Instructions for AI Agents

How to use this MCP server to design circuits and lay out PCBs. These
instructions are agent-agnostic — they apply whether you are Claude, Gemini,
Cursor, or any other MCP-capable agent. (Contributor/development conventions for
hacking on this repo live in `AGENTS.md` and `CONTRIBUTING.md`, not here.)

You have access to <!-- tool-count -->18<!-- /tool-count --> MCP tools. Most are **routers** that dispatch on an `operation=` argument (e.g. `pcb(operation="place_footprint", ...)`, `drc(operation="autofix", ...)`, `library(operation="search", ...)`); the rest are standalone tools (`build_pcb_from_schematic`, `estimate_board_size`, `suggest_placement`, `panelize_pcb`, `analyze_placement_telemetry`, `get_usage_guidance`). See [TOOLS.md](TOOLS.md) for the full router/operation reference. Call `get_usage_guidance()` once at the start of a session — it costs nothing and surfaces known issues and best practices this document doesn't repeat inline.

## Mandatory Rules

The three rules below are authored once as `CRITICAL` entries in
`src/kicad_mcp/tools/usage_guidance.py`'s `NOTES` — the same data that
renders into the server's `instructions` handshake and the
`get_usage_guidance()` tool. This section is a generated view of that data,
not independently authored; run `python scripts/sync_agent_notes_docs.py`
after changing a `CRITICAL` note and commit the result.

<!-- agent-notes:critical -->
### Never guess library names

Never guess library or footprint names from training data — they change between KiCad versions. Search first with library(operation='search').

### Never hand route

LLMs cannot compute spatial clearances reliably by hand — manually placed traces routinely short nets or violate clearances. autoroute(operation='run') wraps FreeRouter and solves routing in seconds with zero violations. add_trace/add_via exist only for minor touch-ups after autorouting.

### No concurrent PCB writes

A per-resolved-path in-process lock (utils/pcb_lock.py) now guards every mutating pcb/autoroute/drc(autofix)/audit(auto_fix_placement)/panelize_pcb/build_pcb_from_schematic call: a second mutating call on a path already locked returns {"status": "error", "error": "...already in progress..."} immediately (never blocks and waits — a queued autoroute pass can legitimately hold the lock for up to 30 minutes). Read-only calls (get_pad_positions, list_nets, list_footprints, drc(run), most audit(...) operations) never acquire it and stay safe to run in parallel or delegate to subagents, same as before.

This is genuinely an in-process fix, not a general one. It does NOT cover: (1) two separate kicad-mcp server processes (e.g. two Claude Code sessions, each with their own stdio-connected server) pointed at the same file — a lock held in one process's memory is invisible to another process, and real cross-process file locking would need OS-level primitives (flock/fcntl/LockFileEx — three different models) that don't even agree with each other over NFS/SMB, and don't apply at all to a board in a Dropbox/OneDrive/iCloud-synced folder; (2) schematic(...) operations, which mutate an in-memory module-level object, not a file, until save() -- a different concurrency model, not locked by this mechanism at all. Across either of those boundaries, the original rule still applies: serialize yourself.
<!-- /agent-notes:critical -->

Example (library search, referenced above):

```
library(operation="search", query="op amp", type="symbol")   → lib_id for schematic add_component
library(operation="search", query="0603 resistor")            → library + name for pcb place_footprint (type="footprint" is default)
```

### Delegate read-only work to keep your context lean

KiCad sessions accumulate large tool results that fill an agent's context window. **If your agent supports sub-tasks or sub-agents, delegate read-only work to them** and keep only the conclusions in your main context. If it doesn't, just be mindful of how much state you pull in.

**Safe to delegate / parallelize (read-only):**
- `library(operation="search")` — symbol/footprint lookup
- `schematic(operation="info"|"list_components")`, `analyze(operation="connections"|"netlist")` — read-only analysis
- `pcb(operation="get_pad_positions"|"list_nets"|"list_footprints")` — PCB state queries
- `drc(operation="run")`, `audit(operation="all"|"placement"|"pad_clearances"|"check_silkscreen_overlaps")` — verification passes
- `estimate_board_size`, `suggest_placement` — planning queries

**Serialize in your main flow (state-mutating — never run two at once):**
- Mutating router operations: `pcb(operation="place_footprint"|"move_footprint"|"add_net"|"bulk_assign_pad_nets"|"add_zone"|"fill_zones"|...)`, `schematic(operation="create"|"add_component"|"save"|...)`, `autoroute(operation="run")`, `drc(operation="autofix")`, `audit(operation="auto_fix_placement")`
- Standalone mutators: `build_pcb_from_schematic`, `panelize_pcb`

## Workflow

> **Firmware-driven path:** the `design` tool can import a firmware project and
> generate the schematic for you — but only within a narrow, explicit envelope
> (2 ESP32 MCUs, PlatformIO layout, C `#define` pins, a fixed set of chips).
> Outside it, `design` emits loud gaps, not a wrong board. Read
> [docs/FIRMWARE_FRONTEND_SCOPE.md](docs/FIRMWARE_FRONTEND_SCOPE.md) before using it.

Follow this order for a complete board design:

### 1. Schematic

```
schematic(operation="create", name="project")
library(operation="search", query="...", type="symbol")           # Find symbol lib_id
schematic(operation="add_component", lib_id=..., reference=..., value=..., position=[x, y])
schematic(operation="connect_pins_with_labels", comp1_ref=..., pin1=..., comp2_ref=..., pin2=..., net_name=...)
schematic(operation="add_label_to_pin", reference=..., pin_number=..., text="GND")  # Power/ground
schematic(operation="save")
schematic(operation="validate")
```

### 2. Board Size Planning

```
estimate_board_size(footprints=[                    # Get dimensions BEFORE creating PCB
    {"library": "...", "footprint_name": "..."},
    ...
])
```

### 3. PCB Setup

```
pcb(operation="create", pcb_path="project.kicad_pcb")
pcb(operation="set_outline", pcb_path=..., x_mm=100, y_mm=100, width_mm=50, height_mm=30)
pcb(operation="set_design_rules", pcb_path=..., min_track_width_mm=0.25, min_clearance_mm=0.2)
```

### 4. Footprint Placement

```
library(operation="search", query="...")     # Find footprint library + name (type="footprint" default)
pcb(operation="place_footprint", pcb_path=..., library=..., footprint_name=..., reference=..., value=..., x_mm=..., y_mm=...)
```

Or after placing footprints anywhere and assigning nets:
```
suggest_placement(pcb_path=...)             # Get optimized positions based on connectivity
# Then apply with pcb(operation="move_footprint", ...) for each suggestion
```

After placing all footprints:
```
audit(operation="all", pcb_path=...)        # Overlaps + keepouts + silkscreen in one call
```

### 5. Net Assignment

For a schematic-driven board, the whole PCB (placement + nets + routing) is built in one step by the pipeline — see `build_pcb_from_schematic(project_path="project.kicad_pro")`.

To assign nets manually on an existing PCB:
```
pcb(operation="add_net", pcb_path=..., net_name="VCC")
pcb(operation="bulk_assign_pad_nets", pcb_path=..., assignments=[
    {"reference": "U1", "pad": "1", "net": "VCC"},
    ...
])
```

Verify: `pcb(operation="get_pad_positions", pcb_path=..., reference="U1")` — every pad should show its net name.

### 6. Autoroute

```
autoroute(operation="run", pcb_path=..., passes=2)
```

FreeRouter is non-deterministic. Use `passes=2` or `passes=3` for complex boards — the tool keeps the best result. Requires Java 17+. (Long routes can run async via `autoroute(operation="start"|"poll")`.)

### 7. Panelization (optional)

```
panelize_pcb(pcb_path=..., rows=2, cols=5, cut_type="vcuts", framing="railstb")
```

Creates a manufacturing panel with V-scores or mousebites. Supports framing rails, tooling holes, and fiducials. Output defaults to `{name}-panel.kicad_pcb`.

### 8. Copper Zones and Finish

```
pcb(operation="add_zone", pcb_path=..., net_name="GND", layer="B.Cu",
    corners=[[x1,y1], [x2,y1], [x2,y2], [x1,y2]])
pcb(operation="fill_zones", pcb_path=...)
drc(operation="run", project_path="project.kicad_pro")
pcb(operation="check_silkscreen_overlaps", pcb_path=...)
```

Zone corners should match or exceed the board outline. Common pattern: GND pour on B.Cu covering the full board.

### 9. DRC Auto-Fix (optional)

If DRC reveals violations, auto-fix them in one shot:

```
drc(operation="autofix", pcb_path=..., project_path="project.kicad_pro", autoroute_passes=2)
```

Fixes courtyard overlaps (nudges footprints), routing violations (clears + re-autoroutes), and silkscreen overlaps in order. Returns before/after DRC comparison.

For targeted fixes:
```
audit(operation="auto_fix_placement", pcb_path=..., spacing_mm=0.5)   # Courtyard overlaps only
pcb(operation="auto_fix_silkscreen", pcb_path=...)                     # Silkscreen overlaps only
```

## Tool Selection

| I need to... | Use this | Not this |
|---|---|---|
| Get best-practices/known-issues guidance before starting a session | `get_usage_guidance()` — costs nothing, call it first | Skipping straight to a design tool |
| Search guidance by symptom instead of by topic name | `get_usage_guidance(operation="find", problem="...")` | Guessing which topic covers it |
| Choose board size | `estimate_board_size` | Guessing dimensions |
| Initial placement | `suggest_placement` | Manual coordinate math |
| Route traces | `autoroute(operation="run")` | `pcb(operation="add_trace"/"add_via")` |
| Find a symbol name | `library(operation="search", type="symbol")` | Guessing from training data |
| Find a footprint name | `library(operation="search")` (footprint default) | Guessing from training data |
| Build a PCB from a schematic | `build_pcb_from_schematic` | Manual `add_net` + `bulk_assign_pad_nets` |
| Check all placement issues | `audit(operation="all")` | Three separate audit calls |
| Fix silkscreen overlaps | `pcb(operation="auto_fix_silkscreen")` | Manual `pcb(operation="update_silkscreen")` |
| Fix courtyard overlaps | `audit(operation="auto_fix_placement")` | Manual `pcb(operation="move_footprint")` guesswork |
| Fix all DRC violations | `drc(operation="autofix")` | Manual fix-by-fix iteration |
| Run DRC | `drc(operation="run")` | Skipping verification |

## Placement Guidelines

- Group related components: IC + decoupling cap + pull-ups should be adjacent
- Leave 2-3mm between component groups for trace routing
- Horizontal screw terminals (Phoenix MKDS) are designed to overhang the board edge — this is normal and expected
- SOIC pin numbering: pins 1-4 left side top-to-bottom, pins 5-8 right side bottom-to-top
- Use `audit(operation="validate_one")` to check a specific position before placing, `audit(operation="placement")` to check all placements after

## DRC Interpretation

**Acceptable on prototype boards:**
- `courtyards_overlap` — OK if hand-solderable
- `starved_thermal` — fewer thermal relief spokes, still connected
- `silk_overlap`, `silk_over_copper` — cosmetic only

**Must fix:**
- `tracks_crossing` — same-layer traces from different nets crossing
- `shorting_items` — different nets shorted
- `clearance` — copper-to-copper too close
- `unconnected_items` — missing connections

## Technical Notes

- PCB tools run via KiCad's bundled Python as subprocesses (`utils/pcbnew_bridge.py`). Each call loads the board, modifies it, saves, and returns JSON.
- Schematic tools use `kicad-sch-api` in-process. One schematic loaded at a time — call `schematic(operation="load")` or `schematic(operation="create")` before other schematic operations.
- Library search indexes are at `~/.cache/kicad-mcp/library_index.db`. They auto-rebuild when KiCad libraries change; force one with `library(operation="rebuild_index")`.
- `autoroute(operation="run")` auto-detects the FreeRouter JAR in common locations. Set `FREEROUTER_JAR` env var to override.
- All tools return `{"status": "ok", ...}` on success or `{"error": "..."}` on failure.
