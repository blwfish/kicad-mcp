"""Tests for src/kicad_mcp/utils/path_validation.py.

Covers: strict-mode toggle, allowed-roots containment, ``..`` traversal,
tilde expansion, must_exist parameter, symlink following, and invalid input.

Design note: ``tempfile.gettempdir()`` is *always* in the allowed-roots list,
and pytest's ``tmp_path`` lives inside it.  Tests that need an "out-of-root"
path therefore also patch ``tempfile.gettempdir`` (inside the module under
test) to return a fake directory that the test controls.
"""
from __future__ import annotations

import logging
import os
import tempfile
from unittest.mock import patch

import pytest

from kicad_mcp.utils.path_validation import validate_project_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _real_tmpdir() -> str:
    return os.path.realpath(tempfile.gettempdir())


def _isolated_roots(monkeypatch, tmp_path, *, fake_tmpdir: bool = True):
    """
    Set up isolated roots so that ``tmp_path`` is NOT automatically allowed.

    * ``KICAD_USER_DIR`` → ``tmp_path / "kicad_root"`` (created)
    * ``ADDITIONAL_SEARCH_PATHS`` → ``[]``
    * ``tempfile.gettempdir`` inside path_validation → ``tmp_path / "fake_tmp"``
      (created, and distinct from tmp_path itself)

    Returns a dict with keys ``kicad_root`` and ``fake_tmp`` (Path objects).
    """
    kicad_root = tmp_path / "kicad_root"
    kicad_root.mkdir()
    fake_tmp = tmp_path / "fake_tmp"
    fake_tmp.mkdir()

    monkeypatch.setattr("kicad_mcp.config.KICAD_USER_DIR", str(kicad_root))
    monkeypatch.setattr("kicad_mcp.config.ADDITIONAL_SEARCH_PATHS", [])

    if fake_tmpdir:
        monkeypatch.setattr(
            "kicad_mcp.utils.path_validation.tempfile.gettempdir",
            lambda: str(fake_tmp),
        )

    return {"kicad_root": kicad_root, "fake_tmp": fake_tmp}


# ---------------------------------------------------------------------------
# 1. Strict-mode toggle via KICAD_MCP_STRICT_PATHS
# ---------------------------------------------------------------------------

class TestStrictModeToggle:
    """Pin which env-var values trigger strict mode and which don't."""

    def _make_out_of_root_file(self, tmp_path, roots):
        """A real file that sits outside all configured roots."""
        outsider = tmp_path / "outsider"
        outsider.mkdir(exist_ok=True)
        f = outsider / "board.kicad_pro"
        f.write_text("")
        return f

    def test_unset_is_permissive(self, tmp_path, monkeypatch, caplog):
        """No env var → permissive; out-of-root path returns None + logs warning."""
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        with caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.path_validation"):
            result = validate_project_path(str(target))

        assert result is None, "permissive mode must return None for out-of-root path"
        assert any("outside" in r.message for r in caplog.records), \
            "warning must be emitted in permissive mode"

    def test_zero_is_permissive(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "0")
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        with caplog.at_level(logging.WARNING, logger="kicad_mcp.utils.path_validation"):
            result = validate_project_path(str(target))

        assert result is None, "'0' must be permissive"

    def test_one_is_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        result = validate_project_path(str(target))
        assert result is not None, "'1' must activate strict mode"
        assert "KICAD_SEARCH_PATHS" in result or "outside" in result.lower()

    def test_true_is_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "true")
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        assert validate_project_path(str(target)) is not None, "'true' must be strict"

    def test_yes_is_strict(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "yes")
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        assert validate_project_path(str(target)) is not None, "'yes' must be strict"

    def test_True_capitalized_is_strict(self, tmp_path, monkeypatch):
        """'True' → .lower() = 'true' → strict."""
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "True")
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        assert validate_project_path(str(target)) is not None, "'True' must be strict"

    def test_False_capitalized_is_permissive(self, tmp_path, monkeypatch):
        """'False' → .lower() = 'false' → NOT in {'1','true','yes'} → permissive.
        This pins the current behavior: the comparison uses .lower(), so 'False'
        is treated permissively even though it looks like a boolean False."""
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "False")
        roots = _isolated_roots(monkeypatch, tmp_path)
        target = self._make_out_of_root_file(tmp_path, roots)

        result = validate_project_path(str(target))
        # 'False'.lower() == 'false', not in {'1','true','yes'} → permissive
        assert result is None, \
            "'False' (capitalized) is permissive per current .lower() comparison"


