"""Tests for the firmware part-name extractor (C1). No KiCad.

Boundary-focused per CLAUDE.md: the whole-word regex is pinned at/below/above,
the hyphenated ICS-43434 case is an I1 regression, and collect_corpus's bounded
scan + overflow/error counters are exercised (data-capture rule — a cap is never
silent).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.utils.firmware import part_extractor as pe
from kicad_mcp.utils.firmware.part_extractor import (
    CorpusEntry,
    FirmwareCorpus,
    collect_corpus,
    extract_part_names,
)

# raw name -> canonical key, as cards.recognized_part_names() would return.
_NAMES = {"INMP441": "INMP441", "ICS-43434": "ICS43434", "SPH0645LM4H": "SPH0645"}


def _corpus(text: str, kind: str = "source", file: str = "x.cpp") -> FirmwareCorpus:
    return FirmwareCorpus(entries=[CorpusEntry(text=text, file=file, kind=kind)])


# --- whole-word matching: at / below / above the boundary --------------------

@pytest.mark.parametrize("line,hits", [
    ("#define MIC INMP441", ["INMP441"]),        # exact (at)
    ("part is INMP4411 maybe", []),              # trailing word char (above) — no match
    ("INMP44 short", []),                        # shorter (below) — no match
    ("the inmp441 mic", ["INMP441"]),            # case-insensitive
    ("// using ICS-43434 here", ["ICS-43434"]),  # hyphen preserved (I1)
    ("XICS-43434", []),                          # leading word char — no boundary
    ("ICS-43434-PDM variant", []),               # trailing hyphen → different part
    ("PRE-INMP441", []),                         # leading hyphen → different part
])
def test_word_boundary(line, hits):
    ev = extract_part_names(_corpus(line), _NAMES)
    assert [e.part_name for e in ev] == hits


@pytest.mark.parametrize("corpus", [
    FirmwareCorpus(entries=[]),                          # empty corpus
    FirmwareCorpus(entries=[CorpusEntry("INMP441", "x", "source")]),
])
def test_extract_empty_recognized_names_is_empty(corpus):
    # no recognized names → nothing to find, regardless of corpus content
    assert extract_part_names(corpus, {}) == []


def test_extract_prefix_name_not_overmatched():
    # SPH0645 is a prefix of SPH0645LM4H; text has only the longer form. The
    # shorter name must NOT match the longer token (lookahead rejects trailing \w).
    names = {"SPH0645": "SPH0645", "SPH0645LM4H": "SPH0645LM4H"}
    ev = extract_part_names(_corpus("part SPH0645LM4H here"), names)
    assert [e.part_name for e in ev] == ["SPH0645LM4H"]


def test_hyphenated_raw_maps_to_canonical_I1():
    # I1: match the RAW hyphenated name in text, resolve via the canonical key.
    ev = extract_part_names(_corpus("sensor: ICS-43434"), _NAMES)
    assert ev[0].part_name == "ICS-43434"
    assert ev[0].canonical == "ICS43434"


def test_alias_maps_to_card_canonical():
    ev = extract_part_names(_corpus('part = "SPH0645LM4H";'), _NAMES)
    assert ev[0].part_name == "SPH0645LM4H"
    assert ev[0].canonical == "SPH0645"


def test_matches_in_comment_string_and_code():
    text = "\n".join([
        "#define MIC_DATA 41   // INMP441 data",   # inline comment
        "// --- INMP441 wiring ---",               # standalone comment
        'const char* part = "INMP441";',           # string literal
    ])
    ev = extract_part_names(_corpus(text), _NAMES)
    assert len(ev) == 3
    assert all(e.part_name == "INMP441" for e in ev)
    assert [e.line for e in ev] == [1, 2, 3]


def test_dead_branch_absent_when_preprocessed():
    # The caller passes PREPROCESSED config text; a part only in the dropped
    # #else branch is already gone, so it is correctly not found.
    preprocessed = "#define MIC INMP441\n"   # the inactive #else SPH0645 line is stripped
    ev = extract_part_names(_corpus(preprocessed, kind="config_comment"), _NAMES)
    assert [e.part_name for e in ev] == ["INMP441"]


def test_priority_source_beats_config_beats_doc():
    corpus = FirmwareCorpus(entries=[
        CorpusEntry("INMP441", "a.md", "doc"),
        CorpusEntry("INMP441", "<config>", "config_comment"),
        CorpusEntry("INMP441", "a.cpp", "source"),
    ])
    ev = extract_part_names(corpus, _NAMES)
    assert [e.kind for e in ev] == ["source", "config_comment", "doc"]


# --- collect_corpus: the bounded I/O sweep -----------------------------------

def _write(p: Path, content: str = "x") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def test_collect_corpus_scans_sources_and_docs_skips_build(tmp_path):
    cfg = tmp_path / "config.h"
    _write(cfg, "#define MIC INMP441\n")
    _write(tmp_path / "main.cpp", "INMP441")
    _write(tmp_path / "README.md", "uses INMP441")
    _write(tmp_path / "build" / "junk.cpp", "INMP441")   # build tree → skipped
    _write(tmp_path / "notes.txt", "INMP441")            # not source/doc → ignored
    corpus = collect_corpus(str(cfg), cfg.read_text())
    files = {Path(e.file).name for e in corpus.entries}
    assert {"config.h", "main.cpp", "README.md"} <= files
    assert "junk.cpp" not in files and "notes.txt" not in files
    assert corpus.files_scanned == 2 and not corpus.truncated
    cfg_entry = next(e for e in corpus.entries if Path(e.file).name == "config.h")
    assert cfg_entry.kind == "config_comment"


def test_collect_corpus_truncation_is_flagged(tmp_path, monkeypatch):
    cfg = tmp_path / "config.h"
    _write(cfg)
    for i in range(5):
        _write(tmp_path / f"f{i}.cpp", "INMP441")
    monkeypatch.setattr(pe, "_MAX_SOURCE_FILES", 2)
    corpus = collect_corpus(str(cfg), cfg.read_text())
    assert corpus.truncated is True
    assert corpus.files_scanned == 2


def test_collect_corpus_counts_read_errors(tmp_path, monkeypatch):
    cfg = tmp_path / "config.h"
    _write(cfg)
    _write(tmp_path / "ok.cpp", "INMP441")
    _write(tmp_path / "bad.cpp", "INMP441")
    orig = Path.read_text

    def flaky(self, *a, **k):
        if self.name == "bad.cpp":
            raise OSError("simulated unreadable file")
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    corpus = collect_corpus(str(cfg), "preprocessed config text")
    assert corpus.files_errored == 1
    names = {Path(e.file).name for e in corpus.entries}
    assert "ok.cpp" in names and "bad.cpp" not in names
