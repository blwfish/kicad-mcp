"""Part-name extraction from firmware — find which SPECIFIC part the user
declared (``INMP441``, not a generic "I2S mic"), so the front end can resolve to
it instead of inventing a substitute (the SPH0645 problem). C1 of the
part-resolution feature; see ``docs/SPEC_Firmware_Part_Resolution.md``.

Two pieces:
  * ``collect_corpus`` — the I/O shell. Gathers searchable text: the PREPROCESSED
    config.h (so a part named only in a dead ``#else`` branch stays invisible),
    bounded sibling source files, and a few docs.
  * ``extract_part_names`` — pure. Scans the corpus for the RAW recognized names
    (``\\bICS-43434\\b``), never the canonical form (blocker I1: canonicalizing
    first would strip the hyphen and never match the source).

Data-capture rule: the source sweep is bounded, but a cap is never silent — the
corpus carries ``truncated`` / ``files_scanned`` / ``files_errored`` so a caller
can tell coverage was limited. Reads catch specific exceptions and count them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


# Evidence strength, low = strongest: a part named in compiled source outranks a
# config comment, which outranks prose docs. Used to rank when a name appears in
# several places (the resolver wants the most authoritative sighting first).
_KIND_PRIORITY = {"source": 0, "config_comment": 1, "doc": 2}

# Bounded source sweep (data-capture rule: a cap MUST be observable, never silent).
_MAX_SOURCE_FILES = 200
_SKIP_DIRS = frozenset({"build", ".pio", ".pio_libdeps", ".git", "node_modules",
                        ".cache", "managed_components"})
_SOURCE_SUFFIXES = frozenset({".cpp", ".c", ".cc", ".h", ".hpp", ".ino"})
_DOC_NAMES_RE = re.compile(r"^(README|WIRING|HARDWARE|BOM|CLAUDE).*\.md$", re.IGNORECASE)


@dataclass(frozen=True)
class CorpusEntry:
    """One searchable blob: the preprocessed config text, a source file, or a doc."""
    text: str
    file: str          # source path, or "<config>" path for the preprocessed text
    kind: str          # "source" | "config_comment" | "doc"


@dataclass
class FirmwareCorpus:
    """The gathered text plus honest coverage accounting (data-capture rule)."""
    entries: list[CorpusEntry] = field(default_factory=list)
    files_scanned: int = 0      # source/doc files actually read
    files_errored: int = 0      # files that failed to read (counted, not dropped silently)
    truncated: bool = False     # hit _MAX_SOURCE_FILES — coverage was capped


@dataclass(frozen=True)
class PartEvidence:
    """A single sighting of a recognized part name in the corpus."""
    part_name: str     # the RAW name as matched (hyphen intact: "ICS-43434")
    canonical: str     # canonical lookup key (the resolver fetches the card by this)
    file: str
    line: int          # 1-indexed
    kind: str
    raw_text: str      # the line it appeared on, trimmed (≤200 chars)


def collect_corpus(config_path: str, config_text: str) -> FirmwareCorpus:
    """Gather searchable firmware text rooted at ``config_path``'s directory.

    ``config_text`` MUST be the preprocessed config.h (post
    ``select_active_branches``) — a part named only in an inactive ``#else`` is
    then correctly absent. Sibling ``*.cpp/.c/.h/.hpp/.ino`` and a few docs
    (README/WIRING/HARDWARE/BOM/CLAUDE ``.md``) under the config's directory are
    read from disk, skipping build/output trees, capped at ``_MAX_SOURCE_FILES``
    with ``truncated`` set when the cap bites.
    """
    cfg = Path(config_path)
    corpus = FirmwareCorpus(
        entries=[CorpusEntry(text=config_text, file=str(cfg), kind="config_comment")]
    )
    root = cfg.resolve().parent
    # Deterministic order so the cap (if hit) drops a stable tail, and evidence
    # ordering is reproducible across runs.
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        is_source = path.suffix.lower() in _SOURCE_SUFFIXES
        is_doc = bool(_DOC_NAMES_RE.match(path.name))
        if not (is_source or is_doc):
            continue
        if path == cfg.resolve():
            continue  # the config is already in as preprocessed config_comment text
        if corpus.files_scanned >= _MAX_SOURCE_FILES:
            corpus.truncated = True
            break
        try:
            text = path.read_text(errors="replace")
        except OSError:
            corpus.files_errored += 1   # counted, never silently swallowed
            continue
        corpus.entries.append(
            CorpusEntry(text=text, file=str(path),
                        kind="source" if is_source else "doc")
        )
        corpus.files_scanned += 1
    return corpus


def extract_part_names(
    corpus: FirmwareCorpus, recognized_names: dict[str, str]
) -> list[PartEvidence]:
    """Find every sighting of a recognized RAW part name in the corpus.

    ``recognized_names`` maps RAW name → canonical key (from
    ``cards.recognized_part_names``). Each raw name is matched whole-word and
    case-insensitively, so ``INMP441`` matches but ``INMP4411`` does not, and the
    hyphen in ``ICS-43434`` is preserved (blocker I1). Returns evidence sorted by
    kind strength (source > config_comment > doc), then file, then line.
    """
    # Guard BOTH sides against a word char OR a hyphen, not just \b: a part name
    # is a unit, so a hyphenated neighbour is a DIFFERENT part — "ICS-43434-PDM"
    # and "PRE-INMP441" must NOT resolve to the bare "ICS-43434" / "INMP441" card.
    # (Plain \b treats the inner hyphen as a boundary and would false-match those.)
    patterns = [
        (raw, canon,
         re.compile(r"(?<![\w-])" + re.escape(raw) + r"(?![\w-])", re.IGNORECASE))
        for raw, canon in recognized_names.items()
    ]
    out: list[PartEvidence] = []
    for entry in corpus.entries:
        for lineno, line in enumerate(entry.text.splitlines(), 1):
            for raw, canon, pat in patterns:
                if pat.search(line):
                    out.append(PartEvidence(
                        part_name=raw, canonical=canon, file=entry.file,
                        # raw_text is DISPLAY-ONLY context; the match (part_name/
                        # canonical/line) is complete, so this cap loses no data.
                        line=lineno, kind=entry.kind, raw_text=line.strip()[:200],
                    ))
    out.sort(key=lambda e: (_KIND_PRIORITY.get(e.kind, 9), e.file, e.line))
    return out
