# kicad-mcp

This tool enables your AI agent to use [KiCad](https://www.kicad.org/) — the industry-standard open-source electronics design software — to design circuits and lay out printed circuit boards for you.

## What This Does

You describe what you need — "design a board for this ESP32 circuit" or "here's the schematic, lay out the PCB" — and your AI agent does the rest: drawing the schematic, choosing components, placing them on the board, routing traces, checking for errors, and producing manufacturing-ready files. All using the same KiCad that professional engineers use, with 96 tools covering the full design workflow.

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

I built this for myself. I use Claude Code on a Mac. Other platforms *should* work — the code handles macOS, Windows, and Linux — but are untested. PRs for other agents and platforms will be considered.

### What's Under the Hood

The server provides 96 tools organized into three groups:

- **Schematic tools** (29) — create and edit circuit schematics, place components, wire connections, pin collision detection
- **PCB tools** (49) — board layout, footprint placement, pre-route readiness checks, autorouting via [FreeRouter](https://github.com/freerouting/freerouting), copper zones, silkscreen management, design rule checking and auto-fix
- **Analysis tools** (18) — project management, BOM generation, netlist extraction, circuit pattern recognition

The server uses [FastMCP](https://github.com/jlowin/fastmcp) and delegates PCB operations to KiCad's bundled Python via subprocess. Schematic operations use [kicad-sch-api](https://pypi.org/project/kicad-sch-api/).

### For Developers

```bash
# 199 tests, no KiCad installation required
pytest

# Lint
ruff check src/ tests/
```

See [AGENT-INSTALL.md](AGENT-INSTALL.md) for full technical details, architecture, contributing guidelines, and how to add new tools.

## License

MIT
