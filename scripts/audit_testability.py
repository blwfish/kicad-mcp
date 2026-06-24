#!/usr/bin/env python3
"""Testability audit — a mechanical net for write-time testability defects.

This is *Stage 1* of the three-layer reduction strategy for "logic that is
untestable by construction": a detector whose misses are independent of author
attention, so it catches what the write-time discipline lets slip.

It is deliberately a NET, not a GATE: it runs report-only (exit 0) by default,
and ratchets against a checked-in baseline so the ~50 pre-existing violations
are grandfathered and only the count of *new* ones can fail (under --check).

Two detectors, each a pure ``str -> list`` function (the detector must obey the
rule it enforces, so it is itself import-and-unit-testable with no I/O):

1. ``find_helperless_boundary_logic`` — a tool module that ships pcbnew decision
   logic across the process boundary as a script string but has extracted no
   ``*_HELPER`` constant, so the logic has no in-process unit a test could reach.
   The canonical *good* shape it is looking for is ``utils/geometry.py``'s
   ``GEOMETRY_HELPER`` (a helper string both injected into the boundary script
   AND ``exec``'d directly in a no-KiCad test).

2. ``find_tautological_tests`` — a test that patches the boundary
   (``run_pcbnew_script``), configures a mock return value, and then asserts a
   scalar straight back out of that configured value. Such a test cannot fail:
   the real logic lives in the mocked-away subprocess.

Usage:
    python scripts/audit_testability.py                 # report-only (exit 0)
    python scripts/audit_testability.py --check         # gate: exit 1 on NEW
    python scripts/audit_testability.py --update-baseline
    python scripts/audit_testability.py --json
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# --- detector configuration --------------------------------------------------

#: Names that, when called/patched, mark a process-boundary crossing whose pure
#: core ought to be extracted into an in-process-testable helper.
BOUNDARY_NAMES: tuple[str, ...] = ("run_pcbnew_script",)

#: A module that references any ``*_HELPER`` identifier is treated as having
#: adopted the extraction pattern (precision-biased: we accept false negatives
#: on modules that extract *some* logic but still inline other blocks).
_HELPER_RE = re.compile(r"\b\w*_HELPER\b")

#: Control-flow at statement start inside a script string == extractable
#: decision logic. Keyed on keywords only (not bare operators) to avoid
#: false positives from ``->`` annotations, dict-building, f-string format, etc.
_LOGIC_LINE_RE = re.compile(r"^[ \t]*(if|elif|for|while)\b")

#: A string is treated as actual pcbnew code only if it accesses a pcbnew
#: attribute (``pcbnew.LoadBoard`` etc.) — the bare word "pcbnew" also appears
#: in prose/docstrings, which we must not flag.
_PCBNEW_MARKER = "pcbnew."

_NO = object()  # sentinel: "operand is not a scalar literal"


# --- violation records -------------------------------------------------------

@dataclass
class BlockViolation:
    lineno: int
    snippet: str
    parse_error: bool = False


@dataclass
class TautoViolation:
    test: str
    lineno: int
    keys: list[str] = field(default_factory=list)
    parse_error: bool = False


@dataclass
class SubprocViolation:
    func: str
    lineno: int
    parse_error: bool = False


# --- small AST helpers -------------------------------------------------------

def _string_payload(node: ast.AST) -> tuple[str | None, int]:
    """Return (text, lineno) for a string literal node, joining the constant
    parts of an f-string. (None, 0) for anything else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, getattr(node, "lineno", 0)
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        if parts:
            return "".join(parts), getattr(node, "lineno", 0)
    return None, 0


def _has_logic(text: str) -> bool:
    """True if the script contains control flow (extractable decision logic)."""
    return any(_LOGIC_LINE_RE.match(line) for line in text.splitlines())


def _first_logic_line(text: str, width: int = 88) -> str:
    """The first control-flow line (what actually triggered detection), so the
    report points at the logic — not the script's json-loads preamble."""
    for raw in text.splitlines():
        if _LOGIC_LINE_RE.match(raw):
            return raw.strip()[:width]
    for raw in text.splitlines():  # fallback: first meaningful line
        line = raw.strip()
        if line and not line.startswith(("import ", "from ", "#")):
            return line[:width]
    return "<no logic line>"


