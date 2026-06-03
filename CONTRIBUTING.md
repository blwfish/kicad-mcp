# Contributing

Thanks for taking the time to read this. This is a personal-use project — I built it because I need custom PCBs for my model railroad and rely on it for real boards I send to fab. That shapes how I handle contributions.

## Found a bug?

Please [open an issue](https://github.com/blwfish/kicad-mcp/issues/new). GitHub Discussions are intentionally off — issues are the single feedback channel for this repo.

A useful bug report includes:

- **What you tried to do.** The agent prompt or tool call, if applicable.
- **What happened instead.** The error message or unexpected output, verbatim.
- **DRC output, if relevant.** `drc(operation="run")` results are gold for layout bugs.
- **The schematic or PCB file.** A minimal `.kicad_sch` or `.kicad_pcb` that reproduces the issue is the fastest path to a fix. Strip out anything proprietary.
- **Versions:** KiCad (`kicad-cli --version`), kicad-sch-api (`pip show kicad-sch-api`), OS, agent (Claude Code, Cursor, etc.).

Don't worry about being exhaustive. *"add_trace put the trace on the wrong layer"* is enough to start.

## Sending a PR

Welcome, but a couple of preflight things:

1. **Open an issue first** if it's a non-trivial change. Saves both of us from wasted effort if I disagree on direction.
2. **Run the tests** before pushing:
   ```bash
   pytest   # 2,000+ tests, ~30 seconds, no KiCad install required
   ```
3. **Match the commit style.** `git log --oneline` shows it. Lowercase prefix (`fix:`, `feat:`, `test:`, `docs:`), short imperative summary, body explains the *why*.
4. **One concern per PR.** Bug fix and unrelated cleanup go in separate PRs.
5. **Update `EXPECTED_TOOLS` in `tests/test_server.py`** if you're adding, removing, or renaming an MCP tool. The docs-check CI workflow will fail otherwise (and TOOLS.md / README.md tool counts need to match too).

## Boundary ops (`pcbnew` / kicad-cli scripts): extract the logic first

Tools that run work inside KiCad's Python ship a script string to
`run_pcbnew_script`. Decision logic written *inside* that string has no
in-process unit a test can reach, so the only possible test mocks the boundary
and asserts its own configured return — a test that cannot fail. Before testing
such code, **extract the pure decision logic into a `*_HELPER` that is `exec`'d
directly in a no-KiCad test**, leaving only board I/O in the script. The pattern,
a copyable skeleton, and the `GEOMETRY_HELPER` exemplar are in
[`docs/BOUNDARY_OPS.md`](docs/BOUNDARY_OPS.md); `scripts/audit_testability.py`
ratchets it. *Then* cover the helper's thresholds per the next section.

## Testing discipline for `utils/` and `tools/` helpers

Most existing tests in this repo verify the Python function returned
the expected shape on a round input.  That's not enough for code that
feeds `pcbnew`, KiCad's DRC engine, or FreeRouter — the consumer often
flips behaviour on threshold values the test never exercised.  When
you add or change tests for helpers with internal numeric thresholds
or strict-inequality comparisons, follow these rules.

**Cover every threshold the function flips on.**  For
`rects_overlap`, `rect_inside`, `_aabb_hit`, `in_board`, the
`voltage < 50` cap in `extract_voltage_from_regulator`, the
`freq >= 1000` cascade in `extract_frequency_from_value`, the
`min_clearance > 0` gate in `audit_footprint_overlaps`: include a
parametrize case for the threshold value itself, one unit below, and
one unit above.  Round numbers far from the threshold catch
arithmetic errors but never the comparison itself.

**Use the consumer's tolerance, not the producer's.**  KiCad's
`ToMM` rounds to 1µm-ish; `pytest.approx(x, abs=0.001)` is **too
loose** for a strict-inequality check — it accepts 1µm coincidence,
which is what KiCad's DRC will then flag as a clearance violation.
Use `abs=1e-9` or exact equality for boundary tests.

**At least one input that is not a docstring example.**  Implementation
examples were the development inputs; they were chosen because they're
easy to think about, not because they exercise the corners.  Pick a
real KiCad library footprint name or a real value from a `.kicad_pcb`
you've worked with.

**Test what the consumer sees, not just the producer's return.**  If
`rects_overlap` returns `False` for touching edges, the test that
matters is "would KiCad's DRC flag this layout?", not "does the
function return False?".  When the consumer is a subprocess we can't
run in CI, assert the invariant the consumer needs — e.g. that the
gap between any two footprints is reported as either strictly positive
or strictly negative, never silently zero.

The boundary-coverage tests added in `test_component_utils.py`,
`test_pcb_keepout.py`, and `test_keepout_rect_helpers.py` model this
pattern.

## Will feature requests be accepted?

Maybe. The project is scoped to what I personally need — small to medium hobbyist boards, microcontroller circuits, model-railroad-grade reliability. If your request overlaps with that (more component types, better DRC autofix, footprint discovery improvements), it's likely. If it's far outside (RF design tools, multi-board assemblies, complex panelization), I'll probably point you at a fork instead.

## Maintainer

One person, in their spare time. Response times are not guaranteed. A polite ping after two silent weeks is fine.
