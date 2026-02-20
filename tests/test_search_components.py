"""
Tests for the search_components MCP tool and underlying _load_library() implementation.

These tests verify that:
1. _load_library() correctly parses .kicad_sym files and populates the symbol cache
2. The search index is built and queried correctly
3. search_components MCP tool returns properly structured results
4. Cache invalidation works based on file modification times
"""

import asyncio
import os
import sqlite3
import tempfile
import time

import pytest
import sexpdata
from fastmcp import FastMCP

from kicad_mcp.tools.schematic import register_schematic_tools
import kicad_mcp.tools.schematic as sch_module


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def sch_server():
    """Create a FastMCP server with only schematic tools registered."""
    mcp = FastMCP("test-search")
    register_schematic_tools(mcp)
    return mcp


@pytest.fixture(autouse=True)
def reset_schematic_state():
    """Reset the module-level schematic state between tests."""
    sch_module._current_schematic = None
    yield
    sch_module._current_schematic = None


@pytest.fixture
def symbol_cache():
    """Get a fresh, isolated symbol cache instance.

    Creates a SymbolLibraryCache without calling discover_libraries(),
    so it starts empty and only contains what the test explicitly adds.
    Patches the global singleton so rebuild_index() sees this cache.
    """
    import kicad_sch_api.library.cache as cache_mod

    old_cache = cache_mod._global_cache
    cache = cache_mod.SymbolLibraryCache()
    cache_mod._global_cache = cache
    yield cache
    cache_mod._global_cache = old_cache


@pytest.fixture
def search_index(tmp_path):
    """Get a fresh search index with a temporary DB."""
    from kicad_sch_api.discovery.search_index import ComponentSearchIndex
    return ComponentSearchIndex(cache_dir=tmp_path)


@pytest.fixture
def minimal_kicad_sym(tmp_path):
    """Create a minimal .kicad_sym file for testing."""
    content = """(kicad_symbol_lib
  (version 20241209)
  (symbol "TestResistor"
    (property "Reference" "R"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "TestResistor"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Description" "Test resistor for unit tests"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "ki_keywords" "R res resistor test"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "Datasheet" "~"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (symbol "TestResistor_0_1"
      (polyline
        (pts (xy 0 -2.286) (xy 0 -2.54))
        (stroke (width 0) (type default))
        (fill (type none))
      )
    )
    (symbol "TestResistor_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))
      )
      (pin passive line (at 0 -3.81 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))
      )
    )
  )
  (symbol "TestCapacitor"
    (property "Reference" "C"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "TestCapacitor"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Description" "Test capacitor for unit tests"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "ki_keywords" "C cap capacitor test"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (symbol "TestCapacitor_1_1"
      (pin passive line (at 0 2.54 270) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "1" (effects (font (size 1.27 1.27))))
      )
      (pin passive line (at 0 -2.54 90) (length 1.27)
        (name "~" (effects (font (size 1.27 1.27))))
        (number "2" (effects (font (size 1.27 1.27))))
      )
    )
  )
  (symbol "TestExtended"
    (extends "TestResistor")
    (property "Reference" "R"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "TestExtended"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Description" "Extended variant, should be skipped by _load_library"
      (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
  )
)"""
    lib_file = tmp_path / "TestLib.kicad_sym"
    lib_file.write_text(content)
    return lib_file


def _get_tool_fn(mcp_server, tool_name):
    """Extract a tool function from the FastMCP 3.0 server by name."""
    tool = asyncio.run(mcp_server.get_tool(tool_name))
    if tool is None:
        raise ValueError(f"Tool {tool_name!r} not found")
    return tool.fn


# -- _load_library() unit tests ---------------------------------------------

