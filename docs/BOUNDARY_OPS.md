# Boundary ops: extract the logic before you weld it to `pcbnew`

Tools in `tools/pcb_*.py` do their work inside KiCad's bundled Python by shipping
a script *string* to `run_pcbnew_script`. That string runs across a process
boundary — it cannot `import` from this package. The trap is to write the
**decision logic** (geometry, thresholds, classification) *inside* that script,
where no in-process unit test can reach it. The only test you can then write
mocks `run_pcbnew_script` and asserts its configured return — a test that
**cannot fail**. (See the tautological-test cluster in
`docs/CODE_REVIEW_2026-05-30.md`; `scripts/audit_testability.py` flags both
halves of this.)

The fix is structural, not "write a better test": **separate the pure decision
logic from the I/O shell, so the logic is callable without KiCad.**

## The shape

A boundary op has three parts:

1. **A pure helper** — the decision logic as plain functions over plain data
   (dicts, numbers, strings). No `pcbnew`. Lives in `utils/` as a string
   constant (`*_HELPER`) so it can be spliced into the script.
   - Exemplar: `utils/geometry.py` → `GEOMETRY_HELPER` (`aabb_overlap`,
     `rects_overlap`, `clearance_violation`, `signed_gap_mm`, …) — pure functions
     over `{"x_min_mm": …}` rects.

2. **A thin shell** — the embedded `pcbnew` script. It does I/O *only*: load the
   board, marshal `pcbnew` objects into plain dicts, **call the helper for every
   decision**, emit JSON. It splices the helper in by string concatenation.
   - Exemplar: `tools/pcb_footprints.py` → `""" + _KEEPOUT_HELPER + """`, then
     `fp_rect = {… pcbnew.ToMM(…) …}` (marshal) and
     `if not rects_overlap(fp_rect, kz_bb)` (decision via the helper).

3. **A no-KiCad exec test** — `exec(HELPER, ns)`, call `ns["fn"](…)`, assert at
   the boundaries. Runs in the default suite, no KiCad required.
   - Exemplar: `tests/test_geometry.py` → `exec(GEOMETRY_HELPER, ns)`, then
     `ns["rects_overlap"](a, b)` asserting touching / at / below / above.

## Two variants

- **Helper-only** (lightest): the logic lives only in the `*_HELPER` string; the
  test `exec`s it. Use when the logic is needed only inside the script — e.g.
  `SPIRAL_HELPER`, `exec`'d in `tests/test_spiral_placement.py`.
- **Python twin + byte-equivalent helper + parity test** (gold standard): the
  logic *also* exists as real Python functions in the module (for in-process
  callers), and a parity test `exec`s the helper and asserts it matches the
  twin. `GEOMETRY_HELPER` is this — the byte-equivalence contract is stated in
  `utils/geometry.py` (the comment above the constant), and `test_helper_defines`
  / `test_helper_*_identical` in `tests/test_geometry.py` guard the drift.

  The duplication is real (the review flags `POWER_NET_HELPER` hand-mirroring as
  a Rule-3 risk). **The parity test is what makes it safe — never ship the twin
  without it.**

## Skeleton

```python
# utils/<area>.py  — the pure logic: exec-testable, no pcbnew
MYOP_HELPER = """
def decide_something(item, threshold_mm):
    # plain data in, bool/number out. No pcbnew. THIS is what the test reaches.
    return item["gap_mm"] < threshold_mm
"""

# tools/<tool>.py  — the thin shell: board I/O + marshalling only
from kicad_mcp.utils.<area> import MYOP_HELPER
_MYOP_HELPER = MYOP_HELPER

def my_op(pcb_path, threshold_mm):
    script = """
import pcbnew, json, sys
params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])
""" + _MYOP_HELPER + """
# marshal pcbnew objects -> plain dicts, then DECIDE via the helper
items = [{"gap_mm": round(pcbnew.ToMM(x.Gap()), 3)} for x in board.GetThings()]
hits = [it for it in items if decide_something(it, params["threshold_mm"])]
print(json.dumps({"status": "ok", "hits": hits}))
"""
    return run_pcbnew_script(
        script, params={"pcb_path": pcb_path, "threshold_mm": threshold_mm}
    )

# tests/test_<area>.py  — no KiCad: exec the helper, assert at the boundary
def test_decide_something_boundary():
    ns = {}
    exec(MYOP_HELPER, ns)
    decide = ns["decide_something"]
    assert decide({"gap_mm": 0.19}, 0.2) is True    # below
    assert decide({"gap_mm": 0.20}, 0.2) is False   # at  (`<` is strict — pin the side)
    assert decide({"gap_mm": 0.21}, 0.2) is False   # above
```

Once the logic is reachable, cover its thresholds per the testing-discipline
section in `CONTRIBUTING.md` (value / just-below / just-above; the consumer's
tolerance, not the producer's).

## When you DON'T need this

If the script is pure marshalling — load → mutate → save, with no `if` / `for` /
comparison decision — there is nothing to extract. The Stage-1 detector
deliberately ignores those (it keys on control flow *inside* the script string).
Don't manufacture a helper for a straight-line save.

## Enforcement

`scripts/audit_testability.py` is a report-only ratchet:

- `find_helperless_boundary_logic` — a tool shipping `pcbnew.` decision logic
  with no extracted `*_HELPER`.
- `find_tautological_tests` — a test that patches `run_pcbnew_script`, configures
  a mock return, then asserts a scalar straight back out of it.

New violations are surfaced (and, under `--check`, fail). Existing ones are
grandfathered in `scripts/testability_baseline.json`; extracting one ratchets
the count down — it may only fall.
