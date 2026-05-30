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

## Report which guarantee you hold — don't let "verified E2E" stand in for "tested"

When you report a change as done, name *which* guarantee you actually have. They
are different, and a green golden-harness run is only the first:

- **"works on this input"** — an end-to-end / integration run passed on a
  specific board or fixture (e.g. "`build_pcb_from_schematic` routes
  0-unconnected on the speed-cal config"). This proves the pipeline works *today,
  on that input*. It does **not** prove the logic is pinned against regression.
- **"logic pinned against regression"** — the decision logic has an in-process
  unit test that fails if the logic changes (a `*_HELPER` exec-tested at its
  boundaries — see `docs/BOUNDARY_OPS.md`). This is what catches a dropped field
  or a `<` → `<=` flip that *still routes*.

Say the one you have. If you only ran E2E, write "works on input X", not
"tested" / "verified" unqualified.

This is checkable, not just etiquette: if `scripts/audit_testability.py` reports
the logic as helperless boundary logic, or its test as a tautological boundary
mock, you cannot honestly claim "pinned against regression" for it — there is no
in-process test that reaches it.