# ---------------------------------------------------------------------------
# 2. Allowed-roots containment
# ---------------------------------------------------------------------------

class TestAllowedRootsContainment:

    def test_path_inside_fake_tmpdir_is_allowed_strict(self, tmp_path, monkeypatch):
        """Path inside the configured tempdir root is allowed in strict mode."""
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        roots = _isolated_roots(monkeypatch, tmp_path)
        # Place file inside fake_tmp, which IS in allowed roots
        project = roots["fake_tmp"] / "board.kicad_pro"
        project.write_text("")

        assert validate_project_path(str(project)) is None

    def test_path_inside_kicad_user_dir_allowed_strict(self, tmp_path, monkeypatch):
        """Path under KICAD_USER_DIR is allowed in strict mode."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        project = roots["kicad_root"] / "myproject" / "board.kicad_pro"
        project.parent.mkdir()
        project.write_text("")

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        assert validate_project_path(str(project)) is None

    def test_path_inside_additional_search_path_allowed_strict(
        self, tmp_path, monkeypatch
    ):
        """Path under ADDITIONAL_SEARCH_PATHS is allowed in strict mode."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        extra_root = tmp_path / "extra_projects"
        extra_root.mkdir()
        project = extra_root / "proj" / "board.kicad_pro"
        project.parent.mkdir()
        project.write_text("")

        monkeypatch.setattr("kicad_mcp.config.ADDITIONAL_SEARCH_PATHS", [str(extra_root)])
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        assert validate_project_path(str(project)) is None

    def test_path_not_in_any_root_strict_rejected(self, tmp_path, monkeypatch):
        """Path outside every root is rejected in strict mode."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        outsider = tmp_path / "outsider"
        outsider.mkdir()
        target = outsider / "board.kicad_pro"
        target.write_text("")

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(str(target))
        assert result is not None
        assert "KICAD_SEARCH_PATHS" in result or "outside" in result.lower()

    def test_path_not_in_any_root_permissive_allowed(self, tmp_path, monkeypatch):
        """Path outside every root returns None in permissive mode."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        outsider = tmp_path / "outsider2"
        outsider.mkdir()
        target = outsider / "board.kicad_pro"
        target.write_text("")

        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)

        assert validate_project_path(str(target)) is None

    def test_exact_root_match_allowed_strict(self, tmp_path, monkeypatch):
        """Path that IS exactly the root dir (not just a child) is allowed."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        # kicad_root exists and IS a root — pass it directly
        assert validate_project_path(str(roots["kicad_root"])) is None

    def test_prefix_collision_not_confused(self, tmp_path, monkeypatch):
        """A root '/tmp/abc' must NOT match a path '/tmp/abcdef/board'.
        The code appends os.sep before the startswith check, so 'abc' prefix
        must not accidentally accept 'abcdef'."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        # Name roots["kicad_root"] as "proj" and create a sibling "projext"
        proj = tmp_path / "proj"
        proj.mkdir()
        projext = tmp_path / "projext"
        projext.mkdir()
        target = projext / "board.kicad_pro"
        target.write_text("")

        monkeypatch.setattr("kicad_mcp.config.KICAD_USER_DIR", str(proj))
        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(str(target))
        assert result is not None, \
            "prefix match must not fire for sibling dir with longer name"


# ---------------------------------------------------------------------------
# 3. ``..`` traversal and tilde expansion
# ---------------------------------------------------------------------------

