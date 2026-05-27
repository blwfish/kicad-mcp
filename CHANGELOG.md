# Changelog

All notable changes to kicad-mcp are documented here.

## [Unreleased] — 0.11.0.dev0

### Breaking — Tool Surface Consolidation (97 → 13 tools)

This release completes the domain-router consolidation described in
`docs/SPEC_Tool_Consolidation.md`. The 97 individual tools that existed in
v0.9.0 are replaced by 9 domain routers + 4 standalone tools = **13 tools
total**. There are no backwards-compatibility aliases — every call site must
be updated to the router form.

**Why:** MCP clients impose hard tool-count limits (Cursor: 40, Gemini: ~100).
System-prompt overhead also scales with tool count (~200–500 tokens per tool).
At 13 tools the server is well within every known client limit and the
per-turn token overhead is negligible.

#### Rename mapping — all 84 removed tools

Each old tool name maps to `router(operation="op_name", ...)`.

**Phase 1 — `library`, `analyze`, `export` routers**

| Old tool name | New call |
|---|---|
| `search` | `library(operation="search", ...)` |
| `rebuild_library_index` | `library(operation="rebuild_index")` |
| `analyze_schematic_connections` | `analyze(operation="connections", ...)` |
| `identify_circuit_patterns` | `analyze(operation="circuit_patterns", ...)` |
| `analyze_project_circuit_patterns` | `analyze(operation="project_patterns", ...)` |
| `analyze_bom` | `analyze(operation="bom", ...)` |
| `extract_netlist` | `analyze(operation="netlist", ...)` |
| `export_gerbers` | `export(operation="gerbers", ...)` |
| `export_bom_csv` | `export(operation="bom_csv", ...)` |
| `generate_pcb_thumbnail` | `export(operation="thumbnail", ...)` |

**Phase 2 — `project`, `drc`, `autoroute` routers**

| Old tool name | New call |
|---|---|
| `list_projects` | `project(operation="list")` |
| `open_project` | `project(operation="open", ...)` |
| `get_project_structure` | `project(operation="get_structure", ...)` |
| `validate_project` | `project(operation="validate", ...)` |
| `run_drc_check` | `drc(operation="run", ...)` |
| `drc_autofix` | `drc(operation="autofix", ...)` |
| `get_drc_history_tool` | `drc(operation="history", ...)` |
| `autoroute_pcb` | `autoroute(operation="run", ...)` |
| `autoroute_pcb_async` | `autoroute(operation="start", ...)` |
| `poll_autoroute` | `autoroute(operation="poll", ...)` |
| `cancel_autoroute` | `autoroute(operation="cancel", ...)` |
| `list_autoroute_jobs` | `autoroute(operation="list_jobs")` |

**Phase 3 — `audit` router**

| Old tool name | New call |
|---|---|
| `audit_all` | `audit(operation="all", ...)` |
| `audit_pcb_placement` | `audit(operation="placement", ...)` |
| `audit_footprint_overlaps` | `audit(operation="footprint_overlaps", ...)` |
| `check_pad_clearances` | `audit(operation="pad_clearances", ...)` |
| `validate_placement` | `audit(operation="validate_one", ...)` |
| `auto_fix_placement` | `audit(operation="auto_fix_placement", ...)` |
| `get_keepout_zones` | `audit(operation="keepouts", ...)` |
| `pre_route_check` | `audit(operation="pre_route_check", ...)` |

Note: `audit(operation="all", detail="full")` replaces the higher-detail output
that was previously only available from the individual standalone audit tools.

**Phase 4 — `pcb` router**

