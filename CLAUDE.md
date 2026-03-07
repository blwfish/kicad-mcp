# KiCad MCP — Instructions for Claude

You have access to 96 MCP tools for KiCad electronic design automation. Follow these instructions when using them.

## Mandatory Rules

### NEVER manually route traces

Do not use `add_trace` or `add_via` to route a PCB. LLMs cannot compute spatial clearances reliably — manual routing produces track crossings, shorts, and clearance violations. Always use `autoroute_pcb` instead. It wraps FreeRouter and solves routing in seconds with zero violations.

The only acceptable use of `add_trace`/`add_via` is minor touch-ups after autorouting, if specifically requested.

### NEVER guess library or footprint names

KiCad library names change between versions. Always search first:

```
search_components(query="op amp")         → lib_id for add_component
search_footprints(query="0603 resistor")  → library + name for place_footprint
```

### NEVER modify the same PCB file in parallel

PCB tools run as subprocesses that load, modify, and save the file. Two concurrent writes will corrupt it. Always serialize PCB operations.

## Workflow

Follow this order for a complete board design:

### 1. Schematic

```
create_schematic(name="project")
search_components(query="...")              # Find symbol lib_id
add_component(lib_id=..., reference=..., value=..., position=[x, y])
connect_pins_with_labels(comp1_ref=..., pin1=..., comp2_ref=..., pin2=..., net_name=...)
add_label_to_pin(reference=..., pin_number=..., text="GND")  # Power/ground
save_schematic()
validate_schematic()
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
create_pcb(pcb_path="project.kicad_pcb")
add_board_outline(pcb_path=..., x_mm=100, y_mm=100, width_mm=50, height_mm=30)
set_design_rules(pcb_path=..., min_track_width_mm=0.25, min_clearance_mm=0.2)
```

### 4. Footprint Placement

```
search_footprints(query="...")              # Find footprint library + name
place_footprint(pcb_path=..., library=..., footprint_name=..., reference=..., value=..., x_mm=..., y_mm=...)
```

Or after placing footprints anywhere and assigning nets:
```
suggest_placement(pcb_path=...)             # Get optimized positions based on connectivity
# Then apply with move_footprint for each suggestion
```

After placing all footprints:
```
audit_all(pcb_path=...)                     # Overlaps + keepouts + silkscreen in one call
```

### 4. Net Assignment

If a schematic exists (preferred):
```
update_pcb_from_schematic(project_path="project.kicad_pro")
```

Otherwise, manually:
```
add_net(pcb_path=..., net_name="VCC")
bulk_assign_pad_nets(pcb_path=..., assignments=[
    {"reference": "U1", "pad": "1", "net": "VCC"},
    ...
])
```

Verify: `get_pad_positions(pcb_path=..., reference="U1")` — every pad should show its net name.

### 5. Autoroute

```
autoroute_pcb(pcb_path=..., passes=2)
```

FreeRouter is non-deterministic. Use `passes=2` or `passes=3` for complex boards — the tool keeps the best result. Requires Java 17+.

### 6. Panelization (optional)

```
panelize_pcb(pcb_path=..., rows=2, cols=5, cut_type="vcuts", framing="railstb")
```

Creates a manufacturing panel with V-scores or mousebites. Supports framing rails, tooling holes, and fiducials. Output defaults to `{name}-panel.kicad_pcb`.

### 7. Copper Zones and Finish

```
add_copper_zone(pcb_path=..., net_name="GND", layer="B.Cu",
    corners=[[x1,y1], [x2,y1], [x2,y2], [x1,y2]])
fill_zones(pcb_path=...)
run_drc_check(project_path="project.kicad_pro")
check_silkscreen_overlaps(pcb_path=...)
```

Zone corners should match or exceed the board outline. Common pattern: GND pour on B.Cu covering the full board.

### 8. DRC Auto-Fix (optional)

If DRC reveals violations, auto-fix them in one shot:

```
drc_autofix(pcb_path=..., project_path="project.kicad_pro", autoroute_passes=2)
```

Fixes courtyard overlaps (nudges footprints), routing violations (clears + re-autoroutes), and silkscreen overlaps in order. Returns before/after DRC comparison.

For targeted fixes:
```
auto_fix_placement(pcb_path=..., spacing_mm=0.5)   # Courtyard overlaps only
auto_fix_silkscreen(pcb_path=...)                    # Silkscreen overlaps only
```

## Tool Selection

| I need to... | Use this | Not this |
|---|---|---|
| Choose board size | `estimate_board_size` | Guessing dimensions |
| Initial placement | `suggest_placement` | Manual coordinate math |
| Route traces | `autoroute_pcb` | `add_trace` / `add_via` |
| Find a symbol name | `search_components` | Guessing from training data |
| Find a footprint name | `search_footprints` | Guessing from training data |
| Create nets from schematic | `update_pcb_from_schematic` | Manual `add_net` + `bulk_assign_pad_nets` |
| Check all placement issues | `audit_all` | Three separate audit calls |
| Fix silkscreen overlaps | `auto_fix_silkscreen` | Manual `update_silkscreen_item` |
| Fix courtyard overlaps | `auto_fix_placement` | Manual `move_footprint` guesswork |
| Fix all DRC violations | `drc_autofix` | Manual fix-by-fix iteration |
| Run DRC | `run_drc_check` | Skipping verification |

## Placement Guidelines

- Group related components: IC + decoupling cap + pull-ups should be adjacent
- Leave 2-3mm between component groups for trace routing
- Horizontal screw terminals (Phoenix MKDS) are designed to overhang the board edge — this is normal and expected
- SOIC pin numbering: pins 1-4 left side top-to-bottom, pins 5-8 right side bottom-to-top
- Use `validate_placement` to check a specific position before placing, `audit_pcb_placement` to check all placements after

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

- PCB tools run via KiCad's bundled Python 3.9 as subprocesses (`utils/pcbnew_bridge.py`). Each call loads the board, modifies it, saves, and returns JSON.
- Schematic tools use `kicad-sch-api` in-process. One schematic loaded at a time — call `load_schematic` or `create_schematic` before using schematic tools.
- Library search indexes are at `~/.cache/kicad-mcp/library_index.db`. They auto-rebuild when KiCad libraries change.
- `autoroute_pcb` auto-detects the FreeRouter JAR in common locations. Set `FREEROUTER_JAR` env var to override.
- All tools return `{"status": "ok", ...}` on success or `{"error": "..."}` on failure.
