#!/usr/bin/env python3
"""Maintainer-time card pre-fetch (Phase 8) — bulk-generate candidate device
cards from the LOCALLY-INSTALLED KiCad symbol libraries.

This runs at *curation time* on a maintainer's machine (KiCad required here; the
END USER stays fully air-gapped — they only ever receive the reviewed, bundled
cards). It enumerates device-category symbol libraries, synthesizes an I2C card
per clean-bus symbol (roles by name-identity + supply/ground from pin names),
and writes the high-confidence ones to a STAGING dir for review — NOT directly
into the shipped ``devices/`` tree.

    python scripts/prefetch_cards.py \
        --symbols-dir /Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols \
        --out ./prefetch_cards --min-confidence high

Every symbol's disposition is reported (generated / skipped + reason) — no silent
truncation. Generated cards carry the symbol's own pre-assigned Footprint
property when it has one, else ``TODO:confirm``, plus a draft header; a human
confirms footprint + reviews before any card is promoted into
``src/kicad_mcp/utils/firmware/devices/``.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

# Curated device-category libraries that commonly host I2C parts (sensible, not
# exhaustive — skip the passive/connector bulk).
DEFAULT_LIBRARIES = [
    "Sensor_Motion", "Sensor_Temperature", "Sensor_Humidity", "Sensor_Pressure",
    "Sensor_Gas", "Sensor_Current", "Sensor_Optical", "Sensor_Distance",
    "Interface_Expansion", "Analog_ADC", "Display_Graphic", "Timing",
]
_DEFAULT_SYMBOLS_DIR = "/Applications/KiCad/KiCad.app/Contents/SharedSupport/symbols"


def _pin_names(symbol) -> list[str]:
    return [str(getattr(p, "name", "") or "") for p in (getattr(symbol, "pins", None) or ())]


def _pin_types(symbol) -> dict[str, str]:
    """Pin name -> KiCad electrical pin type (e.g. "input", "bidirectional").

    Best-effort corroboration for synthesize_i2c_card's role classification,
    which used to be 100% name-regex based -- a pin named SCL is not itself
    proof of an I2C clock. Duplicate pin names (rare, but symbols aren't
    guaranteed unique) keep whichever type is seen last; that's the same
    silent-collision behavior _pin_names' own caller already tolerates for
    names, not a new gap this introduces."""
    out: dict[str, str] = {}
    for p in (getattr(symbol, "pins", None) or ()):
        name = str(getattr(p, "name", "") or "")
        if not name:
            continue
        pin_type = getattr(p, "pin_type", None)
        out[name] = str(getattr(pin_type, "value", pin_type) or "")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bulk-generate I2C device cards from KiCad symbols.")
    ap.add_argument("--symbols-dir", default=os.environ.get("KICAD_SYMBOL_DIR", _DEFAULT_SYMBOLS_DIR))
    ap.add_argument("--libraries", nargs="*", default=DEFAULT_LIBRARIES)
    ap.add_argument("--out", default="./prefetch_cards")
    ap.add_argument("--min-confidence", choices=["high", "low"], default="high")
    args = ap.parse_args(argv)

    from kicad_sch_api.library.cache import get_symbol_cache

    from kicad_mcp.utils.firmware.cards import validate_peripheral_card
    from kicad_mcp.utils.firmware.prefetch import (
        symbol_footprint,
        synthesize_i2c_card,
        top_level_symbol_names,
    )

    sym_dir = Path(args.symbols_dir)
    if not sym_dir.is_dir():
        print(f"error: symbols dir not found: {sym_dir}", file=sys.stderr)
        return 2
    cache = get_symbol_cache()
    cache.discover_libraries([str(sym_dir)])
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    want_high_only = args.min_confidence == "high"
    dispositions: Counter[str] = Counter()
    written = 0
    # which-symbol/why detail for load/synthesis failures -- an aggregate
    # counter alone told a maintainer HOW MANY symbols failed but never
    # WHICH ones or why, so a real regression (e.g. a library that stopped
    # parsing) was indistinguishable from routine skips without re-running
    # under a debugger.
    failures: list[tuple[str, str]] = []
    for lib in args.libraries:
        lib_file = sym_dir / f"{lib}.kicad_sym"
        if not lib_file.is_file():
            print(f"  (library not found, skipping: {lib})")
            continue
        for name in top_level_symbol_names(lib_file.read_text(errors="replace")):
            lib_id = f"{lib}:{name}"
            try:
                sym = cache.get_symbol(lib_id)
                if sym is None:
                    dispositions["symbol-load-failed"] += 1
                    failures.append((lib_id, "get_symbol returned None"))
                    continue
                card, conf, reasons = synthesize_i2c_card(
                    symbol_name=name, lib_id=lib_id, pin_names=_pin_names(sym),
                    footprint=symbol_footprint(sym),
                    unit_count=int(getattr(sym, "unit_count", 1) or 1),
                    pin_types=_pin_types(sym),
                )
            except Exception as e:
                # One malformed symbol (a parse error, an unexpected shape
                # get_symbol doesn't guard against) must not abort the whole
                # bulk run -- this script processes thousands of symbols
                # across multiple libraries; losing all prior progress to
                # one bad symbol is a real cost on a run that can take
                # minutes. Broad on purpose: this is a maintainer CLI's
                # top-level per-item boundary, the same role a tool-call
                # boundary plays in the MCP server itself.
                dispositions["symbol-processing-error"] += 1
                failures.append((lib_id, f"{type(e).__name__}: {e}"))
                continue
            dispositions[conf] += 1
            if card is None or (want_high_only and conf != "high"):
                continue
            errs = validate_peripheral_card(card)
            if errs:
                dispositions["invalid-after-synth"] += 1
                print(f"  ! {lib_id}: invalid card: {errs}")
                continue
            draft = card.pop("_draft", {})
            dest = out_dir / f"{card['type'].lower()}.yaml"
            if dest.exists():   # two symbols canonicalize to one type — don't hide it
                dispositions["type-collision-skipped"] += 1
                print(f"  ! {lib_id}: type {card['type']!r} already written "
                      f"({dest.name}); skipping to avoid silent overwrite")
                continue
            header = (f"# AUTO-GENERATED DRAFT — confidence={conf}. Review + confirm "
                      f"footprint before promoting into devices/.\n"
                      f"# reasons: {'; '.join(reasons)}\n")
            dest.write_text(header + yaml.safe_dump(card, sort_keys=False))
            written += 1
            print(f"  + {lib_id} -> {dest.name} ({conf})")

    print("\n=== pre-fetch summary ===")
    print(f"written: {written} card(s) to {out_dir}")
    for k, v in sorted(dispositions.items()):
        print(f"  {k}: {v}")
    if failures:
        print(f"\n{len(failures)} symbol(s) failed to load/process:")
        for lib_id, why in failures:
            print(f"  ! {lib_id}: {why}")
    print("NOTE: footprint is the symbol's own pre-assigned value when it has "
          "one, else TODO:confirm — review before shipping either way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
