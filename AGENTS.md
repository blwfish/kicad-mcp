# Agent working notes — kicad-mcp

Conventions for any AI agent (Claude Code, Cursor, etc.) working in this repo.
General contributor guidance — bug reports, PR style, threshold-test coverage —
is in `CONTRIBUTING.md`. This file holds the few rules that are easy to violate
while *generating* code and expensive to retrofit.

## Boundary ops: extract decision logic before welding it to `pcbnew`

When you add or edit a tool that runs a `pcbnew` or `kicad-cli` script — anything
that builds a script string for `run_pcbnew_script` — do **not** put decision
logic (geometry, thresholds, classification) inside that string. Across the
process boundary it becomes unreachable to any in-process test, and the only test
left is a tautological boundary-mock that asserts its own configured return and
cannot fail.

Instead:

1. Put the pure logic in a `*_HELPER` string in `utils/` (exemplar:
   `utils/geometry.py` → `GEOMETRY_HELPER`).
2. Keep the embedded script a **thin shell**: load the board, marshal `pcbnew`
   objects into plain dicts, splice the helper in by concatenation
   (`""" + _MY_HELPER + """`), and **call the helper for every decision**.
3. Add a no-KiCad test that `exec`s the helper and asserts at the boundaries
   (value / just-below / just-above).

Full pattern and a copyable skeleton: **`docs/BOUNDARY_OPS.md`**. Straight-line
marshalling (load → mutate → save, no branching) needs no helper — don't invent
one.

Before finishing such work, run the report-only ratchet:

```bash
python scripts/audit_testability.py        # report-only; --check fails on NEW violations
```

It flags new helperless boundary logic and new tautological boundary tests.
Pre-existing violations are grandfathered in `scripts/testability_baseline.json`;
extracting one ratchets the count down. Refresh after an intentional change with
`--update-baseline`.
