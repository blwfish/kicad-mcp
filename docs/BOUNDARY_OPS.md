# Boundary ops — extracting decision logic from `pcbnew`/`kicad-cli` scripts

Referenced from [AGENTS.md](../AGENTS.md) ("Boundary ops") and
[CONTRIBUTING.md](../CONTRIBUTING.md) ("Boundary ops (`pcbnew` / kicad-cli
scripts)"). This page is the full pattern plus a copyable skeleton those two
files point at.

## The problem

Most PCB tools in this repo run a Python script inside KiCad's bundled
interpreter via `run_pcbnew_script` (see `utils/pcbnew_bridge.py`) — the
script is a string, shipped across a subprocess boundary, that loads a
board, does something, and prints JSON back.

If you write **decision logic** — a geometry comparison, a threshold check,
a classification rule — directly inside that script string, it becomes
unreachable to any in-process test. The only test that can reach it mocks
`run_pcbnew_script` itself and asserts on the mock's own configured return
value — a test that is structurally incapable of failing, because it never
executes the logic under test. That's a tautological boundary test, and
`scripts/audit_testability.py` flags exactly this shape.

The fix is always the same: **pull the pure logic out of the script string
into a real Python function** (importable, testable, `mypy`-checked), then
give the embedded script a copy of that same logic as an injectable source
string, so the script can call it without an `import` (KiCad's Python can't
see this package). One function, two representations, one source of truth.

## The pattern, in three steps

