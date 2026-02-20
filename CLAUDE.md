# kicad-mcp

Unified MCP server for KiCad — 69 tools for schematic, PCB layout, and analysis.

## Architecture

```
src/kicad_mcp/
├── server.py              # FastMCP entry point, registers all tool modules
├── config.py              # Platform-specific KiCad path detection
├── tools/
│   ├── pcb_board.py       # create_pcb, load_pcb, add_board_outline, set_design_rules
│   ├── pcb_footprints.py  # place_footprint, move_footprint, list_pcb_footprints, get_pad_positions
│   ├── pcb_nets.py        # add_net, assign_pad_net, bulk_assign_pad_nets, list_pcb_nets, update_pcb_from_schematic
│   ├── pcb_routing.py     # add_trace, add_via
│   ├── pcb_zones.py       # add_copper_zone, fill_zones
│   ├── pcb_silkscreen.py  # list/update/check silkscreen, add_text_to_pcb
│   ├── pcb_keepout.py     # get_keepout_zones, get_board_constraints, validate/audit placement
│   ├── project.py         # list_projects, get_project_structure, open_project, validate_project
│   ├── export.py          # generate_pcb_thumbnail, generate_project_thumbnail
│   ├── drc.py             # run_drc_check, get_drc_history
│   ├── bom.py             # analyze_bom, export_bom_csv
│   ├── netlist.py         # extract netlist, analyze connections, find component connections
│   ├── patterns.py        # identify_circuit_patterns, analyze_project_circuit_patterns
│   └── schematic.py       # All schematic tools (wraps kicad-sch-api PyPI package)
└── utils/
    ├── pcbnew_bridge.py   # Subprocess bridge to KiCad's Python 3.9
    ├── keepout_helpers.py  # Geometry helpers for keepout zone scripts
    ├── kicad_utils.py      # Project discovery
    ├── kicad_cli.py        # kicad-cli wrapper
    ├── file_utils.py       # File operations
    ├── drc_history.py      # DRC history tracking
    ├── netlist_parser.py   # Netlist parsing
    └── pattern_recognition.py
```

## Dependencies

- **fastmcp>=2.0.0** — MCP server framework
- **kicad-sch-api>=0.5.0** — Schematic parser (PyPI, maintained by circuit-synth)
- **pyyaml, defusedxml** — Config and XML handling
- PCB tools run via subprocess bridge to KiCad's bundled Python 3.9 (pcbnew SWIG bindings)

## MCP Registration

Register as a single unified MCP server (replaces the old separate `kicad` and `kicad-sch` servers):

```bash
# Remove old servers if they exist
claude mcp remove kicad
claude mcp remove kicad-sch

# Register the unified server
claude mcp add kicad -- /Volumes/Files/claude/kicad-mcp/.venv/bin/kicad-mcp
```

## Running Tests

```bash
cd /Volumes/Files/claude/kicad-mcp
.venv/bin/python -m pytest tests/ -v
```

130 tests: unit tests run without KiCad, integration tests auto-skip if KiCad is unavailable.

## Key Technical Notes

- PCB tools use a subprocess bridge: generate Python scripts → run via KiCad's Python 3.9 → parse JSON output
- KiCad's Python 3.9: `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9`
- Schematic tools use kicad-sch-api directly (pure Python, no subprocess needed)
- All tools return `{"status": "ok", ...}` on success or `{"error": "message"}` on failure
- Never run parallel MCP calls that modify the same PCB file (race on save)
