"""Bus → part resolution (C4): determine which SPECIFIC part the user declared for
each bus, FROM the firmware corpus — never invent. Pure: no card loading, no I/O.

The rule (no proximity heuristics, no guessing): a recognized part is a CANDIDATE
for a bus iff it appears in the corpus AND the bus type matches what its card
``serves``. Exactly one candidate → resolve it (provenance ``corpus``); more than
one → ambiguous, leave unresolved and let the caller disclose it (NEVER silently
pick); zero → unresolved. Whatever a bus resolves to is the user's declared part;
a template that can't realize it falls back with a disclosed gap (C5/C6), it does
not substitute silently (the SPH0645-for-INMP441 bug this feature exists to kill).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kicad_mcp.utils.firmware.intent import Bus
from kicad_mcp.utils.firmware.part_extractor import PartEvidence


@dataclass(frozen=True)
class BusResolution:
    """One bus's resolution outcome (for the caller to log / emit gaps from)."""
    bus: str
    type: str
    resolved_part: Optional[str]            # canonical key, or None
    candidates: list[str] = field(default_factory=list)  # all corpus parts serving this type
    reason: str = "none"                    # "resolved" | "ambiguous" | "none"


def resolve_bus_parts(
    buses: list[Bus],
    part_evidence: list[PartEvidence],
    serves_by_part: dict[str, str],
) -> list[BusResolution]:
    """Set ``resolved_part`` / ``part_provenance`` on each bus from the corpus, and
    return a per-bus :class:`BusResolution` (so the caller can gap the ambiguous
    ones). ``serves_by_part`` maps canonical part → the bus type it serves (from
    ``cards.part_serves_map``); ``part_evidence`` is the extractor's output.

    A user-supplied resolution (``part_provenance == "user"``, set by a board.yaml
    override before this runs) is left UNTOUCHED — the human's choice wins.
    """
    corpus_parts = {ev.canonical for ev in part_evidence}
    out: list[BusResolution] = []
    for bus in buses:
        if bus.part_provenance == "user":      # explicit override already set — honor it
            out.append(BusResolution(bus.name, bus.type, bus.resolved_part,
                                     [bus.resolved_part] if bus.resolved_part else [],
                                     "user"))
            continue
        candidates = sorted(
            p for p in corpus_parts if serves_by_part.get(p) == bus.type
        )
        if len(candidates) == 1:
            bus.resolved_part = candidates[0]
            bus.part_provenance = "corpus"
            out.append(BusResolution(bus.name, bus.type, candidates[0],
                                     candidates, "resolved"))
        else:
            # 0 → no evidence; >1 → ambiguous (NEVER pick one silently). Both leave
            # resolved_part None, but mark the AMBIGUOUS case on the bus so a later
            # template (_decide_part) can tell it apart from no-evidence and hold
            # the bus unrealized instead of assuming a default. (No-evidence keeps
            # provenance None → assume-with-disclosure is correct there.)
            if candidates:
                bus.part_provenance = "ambiguous"
            out.append(BusResolution(bus.name, bus.type, None, candidates,
                                     "ambiguous" if candidates else "none"))
    return out
