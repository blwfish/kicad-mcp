"""Wire-entry direction for synthesized edge terminals — the single source of
truth that replaces the pad-centroid orientation proxy (spec §1, H2).

A field-wiring terminal must be oriented so its WIRE-ENTRY face points off the
board (the wire enters from outside; the PCB pads stay on-board). Which way the
wire enters is a property of the footprint *family*, not something recoverable
from pad geometry at placement time — the pad centroid is a syntactic proxy that
comes out right only by luck (J3/J4 right, J1/J2/J5/J7 wrong; CLAUDE.md
Syntactic-Semantic Seam Rule). So we resolve it from a small data table keyed by
footprint family, built once at maintainer time from the real ``.kicad_mod``
geometry (see ``scripts/wire_entry_gen.py``) and audited before shipping.

This module is the SINGLE SOURCE for two things both the generator and the
placer consume:
  * :func:`normalize_family` — footprint name -> family key (pin-count collapsed),
  * :func:`derive_wire_entry` — the geometry rule (courtyard-overhang asymmetry),
and the shipped :data:`WIRE_ENTRY` table itself.

**Vector convention.** ``WIRE_ENTRY[family]`` is the unit vector, in the
footprint's 0° frame, pointing toward the WIRE-ENTRY FACE (the side the cable
enters). The placer aims it at the edge's *outward* normal
(``rotation_to_face(vec, outward_normal(edge))``), so the cable side hangs off
the board and the pads stay inboard — the same construction the tier-1 antenna
overhang uses for the MCU keepout.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

# --- Confidence thresholds for the courtyard-overhang asymmetry (mm) ----------
# The signal is thin (~0.6 mm on the MKDS family), so the bands are deliberately
# conservative: only a clear asymmetry becomes a shipped table entry; a marginal
# one is surfaced for human audit, never auto-trusted (CLAUDE.md data-capture).
HIGH_MIN_ASYMMETRY_MM = 0.4   # >= this -> "high" (shippable table entry)
LOW_MIN_ASYMMETRY_MM = 0.15   # [LOW, HIGH) -> "low" (audit, not shipped)
                              # < LOW -> "skip" (symmetric: not a horizontal-clamp family)
# A footprint whose pad-cluster is ~square has no identifiable row axis, so no
# wire-entry direction can be inferred from it.
MIN_ROW_AXIS_SPAN_MM = 0.5


# --- Family key ---------------------------------------------------------------
# Two regexes collapse the pin-count dimension so every size variant of one
# connector family shares a key (they share wire-entry geometry). Order matters:
# the Phoenix mid-name count is normalized before the generic ``NxN`` token.
_PHX_COUNT_RE = re.compile(r"(MKDS-\d+,\d+)-\d+-(\d+\.\d+)")  # MKDS-1,5-3-5.08 -> -1,5-N-5.08
_ARRAY_RE = re.compile(r"\d+x\d+")                            # 1x03 / 2x05 -> NxN


def normalize_family(footprint: str) -> str:
    """Footprint name -> canonical family key, independent of pin count.

    Strips any ``lib:`` prefix and collapses the pin-count tokens. All pin-count
    variants of a family map to one key (they share wire-entry geometry); a name
    with no pin-count tokens is returned unchanged (minus the prefix). This is
    the single source of truth for the family distinction — both the generator
    and the placer key off it, so neither re-encodes the rule."""
    name = footprint.split(":")[-1]
    name = _PHX_COUNT_RE.sub(r"\1-N-\2", name)
    name = _ARRAY_RE.sub("NxN", name)
    return name


# --- Geometry rule ------------------------------------------------------------

def classify_confidence(asymmetry_mm: float) -> str:
    """Map a courtyard-overhang asymmetry (mm) to ``high`` / ``low`` / ``skip``."""
    if asymmetry_mm >= HIGH_MIN_ASYMMETRY_MM:
        return "high"
    if asymmetry_mm >= LOW_MIN_ASYMMETRY_MM:
        return "low"
    return "skip"


def derive_wire_entry(
    pad_bboxes: Sequence[Tuple[float, float, float, float]],
    courtyard_bbox: Optional[Tuple[float, float, float, float]],
) -> Tuple[Optional[Tuple[float, float]], float, str]:
    """Infer the 0°-frame wire-entry vector from footprint geometry.

    ``pad_bboxes`` are ``(xmin, ymin, xmax, ymax)`` in the footprint's 0° frame;
    ``courtyard_bbox`` is the same for the courtyard (or Fab outline). Rule: the
    pad row runs along the longer pad-cluster span; on the PERPENDICULAR axis,
    the courtyard overhangs the pad cluster more on the wire-entry side (the
    clamp/cable opening sits beyond the pins). Returns
    ``(vector_or_None, asymmetry_mm, confidence)``. The vector is non-None only
    for ``high`` confidence; ``low``/``skip`` return None so the placer falls
    back to pad-centroid rather than trusting a marginal signal.

    Note: the silk pin-1 arrow is deliberately NOT used — on the MKDS family it
    points toward the PCB foot, i.e. AWAY from the wire-entry face."""
    if not pad_bboxes or courtyard_bbox is None:
        return (None, 0.0, "skip")
    pxmin = min(b[0] for b in pad_bboxes)
    pymin = min(b[1] for b in pad_bboxes)
    pxmax = max(b[2] for b in pad_bboxes)
    pymax = max(b[3] for b in pad_bboxes)
    cxmin, cymin, cxmax, cymax = courtyard_bbox
    span_x = pxmax - pxmin
    span_y = pymax - pymin
    # No clear row axis (single pad / square cluster) -> cannot infer a direction.
    if abs(span_x - span_y) < MIN_ROW_AXIS_SPAN_MM:
        return (None, 0.0, "skip")
    if span_x >= span_y:
        # Row along X -> wire entry is on the Y (perpendicular) axis.
        over_neg = pymin - cymin
        over_pos = cymax - pymax
        vec: Tuple[float, float] = (0.0, -1.0) if over_neg > over_pos else (0.0, 1.0)
    else:
        over_neg = pxmin - cxmin
        over_pos = cxmax - pxmax
        vec = (-1.0, 0.0) if over_neg > over_pos else (1.0, 0.0)
    # Round BEFORE the threshold test so the >= contract is deterministic at
    # sub-micron precision (float subtraction makes an exact 0.4 land at 0.3999…).
    asym = round(abs(over_neg - over_pos), 3)
    conf = classify_confidence(asym)
    return (vec if conf == "high" else None, asym, conf)


# --- Shipped table ------------------------------------------------------------
# Maintainer-generated (scripts/wire_entry_gen.py) from the real KiCad footprint
# library, then audited. Keyed by normalize_family(); value = 0°-frame unit
# vector toward the wire-entry face. ONLY high-confidence families are shipped;
# anything the placer can't find here falls back to pad-centroid (with an event).
#
# Phoenix MKDS horizontal screw terminals (what placement-locus synthesizes for
# field wiring): wire enters the −Y face at 0° (courtyard overhangs the pad row
# 0.61 mm more on −Y; F.Fab confirms; bit-identical KiCad 9 & 10). Asymmetry is
# thin but consistent across 1x02..1x16.
WIRE_ENTRY: Dict[str, Tuple[float, float]] = {
    "TerminalBlock_Phoenix_MKDS-1,5-N-5.08_NxN_P5.08mm_Horizontal": (0.0, -1.0),
}


def lookup(footprint: str) -> Optional[Tuple[float, float]]:
    """Wire-entry vector for a footprint, or None if the family isn't shipped."""
    return WIRE_ENTRY.get(normalize_family(footprint))
