# KiCad MCP — Agent Installation & Usage Guide

This file is for you, the AI agent. It tells you what needs to be true on this system for the KiCad MCP server to work, how to make it true, and how to use it once it's running. Read it fully before taking any action.

## What This Is

kicad-mcp is a Model Context Protocol (MCP) server providing 98 tools for KiCad electronic design automation — schematic capture, PCB layout, autorouting, DRC, and more. Once installed and registered, these tools appear in your tool list and you can design circuit boards conversationally.

**Origin:** Built by one person for personal use, on a Mac, with Claude Code. Other platforms *should* work (the code handles macOS, Windows, and Linux) but are untested. PRs for other agents and platforms will be considered.

**Reporting bugs:** If something fails in a way that looks like a bug in this MCP server (not a KiCad issue, not a malformed schematic the user supplied), please tell the user to file an issue at https://github.com/blwfish/kicad-mcp/issues/new. GitHub Discussions are intentionally off — issues are the single feedback channel. Include the tool call you made, the verbatim error or symptom, and any DRC output if relevant.

## Prerequisites

The following must be present on the system. Check each one. Install anything missing — the commands and URLs are provided for each platform.

### 1. KiCad 8+ (required)

KiCad provides the pcbnew Python bindings that all PCB tools depend on. The MCP server calls KiCad's bundled Python as a subprocess — it does not import pcbnew directly.

**Check:** `kicad-cli --version` — must be 8.0 or later (9.x is fine).

**Install:**
- **macOS:** GUI installer from https://www.kicad.org/download/ (drag to Applications)
- **Linux:** `sudo apt install kicad` or the KiCad PPA for latest. Flatpak also available. Distro package manager versions are often outdated.
- **Windows:** Installer from https://www.kicad.org/download/

**What it provides:**
- `kicad-cli` — used for DRC checks, netlist export, thumbnail generation
- KiCad's Python 3.9 with pcbnew — used by the subprocess bridge for all PCB modifications
- Symbol and footprint libraries — indexed by the MCP server for `search_components` / `search_footprints`

### 2. Python 3.10+ (required)

