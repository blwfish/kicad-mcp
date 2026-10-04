"""Tests for the `get_usage_guidance` tool.

This is a thin wire-up of mcp-agent-notes (design-docs/mcp-agent-notes/SPEC.md)
into kicad-mcp's FastMCP idiom — rendering/ranking logic itself is tested
upstream in that package. These tests cover: NOTES content (the mandatory
rules survive the migration into structured data), and the router's own
dispatch (operation validation, argument plumbing).
"""

import asyncio
import importlib

import pytest
from fastmcp import FastMCP
from mcp_agent_notes import NoteKind, Priority

from kicad_mcp.tools.usage_guidance import NOTES, register_usage_guidance_tools


@pytest.fixture
def guidance_server():
    mcp = FastMCP("test-usage-guidance")
    register_usage_guidance_tools(mcp)
    return mcp


def _get_guidance_fn(mcp_server):
    tool = asyncio.run(mcp_server.get_tool("get_usage_guidance"))
    if tool is None:
        raise ValueError("Tool 'get_usage_guidance' not found")
    return tool.fn


class TestRegistration:

    def test_tool_registered(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        assert fn is not None

    def test_default_operation_is_strategy(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        assert fn() == fn(operation="strategy")


class TestDispatch:

    def test_strategy_no_topic(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("strategy")
        assert isinstance(result, str) and result

    def test_strategy_with_topic(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("strategy", topic="firmware")
        assert "design" in result.lower()

    def test_tactics_no_topic_lists_topics(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("tactics")
        assert "routing" in result

    def test_tactics_with_topic(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("tactics", topic="routing")
        assert "autoroute" in result

    def test_find_requires_problem(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("find")
        assert "error" in result
        assert "problem" in result

    def test_find_with_problem(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("find", problem="board looks clean but has violations")
        assert "audit" in result.lower()

    def test_unknown_operation(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        result = fn("bogus")
        assert "error" in result
        assert "unknown operation" in result
        assert "strategy|tactics|find" in result


class TestNotesContentCoversMandatoryRules:
    """Every mandatory rule from AGENT-INSTRUCTIONS.md / the old
    SERVER_INSTRUCTIONS string must survive as a Note — this payload is the
    fallback channel for clients that drop the `instructions` field
    entirely, so silently losing a rule in the migration would be a real
    regression, not just a refactor."""

    def _note_text(self, note):
        return f"{note.summary} {note.detail}"

    def _all_text(self):
        return " ".join(self._note_text(n) for n in NOTES)

    def test_mentions_hand_routing_rule(self):
        text = self._all_text()
        assert "add_trace" in text or "add_via" in text
        assert "autoroute" in text

    def test_mentions_library_search_rule(self):
        text = self._all_text().lower()
        assert "library(operation='search')" in text

    def test_mentions_concurrent_write_rule(self):
        text = self._all_text().lower()
        assert "concurrent" in text or "serialize" in text

    def test_mentions_audit_placement_blind_spot(self):
        text = self._all_text()
        assert "audit(operation='all')" in text
        assert "placement" in text.lower()

    def test_three_mandatory_rules_are_critical(self):
        critical_ids = {n.id for n in NOTES if n.priority is Priority.CRITICAL}
        assert critical_ids == {
            "never-hand-route",
            "never-guess-library-names",
            "no-concurrent-pcb-writes",
        }

    def test_at_least_one_strategy_and_one_tactic_note(self):
        kinds = {n.kind for n in NOTES}
        assert kinds == {NoteKind.STRATEGY, NoteKind.TACTIC}

    def test_note_ids_are_unique(self):
        ids = [n.id for n in NOTES]
        assert len(ids) == len(set(ids))


class TestFreerouterVersionNote:
    """The note that tells an assistant a newer-than-validated FreeRouter is slow and
    carries a `freerouter` result field. Its version comes from the SAME constant that
    gates the runtime warning, so guidance and gate cannot drift."""

    _ID = "freerouter-newer-than-validated"

    def _note(self):
        matches = [n for n in NOTES if n.id == self._ID]
        assert len(matches) == 1
        return matches[0]

    def test_note_is_not_critical(self):
        """The three mandatory rules stay the only CRITICAL notes."""
        assert self._note().priority is not Priority.CRITICAL

    def test_names_the_result_field_and_every_status_value(self):
        from kicad_mcp.utils import freerouter_version as fv
        text = f"{self._note().summary} {self._note().detail}"
        assert "`freerouter`" in text or "freerouter" in text
        for status in (fv.STATUS_VALIDATED, fv.STATUS_NEWER, fv.STATUS_UNKNOWN):
            assert status in text, f"status {status!r} missing -- closed vocabulary drifted"
        assert "FREEROUTER_JAR" in text

    def test_states_that_it_warns_and_does_not_refuse(self):
        assert "nothing is refused" in self._note().detail

    def test_version_text_follows_the_constant(self, monkeypatch):
        """Change the gate and the guidance changes with it (no hand-copied '2.2.3')."""
        from kicad_mcp.tools import usage_guidance as ug
        from kicad_mcp.utils import freerouter_version as fv
        assert "2.2.3" in self._note().summary
        monkeypatch.setattr(fv, "FREEROUTER_VALIDATED_MAX", (9, 8, 7))
        try:
            importlib.reload(ug)
            note = next(n for n in ug.NOTES if n.id == self._ID)
            assert "9.8.7" in note.summary and "9.8.7" in note.detail
            assert "2.2.3" not in note.summary.replace("2.2.3 on", "")  # only the measured comparison
        finally:
            monkeypatch.undo()
            importlib.reload(ug)

    @pytest.mark.parametrize("symptom", ["autoroute is slow", "autoroute timed out",
                                         "which FreeRouter version"])
    def test_findable_by_symptom(self, guidance_server, symptom):
        out = _get_guidance_fn(guidance_server)(operation="find", problem=symptom)
        assert "FreeRouter newer" in str(out)

    def test_listed_under_the_autoroute_tactics_topic(self, guidance_server):
        out = _get_guidance_fn(guidance_server)(operation="tactics", topic="autoroute")
        assert "FreeRouter newer" in str(out)
