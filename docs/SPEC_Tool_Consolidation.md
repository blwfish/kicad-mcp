# SPEC — Tool Consolidation (Domain-Router Pattern)

**Status:** Approved for phased implementation
**Author:** Brian + Claude
**Date:** 2026-05-27
**Target:** kicad-mcp at 97 tools → 14 tools

## Motivation

Tool count matters for two independent reasons:

1. **Hard caps in MCP clients.** Cursor caps at 40, Gemini at ~100. Anything above
   Cursor's cap is silently truncated and the agent fails confusingly. Gemini we
   already brush against.
2. **System-prompt overhead, every client.** Each tool's name + description +
   schema costs ~200–500 tokens of system prompt on every turn. At 97 tools that
   is a meaningful slice of the model's per-turn context — including on Claude
   Code, which has no hard cap.

The companion project freecad-mcp uses a domain-router pattern (one tool per
FreeCAD domain, each carrying an `operation=` discriminator) and has held up
well in practice. This spec proposes the same shape for kicad-mcp.

## Non-goals

- **Not** fitting under Cursor's 40-tool cap. That would force aggressive
  per-tool conditional schemas with little headroom. Comfortably under 100 with
  margin is the goal; Cursor users can run the unconsolidated branch or split
  into multiple MCP server registrations.
- **Not** changing the underlying KiCad operations. This is a tool-surface
  refactor. Behavior, params, and return shapes of each operation stay
  equivalent.
- **Not** maintaining backwards-compatible aliases for old tool names. The
  project is on `0.10.0.dev0`; v0.9.0 was the first release. Hard break, one
  migration note in CHANGELOG.

## Pattern

### Domain routers

A domain router is one MCP tool that takes:

- `operation`: string — names a sub-operation within the domain
- additional named params — used by one or more operations; per-operation arg
  validation happens inside the router

Example shape (Python, FastMCP-style):

```python
@mcp.tool()
def schematic(
    operation: str,
    *,
    # Args used by multiple ops (union of needs)
    schematic_path: str | None = None,
    name: str | None = None,
    project_path: str | None = None,
    # ... etc
) -> dict:
    """Schematic-domain operations.

    Operations:
      create(name, project_path?) -> {schematic_path}
      load(schematic_path) -> {component_count, net_count, ...}
      save(schematic_path?) -> {status}
      validate(schematic_path?) -> {issues, summary}
      info(schematic_path?) -> {...}
      clone(schematic_path, new_path) -> {status}
      backup(schematic_path) -> {backup_path}
    """
    if operation == "create":
        return _create(name, project_path)
    elif operation == "load":
        return _load(schematic_path)
    # ...
    else:
        return {"error": f"unknown operation {operation!r}; "
                f"valid: create|load|save|validate|info|clone|backup"}
```

### Schema decisions

Two ways to encode operation-specific args:

| Approach | Pros | Cons |
|---|---|---|
| **A. Flat union of params** (proposed) | Matches freecad-mcp; simple schema; model sees one tool description; easy to add ops | Caller can pass wrong combinations; per-op validation lives in Python |
| **B. `oneOf` discriminated schema** | Strict typing per operation; auto-rejects bad combos at JSON Schema level | Heavier schema; some clients have spotty `oneOf` support |

**Proposal: A.** Match the freecad-mcp shape that's already proven in practice.
Per-operation arg validation in Python with clear error messages on missing
required args. The cost (loose schema) is paid in better operation
discoverability via the docstring.

### Error contract

Every router returns either `{"status": "ok", ...}` or `{"error": "..."}`.
Unknown `operation` returns an error that enumerates valid ops verbatim. Missing
required arg for a given op returns an error naming the op and the missing arg.
Same shape as today's individual tools.

## Domain breakdown

9 routers + 5 standalone = **14 tools** total. Folds aggressively along
freecad-mcp's domain-router shape: one router per coherent KiCad domain.

### 1. `schematic` — all schematic editing

Currently: 37 tools.

Folds: schematic file lifecycle + components + wires + labels + text + sheets
+ junctions + pin-collision check.

