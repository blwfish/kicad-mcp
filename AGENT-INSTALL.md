# KiCad MCP — Agent Installation & Usage Guide

This file is for you, the AI agent. It tells you what needs to be true on this system for the KiCad MCP server to work, how to make it true, and how to use it once it's running. Read it fully before taking any action.

## What This Is

kicad-mcp is a Model Context Protocol (MCP) server providing <!-- tool-count -->17<!-- /tool-count --> tools for KiCad electronic design automation — schematic capture, PCB layout, autorouting, DRC, and more. Once installed and registered, these tools appear in your tool list and you can design circuit boards conversationally.

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
- Symbol and footprint libraries — indexed by the MCP server for `library(operation="search", type="symbol"|"footprint")`

### 2. Python 3.10+ (required)

The MCP server runs on the system Python (not KiCad's Python). 3.10+ is required for type union syntax.

**Check:** `python3 --version`

**Install:**
- **macOS:** `brew install python@3.12`
- **Linux:** `sudo apt install python3`
- **Windows:** https://www.python.org/downloads/

Most systems already have this if an AI agent is running.

### 3. Java 17+ (recommended)

Required for `autoroute(operation="run")`, which wraps the FreeRouter autorouter. Without Java, all other tools work fine but autorouting is unavailable. Autorouting is one of the most valuable capabilities — install Java unless there's a reason not to.

**Check:** `java -version`

**Install:**
- **macOS:** `brew install openjdk@21`
- **Linux:** `sudo apt install openjdk-21-jre` (or distro equivalent)
- **Windows:** https://adoptium.net/

### 4. FreeRouter JAR (recommended)

The FreeRouter autorouter.

> **⚠️ Version matters — use v2.2.3 or later.**
> v2.2.3+ is **10–30× faster** than v2.1.0 and produces **deterministic results** (same board, same result every run). v2.1.0 is non-deterministic — the same board can route to 1 unrouted connection one run and 13 the next, depending on JVM timing. If you're on v2.1.0, upgrade.

Download v2.2.4:

```bash
curl -L -o ~/freerouting.jar https://github.com/freerouting/freerouting/releases/download/v2.2.4/freerouting-2.2.4.jar
```

The server auto-detects these locations (newest version name wins when multiple JARs are present):
- `~/freerouting-2.2.4.jar` (or any `~/freerouting*.jar`)
- `~/freerouting.jar`
- `~/Downloads/freerouting*.jar`
- `freerouting` on the system PATH
- The `FREEROUTER_JAR` environment variable

If the URL above is stale, find the current `.jar` at https://github.com/freerouting/freerouting/releases and download it to `~/freerouting.jar`.

## Installation

> **Install from `main`, not `dev`.** This repo's default branch is `dev` — a
> plain `git clone` with no `-b` flag checks that out. `dev` is where active
> work happens and can be left inconsistent or incomplete between commits;
> `main` is the stable branch releases are cut from. Always clone/pull `main`.

```bash
git clone -b main https://github.com/blwfish/kicad-mcp.git
cd kicad-mcp
python3 -m venv .venv
source .venv/bin/activate    # On Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

To update an existing install: `git checkout main && git pull`.

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
project(operation="list")
```

This should return a list (possibly empty) without errors. If it returns an error about kicad-cli or pcbnew not being found, the KiCad installation isn't being detected — see Troubleshooting below.

A more thorough check:

```
library(operation="search", query="0603 resistor", type="footprint")
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

### Read AGENT-INSTRUCTIONS.md First

The file `AGENT-INSTRUCTIONS.md` in the repo root is your primary reference for **using** the tools, written for any agent (Claude, Gemini, Cursor, …). It contains:

- **Mandatory rules** — three things you must never do (manual routing, guessing library names, parallel PCB writes)
- **Complete workflow** — the step-by-step process from schematic to verified board
- **Tool selection table** — which tool to use for each task
- **Placement guidelines** — component grouping, spacing, pin numbering
- **DRC interpretation** — which violations matter and which are cosmetic

The critical rules are also delivered automatically to every MCP client through the server's `instructions` (no file-reading required), so you see them before opening any doc. (Claude Code users: the repo's local, git-ignored `CLAUDE.md` `@`-imports `AGENT-INSTRUCTIONS.md` and adds Claude-specific notes.)

### Client Compatibility

kicad-mcp exposes <!-- tool-count -->17<!-- /tool-count --> tools — well within every known MCP client limit. Claude Code, Cursor (~40-tool limit), and Gemini (~100-tool limit) are all supported.

Claude Code is the recommended client: it provides automatic prompt caching (critical for iterative KiCad workflows) and subagent support for parallel exploration tasks.

### Model & Interface Selection

**For Claude users:** Use **Claude Opus** with **subagents** for PCB design tasks. The combination is powerful because Opus handles complex multi-step workflows while subagents allow parallel exploration (component research, footprint selection, placement suggestions) without inflating the main conversation. This is the most efficient approach for KiCad design work.

**For most users:** The **Claude Code desktop/web app or CLI** is the right interface. These automatically optimize prompt caching, which is critical for KiCad workflows that rerun planning and design steps across multiple tool calls. Caching dramatically reduces latency and cost for multi-step tasks like placement iteration, autorouting, and DRC fixes. You get the benefit without any configuration.

**For developers using Claude API directly:** Be aware of prompt caching implications. KiCad design workflows involve repeated context (CLAUDE.md rules, board state, DRC results) across multiple tool calls. You should enable prompt caching to avoid re-processing the same context repeatedly. See the [Prompt Caching guide](https://docs.anthropic.com/en/docs/build-a-system-with-claude/prompt-caching) for details.

**For other agents:** Consult your agent's documentation for equivalent caching and model selection guidance. The same principles apply: larger, more capable models handle complex workflows better, and caching provides significant benefits for iterative tool-based tasks.

### The Workflow in Brief

```
1. Schematic    → schematic(operation="create"), schematic(operation="add_component"),
                  schematic(operation="connect_pins_with_labels"), schematic(operation="save")
2. Board size   → estimate_board_size (call BEFORE creating the PCB)
3. PCB setup    → pcb(operation="create"), pcb(operation="set_outline"),
                  pcb(operation="set_design_rules")
4. Footprints   → library(operation="search"), pcb(operation="place_footprint"),
                  suggest_placement, audit(operation="all")
5. Nets         → build_pcb_from_schematic (preferred) or manual
                  pcb(operation="add_net") + pcb(operation="bulk_assign_pad_nets")
6. Autoroute    → autoroute(operation="run", passes=2) or passes=3
7. Zones/finish → pcb(operation="add_zone"), pcb(operation="fill_zones"),
                  pcb(operation="finalize")
8. Verify       → drc(operation="run"); if issues remain, try drc(operation="autofix")
```

### Critical Rules

1. **Never route manually.** Do not use `pcb(operation="add_trace")`/`pcb(operation="add_via")` for routing. You cannot reliably compute spatial clearances. Use `autoroute(operation="run")`.
2. **Never guess library names.** Always call `library(operation="search", type="symbol"|"footprint")` first. Library names change between KiCad versions.
3. **Never write to the same PCB file in parallel.** Each PCB tool call loads, modifies, and saves the file. Concurrent writes corrupt it. Serialize all PCB operations.

## Health and Debugging

When something goes wrong, use these tools to diagnose:

| Symptom | Diagnostic tool | What to look for |
|---------|----------------|------------------|
| Footprints overlapping | `audit(operation="all", pcb_path=...)` | Reports courtyard overlaps, keepout violations, and silkscreen conflicts in one call |
| Traces crossing or shorts | `drc(operation="run", project_path=...)` | Full DRC via kicad-cli; categorizes all violations |
| Pads missing net assignments | `pcb(operation="get_pad_positions", pcb_path=..., reference="U1")` | Each pad should show a net name |
| Schematic wiring issues | `schematic(operation="validate")` | Checks for unconnected pins, missing power, etc. |
| Board won't autoroute | Check that all pads have nets assigned; check `autoroute(operation="run")` return for `incomplete_nets` |
| Library search returns nothing | First run builds the index — try again. If still empty, check that KiCad libraries exist at the detected path |

### Auto-Fix Capabilities

- `audit(operation="auto_fix_placement", pcb_path=...)` — nudges overlapping footprints apart
- `pcb(operation="auto_fix_silkscreen", pcb_path=...)` — moves silkscreen text that overlaps pads or other text
- `drc(operation="autofix", pcb_path=...)` — compound tool: runs DRC, fixes placement/routing/silkscreen, re-routes, verifies improvement
- `pcb(operation="finalize", pcb_path=...)` — one-call finish: fixes silkscreen + fills copper zones

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

Check: (1) Java is installed and on PATH, (2) FreeRouter JAR is findable (see Prerequisites above), (3) all pads have net assignments. The `autoroute(operation="run")` return value includes an `error` field if something went wrong.

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
- New operations fold into an existing router (schematic, pcb, audit, drc, autoroute, library, project, analyze, export, lcsc, schematic_layout, design) or add a new standalone tool if they don't fit any router's domain
- PCB tools use the subprocess bridge (`run_pcbnew_script`); schematic tools use kicad-sch-api in-process
- Run `pytest` before submitting — all tests should pass (2,000+ tests)
- Tools return `{"status": "ok", ...}` on success or `{"error": "..."}` on failure — follow this convention

### Adding a New Operation to an Existing Router

1. Add the `_op_<name>` implementation function to the appropriate `*_impl.py` or module file
2. Add a dispatch branch (`elif operation == "name": ...`) in the router function
3. Document the new op in the router's docstring operations list
4. Add a test in the corresponding `tests/test_router_*.py` file
5. Run `pytest` to verify

### Adding a New Standalone Tool

1. Add the tool function to the appropriate module in `src/kicad_mcp/tools/`
2. Register it in the module's `register_*_tools(mcp)` function
3. If it's a new module, import and call the registration function in `server.py`
4. Add the tool name to `EXPECTED_TOOLS` in `tests/test_server.py`
5. Run `python scripts/sync_tool_count.py` to update the count in all docs
6. Run `pytest` to verify

## License

MIT. See the `LICENSE` file in the repository root.
