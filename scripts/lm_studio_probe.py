#!/usr/bin/env python3
"""Probe kicad-mcp's real MCP protocol output against a local LM Studio model.

Why this exists: LM Studio's own MCP integration lives only in its GUI (its
CLI, `lms chat`, does not wire MCP tools into the model at all), so there's
no scriptable way to drive "a non-Claude model actually using this server"
through LM Studio itself. This script is a minimal MCP client — it speaks
the real protocol to a real `kicad-mcp` subprocess (same `initialize()`,
same `list_tools()`, same `call_tool()` any client would use) — bridged to
LM Studio's OpenAI-compatible local completions API (default port 1234) so
a local model can drive it, with real tool calls executed against the real
server.

Use it to re-verify critical-rule adherence (never hand-route, never guess
library names, ...) and general tool-schema usability whenever tool
schemas, `SERVER_INSTRUCTIONS`, or `usage_guidance.NOTES` change materially
— see AGENT-INSTALL.md's "Client Compatibility" section for the last
recorded results (2026-09-20: qwen2.5-coder-14b, qwen3-32b, gemma-4-e4b, all
via LM Studio).

Prerequisites:
    - LM Studio running with its local server started (`lms server start`)
      and a model loaded (`lms load <model-key>`). Only load one model at a
      time — these are typically multi-GB and this script does not manage
      loading/unloading for you.
    - kicad-mcp installed in this same environment (`uv run` from the repo
      root picks up the project's own venv; `mcp` is already a transitive
      dependency via fastmcp).

Usage:
    uv run scripts/lm_studio_probe.py <model-id> <scenario> [--no-instructions]
    uv run scripts/lm_studio_probe.py --list-scenarios

    <model-id> is LM Studio's identifier for the loaded model, e.g.
    "qwen/qwen2.5-coder-14b" (see `lms ps`).

    --no-instructions simulates a client that drops the MCP `instructions`
    field (a real gap found in freecad-mcp's own LM Studio testing) — useful
    for checking whether a tool's own description/schema alone is enough to
    guide correct behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

LM_STUDIO_URL = "http://localhost:1234/v1/chat/completions"
KICAD_MCP_CMD = str(Path(__file__).resolve().parent.parent / ".venv" / "bin" / "kicad-mcp")

SCENARIOS = {
    "session_start": (
        "I want to design a small PCB for a temperature sensor breakout "
        "board using an ESP32. Where do I start?"
    ),
    "routing": (
        "My board at {pcb_path} has all footprints placed and nets already "
        "assigned. Please route it."
    ),
    "library": (
        "I need to add a 10k 0603 resistor to my KiCad schematic. What's "
        "the exact tool call to do that?"
    ),
}


def _mcp_tool_to_openai(tool) -> dict:
    schema = (
        getattr(tool, "inputSchema", None)
        or getattr(tool, "input_schema", None)
        or {"type": "object", "properties": {}}
    )
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "")[:1024],
            "parameters": schema,
        },
    }


def _post_completion(model: str, messages: list, tools: list, timeout: float = 180.0) -> dict:
    payload = json.dumps(
        {"model": model, "messages": messages, "tools": tools, "temperature": 0.1}
    ).encode("utf-8")
    req = urllib.request.Request(
        LM_STUDIO_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result: dict = json.loads(resp.read().decode("utf-8"))
            return result
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LM Studio HTTP {e.code}: {body}") from e


async def _setup_fixture(pcb_path: str) -> None:
    """Create a minimal real PCB fixture via the real MCP server (no model
    involved) — used by the `routing` scenario."""
    server_params = StdioServerParameters(command=KICAD_MCP_CMD, args=[])
    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("pcb", {"operation": "create", "pcb_path": pcb_path})
        await session.call_tool(
            "pcb",
            {
                "operation": "set_outline",
                "pcb_path": pcb_path,
                "x_mm": 0,
                "y_mm": 0,
                "width_mm": 40,
                "height_mm": 30,
            },
        )


async def run(model: str, scenario: str, include_instructions: bool, max_turns: int = 4) -> None:
    fixture_dir = Path(tempfile.mkdtemp(prefix="kicad_mcp_lmstudio_probe_"))
    pcb_path = str(fixture_dir / "fixture.kicad_pcb")
    if scenario == "routing":
        await _setup_fixture(pcb_path)
    prompt = SCENARIOS[scenario].format(pcb_path=pcb_path)

    server_params = StdioServerParameters(command=KICAD_MCP_CMD, args=[])
    async with stdio_client(server_params) as (read, write), ClientSession(read, write) as session:
        init_result = await session.initialize()
        instructions = init_result.instructions or ""
        tools_result = await session.list_tools()
        openai_tools = [_mcp_tool_to_openai(t) for t in tools_result.tools]

        print(
            f"=== model={model} scenario={scenario} "
            f"instructions={'YES' if include_instructions else 'NO (simulated drop)'} ==="
        )
        print(f"[setup] {len(openai_tools)} tools fetched from real kicad-mcp server")
        print(f"[setup] real instructions length: {len(instructions)} chars")

        messages = []
        if include_instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})

        for turn in range(max_turns):
            data = _post_completion(model, messages, openai_tools)
            msg = data["choices"][0]["message"]
            clean_msg = {"role": "assistant", "content": msg.get("content") or ""}
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                clean_msg["tool_calls"] = tool_calls
            messages.append(clean_msg)

            if not tool_calls:
                print(f"[turn {turn}] FINAL TEXT: {(msg.get('content') or '')[:600]}")
                break

            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                print(f"[turn {turn}] TOOL CALL: {name}({json.dumps(args)})")
                try:
                    result = await session.call_tool(name, args)
                    result_text = "".join(getattr(c, "text", "") for c in result.content)
                except Exception as e:  # noqa: BLE001 - want to see any tool failure inline
                    result_text = f"ERROR calling tool: {e}"
                print(f"[turn {turn}]   -> result: {result_text[:300]}")
                messages.append(
                    {"role": "tool", "tool_call_id": tc["id"], "content": result_text[:2000]}
                )
        else:
            print(f"[stopped after {max_turns} turns without a final answer]")
        print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", nargs="?", help="LM Studio model id, e.g. qwen/qwen2.5-coder-14b")
    parser.add_argument("scenario", nargs="?", choices=sorted(SCENARIOS), help="Scenario to run")
    parser.add_argument(
        "--no-instructions",
        action="store_true",
        help="Simulate a client that drops the MCP `instructions` field",
    )
    parser.add_argument(
        "--list-scenarios", action="store_true", help="Print available scenarios and exit"
    )
    args = parser.parse_args()

    if args.list_scenarios:
        for name, prompt in SCENARIOS.items():
            print(f"{name}: {prompt}")
        return 0

    if not args.model or not args.scenario:
        parser.print_help()
        return 1

    asyncio.run(run(args.model, args.scenario, include_instructions=not args.no_instructions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