def _scalar_literal(node: ast.AST):
    """The Python value of ``node`` if it is a scalar literal (incl. negative
    numbers, bool, None, str), else the ``_NO`` sentinel."""
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (bool, int, float, str, type(None))
    ):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, ast.USub)
        and isinstance(node.operand, ast.Constant)
        and isinstance(node.operand.value, (int, float))
    ):
        return -node.operand.value
    return _NO


#: Mock attributes that expose what the SUT was CALLED WITH, not its result.
#: A subscript rooted at one of these (or at a var assigned from one) reads call
#: arguments — asserting those is legitimate, not a tautological echo.
_CALLARGS_ATTRS = ("call_args", "call_args_list")


def _callargs_derived_names(fn: ast.AST) -> set[str]:
    """Local names assigned from an expression that touches ``.call_args`` — e.g.
    ``params = mock_run.call_args[1]["params"]``."""
    names: set[str] = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and any(
            isinstance(a, ast.Attribute) and a.attr in _CALLARGS_ATTRS
            for a in ast.walk(n.value)
        ):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    return names


def _reads_call_args(sub: ast.Subscript, callargs_names: set[str]) -> bool:
    """True if the subscript chain roots at a call-args var or contains a
    ``.call_args`` attribute — i.e. it reads call arguments, not the SUT result."""
    node: ast.AST = sub
    while True:
        if isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Attribute):
            if node.attr in _CALLARGS_ATTRS:
                return True
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Name):
            return node.id in callargs_names
        else:
            return False  # unknown root — treat as a result read (conservative)


def _result_subscript_keys(node: ast.AST, callargs_names: set[str]) -> list[str]:
    """String subscript keys reachable inside ``node`` that read the SUT RESULT
    (e.g. ``result["a"]`` -> "a"), excluding subscripts that read call arguments
    (``params["a"]`` / ``mock.call_args[...]["a"]``)."""
    keys: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Subscript):
            sl = n.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                if not _reads_call_args(n, callargs_names):
                    keys.append(sl.value)
    return keys


def _compare_pairs(compare: ast.Compare, callargs_names: set[str]) -> list[tuple[str, object]]:
    """(result_subscript_key, scalar_literal) candidate pairs across the operands
    of a comparison — one operand supplies the accessed key, the other the literal.
    Call-argument reads are excluded (they are not tautological echoes)."""
    operands = [compare.left, *compare.comparators]
    keys: list[str] = []
    lits: list[object] = []
    for op in operands:
        keys.extend(_result_subscript_keys(op, callargs_names))
        lit = _scalar_literal(op)
        if lit is not _NO:
            lits.append(lit)
    return [(k, v) for k in keys for v in lits]


