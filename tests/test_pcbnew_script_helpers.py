"""
Meta-test for the bug CLASS behind the pcb_drc_fix.py silkscreen NameError.

The bug: an embedded pcbnew script (run in a subprocess via run_pcbnew_script)
called ``aabb_overlap``/``aabb_inside`` but never concatenated ``GEOMETRY_HELPER``
into its source, so those names were undefined in the subprocess and the script
raised NameError on its first real execution. It stayed hidden because the unit
tests mock run_pcbnew_script, so the embedded string was never actually run.

This test catches the whole class statically (no pcbnew needed): for every
``run_pcbnew_script(<arg>)`` call in the package, gather the string-literal text
of ``<arg>`` and the set of Name nodes it references. If the literal text CALLS a
GEOMETRY_HELPER primitive but the arg neither injects ``GEOMETRY_HELPER`` nor
defines the primitive inline, that's a latent NameError waiting to fire.
"""

import ast
import re
from pathlib import Path

import kicad_mcp
from kicad_mcp.utils.geometry import GEOMETRY_HELPER

PKG_ROOT = Path(kicad_mcp.__file__).resolve().parent

# Primitives GEOMETRY_HELPER provides — derived from the helper source itself so
# a newly added helper function is covered automatically (single source of truth).
HELPER_PRIMITIVES = sorted(set(re.findall(r"(?m)^def\s+(\w+)\s*\(", GEOMETRY_HELPER)))


def _iter_run_pcbnew_calls():
    """Yield (path, lineno, literal_text, referenced_names) for each call."""
    for path in PKG_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError:  # pragma: no cover - package must parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            fname = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if fname != "run_pcbnew_script" or not node.args:
                continue
            arg = node.args[0]
            literals: list[str] = []
            names: set[str] = set()
            for n in ast.walk(arg):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    literals.append(n.value)
                elif isinstance(n, ast.Name):
                    names.add(n.id)
            yield path, node.lineno, "".join(literals), names


def test_geometry_helper_primitives_discovered():
    """Sanity: GEOMETRY_HELPER's function names were actually parsed out."""
    assert "aabb_overlap" in HELPER_PRIMITIVES
    assert "aabb_inside" in HELPER_PRIMITIVES


def test_embedded_scripts_inject_helpers_they_use():
    """No embedded pcbnew script may call a helper primitive it doesn't provide."""
    violations: list[str] = []
    blocks_checked = 0
    primitive_uses = 0

    for path, lineno, text, names in _iter_run_pcbnew_calls():
        blocks_checked += 1
        for prim in HELPER_PRIMITIVES:
            if not re.search(rf"\b{prim}\s*\(", text):
                continue  # this primitive isn't called in this script
            # A "def <prim>(" in the literal is the inline definition, not a use.
            if re.search(rf"\bdef\s+{prim}\s*\(", text):
                continue
            primitive_uses += 1
            injected = "GEOMETRY_HELPER" in names
            if not injected:
                rel = path.relative_to(PKG_ROOT.parent)
                violations.append(
                    f"{rel}:{lineno} embedded script calls {prim}() but does not "
                    f"inject GEOMETRY_HELPER (referenced names: {sorted(names)})"
                )

    assert blocks_checked > 0, "no run_pcbnew_script calls found — test is mis-targeted"
    assert primitive_uses > 0, (
        "no embedded script uses a GEOMETRY_HELPER primitive — the rule is "
        "vacuous; check HELPER_PRIMITIVES extraction"
    )
    assert not violations, (
        "embedded pcbnew scripts use GEOMETRY_HELPER primitives without injecting "
        "the helper (latent NameError in subprocess):\n  " + "\n  ".join(violations)
    )
