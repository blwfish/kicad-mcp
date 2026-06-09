# Tool Summary

This MCP server exposes <!-- tool-count -->17<!-- /tool-count --> MCP tools for KiCad EDA — 12 domain routers and 5 standalones.

Each router takes an `operation` parameter that selects the sub-operation.
Unknown operations return an error listing valid choices.

## Domain Routers

### `schematic` — schematic editing
Operations: `create`, `load`, `save`, `validate`, `info`, `clone`, `backup`,
`check_pin_collisions`,
`add_component`, `remove_component`, `move_component`, `list_components`,
`filter_components`, `components_in_area`, `bulk_update_components`,
`add_multi_unit_component`, `get_component_pin_position`, `list_component_pins`,
`find_component_connections`,
`add_wire`, `remove_wire`, `add_wire_between_pins`, `add_junction`,
`add_label`, `remove_label`, `edit_label`, `add_label_to_pin`,
`add_hierarchical_label`, `connect_pins_with_labels`,
`add_text`, `add_text_box`,
`add_sheet`, `add_sheet_pin`

### `pcb` — PCB editing
Operations: `create`, `load`, `finalize`,
`set_outline`, `set_design_rules`, `get_constraints`,
`place_footprint`, `move_footprint`, `list_footprints`, `get_pad_positions`,
`get_footprint_dimensions`,
`add_net`, `rename_net`, `list_nets`, `set_net_class`, `assign_pad_net`,
`bulk_assign_pad_nets`,
`add_trace`, `add_via`, `clear_routing`, `edit_trace_width`,
`add_zone`, `fill_zones`,
`add_text`,
`list_silkscreen`, `update_silkscreen`, `auto_fix_silkscreen`,
`check_silkscreen_overlaps`

### `audit` — placement and clearance verification
Operations: `all`, `placement`, `footprint_overlaps`, `pad_clearances`,
`validate_one`, `auto_fix_placement`, `keepouts`, `pre_route_check`,
`constraints`, `check_silkscreen_overlaps`

`all` accepts `detail="summary"` (default) or `detail="full"` for full bounding-box
and gap data matching the standalone audit tools.

### `drc` — KiCad DRC engine
Operations: `run`, `autofix`, `history`

### `autoroute` — FreeRouter integration
Operations: `run` (sync), `start` (async), `poll`, `cancel`, `list_jobs`

### `library` — symbol/footprint library search
Operations: `search` (accepts `type="symbol"|"footprint"`), `rebuild_index`

### `project` — KiCad project files
Operations: `list`, `open`, `get_structure`, `validate`

### `analyze` — read-only schematic/board analysis
Operations: `connections`, `circuit_patterns`, `project_patterns`, `bom`, `netlist`

### `export` — manufacturing output files
Operations: `gerbers`, `bom_csv`, `thumbnail`

### `lcsc` — LCSC/JLCPCB component intelligence
Operations: `search`, `resolve`, `assign`, `accept_tos`, `refresh_snapshot`

### `schematic_layout` — topology-aware schematic placement
Operations: `suggest`, `apply`, `clear_cache`.

### `design` — firmware-driven design + device cards
Operations: `import_firmware`, `expand_templates`, `generate_schematic`,
`show_intent`, `suggest_cards`.
Works within a **narrow, explicit envelope** (2 ESP32 MCUs; a PlatformIO
`config.h` or an Arduino `.ino` sketch; `#define` or `const`/`constexpr` pins;
board from `platformio.ini` or a `board.yaml` `board_id`; a fixed set of
recognized chips); outside it, it emits loud gaps rather than a wrong board. See
[docs/FIRMWARE_FRONTEND_SCOPE.md](docs/FIRMWARE_FRONTEND_SCOPE.md).

## Standalone Tools

| Tool | Purpose |
|---|---|
| `build_pcb_from_schematic` | Top-level pipeline: schematic → fully placed + netted PCB |
| `panelize_pcb` | Manufacturing panelization with V-scores or mouse-bites |
| `estimate_board_size` | Pre-PCB planning: estimate board dimensions from a footprint list |
| `suggest_placement` | Connectivity-based component placement suggestions |
| `analyze_placement_telemetry` | Read-only telemetry analysis for placement calibration |

## Migration from v0.9.0

v0.9.0 exposed 97 individual tools. All have been consolidated into the routers above.
See [CHANGELOG.md](CHANGELOG.md) for the full rename mapping table.
