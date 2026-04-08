# kicad-mcp

This tool enables your AI agent to use [KiCad](https://www.kicad.org/) — the industry-standard open-source electronics design software — to design circuits and lay out printed circuit boards for you.

## What This Does

You describe what you need — "design a board for this ESP32 circuit" or "here's the schematic, lay out the PCB" — and your AI agent does the rest: drawing the schematic, choosing components, placing them on the board, routing traces, checking for errors, and producing manufacturing-ready files. All using the same KiCad that professional engineers use, with 98 tools covering the full design workflow.

You don't need to know KiCad. You don't need to know what a PCB layout tool does. You just need an AI agent (like [Claude](https://claude.ai/)).

## Getting Started

Tell your AI agent:

> Go to https://github.com/blwfish/kicad-mcp and read the AGENT-INSTALL.md file. Follow the instructions to install and configure the KiCad MCP server on this machine.

Your agent will handle the rest — installing prerequisites, cloning the repo, downloading the autorouter, and registering itself. Once setup is complete, you can ask your agent to design PCBs.

## What You Can Ask Your Agent To Do

- **Design a PCB from a description** — "I need a board with an ATmega328, three LEDs, and a USB-C connector"
- **Lay out a PCB from a schematic** — "Here's my schematic, create the board layout and route it"
- **Modify an existing board** — "Move the voltage regulator closer to the connector and re-route"
- **Check a design** — "Run DRC on my board and fix any issues"
- **Prepare for manufacturing** — "Panelize this board 2x5 with V-scores and generate the files"

The agent knows the full workflow — schematic → board sizing → component placement → routing → copper zones → verification — and will walk through it step by step.

## Background

I built this because I need it. I'm not an electrical engineer — I build things for my model railroad that need custom PCBs, and I can't design circuits without this tool and Claude. This is how I actually get boards made: I describe what I need, Claude drives KiCad through this server, and I send the Gerbers to fab. It's not a demo or a hackathon project.

That means reliability matters. The test suite (479 tests) exists because I depend on this working correctly when I sit down to design a board. Bugs in PCB layout tools turn into real problems — wrong footprints, shorted traces, boards that don't work when they arrive from the manufacturer.

I use Claude Code on a Mac. Other platforms *should* work — the code handles macOS, Windows, and Linux — but are untested. PRs for other agents and platforms will be considered.

### What's Under the Hood

The server provides 98 tools organized into three groups:

- **Schematic tools** (29) — create and edit circuit schematics, place components, wire connections, pin collision detection
- **PCB tools** (49) — board layout, footprint placement, pre-route readiness checks, autorouting via [FreeRouter](https://github.com/freerouting/freerouting), copper zones, silkscreen management, design rule checking and auto-fix
- **Analysis & export tools** (19) — project management, BOM generation, Gerber/drill export, netlist extraction, circuit pattern recognition

The server uses [FastMCP](https://github.com/jlowin/fastmcp) and delegates PCB operations to KiCad's bundled Python via subprocess. Schematic operations use [kicad-sch-api](https://pypi.org/project/kicad-sch-api/).

### For Developers

```bash
# 479 tests, no KiCad installation required — runs in ~9 seconds
pytest

# Lint
ruff check src/ tests/
```

Tests cover all 98 tools across every module — schematic, PCB board setup, footprints, nets, routing, zones, silkscreen, planning, DRC, BOM, autorouting, netlist/patterns — plus utilities like the pcbnew subprocess bridge, component value parsing, and project file handling. Everything is unit-testable without a KiCad installation because PCB operations go through a single subprocess bridge (`run_pcbnew_script`) that's easy to mock.

See [AGENT-INSTALL.md](AGENT-INSTALL.md) for full technical details, architecture, contributing guidelines, and how to add new tools.

## Security

This MCP server runs KiCad operations on your behalf, including reading and writing PCB, schematic, and export files anywhere on your filesystem. You should be aware of the implications:

- **Unrestricted file access**: Tools accept arbitrary filesystem paths for PCBs, schematics, Gerber exports, and BOMs. The server can read and write any file your user account can access.
- **Subprocess execution**: PCB operations run KiCad's Python interpreter as subprocesses. Parameters are passed via JSON temp files to avoid injection, but the underlying mechanism executes code.
- **Local-only transport**: The server uses stdio transport (stdin/stdout). It does not bind any network socket and is not exposed to the network.
- **No secrets**: The server stores no credentials and makes no network connections beyond the local filesystem and KiCad CLI.

**This tool is intended for local development use on a single-user machine.** Do not expose it to untrusted networks or users.

## License

MIT
