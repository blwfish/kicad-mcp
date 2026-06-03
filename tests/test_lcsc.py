"""Tests for the LCSC/JLCPCB component intelligence tool and utilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kicad_mcp.utils import lcsc_db
from kicad_mcp.utils.lcsc_db import (
    _score_package,
    _passive_family,
    score_candidate,
    score_stock,
    score_tier,
    build_deviations,
    resolve_footprint,
    get_component,
    search_components,
    search_components_with_total,
    db_exists,
    get_snapshot_age_days,
    get_snapshot_date,
    _parse_price,
    _tier_from_attributes,
    _package_from_attributes,
    _manufacturer_from_attributes,
    _decode_shard_rows,
    _decode_attributes,
    _live_attributes,
    _live_part_to_row,
)
from kicad_mcp.tools.lcsc import _parse_attributes, _row_to_resolved

FIXTURE_DB = Path(__file__).parent / "fixtures" / "jlcparts_synthetic.sqlite3"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_db(tmp_path: Path) -> tuple[Path, Path]:
    """Copy fixture DB + write companion meta file to tmp_path."""
    import shutil
    db = tmp_path / "jlcparts.db"
    meta = tmp_path / "jlcparts_meta.json"
    shutil.copy(FIXTURE_DB, db)
    from datetime import datetime, timezone
    meta.write_text(json.dumps({
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_date": "2026-05-27",
        "total_components": 19,
        "schema_version": 1,
    }))
    return db, meta


def _get_tool():
    from kicad_mcp.server import create_server
    server = create_server()
    return asyncio.run(server.get_tool("lcsc")).fn


# ---------------------------------------------------------------------------
# Package scoring
# ---------------------------------------------------------------------------

class TestScorePackage:
    def test_exact_match(self):
        assert _score_package("SOT-23", "SOT-23") == 1.0

    def test_exact_match_case_insensitive(self):
        assert _score_package("sot-23", "SOT-23") == 1.0

    def test_hyphen_normalized_exact(self):
        assert _score_package("SOT23", "SOT-23") == 1.0

    def test_family_match_pin_variant(self):
        assert _score_package("SOT-23", "SOT-23-5") == pytest.approx(0.7)

    def test_family_match_other_direction(self):
        assert _score_package("SOT-23-5", "SOT-23") == pytest.approx(0.7)

    def test_family_match_sot223(self):
        assert _score_package("SOT-223", "SOT-223-3") == pytest.approx(0.7)

    def test_different_package(self):
        assert _score_package("SOT-23", "SOT-223") == 0.0

    def test_soic_different_sizes_not_family(self):
        # SOIC-8 vs SOIC-16 are genuinely different packages, not variants
        assert _score_package("SOIC-8", "SOIC-16") == 0.0

    def test_no_requested_package(self):
        assert _score_package(None, "SOT-23") == 1.0

    def test_0402_exact(self):
        assert _score_package("0402", "0402") == 1.0

    def test_0402_vs_0603_different(self):
        assert _score_package("0402", "0603") == 0.0


# ---------------------------------------------------------------------------
# Tier scoring
# ---------------------------------------------------------------------------

class TestScoreTier:
    def test_basic_vs_basic(self):
        assert score_tier("basic", "basic") == 1.0

    def test_basic_vs_extended(self):
        assert score_tier("basic", "extended") == pytest.approx(0.5)

    def test_basic_vs_preferred(self):
        assert score_tier("basic", "preferred") == pytest.approx(0.5)

    def test_extended_vs_basic(self):
        # 'extended' = "any tier, no preference" (mirrors the SQL no-filter
        # branch), so a basic part is NOT penalized (m-extended-tier).
        assert score_tier("extended", "basic") == 1.0

    def test_extended_vs_preferred(self):
        assert score_tier("extended", "preferred") == 1.0

    def test_extended_vs_extended(self):
        assert score_tier("extended", "extended") == 1.0

    def test_extended_treats_all_tiers_equally(self):
        # Coherent with the SQL filter: requesting 'extended' adds no tier
        # preference, identical to 'any'.
        assert (score_tier("extended", "basic")
                == score_tier("extended", "preferred")
                == score_tier("extended", "extended") == 1.0)

    def test_preferred_vs_preferred(self):
        assert score_tier("preferred", "preferred") == 1.0

    def test_preferred_vs_basic(self):
        assert score_tier("preferred", "basic") == pytest.approx(0.7)

    def test_preferred_vs_extended(self):
        assert score_tier("preferred", "extended") == pytest.approx(0.7)

    def test_any_vs_all_tiers(self):
        assert score_tier("any", "basic") == 1.0
        assert score_tier("any", "extended") == 1.0
        assert score_tier("any", "preferred") == 1.0

    def test_none_available_defaults_extended(self):
        assert score_tier("basic", None) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Stock scoring — boundary values per threshold-testing rule
# ---------------------------------------------------------------------------

class TestScoreStock:
    def test_stock_0(self):
        assert score_stock(0) == 0.0

    def test_stock_1(self):
        assert score_stock(1) == pytest.approx(0.3)

    def test_stock_99(self):
        assert score_stock(99) == pytest.approx(0.3)

    def test_stock_100(self):
        assert score_stock(100) == pytest.approx(0.7)

    def test_stock_999(self):
        assert score_stock(999) == pytest.approx(0.7)

    def test_stock_1000(self):
        assert score_stock(1000) == 1.0

    def test_stock_large(self):
        assert score_stock(100000) == 1.0


# ---------------------------------------------------------------------------
# Passive family classification
# ---------------------------------------------------------------------------

class TestPassiveFamily:
    def test_resistor_default(self):
        assert _passive_family("100kΩ 1% Resistor thick film") == "resistor"

    def test_capacitor_by_keyword(self):
        assert _passive_family("100nF MLCC Capacitor 10V X5R") == "capacitor"

    def test_capacitor_by_suffix(self):
        assert _passive_family("10uF ceramic") == "capacitor"

    def test_inductor_keyword(self):
        assert _passive_family("1uH Inductor 200mA SMD") == "inductor"

    def test_ferrite_bead(self):
        assert _passive_family("600Ω Ferrite Bead EMI filter") == "inductor"

    def test_ambiguous_defaults_resistor(self):
        assert _passive_family("SMD component 0402") == "resistor"


# ---------------------------------------------------------------------------
# Footprint resolution
# ---------------------------------------------------------------------------

class TestResolveFootprint:
    def test_sot23_maps(self):
        fp = resolve_footprint("SOT-23")
        assert fp == "Package_TO_SOT_SMD:SOT-23"

    def test_sot223_maps(self):
        fp = resolve_footprint("SOT-223")
        assert fp is not None
        assert "SOT-223" in fp

    def test_soic8_maps(self):
        fp = resolve_footprint("SOIC-8")
        assert fp is not None
        assert "SOIC-8" in fp

    def test_0402_resistor_family(self):
        fp = resolve_footprint("0402", "100kΩ Resistor")
        assert fp is not None
        assert "Resistor" in fp

    def test_0402_capacitor_family(self):
        fp = resolve_footprint("0402", "100nF Capacitor X5R")
        assert fp is not None
        assert "Capacitor" in fp

    def test_0402_inductor_family(self):
        fp = resolve_footprint("0402", "1uH Inductor")
        assert fp is not None
        assert "Inductor" in fp

    def test_unmapped_package_returns_none(self):
        fp = resolve_footprint("XQFN-24")
        assert fp is None

    def test_empty_package_returns_none(self):
        fp = resolve_footprint("")
        assert fp is None


# ---------------------------------------------------------------------------
# Score candidate (integration of all criteria)
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    BASE_ROW: dict[str, Any] = {
        "lcsc": "C6186",
        "mfr": "AMS1117-3.3",
        "manufacturer": "Advanced Monolithic Systems",
        "package": "SOT-223",
        "joints": 3,
        "assembly_tier": "basic",
        "description": "1A Low Dropout Regulator 3.3V SOT-223",
        "datasheet": "https://ds.example.com",
        "stock": 5000,
        "price": '[{"qFrom":1,"qTo":9,"price":0.1856}]',
    }

    def test_perfect_match_score_near_1(self):
        keywords = ["3.3v", "ldo", "regulator"]
        footprint = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        score = score_candidate(dict(self.BASE_ROW), keywords, "SOT-223", "basic", footprint)
        # All criteria near 1.0 → expect high score
        assert score > 0.85

    def test_no_keyword_match_reduces_score(self):
        keywords = ["buck", "converter", "12v"]  # completely wrong keywords
        footprint = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        score = score_candidate(dict(self.BASE_ROW), keywords, "SOT-223", "basic", footprint)
        # description weight 0.4 → 0.0 → overall <= 0.6
        assert score <= 0.65

    def test_wrong_package_reduces_score(self):
        keywords = ["3.3v", "ldo"]
        score = score_candidate(dict(self.BASE_ROW), keywords, "SOT-23", "basic",
                                "Package_TO_SOT_SMD:SOT-23")
        # SOT-23 vs SOT-223 → different → 0.0 × 0.25 deduction
        assert score < score_candidate(dict(self.BASE_ROW), keywords, "SOT-223", "basic",
                                       "Package_TO_SOT_SMD:SOT-223-3_TabPin2")

    def test_zero_stock_reduces_score(self):
        row = dict(self.BASE_ROW)
        row["stock"] = 0
        keywords = ["3.3v", "ldo"]
        footprint = "Package_TO_SOT_SMD:SOT-223-3_TabPin2"
        score_zero = score_candidate(row, keywords, "SOT-223", "basic", footprint)
        row2 = dict(self.BASE_ROW)
        row2["stock"] = 5000
        score_high = score_candidate(row2, keywords, "SOT-223", "basic", footprint)
        assert score_zero < score_high

    def test_no_footprint_reduces_score(self):
        keywords = ["3.3v", "ldo"]
        score_with = score_candidate(dict(self.BASE_ROW), keywords, None, "basic",
                                     "Package_TO_SOT_SMD:SOT-223-3_TabPin2")
        score_without = score_candidate(dict(self.BASE_ROW), keywords, None, "basic", None)
        assert score_without < score_with

    def test_score_bounded_0_to_1(self):
        for stock in [0, 1, 99, 100, 1000]:
            for tier in ["basic", "extended", "preferred"]:
                row = dict(self.BASE_ROW)
                row["stock"] = stock
                row["assembly_tier"] = tier
                score = score_candidate(row, ["test"], None, "any", "some:footprint")
                assert 0.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Deviations
# ---------------------------------------------------------------------------

class TestBuildDeviations:
    BASE_ROW: dict[str, Any] = {
        "package": "SOT-23-5",
        "assembly_tier": "extended",
        "stock": 47,
    }

    def test_exact_match_no_deviations(self):
        devs = build_deviations(
            {"package": "SOT-23", "assembly_tier": "basic", "stock": 5000},
            "SOT-23", "basic", "Package_TO_SOT_SMD:SOT-23",
        )
        assert devs == []

    def test_wrong_package_in_deviations(self):
        devs = build_deviations(dict(self.BASE_ROW), "SOT-23", "basic", "fp:X")
        pkg_devs = [d for d in devs if "package" in d]
        assert pkg_devs

    def test_tier_downgrade_in_deviations(self):
        devs = build_deviations(dict(self.BASE_ROW), None, "basic", "fp:X")
        tier_devs = [d for d in devs if "assembly_tier" in d]
        assert tier_devs
        # extended should mention fee
        assert any("fee" in d.lower() for d in tier_devs)

    def test_low_stock_in_deviations(self):
        devs = build_deviations(dict(self.BASE_ROW), None, "any", "fp:X")
        stock_devs = [d for d in devs if "stock" in d]
        assert stock_devs

    def test_unmapped_footprint_in_deviations(self):
        devs = build_deviations(
            {"package": "XQFN-24", "assembly_tier": "basic", "stock": 1000},
            None, "any", None,
        )
        fp_devs = [d for d in devs if "footprint" in d.lower()]
        assert fp_devs

    def test_basic_part_not_flagged_when_requesting_extended(self):
        # m-extended-tier: a basic part requested under 'extended' must NOT be
        # flagged as a tier deviation (and certainly not "adds assembly fee").
        devs = build_deviations(
            {"package": "SOT-23", "assembly_tier": "basic", "stock": 5000},
            None, "extended", "fp:X",
        )
        tier_devs = [d for d in devs if "assembly_tier" in d]
        assert tier_devs == []


# ---------------------------------------------------------------------------
# Parse price helper
# ---------------------------------------------------------------------------

class TestParsePrice:
    def test_json_string(self):
        raw = '[{"qFrom":1,"qTo":9,"price":0.1856}]'
        result = _parse_price(raw)
        assert result == [{"qFrom": 1, "qTo": 9, "price": 0.1856}]

    def test_already_list(self):
        raw = [{"qFrom": 1, "qTo": None, "price": 0.25}]
        result = _parse_price(raw)
        assert result == raw

    def test_empty_string(self):
        assert _parse_price("") == []

    def test_none(self):
        assert _parse_price(None) == []

    def test_qto_null_preserved(self):
        raw = '[{"qFrom":100,"qTo":null,"price":0.15}]'
        result = _parse_price(raw)
        assert result[0]["qTo"] is None


# ---------------------------------------------------------------------------
# Attribute LUT helpers
# ---------------------------------------------------------------------------

class TestAttributeLutHelpers:
    SAMPLE_LUT: list[list] = [
        ["Package", {"format": "${default}", "primary": "default",
                     "values": {"default": ["SOT-23", "string"]}}],
        ["Basic/Extended", {"format": "${default}", "primary": "default",
                            "values": {"default": ["Basic", "string"]}}],
        ["Basic/Extended", {"format": "${default}", "primary": "default",
                            "values": {"default": ["Extended", "string"]}}],
        ["Manufacturer", {"format": "${default}", "primary": "default",
                          "values": {"default": ["SGMICRO", "string"]}}],
    ]

    def test_package_from_lut(self):
        pkg = _package_from_attributes([0], self.SAMPLE_LUT)
        assert pkg == "SOT-23"

    def test_tier_basic_from_lut(self):
        tier = _tier_from_attributes([1], self.SAMPLE_LUT)
        assert tier == "basic"

    def test_tier_extended_from_lut(self):
        tier = _tier_from_attributes([2], self.SAMPLE_LUT)
        assert tier == "extended"

    def test_tier_defaults_extended_when_no_attribute(self):
        tier = _tier_from_attributes([3], self.SAMPLE_LUT)  # Manufacturer attr
        assert tier == "extended"

    def test_manufacturer_from_lut(self):
        mfr = _manufacturer_from_attributes([3], self.SAMPLE_LUT)
        assert mfr == "SGMICRO"

    def test_out_of_range_index_ignored(self):
        tier = _tier_from_attributes([99999], self.SAMPLE_LUT)
        assert tier == "extended"

    def test_empty_attributes_list(self):
        assert _package_from_attributes([], self.SAMPLE_LUT) == ""
        assert _tier_from_attributes([], self.SAMPLE_LUT) == "extended"

    # --- malformed-index guards: one bad component must not abort the DB build ---

    def test_tier_float_index_ignored(self):
        # A float index passes a naive `>= len(lut)` check but raises TypeError on
        # lut[idx]. Guard must skip it (not crash the whole build).
        assert _tier_from_attributes([1.5], self.SAMPLE_LUT) == "extended"

    def test_tier_negative_index_ignored(self):
        # A negative index is a valid Python list index → silently reads the WRONG
        # entry. Guard must reject it.
        assert _tier_from_attributes([-1], self.SAMPLE_LUT) == "extended"

    def test_tier_empty_value_list_ignored(self):
        # values["default"] == [] : `values.get(primary, [None])[0]` IndexErrors
        # (the [None] default only applies to a MISSING key, not an empty one) —
        # one such JLCPCB component would abort the entire shard build.
        lut = [["Basic/Extended", {"primary": "default", "values": {"default": []}}]]
        assert _tier_from_attributes([0], lut) == "extended"
        assert _package_from_attributes([0], [["Package", {"primary": "default",
                                                           "values": {"default": []}}]]) == ""


class TestParametricAttributeCapture:
    """m-resolved-attrs: parametric attributes must be captured end-to-end,
    not silently dropped to {}."""

    LUT: list[list] = [
        ["Package", {"primary": "default", "values": {"default": ["SOT-23", "string"]}}],
        ["Resistance", {"primary": "default", "values": {"default": ["10kΩ", "resistance"]}}],
        ["Tolerance", {"primary": "default", "values": {"default": ["1%", "string"]}}],
        ["Manufacturer", {"primary": "default", "values": {"default": ["SGMICRO", "string"]}}],
    ]

    def test_decode_captures_non_promoted_attributes(self):
        attrs = _decode_attributes([0, 1, 2, 3], self.LUT)
        # The parametric attrs that used to be dropped are now present.
        assert attrs["Resistance"] == "10kΩ"
        assert attrs["Tolerance"] == "1%"
        # Promoted ones are still decoded (same single source of truth).
        assert attrs["Package"] == "SOT-23"
        assert attrs["Manufacturer"] == "SGMICRO"

    def test_decode_first_wins_on_duplicate_name(self):
        lut = [
            ["Package", {"primary": "default", "values": {"default": ["SOT-23", "s"]}}],
            ["Package", {"primary": "default", "values": {"default": ["SOT-89", "s"]}}],
        ]
        assert _decode_attributes([0, 1], lut)["Package"] == "SOT-23"

    def test_decode_ignores_out_of_range_and_malformed(self):
        assert _decode_attributes([999, -1, 0], [["X", "not-a-dict"]]) == {}

    def test_parse_attributes_json_string(self):
        assert _parse_attributes('{"Resistance": "10k"}') == {"Resistance": "10k"}

    def test_parse_attributes_dict_passthrough(self):
        assert _parse_attributes({"Voltage": "3.3V"}) == {"Voltage": "3.3V"}

    def test_parse_attributes_none_is_empty(self):
        assert _parse_attributes(None) == {}

    def test_parse_attributes_malformed_json_is_empty(self):
        assert _parse_attributes("{not json") == {}

    def test_parse_attributes_coerces_values_to_str(self):
        assert _parse_attributes({"Pins": 8}) == {"Pins": "8"}

    def _row(self, **over):
        row = {"lcsc": "C1", "mfr": "M", "manufacturer": "MAN",
               "package": "SOT-23", "joints": 3, "description": "d",
               "datasheet": "", "stock": 5, "price": "[]"}
        row.update(over)
        return row

    def test_row_to_resolved_carries_json_attributes(self):
        rp = _row_to_resolved(
            self._row(attributes=json.dumps({"Resistance": "10k"})),
            match_score=None, deviations=[], snapshot_date="",
            fetched_live=False, kicad_symbol_lib_id=None)
        assert rp.attributes == {"Resistance": "10k"}

    def test_row_to_resolved_carries_dict_attributes(self):
        rp = _row_to_resolved(
            self._row(attributes={"Voltage": "3.3V"}),
            match_score=None, deviations=[], snapshot_date="",
            fetched_live=False, kicad_symbol_lib_id=None)
        assert rp.attributes == {"Voltage": "3.3V"}

    def test_row_to_resolved_missing_attributes_is_empty(self):
        rp = _row_to_resolved(
            self._row(),  # v1 snapshot: no attributes column
            match_score=None, deviations=[], snapshot_date="",
            fetched_live=False, kicad_symbol_lib_id=None)
        assert rp.attributes == {}

    def test_live_attributes_list_shape(self):
        data = {"attributes": [
            {"attribute_name_en": "Resistance", "attribute_value_name": "10kΩ"},
            {"attribute_name_en": "Tolerance", "attribute_value_name": "1%"},
        ]}
        assert _live_attributes(data) == {"Resistance": "10kΩ", "Tolerance": "1%"}

    def test_live_attributes_absent(self):
        assert _live_attributes({}) == {}

    def test_live_part_to_row_includes_attributes(self):
        data = {"componentModelEn": "RT0805", "stockCount": 10,
                "attributes": [{"attribute_name_en": "Power", "attribute_value_name": "0.125W"}]}
        row = _live_part_to_row(data, "C1")
        assert row is not None
        assert row["attributes"] == {"Power": "0.125W"}


# ---------------------------------------------------------------------------
# Shard ingestion drop accounting
# ---------------------------------------------------------------------------

class TestDecodeShardRows:
    """Every malformed row must be counted by reason, never silently dropped."""

    COL_MAP = {"lcsc": 0, "mfr": 1, "joints": 2, "description": 3,
               "price": 4, "attributes": 5, "stock": 6, "datasheet": 7}
    LUT: list[list] = []

    def _row(self, lcsc):
        return json.dumps([lcsc, "MFR", 2, "desc", "[]", [], 100, "http://x"])

    def test_wellformed_rows_inserted_zero_drops(self):
        lines = [self._row("C1"), self._row("C2")]
        batch, drops = _decode_shard_rows(lines, self.COL_MAP, self.LUT)
        assert len(batch) == 2
        assert sum(drops.values()) == 0
        assert batch[0][0] == "C1"

    def test_bad_json_counted(self):
        lines = [self._row("C1"), "{not valid json", self._row("C2")]
        batch, drops = _decode_shard_rows(lines, self.COL_MAP, self.LUT)
        assert len(batch) == 2
        assert drops["bad_json"] == 1

    def test_non_list_row_counted(self):
        lines = [self._row("C1"), json.dumps({"lcsc": "C9"})]
        batch, drops = _decode_shard_rows(lines, self.COL_MAP, self.LUT)
        assert len(batch) == 1
        assert drops["not_list"] == 1

    def test_missing_lcsc_counted(self):
        lines = [self._row("C1"),
                 json.dumps([None, "MFR", 2, "d", "[]", [], 5, "x"])]
        batch, drops = _decode_shard_rows(lines, self.COL_MAP, self.LUT)
        assert len(batch) == 1
        assert drops["no_lcsc"] == 1

    def test_blank_lines_not_counted_as_drops(self):
        lines = [self._row("C1"), "", "   "]
        batch, drops = _decode_shard_rows(lines, self.COL_MAP, self.LUT)
        assert len(batch) == 1
        assert sum(drops.values()) == 0

    def test_all_reasons_independently_counted(self):
        lines = [self._row("C1"), "{bad", json.dumps({"x": 1}),
                 json.dumps([None] + ["y"] * 7)]
        batch, drops = _decode_shard_rows(lines, self.COL_MAP, self.LUT)
        assert len(batch) == 1
        assert drops == {"bad_json": 1, "not_list": 1, "no_lcsc": 1}

    def test_attributes_captured_in_tuple(self):
        lut = [["Resistance", {"primary": "default",
                               "values": {"default": ["10kΩ", "r"]}}]]
        row = json.dumps(["C1", "MFR", 2, "desc", "[]", [0], 100, "http"])
        batch, _ = _decode_shard_rows([row], self.COL_MAP, lut)
        assert json.loads(batch[0][-1]) == {"Resistance": "10kΩ"}


# ---------------------------------------------------------------------------
# DB query layer (using fixture DB)
# ---------------------------------------------------------------------------

class TestDbQueries:
    def test_get_component_existing(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        row = get_component("C6186", db_path=db)
        assert row is not None
        assert row["lcsc"] == "C6186"
        assert row["mfr"] == "AMS1117-3.3"
        assert row["assembly_tier"] == "basic"

    def test_get_component_missing(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        assert get_component("C99999999", db_path=db) is None

    def test_search_finds_ldo_regulators(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("LDO regulator 3.3V", assembly_tier="any",
                                   max_results=5, db_path=db)
        assert len(results) >= 1
        assert any("3.3" in r.get("description", "") for r in results)

    def test_search_basic_tier_only(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("LDO regulator", assembly_tier="basic",
                                   max_results=10, db_path=db)
        assert all(r.get("assembly_tier") == "basic" for r in results)

    def test_search_excludes_zero_stock_by_default(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("LDO 3.3V SOT-23-5", assembly_tier="any",
                                   max_results=10, db_path=db)
        # C7777 has stock=0 and should be filtered out
        lcsc_list = [r["lcsc"] for r in results]
        assert "C7777" not in lcsc_list

    def test_search_package_filter(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("regulator", package="SOT-223", assembly_tier="any",
                                   max_results=10, db_path=db)
        assert all(r.get("package") == "SOT-223" for r in results)

    def test_search_respects_max_results(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("regulator", assembly_tier="any", max_results=2, db_path=db)
        assert len(results) <= 2

    def test_search_ranked_by_score(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("3.3V LDO regulator SOT-223", assembly_tier="any",
                                   max_results=5, db_path=db)
        scores = [r["_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_excludes_unmapped_footprint_by_default(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("LDO regulator XQFN", assembly_tier="any",
                                   max_results=5, db_path=db)
        # C5555 has XQFN-24 — unmapped — should be filtered out
        lcsc_list = [r["lcsc"] for r in results]
        assert "C5555" not in lcsc_list

    def test_search_include_unresolvable(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("XQFN LDO regulator", assembly_tier="any",
                                   max_results=5, include_unresolvable=True, db_path=db)
        lcsc_list = [r["lcsc"] for r in results]
        assert "C5555" in lcsc_list

    def test_search_empty_results(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        results = search_components("xyz_doesnt_exist_qwerty", assembly_tier="basic",
                                   max_results=3, db_path=db)
        assert results == []

    def test_search_stable_ordering(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        r1 = search_components("LDO regulator", assembly_tier="any", max_results=5, db_path=db)
        r2 = search_components("LDO regulator", assembly_tier="any", max_results=5, db_path=db)
        assert [r["lcsc"] for r in r1] == [r["lcsc"] for r in r2]


class TestSearchWithTotal:
    """search_components_with_total must report the pre-slice candidate count so
    callers can distinguish 'only N matched' from 'top N of many' (no silent cap)."""

    def test_total_exceeds_returned_when_capped(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        full = search_components("LDO regulator", assembly_tier="any",
                                 max_results=100, db_path=db)
        assert len(full) >= 2, "fixture must have >=2 LDO candidates for this test"
        rows, total = search_components_with_total(
            "LDO regulator", assembly_tier="any", max_results=1, db_path=db)
        assert len(rows) == 1
        assert total == len(full)
        assert total > len(rows)  # i.e. truncated

    def test_total_equals_returned_when_not_capped(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        rows, total = search_components_with_total(
            "LDO regulator", assembly_tier="any", max_results=100, db_path=db)
        assert total == len(rows)  # not truncated

    def test_total_zero_on_no_match(self, tmp_path):
        db, meta = _mock_db(tmp_path)
        rows, total = search_components_with_total(
            "xyz_doesnt_exist_qwerty", assembly_tier="basic", max_results=3, db_path=db)
        assert rows == [] and total == 0

    def test_sliced_rows_match_plain_search(self, tmp_path):
        """The truncating wrapper must return exactly what search_components does
        (single source of truth — no divergent ranking)."""
        db, meta = _mock_db(tmp_path)
        plain = search_components("LDO regulator", assembly_tier="any",
                                  max_results=3, db_path=db)
        rows, _ = search_components_with_total(
            "LDO regulator", assembly_tier="any", max_results=3, db_path=db)
        assert [r["lcsc"] for r in rows] == [r["lcsc"] for r in plain]


# ---------------------------------------------------------------------------
# Freshness / metadata
# ---------------------------------------------------------------------------

class TestFreshness:
    def test_no_db_returns_none(self, tmp_path):
        meta = tmp_path / "meta.json"
        assert get_snapshot_age_days(meta_path=meta) is None

    def test_fresh_snapshot_age_zero(self, tmp_path):
        from datetime import datetime, timezone
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps({
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "snapshot_date": "2026-05-27",
        }))
        age = get_snapshot_age_days(meta_path=meta)
        assert age == 0

    def test_snapshot_date_extracted(self, tmp_path):
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps({
            "downloaded_at": "2026-05-01T10:00:00+00:00",
            "snapshot_date": "2026-04-30",
        }))
        assert get_snapshot_date(meta_path=meta) == "2026-04-30"

    def test_14_days_boundary(self, tmp_path):
        """Exactly 14 days should still be info-level, not warn."""
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps({"downloaded_at": ts}))
        age = get_snapshot_age_days(meta_path=meta)
        assert age == 14

    def test_15_days_boundary(self, tmp_path):
        """Exactly 15 days crosses into warn territory."""
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        meta = tmp_path / "meta.json"
        meta.write_text(json.dumps({"downloaded_at": ts}))
        age = get_snapshot_age_days(meta_path=meta)
        assert age == 15

    def test_30_vs_31_day_boundary(self, tmp_path):
        from datetime import datetime, timedelta, timezone
        for days, expected in [(30, 30), (31, 31)]:
            ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            meta = tmp_path / "meta.json"
            meta.write_text(json.dumps({"downloaded_at": ts}))
            age = get_snapshot_age_days(meta_path=meta)
            assert age == expected


# ---------------------------------------------------------------------------
# Tool-level tests (MCP integration)
# ---------------------------------------------------------------------------

class TestLcscTool:
    """Test the lcsc() MCP tool via the server's tool registry."""

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path, monkeypatch):
        # Use fixture DB
        db, meta = _mock_db(tmp_path)
        monkeypatch.setattr(lcsc_db, "DB_PATH", db)
        monkeypatch.setattr(lcsc_db, "META_PATH", meta)
        # Bypass TOS check via env
        monkeypatch.setenv("KICAD_MCP_LCSC_TOS_ACCEPTED", "1")
        # Patch TOS file path
        import kicad_mcp.tools.lcsc as lcsc_module
        monkeypatch.setattr(lcsc_module, "TOS_FILE", tmp_path / "tos.json")
        monkeypatch.setattr(lcsc_module, "DB_PATH", db)
        monkeypatch.setattr(lcsc_module, "META_PATH", meta)

    @property
    def _fn(self):
        return _get_tool()

    def test_search_returns_ok(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search", description="3.3V LDO regulator SOT-223")
        assert result["status"] == "ok"
        assert isinstance(result["results"], list)

    def test_search_results_have_resolved_part_shape(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search", description="3.3V LDO regulator")
        results = result.get("results", [])
        assert len(results) >= 1
        r = results[0]
        # Check required fields from ResolvedPart
        for field in ("supplier_name", "supplier_part_number", "mpn", "manufacturer",
                       "description", "package", "assembly_tier", "stock",
                       "price_tiers", "match_score", "match_deviations", "snapshot_date"):
            assert field in r, f"Missing field: {field}"

    def test_search_match_score_bounds(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search", description="LDO regulator", assembly_tier="any")
        for r in result.get("results", []):
            score = r.get("match_score")
            assert score is None or (0.0 <= score <= 1.0)

    def test_search_no_results_empty_list(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search", description="xyzzy_nonexistent_part_abc123")
        assert result["status"] == "ok"
        assert result["results"] == []

    def test_search_requires_description(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search")
        assert result["status"] == "error"
        assert "description" in result.get("message", "").lower()

    def test_search_invalid_tier_returns_error(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search", description="LDO", assembly_tier="invalid_tier")
        assert result["status"] == "error"

    def test_search_max_results_respected(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="search", description="regulator", assembly_tier="any", max_results=2)
        assert len(result.get("results", [])) <= 2

    def test_search_description_truncated_at_256(self, tmp_path, monkeypatch):
        fn = self._fn
        long_desc = "LDO regulator " + "x" * 300
        result = fn(operation="search", description=long_desc, assembly_tier="any")
        # Should succeed without error (truncation + warn event)
        assert result["status"] == "ok"

    def test_resolve_existing_part(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="resolve", part_number="C6186")
        assert result["status"] == "ok"
        part = result.get("part", {})
        assert part.get("supplier_part_number") == "C6186"
        assert part.get("mpn") == "AMS1117-3.3"

    def test_resolve_without_c_prefix(self, tmp_path, monkeypatch):
        """resolve should accept '6186' and normalize to 'C6186'."""
        fn = self._fn
        result = fn(operation="resolve", part_number="6186")
        assert result["status"] == "ok"
        assert result["part"]["supplier_part_number"] == "C6186"

    def test_resolve_missing_part_live_fallback(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.lcsc as lcsc_module
        monkeypatch.setattr(lcsc_module, "_fetch_live_part", lambda pn: None)
        fn = self._fn
        result = fn(operation="resolve", part_number="C99999999")
        assert result["status"] == "error"
        assert result["code"] == "part_not_found"

    def test_resolve_requires_part_number(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="resolve")
        assert result["status"] == "error"
        assert "part_number" in result.get("message", "").lower()

    def test_resolve_match_score_is_none(self, tmp_path, monkeypatch):
        """resolve returns exact lookup; match_score should be None."""
        fn = self._fn
        result = fn(operation="resolve", part_number="C6186")
        assert result["part"]["match_score"] is None
        assert result["part"]["match_deviations"] == []

    def test_assign_no_schematic_loaded(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.schematic_impl as sch_mod
        monkeypatch.setattr(sch_mod, "_current_schematic", None)
        fn = self._fn
        result = fn(operation="assign", reference="U1", part_number="C6186")
        assert result["status"] == "error"
        assert result["code"] == "no_schematic_loaded"

    def test_assign_reference_not_found(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.schematic_impl as sch_mod
        mock_sch = MagicMock()
        mock_sch.components.get.return_value = None
        monkeypatch.setattr(sch_mod, "_current_schematic", mock_sch)
        fn = self._fn
        result = fn(operation="assign", reference="U99", part_number="C6186")
        assert result["status"] == "error"
        assert result["code"] == "reference_not_found"

    def test_assign_sets_value_and_lcsc(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.schematic_impl as sch_mod
        mock_comp = MagicMock()
        mock_comp._data.lib_id = ""
        mock_sch = MagicMock()
        mock_sch.components.get.return_value = mock_comp
        monkeypatch.setattr(sch_mod, "_current_schematic", mock_sch)
        fn = self._fn
        result = fn(operation="assign", reference="U1", part_number="C6186")
        assert result["status"] == "ok"
        assert result["applied"]["value"] == "AMS1117-3.3"
        assert result["applied"]["lcsc_property"] == "C6186"

    def test_assign_sets_footprint_when_mapped(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.schematic_impl as sch_mod
        mock_comp = MagicMock()
        mock_comp._data.lib_id = ""
        mock_sch = MagicMock()
        mock_sch.components.get.return_value = mock_comp
        monkeypatch.setattr(sch_mod, "_current_schematic", mock_sch)
        fn = self._fn
        result = fn(operation="assign", reference="U1", part_number="C6186")
        # C6186 has SOT-223 package → footprint should be set
        assert result["applied"]["footprint"] is not None
        assert "SOT-223" in result["applied"]["footprint"]

    def test_assign_footprint_none_when_unmapped(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.schematic_impl as sch_mod
        mock_comp = MagicMock()
        mock_comp._data.lib_id = ""
        mock_sch = MagicMock()
        mock_sch.components.get.return_value = mock_comp
        monkeypatch.setattr(sch_mod, "_current_schematic", mock_sch)
        fn = self._fn
        # C5555 has XQFN-24 — unmapped
        result = fn(operation="assign", reference="U1", part_number="C5555")
        assert result["status"] == "ok"
        assert result["applied"]["footprint"] is None

    def test_unknown_operation(self, tmp_path, monkeypatch):
        fn = self._fn
        result = fn(operation="frobnicate")
        assert result["status"] == "error"
        assert result["code"] == "unknown_operation"


# ---------------------------------------------------------------------------
# ToS flow
# ---------------------------------------------------------------------------

class TestTosFlow:
    def test_first_call_without_acceptance_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.delenv("KICAD_MCP_LCSC_TOS_ACCEPTED", raising=False)
        import kicad_mcp.tools.lcsc as lcsc_module
        monkeypatch.setattr(lcsc_module, "TOS_FILE", tmp_path / "tos_nonexistent.json")
        fn = _get_tool()
        result = fn(operation="search", description="LDO")
        assert result["status"] == "error"
        assert result["code"] == "lcsc_tos_acceptance_required"
        assert "accept_tos" in result.get("message", "")

    def test_accept_tos_creates_file(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.lcsc as lcsc_module
        tos_path = tmp_path / "tos.json"
        monkeypatch.setattr(lcsc_module, "TOS_FILE", tos_path)
        monkeypatch.setattr(lcsc_module, "CACHE_DIR", tmp_path)
        fn = _get_tool()
        result = fn(operation="accept_tos")
        assert result["status"] == "ok"
        assert tos_path.exists()
        data = json.loads(tos_path.read_text())
        assert "accepted_at" in data
        assert data["acceptance_hash"] == lcsc_module._TOS_HASH

    def test_accept_tos_idempotent(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.lcsc as lcsc_module
        tos_path = tmp_path / "tos.json"
        monkeypatch.setattr(lcsc_module, "TOS_FILE", tos_path)
        monkeypatch.setattr(lcsc_module, "CACHE_DIR", tmp_path)
        fn = _get_tool()
        r1 = fn(operation="accept_tos")
        r2 = fn(operation="accept_tos")
        assert r1["status"] == "ok"
        assert r2["status"] == "ok"

    def test_env_var_bypasses_tos(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KICAD_MCP_LCSC_TOS_ACCEPTED", "1")
        import kicad_mcp.tools.lcsc as lcsc_module
        monkeypatch.setattr(lcsc_module, "TOS_FILE", tmp_path / "nonexistent.json")
        # patch DB so we don't download
        db, meta = _mock_db(tmp_path)
        monkeypatch.setattr(lcsc_module, "DB_PATH", db)
        monkeypatch.setattr(lcsc_module, "META_PATH", meta)
        monkeypatch.setattr(lcsc_db, "DB_PATH", db)
        monkeypatch.setattr(lcsc_db, "META_PATH", meta)
        fn = _get_tool()
        result = fn(operation="search", description="LDO")
        assert result["status"] == "ok"

    def test_changed_tos_text_invalidates_acceptance(self, tmp_path, monkeypatch):
        import kicad_mcp.tools.lcsc as lcsc_module
        monkeypatch.delenv("KICAD_MCP_LCSC_TOS_ACCEPTED", raising=False)
        tos_path = tmp_path / "tos.json"
        tos_path.write_text(json.dumps({
            "accepted_at": "2026-01-01T00:00:00Z",
            "acceptance_hash": "old_stale_hash_that_doesnt_match",
        }))
        monkeypatch.setattr(lcsc_module, "TOS_FILE", tos_path)
        fn = _get_tool()
        result = fn(operation="search", description="LDO")
        assert result["status"] == "error"
        assert result["code"] == "lcsc_tos_acceptance_required"