class TestTraversalAndExpansion:

    def test_dotdot_escape_strict_rejected(self, tmp_path, monkeypatch):
        """``/tmp/sub/../../etc/passwd`` resolves to /etc/passwd — must be rejected."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        sub = roots["kicad_root"] / "sub"
        sub.mkdir()

        # Path that traverses out of the kicad_root and into /etc
        traversal = str(sub) + "/../../etc/passwd"
        resolved = os.path.realpath(os.path.expanduser(traversal))

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(traversal, must_exist=False)
        assert result is not None, \
            f"Traversal resolving to {resolved!r} must be rejected in strict mode"

    def test_dotdot_stays_inside_root_allowed_strict(self, tmp_path, monkeypatch):
        """Redundant ``..`` that stays inside the root is allowed."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        inner = roots["kicad_root"] / "foo" / "bar"
        inner.mkdir(parents=True)
        file = inner / "board.kicad_pro"
        file.write_text("")

        # Build path with redundant `..` that cancels out
        path_with_dotdot = str(inner.parent) + "/../foo/bar/board.kicad_pro"

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        assert validate_project_path(path_with_dotdot) is None

    def test_tilde_expansion_runs(self, monkeypatch):
        """validate_project_path must call expanduser: ~/foo must not be 'Invalid path'."""
        # Any path starting with ~ that makes it past the isinstance check proves
        # expanduser ran (a raw ~-string would fail isinstance only if it's not str,
        # but here we confirm the result is about existence/roots, not "Invalid path").
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        tilde_path = "~/nonexistent_kicad_test_xyz.kicad_pro"
        result = validate_project_path(tilde_path, must_exist=False)
        # Either None (permissive, path processed normally) or a root-rejection error —
        # but NOT "Invalid path" (which would mean expanduser was never called or the
        # str check tripped on the tilde form).
        assert result is None or "Invalid path" not in (result or ""), \
            "tilde path should pass the str check and be expanded, not flagged as invalid"

    def test_tilde_and_explicit_home_same_result(self, monkeypatch):
        """~/foo and $HOME/foo should produce identical validation results."""
        home = os.path.expanduser("~")
        tilde_form = "~/Documents/kicad_tilde_test_xyz.kicad_pro"
        explicit_form = os.path.join(home, "Documents", "kicad_tilde_test_xyz.kicad_pro")

        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        r1 = validate_project_path(tilde_form, must_exist=False)
        r2 = validate_project_path(explicit_form, must_exist=False)
        assert r1 == r2, "tilde form and explicit home prefix must produce same result"


# ---------------------------------------------------------------------------
# 4. must_exist parameter
# ---------------------------------------------------------------------------

