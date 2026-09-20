"""Tests for the `get_usage_guidance` tool.

Unit tests only — this tool is a static, bridge-only payload with no
subprocess/pcbnew round-trip, so nothing here needs mocking.
"""

import asyncio

import pytest
from fastmcp import FastMCP

from kicad_mcp.tools.usage_guidance import (
    _usage_guidance_payload,
    register_usage_guidance_tools,
)


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

    def test_takes_no_arguments_and_has_no_side_effects(self, guidance_server):
        fn = _get_guidance_fn(guidance_server)
        # Calling twice must be safe and return equal payloads (idempotent,
        # no state).
        assert fn() == fn()


class TestPayloadShape:
    """The four top-level keys must always be present, never merely absent.

    An absent key is ambiguous between "nothing to report" and "forgot to
    populate this" — see the data-capture backward-chaining convention this
    tool follows.
    """

    def test_all_four_top_level_keys_present(self):
        payload = _usage_guidance_payload()
        assert set(payload.keys()) == {
            "avoid_these_issues",
            "best_practices",
            "strategy",
            "tactics",
        }

    def test_avoid_these_issues_is_nonempty_list_of_dicts(self):
        payload = _usage_guidance_payload()
        issues = payload["avoid_these_issues"]
        assert isinstance(issues, list)
        assert len(issues) > 0
        for entry in issues:
            assert set(entry.keys()) == {"issue", "guidance", "confidence"}
            assert entry["issue"]
            assert entry["guidance"]
            assert entry["confidence"]

    def test_best_practices_is_nonempty_list_of_strings(self):
        payload = _usage_guidance_payload()
        practices = payload["best_practices"]
        assert isinstance(practices, list)
        assert len(practices) > 0
        assert all(isinstance(p, str) and p for p in practices)

    def test_strategy_and_tactics_are_nonempty_strings(self):
        payload = _usage_guidance_payload()
        assert isinstance(payload["strategy"], str) and payload["strategy"]
        assert isinstance(payload["tactics"], str) and payload["tactics"]


class TestContentCoversMandatoryRules:
    """Every mandatory rule from AGENT-INSTRUCTIONS.md / SERVER_INSTRUCTIONS
    should be reachable from this payload — it's the fallback channel for
    clients that drop the `instructions` field entirely."""

    def _all_issue_text(self):
        payload = _usage_guidance_payload()
        return " ".join(
            f"{e['issue']} {e['guidance']}" for e in payload["avoid_these_issues"]
        )

    def test_mentions_hand_routing_rule(self):
        text = self._all_issue_text()
        assert "add_trace" in text or "add_via" in text
        assert "autoroute" in text

    def test_mentions_library_search_rule(self):
        text = self._all_issue_text()
        assert "library(operation='search')" in text or "library" in text.lower()

    def test_mentions_concurrent_write_rule(self):
        text = self._all_issue_text()
        assert "concurrent" in text.lower() or "serialize" in text.lower()

    def test_mentions_audit_placement_blind_spot(self):
        text = self._all_issue_text()
        assert "audit(operation='all')" in text
        assert "placement" in text.lower()
