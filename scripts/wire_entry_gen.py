#!/usr/bin/env python3
"""Maintainer-time wire-entry table generator (spec §1, H2).

Runs at curation time on a maintainer's machine against the LOCALLY-INSTALLED
KiCad footprint libraries (the END USER stays air-gapped — they receive only the
reviewed, bundled :data:`kicad_mcp.utils.placement.wire_entry.WIRE_ENTRY` table).

For each footprint it parses the ``.kicad_mod`` directly (pure S-expression text;
no KiCad/pcbnew launch), measures the courtyard-vs-pad overhang asymmetry, and
derives the 0°-frame wire-entry vector via the SAME pure rule the placer trusts
(:func:`derive_wire_entry`). Footprints are grouped by family; a family ships
only if its variants agree and the asymmetry clears the ``high`` bar. Every
footprint's disposition is reported — no silent drops — and ``low``/ambiguous
families are surfaced for human audit, never auto-trusted (CLAUDE.md
data-capture: the silk pin-1 arrow points the WRONG way, so a thin signal must
be reviewed, not believed).

    python scripts/wire_entry_gen.py \
        --footprints-dir /Volumes/Files/claude/kicad-versions/10.0/KiCad.app/Contents/SharedSupport/footprints \
        --pretty TerminalBlock_Phoenix Connector_PinHeader_2.54mm
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, List, Optional, Tuple

# Pure logic is the single source of truth — the script only parses + enumerates.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from kicad_mcp.utils.placement.wire_entry import (  # noqa: E402
    derive_wire_entry, normalize_family,
)

_COURTYARD_HINTS = ("CrtYd", "Courtyard")
_FAB_HINTS = ("Fab",)
_TOKEN_RE = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()]+')


def parse_sexpr(text: str) -> Any:
    """Parse one S-expression into nested lists (atoms as strings, sans quotes)."""
    tokens = _TOKEN_RE.findall(text)
    pos = 0

    def parse() -> Any:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        if tok == "(":
            lst: List[Any] = []
            while tokens[pos] != ")":
                lst.append(parse())
            pos += 1  # consume ")"
            return lst
        if tok.startswith('"') and tok.endswith('"'):
            return tok[1:-1]
        return tok

    return parse()


def _find(node: Any, key: str) -> Optional[list]:
    """First direct child list whose head atom == key."""
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and child and child[0] == key:
            return child
    return None


def _floats(seq) -> List[float]:
    out: List[float] = []
    for s in seq:
        try:
            out.append(float(s))
        except (TypeError, ValueError):
            pass
    return out


def _layer_of(item: list) -> str:
    ly = _find(item, "layer")
    return " ".join(str(x) for x in ly[1:]) if ly else ""


def extract_geometry(
    fp_node: list,
) -> Tuple[List[Tuple[float, float, float, float]], Optional[Tuple[float, float, float, float]]]:
    """Return (pad_bboxes, courtyard_bbox) in the footprint's 0° frame. Falls
    back to the Fab outline when no courtyard graphics are present."""
    pads: List[Tuple[float, float, float, float]] = []
    cy: List[float] = []  # [xmin, ymin, xmax, ymax] accumulator for courtyard
    fab: List[float] = []
    cy_box: Optional[List[float]] = None
    fab_box: Optional[List[float]] = None

    def grow(box: Optional[List[float]], xs: List[float], ys: List[float]) -> Optional[List[float]]:
        if not xs or not ys:
            return box
        b = box or [min(xs), min(ys), max(xs), max(ys)]
        return [min(b[0], *xs), min(b[1], *ys), max(b[2], *xs), max(b[3], *ys)]

    for item in fp_node:
        if not isinstance(item, list) or not item:
            continue
        head = item[0]
        if head == "pad":
            at = _find(item, "at")
            size = _find(item, "size")
            if not at or not size:
                continue
            a = _floats(at[1:])
            s = _floats(size[1:])
            if len(a) < 2 or len(s) < 2:
                continue
            x, y = a[0], a[1]
            w, h = s[0], s[1]
            pads.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2))
        elif head in ("fp_line", "fp_rect", "fp_poly", "fp_circle"):
            layer = _layer_of(item)
            xs: List[float] = []
            ys: List[float] = []
            for key in ("start", "end", "center", "mid"):
                seg = _find(item, key)
                if seg:
                    f = _floats(seg[1:])
                    if len(f) >= 2:
                        xs.append(f[0]); ys.append(f[1])
            pts = _find(item, "pts")
            if pts:
                for xy in pts[1:]:
                    if isinstance(xy, list) and xy and xy[0] == "xy":
                        f = _floats(xy[1:])
                        if len(f) >= 2:
                            xs.append(f[0]); ys.append(f[1])
            if any(h in layer for h in _COURTYARD_HINTS):
                cy_box = grow(cy_box, xs, ys)
            elif any(h in layer for h in _FAB_HINTS):
                fab_box = grow(fab_box, xs, ys)

    box = cy_box if cy_box is not None else fab_box
    return pads, (tuple(box) if box is not None else None)  # type: ignore[return-value]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Derive the WIRE_ENTRY table from KiCad footprints.")
    ap.add_argument("--footprints-dir", required=True,
                    help="KiCad SharedSupport/footprints directory")
    ap.add_argument("--pretty", nargs="*", default=["TerminalBlock_Phoenix"],
                    help="*.pretty library folders to scan")
    args = ap.parse_args(argv)

    root = Path(args.footprints_dir)
    if not root.is_dir():
        print(f"error: footprints dir not found: {root}", file=sys.stderr)
        return 2

    # family -> list of (footprint_item_name, vector, asymmetry, confidence)
    by_family: dict[str, list] = defaultdict(list)
    dispositions: dict[str, int] = defaultdict(int)
    total = 0
    for pretty in args.pretty:
        pdir = root / f"{pretty}.pretty"
        if not pdir.is_dir():
            print(f"  (library not found, skipping: {pretty})")
            continue
        for mod in sorted(pdir.glob("*.kicad_mod")):
            total += 1
            try:
                node = parse_sexpr(mod.read_text(errors="replace"))
            except Exception as e:  # noqa: BLE001 — report, never silently drop
                dispositions["parse-error"] += 1
                print(f"  ! {mod.name}: parse error: {e}")
                continue
            pads, cy = extract_geometry(node)
            vec, asym, conf = derive_wire_entry(pads, cy)
            dispositions[conf] += 1
            by_family[normalize_family(mod.stem)].append((mod.stem, vec, asym, conf))

    print("\n=== wire-entry families ===")
    shipped: dict[str, Tuple[float, float]] = {}
    for fam in sorted(by_family):
        rows = by_family[fam]
        highs = [r for r in rows if r[3] == "high"]
        vecs = {r[1] for r in highs}
        asyms = sorted({round(r[2], 2) for r in rows})
        if highs and len(vecs) == 1:
            vec = highs[0][1]
            shipped[fam] = vec
            print(f"  HIGH  {fam}\n          vector={vec} asym(mm)={asyms} "
                  f"({len(highs)}/{len(rows)} variants high)")
        elif highs and len(vecs) > 1:  # variants disagree — must be audited
            print(f"  AUDIT {fam}: variants disagree on vector {vecs} — NOT shipped")
        else:
            print(f"  skip  {fam}: max confidence "
                  f"{max((r[3] for r in rows), key=_conf_rank)} asym(mm)={asyms}")

    print("\n=== summary ===")
    print(f"footprints scanned: {total}")
    for k, v in sorted(dispositions.items()):
        print(f"  {k}: {v}")
    print(f"\nshippable families ({len(shipped)}):")
    for fam, vec in sorted(shipped.items()):
        print(f'    "{fam}": {vec},')
    print("\nNOTE: transcribe HIGH families into wire_entry.WIRE_ENTRY after audit. "
          "The silk pin-1 arrow is NOT used (it points the wrong way) — eyeball the "
          "courtyard outline before trusting a thin asymmetry.")
    return 0


def _conf_rank(c: str) -> int:
    return {"high": 2, "low": 1, "skip": 0}.get(c, 0)


if __name__ == "__main__":
    raise SystemExit(main())
