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
from kicad_mcp.utils.netlist_parser import SchematicParser, analyze_netlist


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


class TestMalformedComponentsSkippedSurfaced:
    """Regression: _extract_components counted+logged symbols with no
    Reference property (library-definition blocks inside (lib_symbols ...))
    but never surfaced the count in parse()'s result dict at all -- the
    sibling XML/cli parser path (_parse_kicadxml) already returns
    malformed_components_skipped unconditionally (explicitly documented as
    "uniform shape across paths"), so this was an asymmetry between the two
    parser paths for the identical failure signal."""

    def test_skipped_count_present_and_zero_when_all_valid(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(symbol (lib_id "Device:R") '
            '(property "Reference" "R1") (at 10 20 0)'
            ')\n'
        )
        result = p.parse()
        assert result["malformed_components_skipped"] == 0

    def test_skipped_count_reflects_library_definition_blocks(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(symbol (lib_id "Device:R") '
            '(property "Reference" "R1") (at 10 20 0)'
            ')\n'
            '(symbol (lib_id "Device:C") (at 30 40 0))\n'  # no Reference property
        )
        result = p.parse()
        assert result["malformed_components_skipped"] == 1
        assert result["component_count"] == 1


class TestJunctionModernFormat:
    """Regression: verified against real KiCad 10 templates (Arduino_Mega) --
    modern junctions are `(junction (at X Y) (diameter D) (color ...)
    (uuid ...))`, never a bare `(xy X Y)` child. The original pattern
    (`\\(junction\\s+\\(xy ...\\)\\)`, immediately double-closed) never
    matched a real modern schematic's junctions at all -- every junction
    was silently dropped on every real board."""

    def test_junction_with_modern_at_form_is_extracted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(junction (at 21.59 132.08) (diameter 1.016) '
            '(color 0 0 0 0) (uuid "127679a9-3981-4934-815e-896a4e3ff56e"))\n'
        )
        p._extract_junctions()
        assert p.junctions == [{"x": 21.59, "y": 132.08}]
        assert "junctions" not in p.extraction_skip_counts

    def test_junction_with_legacy_xy_form_still_works(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(junction (xy 10 20))\n'
        p._extract_junctions()
        assert p.junctions == [{"x": 10.0, "y": 20.0}]


class TestExtractionSkipCounts:
    """Regression: every _extract_* regex-fallback helper (wires, junctions,
    labels, power symbols, no-connects) used to silently `continue` past a
    non-matching S-expression with no error counter at all -- systemic
    across the whole regex-fallback path. parse() must surface a dict a
    caller can inspect to tell "genuinely none of this construct" from
    "some were dropped"."""

    def test_clean_parse_has_no_skips(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(wire (pts (xy 0 0) (xy 10 10)))\n'
            '(junction (at 5 5) (diameter 1) (uuid "x"))\n'
            '(label "NET1" (at 1 1 0))\n'
            '(no_connect (at 2 2))\n'
        )
        result = p.parse()
        assert result["extraction_skip_counts"] == {}

    def test_malformed_wire_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        # 3-point (bus/multi-segment) wire the 2-point regex can't match.
        p.content = '(wire (pts (xy 0 0) (xy 5 5) (xy 10 10)))\n'
        result = p.parse()
        assert result["extraction_skip_counts"] == {"wires": 1}

    def test_malformed_junction_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(junction (weird_field 1 2))\n'
        result = p.parse()
        assert result["extraction_skip_counts"] == {"junctions": 1}

    def test_malformed_local_label_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(label "NET1")\n'  # missing (at ...)
        result = p.parse()
        assert result["extraction_skip_counts"] == {"local_labels": 1}

    def test_malformed_global_label_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(global_label "VBUS" (shape input))\n'  # missing (at ...)
        result = p.parse()
        assert result["extraction_skip_counts"] == {"global_labels": 1}

    def test_malformed_hierarchical_label_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(hierarchical_label "RESET" (shape output))\n'  # missing (at ...)
        result = p.parse()
        assert result["extraction_skip_counts"] == {"hierarchical_labels": 1}

    def test_malformed_power_symbol_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(symbol (lib_id "power:GND"))\n'  # missing (at ...)
        result = p.parse()
        assert result["extraction_skip_counts"] == {"power_symbols": 1}

    def test_malformed_no_connect_is_counted(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(no_connect (weird_field 1 2))\n'
        result = p.parse()
        assert result["extraction_skip_counts"] == {"no_connects": 1}

    def test_multiple_constructs_accumulate_independently(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(junction (weird_field 1 2))\n'
            '(no_connect (weird_field 1 2))\n'
            '(no_connect (weird_field 3 4))\n'
        )
        result = p.parse()
        assert result["extraction_skip_counts"] == {"junctions": 1, "no_connects": 2}


class TestBuildNetlistLabelCoverage:
    """Regression: _build_netlist only ever registered global_labels and
    power_symbols as net keys -- local labels (self.labels, the far more
    common net-naming mechanism in a typical schematic) and hierarchical
    labels were extracted but never consulted here at all, so net_count
    silently undercounted on the regex-fallback path for any schematic
    that names its nets with local labels (the normal case)."""

    def test_local_label_becomes_a_net(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(label "SDA" (at 10 20 0))\n'
        result = p.parse()
        assert "SDA" in result["nets"]

    def test_hierarchical_label_becomes_a_net(self, tmp_path):
        p = _parser(tmp_path)
        p.content = '(hierarchical_label "RESET" (shape output) (at 10 20 0))\n'
        result = p.parse()
        assert "RESET" in result["nets"]

    def test_global_label_still_becomes_a_net(self, tmp_path):
        # Pre-existing behavior must survive this change unchanged.
        p = _parser(tmp_path)
        p.content = '(global_label "VBUS" (shape input) (at 10 20 0))\n'
        result = p.parse()
        assert "VBUS" in result["nets"]

    def test_same_name_across_label_kinds_is_one_net_not_duplicated(self, tmp_path):
        p = _parser(tmp_path)
        p.content = (
            '(label "GND" (at 1 1 0))\n'
            '(global_label "GND" (shape input) (at 2 2 0))\n'
        )
        result = p.parse()
        assert list(result["nets"]).count("GND") == 1


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


class TestAnalyzeNetlistComponentTypes:
    """Regression: analyze_netlist's component_types tally used to re-encode
    its own copy of the reference-prefix-extraction regex (`re.match(r"^([A-
    Za-z_]+)", ref)`), identical to (and independently maintained from)
    netlist.py's own copy and component_utils.py's canonical (until the
    2026-09-23 full review's finding #6, unused) get_component_type_from_
    reference. Now delegates to that single source of truth."""

    def test_tallies_by_reference_prefix(self):
        result = analyze_netlist({
            "components": {"R1": {}, "R2": {}, "C1": {}, "U1": {}},
            "nets": {},
        })
        assert result["component_types"] == {"R": 2, "C": 1, "U": 1}

    def test_reference_with_no_letter_prefix_is_not_tallied(self):
        # get_component_type_from_reference returns "" for a ref with no
        # leading letters -- must not create a spurious "" bucket.
        result = analyze_netlist({"components": {"1R1": {}}, "nets": {}})
        assert "" not in result["component_types"]