Ops: `create`, `load`, `save`, `validate`, `info`, `clone`, `backup`,
`check_pin_collisions`,
`add_component`, `remove_component`, `move_component`, `list_components`,
`filter_components`, `components_in_area`, `bulk_update_components`,
`add_multi_unit_component`, `get_component_pin_position`, `list_component_pins`,
`find_component_connections`,
`add_wire`, `remove_wire`, `add_wire_between_pins`, `add_junction`,
`add_label`, `remove_label`, `edit_label`, `add_label_to_pin`,
`add_hierarchical_label`, `connect_pins_with_labels`,
`add_text`, `add_text_box`, `edit_text`,
`add_sheet`, `add_sheet_pin`

Highest op count by far. Spot-check the docstring size when implementing — if
it exceeds FastMCP's tool-description limit, split into `schematic` and
`schematic_edit` (or similar) along the file-lifecycle / item-CRUD seam.

### 2. `pcb` — all PCB editing

Currently: 27 tools.

Folds: PCB file lifecycle + board outline/rules + footprints + nets + manual
routing + zones + silkscreen + pcb-text.

Ops: `create`, `load`, `finalize`,
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

Second-highest op count. Same spot-check applies. If split is needed, natural
seam is `pcb_edit` (footprints/nets/routing/zones) vs `pcb_text` (silkscreen +
text + drawings).

### 3. `audit` — placement + clearance verification + fixes

Currently: 9 tools (audit_all, audit_pcb_placement, audit_footprint_overlaps,
check_pad_clearances, validate_placement, auto_fix_placement, get_keepout_zones,
pre_route_check, plus overlap with silkscreen).

Ops: `all`, `placement`, `footprint_overlaps`, `pad_clearances`, `validate_one`,
`auto_fix_placement`, `keepouts`, `pre_route_check`

**Detail flag:** the earlier finding that `audit_all` returns less detail than
the standalone variants is resolved by a `detail="summary"|"full"` flag.
Default `summary` matches current `audit_all` output; `full` matches the
standalone tools (bboxes, gap_mm, severity messages, silk_text, pad_number).

### 4. `drc` — KiCad DRC engine

Currently: 3 tools.

Ops: `run`, `autofix`, `history`

### 5. `autoroute` — FreeRouter integration

Currently: 5 tools.

Ops: `run` (sync), `start` (async), `poll`, `cancel`, `list_jobs`

Both sync and async live here. Different return shapes per op — sync `run`
returns the result; `start` returns a `job_id`; `poll` returns status or
result-when-done. Distinct from the freecad async issue (UI-thread blocking
forced all-async there); here sync is fine for short routes and async is
useful for routes that exceed model-turn time budgets.

### 6. `library` — symbol/footprint library search

Currently: 2 tools (search, rebuild_library_index).

Ops: `search` (with `type="symbol"|"footprint"`), `rebuild_index`

### 7. `project` — KiCad project files

Currently: 4 tools.

Ops: `list`, `open`, `get_structure`, `validate`

### 8. `analyze` — read-only analysis

Currently: 5 tools (analyze_schematic_connections, identify_circuit_patterns,
analyze_project_circuit_patterns, analyze_bom, extract_netlist).

Ops: `connections`, `circuit_patterns`, `project_patterns`, `bom`, `netlist`

### 9. `export` — manufacturing/output files

Currently: 3 tools (export_gerbers, export_bom_csv, generate_pcb_thumbnail).

Ops: `gerbers`, `bom_csv`, `thumbnail`

### Standalone (no router) — heavy hitters with distinctive semantics

| Tool | Why standalone |
|---|---|
| `build_pcb_from_schematic` | Top-level orchestration; not a verb within a domain |
| `update_pcb_from_schematic` | Cross-domain sync; semantically loud enough to deserve its own name |
| `panelize_pcb` | One-shot manufacturing op; no related siblings |
| `estimate_board_size` | Planning aid called before any pcb router op |
| `suggest_placement` | Planning aid; not a CRUD op on footprints |

**Standalone count: 5.**

**Total: 9 routers + 5 standalone = 14 tools.** Net reduction: 97 → 14, an 85% cut.

## Migration strategy

