# kicad-mcp

MCP server for KiCad — 72 tools for AI-assisted electronics design via the [Model Context Protocol](https://modelcontextprotocol.io/).

Design schematics, lay out PCBs, autoroute traces, run DRC, and analyze circuits — all from an AI assistant like Claude.

## Quick Start

### Prerequisites

- **KiCad 8+** installed (provides pcbnew Python bindings)
- **Python 3.10+**
- **Java 17+** (for FreeRouter autorouting — optional but recommended)

### Install

```bash
git clone https://github.com/blwfish/kicad-mcp.git
cd kicad-mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Register with Claude Code

```bash
claude mcp add kicad -- /path/to/kicad-mcp/.venv/bin/kicad-mcp
```

## Tools (71)

### Schematic Design (28 tools)

**Management** — `create_schematic`, `load_schematic`, `save_schematic`, `get_schematic_info`, `validate_schematic`, `backup_schematic`, `clone_schematic`

**Components** — `add_component`, `remove_component`, `list_components`, `search_components`, `filter_components`, `bulk_update_components`, `components_in_area`

**Connections** — `add_wire`, `remove_wire`, `add_label`, `add_label_to_pin`, `connect_pins_with_labels`, `get_component_pin_position`, `list_component_pins`, `remove_label`, `add_hierarchical_label`, `add_junction`

**Drawing** — `add_text`, `add_text_box`, `add_sheet`, `add_sheet_pin`

### PCB Layout (28 tools)

**Board** — `create_pcb`, `load_pcb`, `add_board_outline`, `set_design_rules`

**Footprints** — `place_footprint`, `move_footprint`, `list_pcb_footprints`, `get_pad_positions`, `search_footprints`

**Nets** — `add_net`, `assign_pad_net`, `bulk_assign_pad_nets`, `list_pcb_nets`, `update_pcb_from_schematic`

**Routing** — `add_trace`, `add_via`, `autoroute_pcb`

**Zones** — `add_copper_zone`, `fill_zones`, `get_keepout_zones`

**Silkscreen** — `add_text_to_pcb`, `list_silkscreen_items`, `update_silkscreen_item`, `check_silkscreen_overlaps`

**Validation** — `get_board_constraints`, `validate_placement`, `audit_pcb_placement`, `audit_footprint_overlaps`

### Project & Analysis (16 tools)

**Project** — `list_projects`, `get_project_structure`, `open_project`, `validate_project`

**DRC & Export** — `run_drc_check`, `get_drc_history_tool`, `generate_pcb_thumbnail`, `generate_project_thumbnail`

**BOM** — `analyze_bom`, `export_bom_csv`

**Netlist** — `extract_schematic_netlist`, `extract_project_netlist`, `analyze_schematic_connections`, `find_component_connections`

**Circuit Patterns** — `identify_circuit_patterns`, `analyze_project_circuit_patterns`

## Key Capabilities

### Autorouting

The `autoroute_pcb` tool wraps the [FreeRouter](https://github.com/freerouting/freerouting) autorouter in a single MCP call:

1. Removes copper pour zones (FreeRouter doesn't understand them)
2. Exports the board as a Specctra DSN file
3. Runs FreeRouter headless
4. Imports the routed SES session back into the PCB

```
autoroute_pcb(pcb_path="board.kicad_pcb", passes=2)
```

FreeRouter is non-deterministic — use `passes=2` or `passes=3` and the tool keeps the best result. After autorouting, re-add copper zones with `add_copper_zone` + `fill_zones`.

### Schematic-to-PCB Sync

`update_pcb_from_schematic` is the MCP equivalent of KiCad's F8 "Update PCB from Schematic":

```
update_pcb_from_schematic(project_path="project.kicad_pro")
```

Exports the netlist from the schematic, creates all nets in the PCB, and assigns them to the correct pads.

### Library Search

`search_components` and `search_footprints` maintain SQLite FTS5 indexes of all KiCad symbol and footprint libraries respectively. Both auto-rebuild when library files change (e.g. after a KiCad upgrade):

```
search_components(query="op amp")          # → Device:LM358, Amplifier_Operational:LM741, ...
search_footprints(query="SOT-23")          # → Package_TO_SOT_SMD:SOT-23, ...
search_footprints(query="0603 resistor")   # → Resistor_SMD:R_0603_1608Metric, ...
```

## Typical Workflow

```
1. Create schematic        → add_component, connect_pins_with_labels
2. Place footprints        → place_footprint, audit_footprint_overlaps
3. Sync nets               → update_pcb_from_schematic
4. Autoroute               → autoroute_pcb
5. Add copper zones        → add_copper_zone, fill_zones
6. Verify                  → run_drc_check, check_silkscreen_overlaps
```

## Architecture

The server uses [FastMCP](https://github.com/jlowin/fastmcp) and delegates PCB operations to KiCad's bundled Python 3.9 via subprocess (since pcbnew is a compiled C++ module tied to KiCad's own Python). Schematic operations use the [kicad-sch-api](https://pypi.org/project/kicad-sch-api/) library.

```
src/kicad_mcp/
  server.py                 # Entry point, tool registration
  tools/
    pcb_board.py             # Board creation, outline, design rules
    pcb_footprints.py        # Footprint placement and queries
    pcb_nets.py              # Net management and schematic sync
    pcb_routing.py           # Manual trace/via routing
    pcb_autoroute.py         # FreeRouter autorouting pipeline
    pcb_zones.py             # Copper zones and fills
    pcb_silkscreen.py        # Silkscreen text management
    pcb_keepout.py           # Keepout zones and placement validation
    schematic.py             # All schematic tools (wraps kicad-sch-api)
    project.py               # Project management
    export.py                # Thumbnail generation
    drc.py                   # Design rule checking
    bom.py                   # Bill of materials
    netlist.py               # Netlist extraction and analysis
    patterns.py              # Circuit pattern recognition
  utils/
    pcbnew_bridge.py         # Subprocess bridge to KiCad's Python/pcbnew
```

## Development

```bash
# Run tests (197 tests, no KiCad installation required)
pytest

# Run with coverage
pytest --cov=kicad_mcp

# Lint
ruff check src/ tests/
```

## License

MIT