class TestMustExist:

    def test_must_exist_true_nonexistent_returns_not_found(self, tmp_path):
        missing = str(tmp_path / "does_not_exist.kicad_pro")
        result = validate_project_path(missing, must_exist=True)
        assert result is not None
        assert "not found" in result.lower()

    def test_must_exist_false_nonexistent_inside_root_returns_none(
        self, tmp_path, monkeypatch
    ):
        """must_exist=False + nonexistent path inside root (fake_tmp) → None."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)

        missing = str(roots["fake_tmp"] / "ghost.kicad_pro")
        assert not os.path.exists(missing)

        result = validate_project_path(missing, must_exist=False)
        assert result is None

    def test_must_exist_false_out_of_root_strict_rejected(
        self, tmp_path, monkeypatch
    ):
        """must_exist=False + path outside root + strict → rejected (existence irrelevant)."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        outsider = tmp_path / "outsider_exist"
        outsider.mkdir()
        missing = str(outsider / "ghost.kicad_pro")

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(missing, must_exist=False)
        assert result is not None

    def test_must_exist_true_symlink_to_real_file_inside_root_allowed(
        self, tmp_path, monkeypatch
    ):
        """Symlink inside root → real file inside root → allowed."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        real_file = roots["kicad_root"] / "real_board.kicad_pro"
        real_file.write_text("")
        link = roots["kicad_root"] / "link_board.kicad_pro"
        link.symlink_to(real_file)

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")
        assert validate_project_path(str(link), must_exist=True) is None

    def test_must_exist_true_symlink_outside_root_strict_rejected(
        self, tmp_path, monkeypatch
    ):
        """Symlink whose realpath escapes the allowed root → rejected in strict mode."""
        roots = _isolated_roots(monkeypatch, tmp_path)

        outside = tmp_path / "outside_secret"
        outside.mkdir()
        real_target = outside / "secret.kicad_pro"
        real_target.write_text("sensitive")

        # symlink sits lexically inside kicad_root but resolves to outside
        link = roots["kicad_root"] / "tricky.kicad_pro"
        link.symlink_to(real_target)

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(str(link), must_exist=True)
        assert result is not None, \
            "symlink escaping allowed root must be rejected in strict mode"

    def test_must_exist_default_is_true(self, tmp_path):
        """Default for must_exist is True: missing file → error without explicit kwarg."""
        missing = str(tmp_path / "no_such_file.kicad_pro")
        # Call without must_exist kwarg
        result = validate_project_path(missing)
        assert result is not None
        assert "not found" in result.lower()


# ---------------------------------------------------------------------------
# 5. Invalid input
# ---------------------------------------------------------------------------

class TestInvalidInput:

    def test_empty_string(self):
        result = validate_project_path("")
        assert result is not None
        assert "Invalid path" in result

    def test_none(self):
        result = validate_project_path(None)  # type: ignore[arg-type]
        assert result is not None
        assert "Invalid path" in result

    def test_integer(self):
        result = validate_project_path(42)  # type: ignore[arg-type]
        assert result is not None
        assert "Invalid path" in result

    def test_list(self):
        result = validate_project_path(["/tmp/foo"])  # type: ignore[arg-type]
        assert result is not None
        assert "Invalid path" in result

    def test_trailing_slash_real_dir(self, tmp_path, monkeypatch):
        """Trailing slash on a real directory inside a root → allowed (pins behavior)."""
        # tmp_path is inside real tempdir — use that (no isolation needed)
        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)
        path_with_slash = str(tmp_path) + "/"
        result = validate_project_path(path_with_slash, must_exist=True)
        assert result is None, "trailing slash on existing dir should be allowed"

    def test_leading_whitespace_not_silently_stripped(self, tmp_path):
        """' /path/...' with leading space is not silently treated as the same path.
        The space-prefixed string is not the same file; must_exist=True → 'not found'."""
        real_file = tmp_path / "board.kicad_pro"
        real_file.write_text("")
        path_with_space = " " + str(real_file)
        result = validate_project_path(path_with_space, must_exist=True)
        # Leading space means the path won't exist at that literal name
        assert result is not None, \
            "leading whitespace must not be silently stripped"

    def test_path_is_just_whitespace(self):
        """A string of only spaces must be rejected as invalid (falsy after strip? — pin it)."""
        # The validator checks `not path` on the raw string; '   ' is truthy in Python,
        # so it will NOT be caught by the `not path` guard. It passes as a str
        # and resolves to some real path (os.path.realpath('   ')).
        # Pin the current behavior: '   ' is NOT rejected as "Invalid path".
        result = validate_project_path("   ", must_exist=True)
        # Current code: isinstance('   ', str) = True, '   ' is truthy → no "Invalid path"
        # → goes to must_exist check → path won't exist → "Path not found"
        # OR → resolves and exists. Either way: NOT "Invalid path".
        assert result is None or "Invalid path" not in (result or ""), \
            "whitespace-only string passes the isinstance+truthy check (pin current behavior)"


# ---------------------------------------------------------------------------
# 6. Real symlinks (using tmp_path)
# ---------------------------------------------------------------------------

class TestRealSymlinks:

    def test_symlink_outside_root_permissive_allowed(self, tmp_path, monkeypatch):
        """Symlink escaping root in permissive mode → allowed (returns None)."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        outside = tmp_path / "outside_perm"
        outside.mkdir()
        target_file = outside / "data.kicad_pro"
        target_file.write_text("")

        link = roots["kicad_root"] / "alias.kicad_pro"
        link.symlink_to(target_file)

        monkeypatch.delenv("KICAD_MCP_STRICT_PATHS", raising=False)

        result = validate_project_path(str(link), must_exist=True)
        assert result is None, "permissive mode allows symlink escaping root"

    def test_dangling_symlink_must_exist_true(self, tmp_path):
        """Dangling symlink + must_exist=True → 'Path not found'."""
        dangling = tmp_path / "dangling.kicad_pro"
        dangling.symlink_to(tmp_path / "nowhere.kicad_pro")  # target doesn't exist

        result = validate_project_path(str(dangling), must_exist=True)
        assert result is not None
        assert "not found" in result.lower()

    def test_realpath_used_not_lexical(self, tmp_path, monkeypatch):
        """Validator uses realpath (follows symlinks), not lexical path.
        Symlink inside root pointing outside must be rejected in strict mode."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        outside = tmp_path / "outside_real"
        outside.mkdir()
        real_file = outside / "secret.kicad_pro"
        real_file.write_text("")

        # Link sits lexically inside kicad_root but resolves to outside
        link = roots["kicad_root"] / "project.kicad_pro"
        link.symlink_to(real_file)

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(str(link), must_exist=True)
        assert result is not None, \
            "realpath must be used; lexical containment inside root is not enough"

    def test_chained_symlinks_followed(self, tmp_path, monkeypatch):
        """Multiple levels of symlinks are fully resolved via realpath."""
        roots = _isolated_roots(monkeypatch, tmp_path)
        outside = tmp_path / "outside_chain"
        outside.mkdir()
        real_file = outside / "deep.kicad_pro"
        real_file.write_text("")

        # Chain: link1 → link2 → real_file (outside root)
        link2 = roots["kicad_root"] / "link2.kicad_pro"
        link2.symlink_to(real_file)
        link1 = roots["kicad_root"] / "link1.kicad_pro"
        link1.symlink_to(link2)

        monkeypatch.setenv("KICAD_MCP_STRICT_PATHS", "1")

        result = validate_project_path(str(link1), must_exist=True)
        assert result is not None, \
            "chained symlinks must be fully resolved; the chain escapes the root"