| Old tool name | New call |
|---|---|
| `create_pcb` | `pcb(operation="create", ...)` |
| `load_pcb` | `pcb(operation="load", ...)` |
| `finalize_pcb` | `pcb(operation="finalize", ...)` |
| `add_board_outline` | `pcb(operation="set_outline", ...)` |
| `set_design_rules` | `pcb(operation="set_design_rules", ...)` |
| `get_board_constraints` | `pcb(operation="get_constraints", ...)` |
| `place_footprint` | `pcb(operation="place_footprint", ...)` |
| `move_footprint` | `pcb(operation="move_footprint", ...)` |
| `list_pcb_footprints` | `pcb(operation="list_footprints", ...)` |
| `get_pad_positions` | `pcb(operation="get_pad_positions", ...)` |
| `get_footprint_dimensions` | `pcb(operation="get_footprint_dimensions", ...)` |
| `add_net` | `pcb(operation="add_net", ...)` |
| `rename_net` | `pcb(operation="rename_net", ...)` |
| `list_pcb_nets` | `pcb(operation="list_nets", ...)` |
| `set_net_class` | `pcb(operation="set_net_class", ...)` |
| `assign_pad_net` | `pcb(operation="assign_pad_net", ...)` |
| `bulk_assign_pad_nets` | `pcb(operation="bulk_assign_pad_nets", ...)` |
| `add_trace` | `pcb(operation="add_trace", ...)` |
| `add_via` | `pcb(operation="add_via", ...)` |
| `clear_routing` | `pcb(operation="clear_routing", ...)` |
| `edit_trace_width` | `pcb(operation="edit_trace_width", ...)` |
| `add_copper_zone` | `pcb(operation="add_zone", ...)` |
| `fill_zones` | `pcb(operation="fill_zones", ...)` |
| `add_text_to_pcb` | `pcb(operation="add_text", ...)` |
| `list_silkscreen_items` | `pcb(operation="list_silkscreen", ...)` |
| `update_silkscreen_item` | `pcb(operation="update_silkscreen", ...)` |
| `auto_fix_silkscreen` | `pcb(operation="auto_fix_silkscreen", ...)` |
| `check_silkscreen_overlaps` | `pcb(operation="check_silkscreen_overlaps", ...)` |

**Phase 5 — `schematic` router**

| Old tool name | New call |
|---|---|
| `create_schematic` | `schematic(operation="create", ...)` |
| `load_schematic` | `schematic(operation="load", ...)` |
| `save_schematic` | `schematic(operation="save", ...)` |
| `validate_schematic` | `schematic(operation="validate", ...)` |
| `get_schematic_info` | `schematic(operation="info", ...)` |
| `clone_schematic` | `schematic(operation="clone", ...)` |
| `backup_schematic` | `schematic(operation="backup", ...)` |
| `check_pin_collisions` | `schematic(operation="check_pin_collisions", ...)` |
| `add_component` | `schematic(operation="add_component", ...)` |
| `remove_component` | `schematic(operation="remove_component", ...)` |
| `move_component` | `schematic(operation="move_component", ...)` |
| `list_components` | `schematic(operation="list_components", ...)` |
| `filter_components` | `schematic(operation="filter_components", ...)` |
| `components_in_area` | `schematic(operation="components_in_area", ...)` |
| `bulk_update_components` | `schematic(operation="bulk_update_components", ...)` |
| `add_multi_unit_component` | `schematic(operation="add_multi_unit_component", ...)` |
| `get_component_pin_position` | `schematic(operation="get_component_pin_position", ...)` |
| `list_component_pins` | `schematic(operation="list_component_pins", ...)` |
| `find_component_connections` | `schematic(operation="find_component_connections", ...)` |
| `add_wire` | `schematic(operation="add_wire", ...)` |
| `remove_wire` | `schematic(operation="remove_wire", ...)` |
| `add_wire_between_pins` | `schematic(operation="add_wire_between_pins", ...)` |
| `add_junction` | `schematic(operation="add_junction", ...)` |
| `add_label` | `schematic(operation="add_label", ...)` |
| `remove_label` | `schematic(operation="remove_label", ...)` |
| `edit_label` | `schematic(operation="edit_label", ...)` |
| `add_label_to_pin` | `schematic(operation="add_label_to_pin", ...)` |
| `add_hierarchical_label` | `schematic(operation="add_hierarchical_label", ...)` |
| `connect_pins_with_labels` | `schematic(operation="connect_pins_with_labels", ...)` |
| `add_text` | `schematic(operation="add_text", ...)` |
| `add_text_box` | `schematic(operation="add_text_box", ...)` |
| `edit_text` | `schematic(operation="edit_text", ...)` |
| `add_sheet` | `schematic(operation="add_sheet", ...)` |
| `add_sheet_pin` | `schematic(operation="add_sheet_pin", ...)` |
| `add_net` (schematic context) | `schematic(operation="add_net", ...)` |

**Phase 6 — Cleanup**

- Deleted no-op stubs `register_pcb_drc_fix_tools` and `register_netlist_tools`.
- Fixed internal docstring references to the non-existent `update_pcb_from_schematic`
  (this tool was planned but never shipped; `build_pcb_from_schematic` covers the
  pipeline use case).
- Updated all workflow docs to router-style calls.

#### Standalone tools (unchanged names, still available)

| Tool | Notes |
|---|---|
| `build_pcb_from_schematic` | Top-level schematic → PCB pipeline |
| `panelize_pcb` | Manufacturing panelization |
| `estimate_board_size` | Pre-PCB planning aid |
| `suggest_placement` | Connectivity-based placement suggestions |

---

## [0.9.0] — 2026-05-26

First tagged release. Baseline for the consolidation that follows in 0.11.0.
97 tools covering the full KiCad design workflow.
