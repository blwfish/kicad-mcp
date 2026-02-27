# kicad-mcp

An [MCP server](https://modelcontextprotocol.io/) that lets your AI agent design circuit boards using [KiCad](https://www.kicad.org/). 91 tools covering schematic capture, PCB layout, autorouting, design rule checking, and more.

## What This Does

You talk to your AI agent. Your agent talks to KiCad. You describe what you need — "design a board for this ESP32 circuit" or "here's the schematic, lay out the PCB" — and the agent handles the rest: placing components, routing traces, checking for errors, and producing manufacturing-ready files.

You don't need to know KiCad. You don't need to know what an MCP server is. You just need an AI agent (like [Claude](https://claude.ai/)) and KiCad installed on your computer.

## Getting Started

**Step 1:** Make sure [KiCad 8+](https://www.kicad.org/download/) is installed on your machine.

**Step 2:** Tell your AI agent:

> Go to https://github.com/blwfish/kicad-mcp and read the AGENT-INSTALL.md file. Follow the instructions to install and configure the KiCad MCP server on this machine.

That's it. Your agent will handle cloning the repo, installing dependencies, downloading any additional tools needed (like the autorouter), and registering itself. Once setup is complete, you can ask your agent to design PCBs.

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

The server provides 91 tools organized into three groups:

- **Schematic tools** (28) — create and edit circuit schematics, place components, wire connections
- **PCB tools** (47) — board layout, footprint placement, autorouting via [FreeRouter](https://github.com/freerouting/freerouting), copper zones, silkscreen management, design rule checking and auto-fix
- **Analysis tools** (16) — project management, BOM generation, netlist extraction, circuit pattern recognition

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