def _function_span(source: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Full source of a function INCLUDING its decorators (get_source_segment
    drops decorators, where ``@patch(...)`` usually lives)."""
    lines = [fn.lineno] + [d.lineno for d in fn.decorator_list]
    start = min(lines)
    end = fn.end_lineno or fn.lineno
    return "\n".join(source.splitlines()[start - 1:end])


@dataclass
class _Cfg:
    known: bool
    value: object


def _configured_return_values(fn: ast.AST) -> dict[str, _Cfg]:
    """Map of {key: _Cfg} from ``<mock>.return_value = {dict}`` assignments
    inside ``fn``. ``known`` is False when the dict value is not a scalar."""
    cfg: dict[str, _Cfg] = {}
    for n in ast.walk(fn):
        if not isinstance(n, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Attribute) and t.attr == "return_value"
            for t in n.targets
        ):
            continue
        if not isinstance(n.value, ast.Dict):
            continue
        for k, v in zip(n.value.keys, n.value.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                lit = _scalar_literal(v)
                cfg[k.value] = _Cfg(known=lit is not _NO, value=None if lit is _NO else lit)
    return cfg


def _assertions_echoing_config(fn: ast.AST, cfg: dict[str, _Cfg]) -> list[str]:
    """Keys whose configured scalar value an ``assert`` reads straight back out of
    the SUT result (call-argument assertions are excluded)."""
    callargs_names = _callargs_derived_names(fn)
    hits: list[str] = []
    for a in ast.walk(fn):
        if not isinstance(a, ast.Assert):
            continue
        for cmp in (n for n in ast.walk(a.test) if isinstance(n, ast.Compare)):
            for key, lit in _compare_pairs(cmp, callargs_names):
                c = cfg.get(key)
                if c and c.known and type(c.value) is type(lit) and c.value == lit:
                    hits.append(key)
    return hits


# --- detector 1: helperless boundary logic -----------------------------------

def find_helperless_boundary_logic(source: str) -> list[BlockViolation]:
    """Pcbnew decision-logic blocks shipped across the boundary by a module that
    has extracted no ``*_HELPER``. Empty if the module is not a boundary module,
    or already references a helper."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [BlockViolation(lineno=e.lineno or 0, snippet=f"<syntax-error: {e.msg}>", parse_error=True)]

    if _HELPER_RE.search(source):
        return []  # module already extracts a *_HELPER → considered adopted
    if "run_pcbnew_script" not in source:
        return []  # not a boundary module

    # An f-string script is one block, but ast.walk also visits the Constant
    # parts *inside* its JoinedStr — collect those so we don't double-count.
    inner_const_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                if isinstance(child, ast.Constant):
                    inner_const_ids.add(id(child))

    out: list[BlockViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) in inner_const_ids:
            continue  # already represented by its enclosing JoinedStr
        text, lineno = _string_payload(node)
        if text is None:
            continue
        if _PCBNEW_MARKER in text and _has_logic(text):
            out.append(BlockViolation(lineno=lineno, snippet=_first_logic_line(text)))
    return out


# --- detector 2: tautological boundary tests ---------------------------------

def find_tautological_tests(
    source: str, boundary_names: Iterable[str] = BOUNDARY_NAMES
) -> list[TautoViolation]:
    """Test functions that patch the boundary, configure a mock return value,
    and assert a scalar straight back out of it (so they cannot fail)."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [TautoViolation(test="<syntax-error>", lineno=e.lineno or 0, keys=[e.msg or ""], parse_error=True)]

    names = tuple(boundary_names)
    out: list[TautoViolation] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not fn.name.startswith("test"):
            continue
        if not any(b in _function_span(source, fn) for b in names):
            continue
        cfg = _configured_return_values(fn)
        if not cfg:
            continue
        hits = _assertions_echoing_config(fn, cfg)
        if hits:
            out.append(TautoViolation(test=fn.name, lineno=fn.lineno, keys=sorted(set(hits))))
    return out


# --- detector 3: subprocess output parsed inline (no pure extractor) ---------

#: A child process is spawned (its output then parsed inline).
_SUBPROCESS_SPAWNS = (
    "subprocess.run(", "subprocess.Popen(", "subprocess.check_output(",
    "subprocess.check_call(", "subprocess.call(",
)
#: Inline parsing of that external output (the field-capture logic). "json.load("
#: (file form) is distinct from "json.loads(" — both are listed; the substring
#: "json.load(" does not match "json.loads(" (the char after "load" is "s").
_INLINE_PARSE = (
    "ET.parse(", "ET.fromstring(", "ElementTree.parse(", "ElementTree.fromstring(",
    "json.loads(", "json.load(", ".findall(", ".iterfind(",
)


def find_helperless_subprocess_parsing(source: str) -> list[SubprocViolation]:
    """Functions that spawn a subprocess AND parse its output inline with an
    extraction loop — the field-capture logic has no in-process seam a test can
    reach (cf. extract_netlist_via_cli before _parse_kicadxml was split out). A
    function that hands the raw output to a pure ``_parse_*`` helper (so it has no
    inline parse + loop of its own) is NOT flagged."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [SubprocViolation(func="<syntax-error>", lineno=e.lineno or 0, parse_error=True)]

    out: list[SubprocViolation] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        fnsrc = _function_span(source, fn)
        if not any(p in fnsrc for p in _SUBPROCESS_SPAWNS):
            continue
        if not any(p in fnsrc for p in _INLINE_PARSE):
            continue
        if not any(isinstance(n, (ast.For, ast.AsyncFor)) for n in ast.walk(fn)):
            continue  # no extraction loop -> nothing to extract
        out.append(SubprocViolation(func=fn.name, lineno=fn.lineno))
    return out


# --- repository scan ---------------------------------------------------------

@dataclass
class ScanResult:
    helperless: dict[str, list[BlockViolation]]      # relpath -> blocks
    tautological: dict[str, TautoViolation]          # "relpath::test" -> v
    errors: list[tuple[str, str]]                    # (relpath, reason)
    subprocess_parsing: dict[str, SubprocViolation] = field(default_factory=dict)  # "relpath::func" -> v


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def scan(root: Path) -> ScanResult:
    helperless: dict[str, list[BlockViolation]] = {}
    tautological: dict[str, TautoViolation] = {}
    subprocess_parsing: dict[str, SubprocViolation] = {}
    errors: list[tuple[str, str]] = []

    src_root = root / "src" / "kicad_mcp"
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        text = _read(path)
        if text is None:
            errors.append((rel, "could not read file"))
            continue
        blocks = find_helperless_boundary_logic(text)
        real = [b for b in blocks if not b.parse_error]
        for b in (b for b in blocks if b.parse_error):
            errors.append((rel, b.snippet))
        if real:
            helperless[rel] = real
        for sv in find_helperless_subprocess_parsing(text):
            if not sv.parse_error:  # parse errors already recorded above
                subprocess_parsing[f"{rel}::{sv.func}"] = sv

    test_root = root / "tests"
    for path in sorted(test_root.rglob("*.py")):
        if "integration" in path.parts:
            continue  # integration tests use real KiCad, not boundary mocks
        rel = path.relative_to(root).as_posix()
        text = _read(path)
        if text is None:
            errors.append((rel, "could not read file"))
            continue
        for tv in find_tautological_tests(text):
            if tv.parse_error:
                errors.append((rel, f"<syntax-error: {tv.keys[0] if tv.keys else ''}>"))
                continue
            tautological[f"{rel}::{tv.test}"] = tv

    return ScanResult(helperless, tautological, errors, subprocess_parsing)


# --- baseline (ratchet) ------------------------------------------------------

BASELINE_PATH = Path(__file__).resolve().parent / "testability_baseline.json"
_BASELINE_NOTE = (
    "Grandfathered testability violations (Stage-1 audit). Run "
    "`python scripts/audit_testability.py --update-baseline` to refresh after "
    "fixing or intentionally adding. The count may only fall: --check fails on "
    "any violation NOT listed here."
)


def current_ids(result: ScanResult) -> tuple[set[str], set[str], set[str]]:
    return set(result.helperless), set(result.tautological), set(result.subprocess_parsing)


def load_baseline(path: Path = BASELINE_PATH) -> tuple[set[str], set[str], set[str]]:
    if not path.exists():
        return set(), set(), set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return (
        set(data.get("helperless_boundary_logic", [])),
        set(data.get("tautological_tests", [])),
        set(data.get("helperless_subprocess_parsing", [])),
    )


def write_baseline(result: ScanResult, path: Path = BASELINE_PATH) -> None:
    helperless, tautological, subproc = current_ids(result)
    payload = {
        "version": 1,
        "note": _BASELINE_NOTE,
        "helperless_boundary_logic": sorted(helperless),
        "tautological_tests": sorted(tautological),
        "helperless_subprocess_parsing": sorted(subproc),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# --- CLI ---------------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Testability audit (Stage-1 net).")
    ap.add_argument("--check", action="store_true",
                    help="gate mode: exit 1 if any NEW (non-baseline) violation exists")
    ap.add_argument("--update-baseline", action="store_true",
                    help="rewrite the baseline to the current violation set")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--root", type=Path, default=_repo_root(), help="repo root")
    args = ap.parse_args(argv)

    result = scan(args.root)
    cur_h, cur_t, cur_s = current_ids(result)

    if args.update_baseline:
        write_baseline(result)
        print(f"baseline updated: {len(cur_h)} helperless modules, "
              f"{len(cur_t)} tautological tests, {len(cur_s)} inline subprocess "
              f"parsers -> {BASELINE_PATH.name}")
        return 0

    base_h, base_t, base_s = load_baseline()
    new_h, fixed_h = sorted(cur_h - base_h), sorted(base_h - cur_h)
    new_t, fixed_t = sorted(cur_t - base_t), sorted(base_t - cur_t)
    new_s, fixed_s = sorted(cur_s - base_s), sorted(base_s - cur_s)

    if args.json:
        print(json.dumps({
            "current": {"helperless": sorted(cur_h), "tautological": sorted(cur_t),
                        "subprocess_parsing": sorted(cur_s)},
            "new": {"helperless": new_h, "tautological": new_t, "subprocess_parsing": new_s},
            "fixed": {"helperless": fixed_h, "tautological": fixed_t, "subprocess_parsing": fixed_s},
            "errors": result.errors,
        }, indent=2))
    else:
        _print_report(result, cur_h, cur_t, cur_s, base_h, base_t, base_s,
                      new_h, new_t, new_s, fixed_h, fixed_t, fixed_s)

    if args.check:
        return 1 if (new_h or new_t or new_s) else 0
    return 0  # report-only: never blocks


def _print_report(result, cur_h, cur_t, cur_s, base_h, base_t, base_s,
                  new_h, new_t, new_s, fixed_h, fixed_t, fixed_s) -> None:
    print("Testability audit (report-only net)")
    print("=" * 52)
    print(f"  helperless boundary modules  : {len(cur_h):3d}  "
          f"(baseline {len(base_h)}, new {len(new_h)}, fixed {len(fixed_h)})")
    print(f"  tautological boundary tests  : {len(cur_t):3d}  "
          f"(baseline {len(base_t)}, new {len(new_t)}, fixed {len(fixed_t)})")
    print(f"  inline subprocess parsers    : {len(cur_s):3d}  "
          f"(baseline {len(base_s)}, new {len(new_s)}, fixed {len(fixed_s)})")
    if result.errors:
        print(f"  unparseable / unreadable     : {len(result.errors)} (see below)")

    if new_h:
        print("\nNEW helperless boundary logic (extract a *_HELPER, see "
              "utils/geometry.py:GEOMETRY_HELPER):")
        for rel in new_h:
            for b in result.helperless[rel]:
                print(f"  {rel}:{b.lineno}: {b.snippet}")
    if new_t:
        print("\nNEW tautological tests (asserts a value it configured on the "
              "boundary mock — cannot fail):")
        for key in new_t:
            tv = result.tautological[key]
            print(f"  {key} (line {tv.lineno}) echoes: {', '.join(tv.keys)}")
    if new_s:
        print("\nNEW inline subprocess parsing (extract a pure _parse_*(text), "
              "see netlist_parser.py:_parse_kicadxml):")
        for key in new_s:
            sv = result.subprocess_parsing[key]
            print(f"  {key} (line {sv.lineno}): spawns a subprocess and parses its output inline")
    if fixed_h or fixed_t or fixed_s:
        print("\nFIXED since baseline (run --update-baseline to ratchet down):")
        for rel in fixed_h:
            print(f"  [helperless] {rel}")
        for key in fixed_t:
            print(f"  [tautological] {key}")
        for key in fixed_s:
            print(f"  [subprocess] {key}")
    if result.errors:
        print("\nFiles that could not be analyzed (not silently dropped):")
        for rel, reason in result.errors:
            print(f"  {rel}: {reason}")

    if not (new_h or new_t or new_s):
        print("\nNo new violations vs baseline. (Net is report-only; exit 0.)")


if __name__ == "__main__":
    raise SystemExit(main())
