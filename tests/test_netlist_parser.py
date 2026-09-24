"""Direct unit tests for SchematicParser's regex-based extraction methods
(netlist_parser.py). This class had only 41% coverage before this file --
exercised only indirectly through test_netlist_tools.py's mocking of
_parse_netlist and integration tests.

Regression coverage: pin-name, local/global/hierarchical label, and power-
symbol-type extraction all used to match quoted strings via a naive
`"([^"]+)"` pattern that silently truncates at the first ESCAPED quote --
e.g. a label containing inch marks (`4.7\\" spacer`) would have its
trailing portion dropped. The sibling lib_id/property extraction in the
same class already used the escape-aware `_QSTR` pattern; these sites
didn't, until this fix promoted _QSTR to a module-level constant every
site now shares.
"""
from kicad_mcp.utils.netlist_parser import SchematicParser


def _parser(tmp_path, content: str = "") -> SchematicParser:
    sch = tmp_path / "test.kicad_sch"
    sch.write_text(content or '(kicad_sch (version 20230121) (generator "test"))\n')
    p = SchematicParser(str(sch))
    return p


class TestPinNameEscapedQuote:
    def test_pin_name_with_escaped_quote_not_truncated(self, tmp_path):
        p = _parser(tmp_path)
        symbol_expr = (
            '(symbol (lib_id "Device:R") '
            '(pin (num "1") (name "4.7\\" spacer"))'
            ')'
        )
        result = p._parse_component(symbol_expr)
        assert result["pins"][0]["name"] == '4.7\\" spacer'

    def test_pin_name_without_escape_still_works(self, tmp_path):
        p = _parser(tmp_path)
        symbol_expr = (
            '(symbol (lib_id "Device:R") '
            '(pin (num "1") (name "SDA"))'
            ')'
        )
        result = p._parse_component(symbol_expr)
        assert result["pins"][0]["name"] == "SDA"


class TestLocalLabelEscapedQuote:
    def test_local_label_with_escaped_quote_not_truncated(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(label "4.7\\" spacer" (at 10 20 0))\n'
        p._extract_labels()
        assert len(p.labels) == 1
        assert p.labels[0]["text"] == '4.7" spacer'  # _unescape_sexpr decodes \"

    def test_local_label_without_escape_still_works(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(label "NET1" (at 10 20 0))\n'
        p._extract_labels()
        assert len(p.labels) == 1
        assert p.labels[0]["text"] == "NET1"


class TestGlobalLabelEscapedQuote:
    def test_global_label_with_escaped_quote_not_truncated(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(global_label "4.7\\" spacer" (shape input) (at 10 20 0))\n'
        p._extract_labels()
        assert len(p.global_labels) == 1
        assert p.global_labels[0]["text"] == '4.7" spacer'

    def test_global_label_without_escape_still_works(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(global_label "VBUS" (shape input) (at 10 20 0))\n'
        p._extract_labels()
        assert len(p.global_labels) == 1
        assert p.global_labels[0]["text"] == "VBUS"
        assert p.global_labels[0]["shape"] == "input"


class TestHierarchicalLabelEscapedQuote:
    def test_hierarchical_label_with_escaped_quote_not_truncated(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(hierarchical_label "4.7\\" spacer" (shape output) (at 10 20 0))\n'
        p._extract_labels()
        assert len(p.hierarchical_labels) == 1
        assert p.hierarchical_labels[0]["text"] == '4.7" spacer'

    def test_hierarchical_label_without_escape_still_works(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(hierarchical_label "RESET" (shape output) (at 10 20 0))\n'
        p._extract_labels()
        assert len(p.hierarchical_labels) == 1
        assert p.hierarchical_labels[0]["text"] == "RESET"


class TestPowerSymbolTypeEscapedQuote:
    def test_power_symbol_type_with_escaped_quote_not_truncated(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(symbol (lib_id "power:4.7\\" spacer") (at 10 20 0))\n'
        )
        p._extract_power_symbols()
        assert len(p.power_symbols) == 1
        assert p.power_symbols[0]["type"] == '4.7\\" spacer'

    def test_power_symbol_type_without_escape_still_works(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(symbol (lib_id "power:GND") (at 10 20 0))\n'
        p._extract_power_symbols()
        assert len(p.power_symbols) == 1
        assert p.power_symbols[0]["type"] == "GND"
