"""Tests for the design router's error handling.

h-design-intent: load_intent runs on a possibly hand-edited / truncated /
wrong-schema-version YAML doc. A parse failure must surface as a structured
{status: error, code: invalid_intent} result, not a raw traceback to fastmcp.
Before the fix, _op_expand/_op_generate/_op_show called load_intent unguarded.
"""
from kicad_mcp.tools.design import _op_expand, _op_generate, _op_show


def test_op_expand_malformed_yaml_returns_structured_error(tmp_path):
    p = tmp_path / "intent.yaml"
    p.write_text("{a: 1")  # unclosed flow mapping -> yaml.YAMLError
    result = _op_expand(intent_path=str(p), out_path=None)
    assert result["status"] == "error"
    assert result["code"] == "invalid_intent"


def test_op_show_malformed_yaml_returns_structured_error(tmp_path):
    p = tmp_path / "intent.yaml"
    p.write_text("[1, 2, 3")  # unclosed flow sequence -> yaml.YAMLError
    result = _op_show(intent_path=str(p))
    assert result["status"] == "error"
    assert result["code"] == "invalid_intent"


def test_op_generate_truncated_intent_returns_structured_error(tmp_path):
    # A truncated/empty doc -> yaml.safe_load returns None -> from_dict(None)
    # does None.get(...) -> AttributeError, which the loader must convert.
    p = tmp_path / "intent.yaml"
    p.write_text("")
    result = _op_generate(intent_path=str(p), schematic_path=str(tmp_path / "out.kicad_sch"))
    assert result["status"] == "error"
    assert result["code"] == "invalid_intent"
