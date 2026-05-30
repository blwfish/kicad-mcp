"""Unit tests for the Stage-1 testability audit (scripts/audit_testability.py).

The detector enforces "pure logic must be reachable in-process"; it must obey
its own rule, so it is a set of pure ``str -> list`` functions tested here with
crafted inputs (no file I/O, no KiCad). Coverage is boundary-aware: for each
detector we test just-inside / just-outside its decision line and the ambiguous
inputs (helper present, no logic, different value, key-not-configured).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audit_testability as at  # noqa: E402


# --- detector 1: helperless boundary logic -----------------------------------

# A boundary module: ships a pcbnew script string with control flow, no *_HELPER.
HELPERLESS = '''
def add_zone(pcb_path, layer):
    script = """
import pcbnew
board = pcbnew.LoadBoard(pcb_path)
if board.GetLayerID(layer) < 0:
    print("bad layer")
board.Save(pcb_path)
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})
'''

# Same logic, but the module references a *_HELPER → considered adopted.
WITH_HELPER = '''
_GEO_HELPER = GEOMETRY_HELPER

def add_zone(pcb_path, layer):
    script = _GEO_HELPER + """
import pcbnew
if board.GetLayerID(layer) < 0:
    print("bad layer")
"""
    return run_pcbnew_script(script, params={})
'''

# Boundary module, but the shipped script has NO decision logic (just save).
NO_LOGIC = '''
def save(pcb_path):
    script = """
import pcbnew
board = pcbnew.LoadBoard(pcb_path)
board.Save(pcb_path)
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})
'''

# Not a boundary module at all (pure helper that lives in-process).
NOT_BOUNDARY = '''
def clearance_violation(gap, min_cl):
    return gap < min_cl
'''


def test_helperless_module_is_flagged():
    v = at.find_helperless_boundary_logic(HELPERLESS)
    assert len(v) == 1
    assert v[0].lineno > 0 and not v[0].parse_error
    # snippet is the first meaningful (non-import) line, for human context
    assert v[0].snippet and not v[0].snippet.startswith(("import", "from"))


def test_module_with_helper_is_exempt():
    # Same pcbnew logic, but a *_HELPER reference marks it as adopted.
    assert at.find_helperless_boundary_logic(WITH_HELPER) == []


def test_boundary_script_without_logic_is_not_flagged():
    # No if/for/while in the script → nothing pure to extract → not a violation.
    assert at.find_helperless_boundary_logic(NO_LOGIC) == []


def test_non_boundary_module_is_not_flagged():
    assert at.find_helperless_boundary_logic(NOT_BOUNDARY) == []


def test_helperless_detects_logic_in_fstring_script():
    # f-string script strings are common (values injected); literal parts still
    # carry the control flow and must be detected.
    src = (
        'def f(p):\n'
        '    script = f"""\n'
        'import pcbnew\n'
        'board = pcbnew.LoadBoard(p)\n'
        'if board.GetX() < {p}:\n'
        '    pass\n'
        '"""\n'
        '    return run_pcbnew_script(script, params={})\n'
    )
    v = at.find_helperless_boundary_logic(src)
    assert len(v) == 1


def test_helperless_syntax_error_is_surfaced_not_dropped():
    v = at.find_helperless_boundary_logic("def broken(:\n    pass")
    assert len(v) == 1 and v[0].parse_error  # data-capture: never silently empty


# --- detector 2: tautological boundary tests ---------------------------------

# Canonical tautology: patches the boundary, configures a scalar, asserts it back.
TAUTO_IS = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_truncation(mock_run):
    mock_run.return_value = {"violations_truncated": True, "violation_count": 75}
    result = fn("pad_clearances")
    assert result["violations_truncated"] is True
'''

TAUTO_EQ = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_count(mock_run):
    mock_run.return_value = {"violation_count": 75}
    result = fn("pad_clearances")
    assert result["violation_count"] == 75
'''

# Patches boundary, but asserts a value DIFFERENT from the one configured.
NOT_ECHO = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_real(mock_run):
    mock_run.return_value = {"violation_count": 75}
    result = fn("pad_clearances")
    assert result["violation_count"] == 0
'''

# Patches boundary, asserts on a key NOT in the configured dict (post-processed).
POST_PROCESSED = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_summary(mock_run):
    mock_run.return_value = {"violation_count": 75}
    result = fn("pad_clearances")
    assert result["summary"] == "75 violations"
'''

# Does not patch the boundary at all → out of scope for this detector.
NO_BOUNDARY = '''
def test_pure(mock_run):
    obj = {"x": 1}
    assert obj["x"] == 1
'''

# Patches boundary but never configures a return_value.
NO_RETURN = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_smoke(mock_run):
    result = fn("pad_clearances")
    assert result is not None
'''


@pytest.mark.parametrize("src,name", [(TAUTO_IS, "test_truncation"), (TAUTO_EQ, "test_count")])
def test_tautological_is_and_eq_are_flagged(src, name):
    v = at.find_tautological_tests(src)
    assert [t.test for t in v] == [name]


def test_different_value_is_not_tautological():
    assert at.find_tautological_tests(NOT_ECHO) == []


def test_key_not_in_config_is_not_flagged():
    # "summary" is asserted but was never configured on the mock → real assertion.
    assert at.find_tautological_tests(POST_PROCESSED) == []


def test_no_boundary_patch_is_out_of_scope():
    assert at.find_tautological_tests(NO_BOUNDARY) == []


def test_no_configured_return_is_not_flagged():
    assert at.find_tautological_tests(NO_RETURN) == []


# Patches boundary, asserts a CALL ARGUMENT (via a var bound to .call_args) — a
# legitimate test, even when the value coincides with a configured mock return.
CALLARGS_VAR = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_passes_clearance(mock_run):
    mock_run.return_value = {"min_clearance_mm": 0.5}
    fn("pad_clearances", min_clearance_mm=0.5)
    params = mock_run.call_args[1]["params"]
    assert params["min_clearance_mm"] == 0.5
'''