The MCP server runs on the system Python (not KiCad's Python). 3.10+ is required for type union syntax.

**Check:** `python3 --version`

**Install:**
- **macOS:** `brew install python@3.12`
- **Linux:** `sudo apt install python3`
- **Windows:** https://www.python.org/downloads/

Most systems already have this if an AI agent is running.

### 3. Java 17+ (recommended)

Required for `autoroute_pcb`, which wraps the FreeRouter autorouter. Without Java, all other tools work fine but autorouting is unavailable. Autorouting is one of the most valuable capabilities — install Java unless there's a reason not to.

**Check:** `java -version`

**Install:**
- **macOS:** `brew install openjdk@21`
- **Linux:** `sudo apt install openjdk-21-jre` (or distro equivalent)
- **Windows:** https://adoptium.net/

### 4. FreeRouter JAR (recommended)

The FreeRouter autorouter.

> **⚠️ Version matters — use v2.2.3 or later.**
> v2.2.3 is **10–30× faster** than v2.1.0 and produces **deterministic results** (same board, same result every run). v2.1.0 is non-deterministic — the same board can route to 1 unrouted connection one run and 13 the next, depending on JVM timing. If you're on v2.1.0, upgrade.

Download v2.2.3:

```bash
curl -L -o ~/freerouting.jar https://github.com/freerouting/freerouting/releases/download/v2.2.3/freerouting-2.2.3.jar
```

The server auto-detects these locations (newest version name wins when multiple JARs are present):
- `~/freerouting-2.2.3.jar` (or any `~/freerouting*.jar`)
- `~/freerouting.jar`
- `~/Downloads/freerouting*.jar`
- `freerouting` on the system PATH
- The `FREEROUTER_JAR` environment variable

If the URL above is stale, find the current `.jar` at https://github.com/freerouting/freerouting/releases and download it to `~/freerouting.jar`.

## Installation

```bash
git clone https://github.com/blwfish/kicad-mcp.git
cd kicad-mcp
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Clone location: `~/Projects/`, `~/src/`, or wherever repos live on this system. `~/kicad-mcp` is a safe default.

### Register as an MCP Server

**Claude Code:**
```bash
claude mcp add kicad -- /absolute/path/to/kicad-mcp/.venv/bin/kicad-mcp
```

The path must be **absolute**. Relative paths break when the working directory changes.

**Other agents** — the server speaks standard MCP over stdio:
```json
{
  "mcpServers": {
    "kicad": {
      "command": "/absolute/path/to/kicad-mcp/.venv/bin/kicad-mcp"
    }
  }
}
```

Config format and location varies by agent platform.

## Verify Installation

After registering, confirm the server works by calling any lightweight tool:

```
list_projects()
```

This should return a list (possibly empty) without errors. If it returns an error about kicad-cli or pcbnew not being found, the KiCad installation isn't being detected — see Troubleshooting below.

A more thorough check:

```
search_footprints(query="0603 resistor")
```

This exercises the library index. On first run it builds a SQLite FTS5 index of all KiCad libraries (takes a few seconds). If it returns results like `Resistor_SMD:R_0603_1608Metric`, everything is working.

## Environment Variables

All are optional. The server auto-detects sensible defaults for each platform.

| Variable | Purpose | When to set it |
|----------|---------|----------------|
| `FREEROUTER_JAR` | Path to FreeRouter JAR | If the JAR isn't in `~/` or `~/Downloads/` |
| `KICAD_CLI_PATH` | Path to `kicad-cli` executable | If KiCad is installed in a non-standard location |
| `KICAD_SEARCH_PATHS` | Comma-separated project directories | If KiCad projects are in unusual locations |
| `KICAD_FOOTPRINT_DIR` | Override footprint library directory | If using custom/third-party footprint libraries |
| `KICAD_SYMBOL_DIR` | Override symbol library directory | If using custom/third-party symbol libraries |

## How to Use the Tools

### Read CLAUDE.md First

The file `CLAUDE.md` in the repo root is your primary reference for **using** the tools. It contains:

- **Mandatory rules** — three things you must never do (manual routing, guessing library names, parallel PCB writes)
- **Complete workflow** — the 8-step process from schematic to verified board
- **Tool selection table** — which tool to use for each task
- **Placement guidelines** — component grouping, spacing, pin numbering
- **DRC interpretation** — which violations matter and which are cosmetic

If you are registered as an MCP server for a project that does KiCad work, the project's claude configuration should include kicad-mcp's `CLAUDE.md` so you always have it in context.

### Model & Interface Selection

**For Claude users:** Use **Claude Opus** with **subagents** for PCB design tasks. The combination is powerful because Opus handles complex multi-step workflows while subagents allow parallel exploration (component research, footprint selection, placement suggestions) without inflating the main conversation. This is the most efficient approach for KiCad design work.

**For most users:** The **Claude Code desktop/web app or CLI** is the right interface. These automatically optimize prompt caching, which is critical for KiCad workflows that rerun planning and design steps across multiple tool calls. Caching dramatically reduces latency and cost for multi-step tasks like placement iteration, autorouting, and DRC fixes. You get the benefit without any configuration.

**For developers using Claude API directly:** Be aware of prompt caching implications. KiCad design workflows involve repeated context (CLAUDE.md rules, board state, DRC results) across multiple tool calls. You should enable prompt caching to avoid re-processing the same context repeatedly. See the [Prompt Caching guide](https://docs.anthropic.com/en/docs/build-a-system-with-claude/prompt-caching) for details.

**For other agents:** Consult your agent's documentation for equivalent caching and model selection guidance. The same principles apply: larger, more capable models handle complex workflows better, and caching provides significant benefits for iterative tool-based tasks.

### The Workflow in Brief

```
1. Schematic    → create_schematic, add_component, connect_pins_with_labels, save_schematic
2. Board size   → estimate_board_size (call BEFORE creating the PCB)
3. PCB setup    → create_pcb, add_board_outline, set_design_rules
4. Footprints   → search_footprints, place_footprint, suggest_placement, audit_all
5. Nets         → update_pcb_from_schematic (preferred) or manual add_net + bulk_assign_pad_nets
6. Autoroute    → autoroute_pcb with passes=2 or passes=3
7. Zones/finish → add_copper_zone, fill_zones, finalize_pcb
8. Verify       → run_drc_check; if issues remain, try drc_autofix
```

### Critical Rules

1. **Never route manually.** Do not use `add_trace`/`add_via` for routing. You cannot reliably compute spatial clearances. Use `autoroute_pcb`.
2. **Never guess library names.** Always call `search_components` or `search_footprints` first. Library names change between KiCad versions.
3. **Never write to the same PCB file in parallel.** Each PCB tool call loads, modifies, and saves the file. Concurrent writes corrupt it. Serialize all PCB operations.

## Health and Debugging

When something goes wrong, use these tools to diagnose:

| Symptom | Diagnostic tool | What to look for |
|---------|----------------|------------------|
| Footprints overlapping | `audit_all(pcb_path=...)` | Reports courtyard overlaps, keepout violations, and silkscreen conflicts in one call |
| Traces crossing or shorts | `run_drc_check(project_path=...)` | Full DRC via kicad-cli; categorizes all violations |
| Pads missing net assignments | `get_pad_positions(pcb_path=..., reference="U1")` | Each pad should show a net name |
| Schematic wiring issues | `validate_schematic()` | Checks for unconnected pins, missing power, etc. |
| Board won't autoroute | Check that all pads have nets assigned; check `autoroute_pcb` return for `incomplete_nets` |
| Library search returns nothing | First run builds the index — try again. If still empty, check that KiCad libraries exist at the detected path |

### Auto-Fix Capabilities

- `auto_fix_placement(pcb_path=...)` — nudges overlapping footprints apart
- `auto_fix_silkscreen(pcb_path=...)` — moves silkscreen text that overlaps pads or other text
- `drc_autofix(pcb_path=...)` — compound tool: runs DRC, fixes placement/routing/silkscreen, re-routes, verifies improvement
- `finalize_pcb(pcb_path=...)` — one-call finish: fixes silkscreen + fills copper zones

## Troubleshooting

### "kicad-cli not found"

The server searches standard installation paths per platform. If KiCad is installed somewhere unusual, set `KICAD_CLI_PATH`:

```bash
export KICAD_CLI_PATH=/path/to/kicad-cli
```

On macOS, the default is `/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`. On Linux, it's expected on PATH.

### "pcbnew Python not found" or subprocess errors

PCB tools run via KiCad's bundled Python (not the system Python). The bridge looks for it at:

- **macOS:** `/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3.9`
- **Windows:** `C:\Program Files\KiCad\bin\python.exe`
- **Linux:** `/usr/bin/python3` (pcbnew must be importable from system Python)

If KiCad is installed but pcbnew tools fail, verify the KiCad installation includes the Python scripting console (it's included by default in standard installs).

### Autorouting fails silently

Check: (1) Java is installed and on PATH, (2) FreeRouter JAR is findable (see Prerequisites above), (3) all pads have net assignments. The `autoroute_pcb` return value includes an `error` field if something went wrong.

### Library index is empty

The SQLite FTS5 index at `~/.cache/kicad-mcp/library_index.db` is built on first use. If it's empty, the library paths aren't being detected. Check that KiCad's footprint/symbol directories exist at the expected locations (see config.py), or set `KICAD_FOOTPRINT_DIR` / `KICAD_SYMBOL_DIR`.

## Contributing

### Filing Issues

When filing an issue, include:
- Platform (macOS/Windows/Linux) and version
- KiCad version (`kicad-cli --version`)
- Python version
- The tool call that failed and the complete error response
- The PCB or schematic file if possible (or a minimal reproducer)

### Pull Requests

- PRs for bug fixes, new platform support, and new tools are welcome
- Follow the existing code patterns: each tool module has a `register_*_tools(mcp)` function
- PCB tools use the subprocess bridge (`run_pcbnew_script`); schematic tools use kicad-sch-api in-process
- Run `pytest` before submitting — all tests should pass (currently 199 tests)
- The CI checks that tool counts in `test_server.py`, `README.md`, and `CLAUDE.md` stay in sync — update all three if you add or remove tools
- Tools return `{"status": "ok", ...}` on success or `{"error": "..."}` on failure — follow this convention

### Adding a New Tool

1. Add the tool function to the appropriate module in `src/kicad_mcp/tools/`
2. Register it in the module's `register_*_tools(mcp)` function
3. If it's a new module, import and call the registration function in `server.py`
4. Add the tool name to `EXPECTED_TOOLS` in `tests/test_server.py`
5. Update the snapshot tool count in `test_server.py`
6. Update tool counts in `README.md` and `CLAUDE.md`
7. Run `pytest` to verify

## License

MIT. See the `LICENSE` file in the repository root.
