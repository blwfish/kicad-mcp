#!/usr/bin/env python3
"""Phase 3 corpus harness — the fail-safe sweep + carding-backlog ranker.

Runs the deterministic firmware front end (import -> expand, NO routing, no
KiCad) over every example sketch in the configured corpus roots and asserts the
fail-safe contract: foreign code NEVER crashes the importer — it degrades to
honest gaps. Exits non-zero on any crash (the nightly red signal).

The corpus is MACHINE STATE (framework installs + per-project libdeps), read in
place — never copied into the repo (licensing) and never a per-PR gate (a new
third-party sketch must not block an unrelated PR). Each run writes a manifest
of exactly what was swept so a regression report can distinguish "the code
changed" from "the corpus changed".

Also emits the carding backlog: a per-PROJECT census of device-shaped library
dependencies (each libdeps entry = a real project uses it), mapped to device
names via the curated LIB_TO_DEVICE table below. Per-project counting avoids
the per-example-file bias (U8g2 ships hundreds of examples; that says nothing
about how many projects use a display). The 2026-06 sampling spike measured why
intent extraction can't produce this ranking: only ~1-6% of real .ino examples
carry parseable pin macros (pins ride constructor arguments), so the dependency
METADATA is the honest chip-frequency signal for the deterministic producer.

Usage:
  uv run python scripts/corpus_sweep.py                  # default local roots
  uv run python scripts/corpus_sweep.py --json out.json  # full per-sketch rows
  uv run python scripts/corpus_sweep.py --root name=board_id=/path [...]
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kicad_mcp.tools.design import _gather_sketch  # noqa: E402
from kicad_mcp.utils.firmware.intent import build_intent  # noqa: E402
from kicad_mcp.utils.firmware.parse import (  # noqa: E402
    idf_target_defines,
    parse_macros,
    partition,
    select_active_branches,
)
from kicad_mcp.utils.firmware.templates import expand_intent  # noqa: E402

# Default corpus roots: (name, board_id for branch-selection/MCU, path).
# All are read-only side effects of normal development on this machine —
# framework-bundled examples + per-project third-party libdeps.
DEFAULT_ROOTS: list[tuple[str, str, str]] = [
    ("esp32-framework", "esp32dev",
     "/Volumes/Files/blw/.platformio/packages/framework-arduinoespressif32/libraries"),
    ("avr-core", "nanoatmega328",
     "/Volumes/Files/blw/Library/Arduino15/packages/arduino/hardware/avr/1.8.6/libraries"),
    ("mr-esp32-libdeps", "esp32dev", "/Volumes/Files/claude/mr-esp32"),
]

# Curated library -> device mapping for the carding backlog (the seam rule:
# chip identity is DATA, not derivable from the library-name text). A library
# absent here is reported as UNMAPPED — listed loudly, never silently dropped —
# so growing this table is itself backlog work. Protocol/infra libraries map to
# None = "not a device" (consciously excluded, with the reason being: nothing
# to place on a board).
LIB_TO_DEVICE: dict[str, str | None] = {
    "U8g2": "SSD1306-class OLED (card exists: oled)",
    "U8x8lib": "SSD1306-class OLED (card exists: oled)",
    "ESP32Encoder": "rotary encoder",
    "ESP32Servo": "servo header",
    "AM2302-Sensor": "AM2302/DHT22 temp-humidity sensor",
    # protocol / infra / tooling — nothing to place:
    "ArduinoJson": None, "AsyncTCP": None, "ESPAsyncTCP": None,
    "ESPAsyncWebServer": None, "ElegantOTA": None, "PubSubClient": None,
    "Unity": None, "RPAsyncTCP": None, "StreamString": None,
}


@dataclass
class SweepStats:
    imported: int = 0
    skipped_no_main: int = 0
    crashed: int = 0
    with_pins: int = 0
    with_buses: int = 0
    with_addrs: int = 0


def sketch_dirs(root: Path) -> list[Path]:
    """Every directory under ``root`` containing at least one .ino (deduped).
    A root containing ANY libdeps sketches is a PROJECT tree: only its libdeps
    examples are corpus — the project's OWN firmware is measured by its own
    design() runs, not the sweep. Detected STRUCTURALLY (libdeps presence),
    not by directory name: the original ``root.name == "mr-esp32"`` check was
    a hardcoded basename carrying the project-tree distinction, which silently
    leaked own firmware into the corpus for any other project tree passed via
    --root (cold-review catch)."""
    inos = list(root.rglob("*.ino"))
    libdeps = [i for i in inos if "libdeps" in i.parts]
    if libdeps:
        picked = libdeps
    else:
        # plain examples tree: everything except .pio build artifacts
        picked = [i for i in inos if ".pio" not in i.parts]
    return sorted({i.parent for i in picked})


def sweep_one(d: Path, board: str) -> dict:
    """Import -> expand one sketch folder; a dict row either way (no row is
    ever dropped — crashes are the signal, not an excuse to skip)."""
    row: dict = {"dir": str(d), "board": board}
    try:
        src = _gather_sketch(d)
        if src is None:
            row["skip"] = "no_main_ino"
            return row
        text = select_active_branches(src.text, idf_target_defines(board))
        parsed = partition(parse_macros(text))
        it = build_intent(parsed, firmware_path=src.anchor, board_id=board)
        ex = expand_intent(it)
        row.update(
            pins=len(parsed.pins), addrs=len(parsed.addresses),
            buses=len(it.buses), nets=len(ex.nets),
            gap_kinds=sorted({g.kind for g in ex.gaps}),
        )
    except Exception as e:  # the fail-safe contract is exactly "no exception"
        row["crash"] = f"{type(e).__name__}: {e}"
        row["traceback"] = traceback.format_exc()
    return row


def _normalize_lib_name(name: str) -> str:
    """The bare library name for LIB_TO_DEVICE lookup. PlatformIO may store a
    libdep dir with a ``@version`` suffix (``ESP32Encoder@1.0.0``) or a registry
    ``_id`` suffix (``ESP32Encoder@src-<hash>``); the curated table keys are bare
    names, so a versioned install would otherwise land in ``unmapped`` — a silent
    miscount of a device the corpus actually uses. Strip at the first ``@``."""
    return name.split("@", 1)[0]


def libdeps_backlog(project_root: Path) -> tuple[Counter, Counter, Counter]:
    """Per-PROJECT dependency census -> (device counts, excluded, unmapped).

    May raise OSError on a broken symlink / unreadable dir in the tree; the caller
    guards each root so one bad tree doesn't lose the whole run's work."""
    per_project: set[tuple[str, str]] = set()
    for d in project_root.rglob("libdeps"):
        if not d.is_dir() or ".pio" not in d.parts:
            continue
        project = str(d).split("/.pio/")[0]
        for env in d.iterdir():
            if env.is_dir() and not env.name.startswith("."):
                for lib in env.iterdir():
                    # dot-dirs are PlatformIO bookkeeping (.cache), not
                    # libraries — counting one as an unmapped "lib" would
                    # silently pollute the backlog census (cold-review catch).
                    if lib.is_dir() and not lib.name.startswith("."):
                        per_project.add((project, _normalize_lib_name(lib.name)))
    devices: Counter = Counter()
    excluded: Counter = Counter()
    unmapped: Counter = Counter()
    for _project, lib in per_project:
        if lib not in LIB_TO_DEVICE:
            unmapped[lib] += 1
        elif LIB_TO_DEVICE[lib] is None:
            excluded[lib] += 1
        else:
            devices[LIB_TO_DEVICE[lib]] += 1
    return devices, excluded, unmapped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", action="append", default=None,
                    help="name=board_id=path (repeatable; replaces defaults)")
    ap.add_argument("--json", default=None, help="write full per-sketch rows here")
    args = ap.parse_args()

    roots: list[tuple[str, str, str]] = []
    for r in args.root or []:
        parts = r.split("=", 2)
        if len(parts) != 3 or not all(p.strip() for p in parts):
            ap.error(f"--root must be name=board_id=path, got {r!r}")
        roots.append((parts[0], parts[1], parts[2]))
    if not roots:
        roots = list(DEFAULT_ROOTS)
    paths = [p for _, _, p in roots]
    if len(set(paths)) != len(paths):
        ap.error("duplicate --root path(s) would double-count the corpus")

    all_rows: list[dict] = []
    manifest: dict = {}
    gap_census: Counter = Counter()
    for name, board, path in roots:
        root = Path(path)
        if not root.is_dir():
            # A missing root is a LOUD manifest entry, not a silent 0-sketch
            # success — the nightly must distinguish "all passed" from
            # "corpus wasn't there".
            manifest[name] = {"path": path, "error": "missing"}
            continue
        dirs = sketch_dirs(root)
        if not dirs:
            # Present-but-EMPTY is the same disease one boundary over: a wiped
            # examples dir, a remounted-empty volume, or a regressed discovery
            # glob must not read as a 0-sketch green run (cold-review catch —
            # the original guard checked existence, not non-emptiness).
            manifest[name] = {"path": path, "error": "no_sketches_found"}
            continue
        rows = [sweep_one(d, board) for d in dirs]
        all_rows += rows
        st = SweepStats(
            imported=sum(1 for r in rows if "crash" not in r and "skip" not in r),
            skipped_no_main=sum(1 for r in rows if r.get("skip") == "no_main_ino"),
            crashed=sum(1 for r in rows if "crash" in r),
            with_pins=sum(1 for r in rows if r.get("pins", 0) > 0),
            with_buses=sum(1 for r in rows if r.get("buses", 0) > 0),
            with_addrs=sum(1 for r in rows if r.get("addrs", 0) > 0),
        )
        manifest[name] = {"path": path, "sketches": len(rows), **st.__dict__}
        for r in rows:
            for k in r.get("gap_kinds", []):
                gap_census[k] += 1

    crashed = [r for r in all_rows if "crash" in r]
    total = len(all_rows)
    print(f"corpus sweep: {total} sketches, "
          f"{sum(m.get('imported', 0) for m in manifest.values())} imported, "
          f"{len(crashed)} CRASHED")
    print(json.dumps(manifest, indent=1))
    print("\ngap-kind census (sketch counts):")
    for k, n in gap_census.most_common():
        print(f"  {n:5d}  {k}")

    print("\ncarding backlog (per-project dependency census):")
    for name, board, path in roots:
        root = Path(path)
        try:
            has_libdeps = root.is_dir() and any(root.rglob("libdeps"))
        except OSError as e:
            print(f"  [{name}] {path}: backlog scan skipped (filesystem error: {e})")
            continue
        if not has_libdeps:
            continue
        try:
            devices, excluded, unmapped = libdeps_backlog(root)
        except OSError as e:
            # A broken symlink / unreadable dir must NOT crash the whole run after
            # the sweep already did its work — degrade this one root to a notice.
            print(f"  [{name}] {path}: backlog census incomplete "
                  f"(filesystem error: {e})")
            continue
        # Label each root: an unlabeled multi-root census prints device counts
        # back to back with no way to tell which corpus contributed which.
        print(f"  [{name}] {path}")
        for dev, n in devices.most_common():
            print(f"    {n:3d} project(s)  {dev}")
        if unmapped:
            print(f"    UNMAPPED libs (extend LIB_TO_DEVICE): "
                  f"{dict(unmapped.most_common())}")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"manifest": manifest, "rows": all_rows}, indent=1))
        print(f"\nrows -> {args.json}")

    # Exit precedence: crash (1) outranks corpus trouble (2) when both occur —
    # a fail-safe violation is the rarer, more actionable signal. Both messages
    # are always printed; only the exit code picks the headline.
    if crashed:
        print("\nFAIL-SAFE VIOLATIONS:")
        for r in crashed:
            print(f"  {r['dir']}\n    {r['crash']}")
    bad_roots = [n for n, m in manifest.items() if m.get("error")]
    if bad_roots:
        print(f"\nMISSING/EMPTY CORPUS ROOT(S): {bad_roots} — see manifest above")
    return 1 if crashed else (2 if bad_roots else 0)


if __name__ == "__main__":
    sys.exit(main())