This is a hard break. v0.10.0.dev0 → v0.11.0 (or 1.0). Steps:

1. Add new routers alongside old tools in one branch (both registered).
2. Update workflow docs (CLAUDE.md, AGENT-INSTALL.md, README.md) to use
   router-style calls everywhere.
3. Bump to a single PR that deletes the old tool wrappers and ships the
   router-only surface.
4. CHANGELOG entry calls out the rename mapping table verbatim.

Do **not** ship deprecation aliases. They double the tool count temporarily
which defeats the purpose, and external users on v0.9.0 will read the
CHANGELOG when they update.

## Test impact

The existing 577 tests are organized by current module. After consolidation:

- Each router gets a new test file: `tests/test_router_schematic.py`,
  `tests/test_router_pcb.py`, etc.
- Per-operation tests inside each router test file (one test class per op).
- `tests/test_server.py` tool-count snapshot bumps from 97 to 18.
- Existing tests are rewritten to call routers (mechanical update of
  `tool_name` lookups + adding `operation=...` arg).

Estimated rewrite: ~1 day, mostly mechanical.

## Implementation phasing

Each phase is an independently reviewable PR. Read-only and self-contained
domains land first to validate the router shape before the larger editing
routers go in.

| Phase | Routers | Tools before → after | Risk |
|---|---|---|---|
| 1 | `library`, `analyze`, `export` | 10 → 3 | Low — read-only |
| 2 | `project`, `drc`, `autoroute` | 12 → 3 | Low — self-contained |
| 3 | `audit` (with `detail` flag) | 9 → 1 | Med — preserves both detail levels |
| 4 | `pcb` | 27 → 1 | High — large op count; spot-check description size |
| 5 | `schematic` | 37 → 1 | High — largest op count |
| 6 | Delete standalones that didn't make it; final cleanup | — | Trivial |

After all phases: 97 → 14 tools (9 routers + 5 standalone).

If phase 4 or 5 hits the FastMCP tool-description-size limit, split that
router along the seam noted in its section above. Worst case: 14 → 16 tools.

## Resolved questions

All six original open questions are now decided:

1. **Naming convention** → snake_case ops (matches freecad-mcp).
2. **Audit/silkscreen merge** → not merged. Silkscreen ops fold into the `pcb`
   router because they're PCB-editing operations, not verification. Only the
   verification op (`check_silkscreen_overlaps`) stays available there too —
   `audit.silkscreen` and `pcb.check_silkscreen_overlaps` both work, sharing
   one impl.
3. **`extract_netlist` standalone vs in `analyze`** → in `analyze`. Adding
   exceptions undermines the pattern.
4. **Sync + async autoroute in one router** → yes. The freecad UI-thread
   reason for going async-only doesn't apply (no UI to block); both have value
   here for different route lengths.
5. **`build_pcb_from_schematic` / `update_pcb_from_schematic`** → standalone.
   Only two pipeline ops, not worth a router; they're semantically distinct
   enough that a router would obscure rather than organize.
6. **MCP `tools/list_changed` for further reduction** → skip. At 14 tools
   we're well below any client cap. Dynamic loading adds client-compat risk
   for marginal further reduction.

## Acceptance criteria

- Tool count: ≤ 16 registered on the FastMCP instance (14 target, 16 ceiling
  if `pcb` or `schematic` has to split).
- All 577 existing tests pass under the new surface (rewritten as needed).
- CLAUDE.md workflow examples use router-style calls.
- CHANGELOG documents every renamed/removed tool with the new operation name.
- No backwards-compat aliases registered.

## Risks

| Risk | Mitigation |
|---|---|
| Models forget operation names because they live in prose | Mirror freecad-mcp's docstring conventions; verify with eval prompts before merging each phase |
| Per-operation arg validation diverges across routers | Shared helper `_require_args(op_name, args_dict)` used by every router |
| External users break | Hard break, clearly documented; v0.9.0 is one minor version old |
| Audit-detail regression (already identified) | `detail=full|summary` flag in `audit` router |
| FastMCP client tool description size limit | Each router's docstring is ~30–60 lines; well within limits. Spot-check with longest (`schematic` or `component`) before merging |
