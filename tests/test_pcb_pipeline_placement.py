"""Unit tests for the pure placement-plumbing helpers in pcb_pipeline.

These cover the Python-side logic added with the human-rational autoplacer:
board-size precedence (a ``<= 0`` threshold), placement-hint merge precedence +
validation passthrough, and the decision→event translation. The embedded
placement *engine* (rotation/ordering/overhang) is gated by the real-KiCad
integration harness; the pure edge-terminal math is covered by
test_edge_terminal_placement. Boundary-focused per the CLAUDE.md threshold rule.
"""
from mcp_events import event_context

from kicad_mcp.tools.pcb_pipeline import (
    _emit_placement_decision,
    _merge_placement_hints,
    _resolve_board_size,
)


# ---------------------------------------------------------------------------
# _resolve_board_size — the explicit > intent > auto precedence, per axis.
# Threshold under test: dimension <= 0 means "auto / inherit"; > 0 is explicit.
# ---------------------------------------------------------------------------

class TestResolveBoardSize:
    INTENT = {"board_size_mm": [40, 30]}

    def test_explicit_both_positive_wins_over_intent(self):
        # Both axes given (>0) → intent ignored entirely.
        assert _resolve_board_size(90, 75, self.INTENT) == (90, 75, False)

    def test_both_zero_uses_intent(self):
        assert _resolve_board_size(0, 0, self.INTENT) == (40.0, 30.0, True)

    def test_mixed_explicit_width_intent_height(self):
        # width explicit, height auto → width kept, height from intent.
        assert _resolve_board_size(90, 0, self.INTENT) == (90, 30.0, True)

    def test_mixed_explicit_height_intent_width(self):
        assert _resolve_board_size(0, 75, self.INTENT) == (40.0, 75, True)

    def test_negative_is_treated_as_auto(self):
        # The threshold is <= 0, so a negative dimension also inherits the intent.
        assert _resolve_board_size(-1, -1, self.INTENT) == (40.0, 30.0, True)

    def test_no_intent_source_keeps_explicit_zero(self):
        assert _resolve_board_size(0, 0, None) == (0, 0, False)
        assert _resolve_board_size(0, 0, {}) == (0, 0, False)

    def test_intent_without_board_size_key(self):
        assert _resolve_board_size(0, 0, {"power_source": "usb"}) == (0, 0, False)

    def test_intent_size_zero_component_ignored(self):
        # v > 0 boundary: a 0 axis is not a valid size, ignored (stays auto).
        assert _resolve_board_size(0, 0, {"board_size_mm": [0, 30]}) == (0, 0, False)

    def test_intent_size_negative_ignored(self):
        assert _resolve_board_size(0, 0, {"board_size_mm": [40, -5]}) == (0, 0, False)

    def test_intent_size_wrong_length_ignored(self):
        assert _resolve_board_size(0, 0, {"board_size_mm": [40]}) == (0, 0, False)
        assert _resolve_board_size(0, 0, {"board_size_mm": [40, 30, 2]}) == (0, 0, False)

    def test_intent_size_non_numeric_ignored(self):
        assert _resolve_board_size(0, 0, {"board_size_mm": ["40", "30"]}) == (0, 0, False)

    def test_intent_size_bool_component_ignored(self):
        # True == 1 would slip past a naive isinstance(int) check.
        assert _resolve_board_size(0, 0, {"board_size_mm": [True, 30]}) == (0, 0, False)

    def test_intent_size_not_a_list_ignored(self):
        assert _resolve_board_size(0, 0, {"board_size_mm": "40x30"}) == (0, 0, False)


# ---------------------------------------------------------------------------
# _merge_placement_hints — intent-first, param-wins; normalize passthrough.
# ---------------------------------------------------------------------------

class TestMergePlacementHints:
    def test_empty_inputs(self):
        assert _merge_placement_hints(None, None) == ({}, [])
        assert _merge_placement_hints({}, {}) == ({}, [])

    def test_intent_only(self):
        clean, warns = _merge_placement_hints({"J6": {"edge": "none"}}, None)
        assert clean == {"J6": {"edge": "none"}}
        assert warns == []

    def test_param_wins_on_collision(self):
        clean, warns = _merge_placement_hints(
            {"J1": {"edge": "top"}}, {"J1": {"edge": "left"}},
        )
        assert clean == {"J1": {"edge": "left"}}

    def test_union_of_refs(self):
        clean, _ = _merge_placement_hints(
            {"J1": {"edge": "top"}}, {"J2": {"rotation": 90}},
        )
        assert clean == {"J1": {"edge": "top"}, "J2": {"rotation": 90}}

    def test_invalid_directive_dropped_with_warning(self):
        clean, warns = _merge_placement_hints({"J9": {"edge": "north"}}, None)
        assert "J9" not in clean
        assert any("J9:" in w for w in warns)

    def test_valid_fixed_passthrough(self):
        clean, _ = _merge_placement_hints(None, {"J3": {"fixed": [10, 20]}})
        assert clean == {"J3": {"fixed": [10.0, 20.0]}}

    def test_partial_valid_partial_invalid_same_ref(self):
        clean, warns = _merge_placement_hints(
            None, {"J4": {"edge": "left", "rotation": 45}},
        )
        assert clean == {"J4": {"edge": "left"}}     # rotation 45 dropped
        assert any("J4:" in w for w in warns)


# ---------------------------------------------------------------------------
# _emit_placement_decision — engine record → OOB event (severity/code/context).
# ---------------------------------------------------------------------------

def _emit_one(decision):
    with event_context() as ev:
        _emit_placement_decision(decision)
        return ev.to_envelope("info")


class TestEmitPlacementDecision:
    def test_rotation_chosen_is_info(self):
        env = _emit_one({"event": "rotation_chosen", "ref": "J1", "edge": "top",
                         "angle": 90, "source": "wire_entry"})
        assert len(env) == 1
        assert env[0]["level"] == "info"
        assert env[0]["code"] == "rotation_chosen"
        assert env[0]["context"] == {"ref": "J1", "edge": "top", "angle": 90,
                                     "source": "wire_entry"}

    def test_rotation_ambiguous_is_warn(self):
        env = _emit_one({"event": "rotation_ambiguous", "ref": "J2"})
        assert env[0]["level"] == "warn"
        assert env[0]["code"] == "rotation_ambiguous"
        assert env[0]["context"]["ref"] == "J2"

    def test_hint_applied_is_info(self):
        env = _emit_one({"event": "placement_hint_applied", "ref": "J6",
                         "directive": {"edge": "none"}})
        assert env[0]["level"] == "info"
        assert env[0]["code"] == "placement_hint_applied"
        assert env[0]["context"]["directive"] == {"edge": "none"}

    def test_hint_offboard_is_warn(self):
        env = _emit_one({"event": "placement_hint_offboard", "ref": "J3"})
        assert env[0]["level"] == "warn"
        assert env[0]["code"] == "placement_hint_offboard"

    def test_unknown_event_falls_through_to_info(self):
        # An unrecognised kind is surfaced generically, never silently dropped.
        env = _emit_one({"event": "something_new", "ref": "J9"})
        assert len(env) == 1
        assert env[0]["code"] == "placement_decision"