1. **Write the pure logic as ordinary Python functions**, then also express
   the same logic as a `*_HELPER` string — a block of source code, byte-for-
   byte equivalent to the Python functions, minus type annotations (the
   embedded script runs under KiCad's Python, which may be an older version;
   don't rely on annotation syntax it might not support).
2. **Keep the embedded script a thin shell**: load the board, marshal
   `pcbnew` objects into plain dicts/tuples, splice the helper string in by
   concatenation, and **call the helper for every decision** — the script
   itself should contain no comparisons, no thresholds, no classification.
3. **Add a no-KiCad test that `exec`s the helper string** and asserts at the
   boundaries (value / just-below / just-above — see CLAUDE.md's
   Threshold-Boundary Testing Rule), plus a drift guard confirming the
   helper defines the same functions the Python module exports.

Straight-line marshalling (load → mutate → save, no branching) needs no
helper — don't invent one for a script that has no decisions to extract.

## Copyable skeleton

Generic version — swap `MY_HELPER` / `my_threshold_check` for your domain:

**1. `utils/my_domain.py` (or, for a helper used by only one tool, directly
in that tool's module — see "Where the helper lives" below):**

```python
def my_threshold_check(value: float, limit: float) -> bool:
    """Pure decision logic. Fully typed, fully testable, no pcbnew import."""
    return value >= limit


# Embedded scripts run inside pcbnew's Python and cannot `import` from this
# module; they string-concatenate MY_HELPER into their source instead. The
# definition below MUST stay byte-equivalent to the Python function above —
# that's the whole point of having one source of truth.
MY_HELPER = """
def my_threshold_check(value, limit):
    return value >= limit
"""
```

**2. The embedded script — a thin shell that splices the helper in and calls
it for every decision:**

```python
from kicad_mcp.utils.my_domain import MY_HELPER
from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

def _op_my_check(pcb_path: str, limit: float) -> dict:
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])
""" + MY_HELPER + """

results = []
for fp in board.GetFootprints():
    value = pcbnew.ToMM(fp.GetPosition().x)   # marshal pcbnew object -> plain value
    results.append({
        "reference": fp.GetReference(),
        "passes": my_threshold_check(value, params["limit"]),  # decision via the helper
    })

print(json.dumps({"status": "ok", "results": results}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path, "limit": limit})
```

Note what the shell does *not* do: no comparison operator appears anywhere
in the script string. Every decision is a call to a function the helper
string defined.

**3. The matching no-KiCad test — exec the helper, assert at the boundary:**

```python
from kicad_mcp.utils.my_domain import MY_HELPER, my_threshold_check

def test_helper_defines_the_function():
    """Drift guard: catches the helper string falling out of sync with the
    Python module (a missing/renamed function here is a runtime NameError
    inside KiCad's Python, which no CI here will ever run)."""
    assert "def my_threshold_check" in MY_HELPER


def test_helper_matches_python_at_boundary():
    namespace: dict = {}
    exec(MY_HELPER, namespace)
    for value, limit in [(4.999, 5.0), (5.0, 5.0), (5.001, 5.0)]:  # below/at/above
        expected = my_threshold_check(value, limit)
        got = namespace["my_threshold_check"](value, limit)
        assert got == expected, f"helper diverged from Python at value={value}, limit={limit}"
```

### Concrete example: `GEOMETRY_HELPER`

The skeleton above is modeled directly on the repo's actual exemplar —
nothing invented. Read the real files for the full picture:

- **Logic + helper string:** [`src/kicad_mcp/utils/geometry.py`](../src/kicad_mcp/utils/geometry.py) —
  `clearance_violation()` (the Python function) and `GEOMETRY_HELPER` (the
  injectable string, byte-equivalent minus type hints) live side by side,
  with a module docstring explaining the dict/tuple rect formats and the
  non-strict (touching-counts-as-violating) semantics that make KiCad DRC
  parity possible.
- **Thin shell that splices it in:** [`src/kicad_mcp/tools/pcb_silkscreen.py`](../src/kicad_mcp/tools/pcb_silkscreen.py),
  `_op_check_silkscreen_overlaps()`. The embedded script does
  `""" + GEOMETRY_HELPER + """` right after `board = pcbnew.LoadBoard(...)`,
  marshals footprint/pad bounding boxes into plain `(x_min, y_min, x_max,
  y_max)` tuples, and calls `aabb_overlap(si_bb, pad_bb)` — the helper — for
  every silkscreen/pad pair. No overlap arithmetic appears in the script
  string itself.
- **The exec'd boundary test:** [`tests/test_geometry.py`](../tests/test_geometry.py),
  `TestGeometryHelperSource`. `test_helper_defines` parametrizes over every
  exported function name and asserts `"def {name}"` appears in
  `GEOMETRY_HELPER` — the drift guard. `test_helper_clearance_violation_identical`
  execs the helper and compares its output to the Python
  `clearance_violation()` at touching / positive-clearance / exactly-at-the-
  threshold / well-separated cases — the actual boundary coverage.

## Where the helper lives: `utils/` vs. tool-local

`GEOMETRY_HELPER` lives in `utils/geometry.py` because three tool modules
splice it in (`pcb_silkscreen.py`, `pcb_drc_fix.py`, `pcb_keepout.py`, via
`utils/keepout_helpers.py`'s `KEEPOUT_HELPER`) — a shared primitive belongs
in `utils/` so every consumer imports the same source.

A helper used by exactly one tool module doesn't need to move to `utils/`
first. `PAD_GAP_HELPER`/`pad_signed_gap` in
[`src/kicad_mcp/tools/pcb_keepout.py`](../src/kicad_mcp/tools/pcb_keepout.py)
is defined directly in that file — it's deliberately *not* a call to
`utils.geometry.signed_gap_mm`, because for a pad nested inside another pad
the two report different (and both correct, for their own purpose) numbers:
`signed_gap_mm` reports the move-to-clear distance, `pad_signed_gap` reports
the overlap extent a pad-clearance report actually wants. That divergence is
pinned by a test, not silently merged — see the comment above
`PAD_GAP_HELPER`'s definition. The requirement is that the logic is a real,
importable, testable Python function with an exec'able string twin — not
that it lives in any particular directory.

## Variant: no splice needed — pure post-processing on the result

Not every boundary-logic extraction needs a `*_HELPER` string at all. If the
decision only needs data the script *already returns* — not a live `pcbnew`
object — write it as an ordinary Python function that runs in the parent
process on the parsed JSON result, after `run_pcbnew_script` returns. No
concatenation, no `exec`, just a normal function you can unit test directly.

Exemplar: `_truncate_violations()` in
[`src/kicad_mcp/tools/pcb_keepout.py`](../src/kicad_mcp/tools/pcb_keepout.py)
caps the user-facing `violations` list at `_PAD_VIOLATION_CAP` (50) and sets
`violations_truncated` by comparing the *true* count against that cap. The
embedded script returns the full list plus a count; the cap and the strict
`count > cap` boundary are applied afterward, entirely in-process:

```python
def _truncate_violations(result: dict, cap: int = _PAD_VIOLATION_CAP) -> dict:
    if "violations" not in result:
        return result
    count = result.get("violation_count", len(result["violations"]))
    result["violations"] = result["violations"][:cap]
    result["violations_truncated"] = count > cap
    return result
```

Tested directly, no mock, no `exec`, at exactly the cap in both directions —
[`tests/test_pad_clearances.py`](../tests/test_pad_clearances.py)'s
`test_truncate_violations_boundary`:

```python
r49 = _truncate_violations(result(49))
assert r49["violations_truncated"] is False and len(r49["violations"]) == 49
r50 = _truncate_violations(result(50))  # at the cap: `>` is strict -> not truncated
assert r50["violations_truncated"] is False and len(r50["violations"]) == 50
r51 = _truncate_violations(result(51))
assert r51["violations_truncated"] is True and len(r51["violations"]) == 50
```

Prefer this variant whenever the logic doesn't need to touch a live
`pcbnew` object — it's strictly simpler than a `*_HELPER` string (no
concatenation, no `exec`, ordinary imports and ordinary test calls).

## Checklist before finishing boundary-op work

```
[ ] Does the embedded script contain any comparison, threshold, or
    classification that isn't a call to an injected helper function?
    -> extract it.
[ ] Can the decision be made entirely from the script's already-returned
    JSON, without a live pcbnew object? -> use the post-processing variant,
    skip the HELPER string.
[ ] Does a *_HELPER string exist for every helper function, kept
    byte-equivalent (minus type hints) to its Python twin?
[ ] Is there a test asserting `"def {name}" in MY_HELPER` for every
    exported function, so a rename/drop can't silently NameError inside
    KiCad's Python?
[ ] Does a test `exec(MY_HELPER, namespace)` and compare its output to the
    Python function at value / just-below / just-above every threshold?
[ ] Run `python scripts/audit_testability.py` -- does it report this as new
    helperless boundary logic or a new tautological boundary test? If a
    finding is pre-existing and out of scope, it's already grandfathered in
    `scripts/testability_baseline.json`; don't chase it just because you
    touched the file.
```