class TestLoadLibrary:
    """Tests for the _load_library() method on SymbolLibraryCache."""

    def test_loads_symbols_from_file(self, symbol_cache, minimal_kicad_sym):
        """_load_library should parse the file and populate self._symbols."""
        result = symbol_cache._load_library(minimal_kicad_sym)
        assert result is True

        # Should have loaded the two top-level symbols (not the extends variant)
        lib_name = minimal_kicad_sym.stem  # "TestLib"
        loaded = [s for s in symbol_cache._symbols.values() if s.library == lib_name]
        assert len(loaded) == 2

    def test_extracts_symbol_names(self, symbol_cache, minimal_kicad_sym):
        """Loaded symbols should have correct names."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        names = {s.name for s in symbol_cache._symbols.values() if s.library == lib_name}
        assert "TestResistor" in names
        assert "TestCapacitor" in names

    def test_skips_extends_symbols(self, symbol_cache, minimal_kicad_sym):
        """Symbols with 'extends' directive should be skipped."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        names = {s.name for s in symbol_cache._symbols.values() if s.library == lib_name}
        assert "TestExtended" not in names

    def test_skips_sub_unit_symbols(self, symbol_cache, minimal_kicad_sym):
        """Sub-unit symbols like 'Name_0_1' should be skipped."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        names = {s.name for s in symbol_cache._symbols.values() if s.library == lib_name}
        assert "TestResistor_0_1" not in names
        assert "TestResistor_1_1" not in names
        assert "TestCapacitor_1_1" not in names

    def test_extracts_description(self, symbol_cache, minimal_kicad_sym):
        """Descriptions should be extracted from properties."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        resistor = symbol_cache._symbols.get(f"{lib_name}:TestResistor")
        assert resistor is not None
        assert resistor.description == "Test resistor for unit tests"

    def test_extracts_keywords(self, symbol_cache, minimal_kicad_sym):
        """Keywords should be extracted from ki_keywords property."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        resistor = symbol_cache._symbols.get(f"{lib_name}:TestResistor")
        assert "resistor" in resistor.keywords
        assert "test" in resistor.keywords

    def test_extracts_reference_prefix(self, symbol_cache, minimal_kicad_sym):
        """Reference prefix should come from the Reference property."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        resistor = symbol_cache._symbols.get(f"{lib_name}:TestResistor")
        assert resistor.reference_prefix == "R"

        capacitor = symbol_cache._symbols.get(f"{lib_name}:TestCapacitor")
        assert capacitor.reference_prefix == "C"

    def test_extracts_lib_id(self, symbol_cache, minimal_kicad_sym):
        """lib_id should be in Library:Symbol format."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        resistor = symbol_cache._symbols.get(f"{lib_name}:TestResistor")
        assert resistor.lib_id == f"{lib_name}:TestResistor"

    def test_updates_lib_stats(self, symbol_cache, minimal_kicad_sym):
        """Library stats should be updated after loading."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        assert lib_name in symbol_cache._lib_stats
        stats = symbol_cache._lib_stats[lib_name]
        assert stats.symbol_count == 2
        assert stats.load_time > 0

    def test_idempotent_reload(self, symbol_cache, minimal_kicad_sym):
        """Loading the same library twice should not duplicate symbols."""
        symbol_cache._load_library(minimal_kicad_sym)
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        loaded = [s for s in symbol_cache._symbols.values() if s.library == lib_name]
        assert len(loaded) == 2

    def test_nonexistent_file_returns_false(self, symbol_cache, tmp_path):
        """Loading a nonexistent file should return False."""
        fake_path = tmp_path / "nonexistent.kicad_sym"
        result = symbol_cache._load_library(fake_path)
        assert result is False

    def test_malformed_file_returns_false(self, symbol_cache, tmp_path):
        """Loading a malformed file should return False."""
        bad_file = tmp_path / "Bad.kicad_sym"
        bad_file.write_text("this is not valid s-expression data {{{")
        result = symbol_cache._load_library(bad_file)
        assert result is False


# -- get_library_symbols() tests ---------------------------------------------

class TestGetLibrarySymbols:
    """Tests for get_library_symbols which calls _load_library internally."""

    def test_returns_list_of_symbol_definitions(self, symbol_cache, minimal_kicad_sym):
        """Should return a list of SymbolDefinition objects."""
        from kicad_sch_api.library.cache import SymbolDefinition

        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym

        symbols = symbol_cache.get_library_symbols(lib_name)
        assert len(symbols) == 2
        assert all(isinstance(s, SymbolDefinition) for s in symbols)

    def test_unknown_library_returns_empty(self, symbol_cache):
        """Requesting an unknown library should return empty list."""
        result = symbol_cache.get_library_symbols("NonexistentLibrary")
        assert result == []


# -- Search index tests ------------------------------------------------------