# Same, asserted inline straight off .call_args (no intermediate variable).
CALLARGS_DIRECT = '''
@patch("kicad_mcp.tools.pcb_keepout.run_pcbnew_script")
def test_passes_clearance_direct(mock_run):
    mock_run.return_value = {"min_clearance_mm": 0.5}
    fn("pad_clearances", min_clearance_mm=0.5)
    assert mock_run.call_args[1]["params"]["min_clearance_mm"] == 0.5
'''


@pytest.mark.parametrize("src", [CALLARGS_VAR, CALLARGS_DIRECT])
def test_call_arg_assertions_are_not_tautological(src):
    # Asserting what the SUT was CALLED WITH (params / call_args) reads call
    # arguments, not the result — so it is not a tautological echo even if the
    # asserted value matches a configured return. (Detector precision fix.)
    assert at.find_tautological_tests(src) == []


# --- detector 3: subprocess output parsed inline -----------------------------

# Spawns a subprocess AND parses its output inline with an extraction loop.
WELDED_SUBPROC = '''
import subprocess, json
def extract(path):
    out = subprocess.run(["tool"], capture_output=True, text=True)
    data = json.loads(out.stdout)
    rows = []
    for item in data["items"]:
        rows.append({"id": item["id"]})
    return rows
'''

# Spawns, but hands the raw text to a pure _parse(); neither function both
# spawns AND parses-with-a-loop, so neither is flagged.
DELEGATING_SUBPROC = '''
import subprocess
def extract(path):
    out = subprocess.run(["tool"], capture_output=True, text=True)
    return _parse(out.stdout)

def _parse(text):
    import json
    data = json.loads(text)
    return [{"id": item["id"]} for item in data["items"]]
'''

# Pure parser with a loop but no subprocess — the GOOD shape (testable in-process).
PURE_PARSER_ONLY = '''
def _parse(text):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(text)
    out = []
    for comp in root.findall("comp"):
        out.append(comp.get("ref"))
    return out
'''


def test_subprocess_welded_parsing_is_flagged():
    v = at.find_helperless_subprocess_parsing(WELDED_SUBPROC)
    assert [s.func for s in v] == ["extract"]


def test_subprocess_delegating_to_pure_parser_is_not_flagged():
    # extract() spawns + delegates; _parse() parses but never spawns.
    assert at.find_helperless_subprocess_parsing(DELEGATING_SUBPROC) == []


def test_pure_parser_without_subprocess_is_not_flagged():
    assert at.find_helperless_subprocess_parsing(PURE_PARSER_ONLY) == []


def test_subprocess_parsing_syntax_error_is_surfaced():
    v = at.find_helperless_subprocess_parsing("def broken(:\n    pass")
    assert len(v) == 1 and v[0].parse_error


def test_tautological_syntax_error_is_surfaced():
    v = at.find_tautological_tests("def test_x(:\n pass")
    assert len(v) == 1 and v[0].parse_error


# --- ratchet / baseline behavior ---------------------------------------------

def _result(helperless_keys, tauto_keys, subproc_keys=()):
    return at.ScanResult(
        helperless={k: [at.BlockViolation(1, "x")] for k in helperless_keys},
        tautological={k: at.TautoViolation(k.split("::")[-1], 1, ["k"]) for k in tauto_keys},
        errors=[],
        subprocess_parsing={k: at.SubprocViolation(k.split("::")[-1], 1) for k in subproc_keys},
    )


def test_baseline_roundtrip_and_new_fixed(tmp_path):
    path = tmp_path / "baseline.json"
    at.write_baseline(_result({"a.py"}, {"t.py::test_a"}, {"m.py::extract"}), path)

    base_h, base_t, base_s = at.load_baseline(path)
    assert base_h == {"a.py"} and base_t == {"t.py::test_a"} and base_s == {"m.py::extract"}

    # b.py is new; a.py was fixed (no longer present).
    cur_h, _, _ = at.current_ids(_result({"b.py"}, {"t.py::test_a"}))
    assert sorted(cur_h - base_h) == ["b.py"]      # new
    assert sorted(base_h - cur_h) == ["a.py"]      # fixed


def test_missing_baseline_is_empty(tmp_path):
    assert at.load_baseline(tmp_path / "nope.json") == (set(), set(), set())


# --- integration: the detectors fire on the real tree ------------------------

def test_scan_finds_the_known_real_violations():
    """End-to-end sanity against the actual repo: the detectors fire on the real
    tree. pcb_zones is a known helperless module; tautological tests still exist
    elsewhere. Specific entries get retired over time, so assert the class is
    non-empty rather than pinning one test that may already be fixed."""
    root = Path(__file__).resolve().parents[1]
    result = at.scan(root)
    assert "src/kicad_mcp/tools/pcb_zones.py" in result.helperless
    assert result.tautological, "detector should still find tautological tests on the real tree"
