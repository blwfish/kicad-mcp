"""Regression tests for the pcbnew.LoadBoard() None-check sweep.

Found via LLM-driven MCP testing (LM Studio, non-Claude local models): a
hallucinating model used a literal `"<new_pcb_path>"` placeholder as a real
path argument. Triage first suspected the `<`/`>` characters, but isolating
it precisely (a well-formed board at a bracketed path loaded FINE) showed
the real cause is the missing `.kicad_pcb` extension -- `pcbnew.LoadBoard()`
selects its IO plugin by file extension internally, and returns None
(its only documented failure mode) rather than raising when it can't
resolve one. None of the ~50 embedded scripts that call it checked for this
before using the result, so any wrong-extension or truncated path (a much
more mundane, realistic mistake than the triggering placeholder) surfaces
as a confusing `AttributeError: 'NoneType' object has no attribute
'GetFootprints'` wrapped in an unrelated wxApp/traits assertion, instead of
a clean error.

Part 1 is a static completeness check (no KiCad needed): every call site
must have the guard, so a new script added later can't silently reintroduce
the gap. Part 2 is a real, KiCad-backed reproduction of the actual isolated
cause.
"""

import re
from pathlib import Path

import pytest

from kicad_mcp.tools.pcb_board import _op_create
from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script

from .conftest import pcbnew_available

TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "kicad_mcp" / "tools"

_LOADBOARD_LINE = re.compile(r"^board = pcbnew\.LoadBoard\(.+\)$")


def _loadboard_call_sites():
    """Yield (file, lineno, line, next_nonblank_line) for every
    `board = pcbnew.LoadBoard(...)` line across all tool modules."""
    for path in sorted(TOOLS_DIR.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if _LOADBOARD_LINE.match(line):
                next_line = lines[i + 1] if i + 1 < len(lines) else ""
                yield path.name, i + 1, line, next_line


class TestEveryLoadBoardCallIsGuarded:
    """Static completeness check -- catches a future call site that forgets
    the None-check, the same way test_geometry.py's TestGeometryHelperSource
    catches a GEOMETRY_HELPER/Python drift."""

    def test_at_least_one_call_site_exists(self):
        """Sanity check the scanner itself isn't silently finding nothing."""
        sites = list(_loadboard_call_sites())
        assert len(sites) >= 40, (
            f"Expected 40+ pcbnew.LoadBoard() call sites across tools/, "
            f"found {len(sites)} -- did the scan pattern break?"
        )

    @pytest.mark.parametrize(
        "file_name,lineno,line,next_line",
        [
            pytest.param(f, ln, ln_text, nxt, id=f"{f}:{ln}")
            for f, ln, ln_text, nxt in _loadboard_call_sites()
        ],
    )
    def test_call_site_checks_for_none(self, file_name, lineno, line, next_line):
        assert next_line.strip() == "if board is None:", (
            f"{file_name}:{lineno} — `{line}` is not immediately followed by "
            f"a `board is None` guard (found: {next_line!r}). "
            f"pcbnew.LoadBoard() returns None rather than raising on failure; "
            f"unguarded, this surfaces as a confusing AttributeError deep in "
            f"the script instead of a clean error."
        )


@pytest.mark.requires_kicad
class TestLoadBoardNoneReproducesCleanly:
    """Real KiCad-backed reproduction of the original bug report."""

    @pytest.fixture(autouse=True)
    def skip_if_unavailable(self):
        if not pcbnew_available():
            pytest.skip("pcbnew not importable under KiCad's Python")

    _LOAD_SCRIPT = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
pcb_path = params["pcb_path"]

board = pcbnew.LoadBoard(pcb_path)
if board is None:
    print(json.dumps({"error": "Failed to load board: " + str(pcb_path)}))
    sys.exit(0)

print(json.dumps({"status": "ok"}))
"""

    def test_angle_brackets_in_path_are_not_the_actual_cause(self, tmp_path):
        """Negative control, kept as documentation: triage first suspected
        the `<`/`>` characters in a hallucinated `"<new_pcb_path>"`
        placeholder. They are not the cause -- a well-formed board at a
        bracketed path, WITH the correct extension, loads fine. Guards
        against re-introducing that wrong hypothesis in a future comment or
        commit message."""
        good_path = str(tmp_path / "<new_pcb_path>.kicad_pcb")
        create_result = _op_create(good_path)
        assert create_result.get("status") == "ok", create_result

        result = run_pcbnew_script(self._LOAD_SCRIPT, params={"pcb_path": good_path})
        assert result.get("status") == "ok", (
            f"Expected brackets-with-correct-extension to load fine, got: {result}"
        )

    def test_missing_extension_reproduces_the_actual_cause(self, tmp_path):
        """Isolated root cause: pcbnew.LoadBoard() selects its IO plugin by
        file extension internally and returns None (rather than raising)
        when it can't resolve one -- confirmed by comparing an identical,
        well-formed board (written by the real _op_create) at a
        `.kicad_pcb`-suffixed path (loads fine) against the same board at a
        path with the extension stripped (returns None). A wrong/missing
        extension is a far more mundane, realistic mistake than the
        triggering hallucinated placeholder happened to be."""
        no_ext_path = str(tmp_path / "board_without_extension")
        create_result = _op_create(no_ext_path)
        assert create_result.get("status") == "ok", create_result

        result = run_pcbnew_script(self._LOAD_SCRIPT, params={"pcb_path": no_ext_path})
        assert "error" in result, f"Expected a clean error, got: {result}"
        assert "Failed to load board" in result["error"]