class TestSearchIndex:
    """Tests for ComponentSearchIndex rebuild and search."""

    def test_rebuild_populates_db(self, search_index, symbol_cache, minimal_kicad_sym):
        """rebuild_index should populate the SQLite database."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym

        count = search_index.rebuild_index()
        assert count == 2

    def test_search_finds_by_name(self, search_index, symbol_cache, minimal_kicad_sym):
        """Search should find symbols by name."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        results = search_index.search("TestResistor")
        assert len(results) >= 1
        assert results[0]["lib_id"] == f"{lib_name}:TestResistor"

    def test_search_finds_by_description(self, search_index, symbol_cache, minimal_kicad_sym):
        """Search should find symbols by description content."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        results = search_index.search("capacitor")
        assert len(results) >= 1
        assert any("TestCapacitor" in r["lib_id"] for r in results)

    def test_search_empty_query_returns_empty(self, search_index, symbol_cache, minimal_kicad_sym):
        """Empty query should return empty results."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        results = search_index.search("")
        assert len(results) == 0

    def test_search_no_match_returns_empty(self, search_index, symbol_cache, minimal_kicad_sym):
        """Query with no matches should return empty."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        results = search_index.search("zzz_nonexistent_component")
        assert len(results) == 0

    def test_search_respects_limit(self, search_index, symbol_cache, minimal_kicad_sym):
        """Search should respect the limit parameter."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        results = search_index.search("Test", limit=1)
        assert len(results) <= 1

    def test_search_filters_by_library(self, search_index, symbol_cache, minimal_kicad_sym):
        """Search should filter by library name when specified."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        results = search_index.search("Test", library=lib_name)
        assert len(results) >= 1
        assert all(r["library"] == lib_name for r in results)

        results = search_index.search("Test", library="NonexistentLib")
        assert len(results) == 0


# -- Cache invalidation tests -----------------------------------------------

class TestCacheInvalidation:
    """Tests for cache freshness checking."""

    def test_detects_modified_library(self, symbol_cache, minimal_kicad_sym):
        """Should reload library when file has been modified."""
        symbol_cache._load_library(minimal_kicad_sym)
        lib_name = minimal_kicad_sym.stem

        initial_count = len([s for s in symbol_cache._symbols.values()
                            if s.library == lib_name])
        assert initial_count == 2

        # Touch the file to update mtime
        time.sleep(0.1)
        minimal_kicad_sym.touch()

        # Clear the symbols but keep stats to trigger the mtime check
        keys_to_remove = [k for k, v in symbol_cache._symbols.items()
                         if v.library == lib_name]
        for k in keys_to_remove:
            del symbol_cache._symbols[k]

        # Should reload because mtime changed and no symbols present
        symbol_cache._load_library(minimal_kicad_sym)
        reloaded_count = len([s for s in symbol_cache._symbols.values()
                             if s.library == lib_name])
        assert reloaded_count == 2


# -- Search index staleness tests -------------------------------------------

class TestSearchIndexStaleness:
    """Tests for is_stale() and automatic rebuild on library changes."""

    def test_empty_index_is_stale(self, search_index, symbol_cache, minimal_kicad_sym):
        """An index with no rows should be stale."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        assert search_index.is_stale() is True

    def test_freshly_built_index_is_not_stale(self, search_index, symbol_cache, minimal_kicad_sym):
        """An index rebuilt from current libraries should not be stale."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()
        assert search_index.is_stale() is False

    def test_modified_library_makes_index_stale(self, search_index, symbol_cache, minimal_kicad_sym):
        """Touching a library file after build should make the index stale."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()
        assert search_index.is_stale() is False

        # Touch the file so its mtime is newer than built_at
        time.sleep(0.1)
        minimal_kicad_sym.touch()
        assert search_index.is_stale() is True

    def test_rebuild_clears_staleness(self, search_index, symbol_cache, minimal_kicad_sym):
        """Rebuilding after a library change should clear staleness."""
        lib_name = minimal_kicad_sym.stem
        symbol_cache._library_index[lib_name] = minimal_kicad_sym
        search_index.rebuild_index()

        time.sleep(0.1)
        minimal_kicad_sym.touch()
        assert search_index.is_stale() is True

        search_index.rebuild_index()
        assert search_index.is_stale() is False


# -- search_components MCP tool tests ----------------------------------------

class TestSearchComponentsTool:
    """Tests for the search_components MCP tool wrapper."""

    def test_tool_returns_structured_result(self, sch_server):
        """search_components should return status and results list."""
        fn = _get_tool_fn(sch_server, "search_components")
        # This may trigger index build on first call; use a query
        # that's unlikely to match much to keep it fast
        result = fn(query="zzz_no_match_expected", limit=5)
        assert result["status"] == "ok"
        assert "count" in result
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_tool_respects_limit(self, sch_server):
        """Results should not exceed the limit parameter."""
        fn = _get_tool_fn(sch_server, "search_components")
        result = fn(query="R", limit=3)
        assert result["status"] == "ok"
        assert len(result["results"]) <= 3

    def test_tool_result_fields(self, sch_server):
        """Each result should have lib_id, name, description, etc."""
        fn = _get_tool_fn(sch_server, "search_components")
        result = fn(query="resistor", limit=1)
        if result["count"] > 0:
            item = result["results"][0]
            assert "lib_id" in item
            assert "name" in item
            assert "description" in item
