# Contributing

Thanks for taking the time to read this. This is a personal-use project — I built it because I need custom PCBs for my model railroad and rely on it for real boards I send to fab. That shapes how I handle contributions.

## Found a bug?

Please [open an issue](https://github.com/blwfish/kicad-mcp/issues/new). GitHub Discussions are intentionally off — issues are the single feedback channel for this repo.

A useful bug report includes:

- **What you tried to do.** The agent prompt or tool call, if applicable.
- **What happened instead.** The error message or unexpected output, verbatim.
- **DRC output, if relevant.** `run_drc_check` results are gold for layout bugs.
- **The schematic or PCB file.** A minimal `.kicad_sch` or `.kicad_pcb` that reproduces the issue is the fastest path to a fix. Strip out anything proprietary.
- **Versions:** KiCad (`kicad-cli --version`), kicad-sch-api (`pip show kicad-sch-api`), OS, agent (Claude Code, Cursor, etc.).

Don't worry about being exhaustive. *"add_trace put the trace on the wrong layer"* is enough to start.

## Sending a PR

Welcome, but a couple of preflight things:

1. **Open an issue first** if it's a non-trivial change. Saves both of us from wasted effort if I disagree on direction.
2. **Run the tests** before pushing:
   ```bash
   pytest   # 479+ tests, ~9 seconds, no KiCad install required
   ```
3. **Match the commit style.** `git log --oneline` shows it. Lowercase prefix (`fix:`, `feat:`, `test:`, `docs:`), short imperative summary, body explains the *why*.
4. **One concern per PR.** Bug fix and unrelated cleanup go in separate PRs.
5. **Update `EXPECTED_TOOLS` in `tests/test_server.py`** if you're adding, removing, or renaming an MCP tool. The docs-check CI workflow will fail otherwise (and TOOLS.md / README.md tool counts need to match too).

## Will feature requests be accepted?

Maybe. The project is scoped to what I personally need — small to medium hobbyist boards, microcontroller circuits, model-railroad-grade reliability. If your request overlaps with that (more component types, better DRC autofix, footprint discovery improvements), it's likely. If it's far outside (RF design tools, multi-board assemblies, complex panelization), I'll probably point you at a fork instead.

## Maintainer

One person, in their spare time. Response times are not guaranteed. A polite ping after two silent weeks is fine.
