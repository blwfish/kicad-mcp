"""Verify the generated agent-notes doc sections are in sync with NOTES.

scripts/sync_agent_notes_docs.py is the write path (run it and commit when a
CRITICAL note changes); this is the read-only check, so `uv run pytest`
catches drift locally without requiring a separate script invocation, the
same way CI's docs-check.yml does for CI.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from sync_agent_notes_docs import MARKER_RE, TARGETS  # noqa: E402


class TestAgentNotesDocsInSync:

    def test_all_targets_have_markers(self):
        for path, _render in TARGETS:
            text = path.read_text(encoding="utf-8")
            assert MARKER_RE.search(text), (
                f"{path.name} is missing the <!-- agent-notes:critical --> "
                f"marker pair"
            )

    def test_marked_block_matches_current_notes(self):
        for path, render in TARGETS:
            text = path.read_text(encoding="utf-8")
            match = MARKER_RE.search(text)
            assert match is not None, f"{path.name} is missing markers"
            assert match.group(0) == render(), (
                f"{path.name}'s generated block is stale — run "
                f"scripts/sync_agent_notes_docs.py and commit the result"
            )
