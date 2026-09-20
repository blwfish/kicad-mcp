"""Tests for the `get_usage_guidance` tool.

This is a thin wire-up of mcp-agent-notes (design-docs/mcp-agent-notes/SPEC.md)
into kicad-mcp's FastMCP idiom — rendering/ranking logic itself is tested
upstream in that package. These tests cover: NOTES content (the mandatory
rules survive the migration into structured data), and the router's own
dispatch (operation validation, argument plumbing).
"""

import asyncio

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
