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
truncation. Generated cards carry footprint ``TODO:confirm`` (symbols don't carry
a footprint) and a draft header; a human confirms footprint + reviews before any
card is promoted into ``src/kicad_mcp/utils/firmware/devices/``.
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bulk-generate I2C device cards from KiCad symbols.")
    ap.add_argument("--symbols-dir", default=os.environ.get("KICAD_SYMBOL_DIR", _DEFAULT_SYMBOLS_DIR))
    ap.add_argument("--libraries", nargs="*", default=DEFAULT_LIBRARIES)
    ap.add_argument("--out", default="./prefetch_cards")
    ap.add_argument("--min-confidence", choices=["high", "low"], default="high")
    args = ap.parse_args(argv)

    from kicad_sch_api.library.cache import get_symbol_cache

    from kicad_mcp.utils.firmware.cards import validate_peripheral_card
    from kicad_mcp.utils.firmware.prefetch import synthesize_i2c_card, top_level_symbol_names

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
    for lib in args.libraries:
        lib_file = sym_dir / f"{lib}.kicad_sym"
        if not lib_file.is_file():
            print(f"  (library not found, skipping: {lib})")
            continue
        for name in top_level_symbol_names(lib_file.read_text(errors="replace")):
            lib_id = f"{lib}:{name}"
            sym = cache.get_symbol(lib_id)
            if sym is None:
                dispositions["symbol-load-failed"] += 1
                continue
            card, conf, reasons = synthesize_i2c_card(
                symbol_name=name, lib_id=lib_id, pin_names=_pin_names(sym),
                unit_count=int(getattr(sym, "unit_count", 1) or 1),
            )
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
    print("NOTE: generated cards have footprint TODO:confirm — review before shipping.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
