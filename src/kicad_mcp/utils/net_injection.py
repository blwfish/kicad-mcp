"""Single source of truth for inserting ``(net N "name")`` definitions into a
``.kicad_pcb`` S-expression via direct text editing.

Both the single-net injector (``pcb_nets._op_add_net``) and the bulk injector
(``pcb_pipeline._step_inject_nets_and_assign_pads``) used to re-implement the
insertion-point decision and the line formatting independently. The pipeline
copy was missing the KiCad-10 ``(net 0 "")``-absence fallback, which produced
electrically-dead boards (0 nets/pads/tracks while returning ``status:ok``).

This module is the one place that decides *where* a net line goes and *how* it
is formatted. Every direct-edit net injector consumes it; none re-encodes the
rule. (CLAUDE.md Rule 3 — a semantic distinction gets a single source of truth.)
"""

import logging
import re
from typing import List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# Escape-aware match for an existing ``(net N "name")`` definition line. The
# quoted-string body tolerates S-expression escapes (``\"``, ``\\``) so a net
# name containing an escaped quote does not prematurely terminate the match.
# Captures (code, name) so existing_net_codes() can reuse this SAME compiled
# pattern instead of re-encoding its own copy of "what is a net line" -- the
# two used to be independent regexes that a KiCad format change could update
# one of and silently miss the other (finding #80 of the 2026-09-23 review).
_NET_LINE_RE = re.compile(r'\(net\s+(\d+)\s+"((?:[^"\\]|\\.)*)"\)')

# Top-level footprint block — used as the KiCad-10 fallback anchor when a fresh
# board omits the ``(net 0 "")`` sentinel and therefore has no net line to
# append after. Accepts space OR tab indentation: real KiCad output always
# uses tabs, but a hand-edited or third-party-reformatted file could use
# spaces -- hardcoding \t alone would silently fall through to the weaker
# end-of-file fallback with no signal. finding #78 of the 2026-09-23 review.
_FOOTPRINT_RE = re.compile(r'\n[ \t]+\(footprint\b')

# Loose anchor: anything shaped like the START of a net definition line,
# regardless of whether the rest of it actually matches _NET_LINE_RE. Used
# only to COUNT candidates existing_net_codes() didn't fully match (a
# malformed quote escape, an unexpected format) -- never for extraction.
_NET_CANDIDATE_RE = re.compile(r'\(net\s+\d+\s+"')


def find_net_insert_pos(pcb_content: str) -> Optional[int]:
    """Return the byte offset at which new ``(net ...)`` lines should be inserted.

    Resolution order (canonical KiCad placement, with KiCad-10 fallbacks):

    1. Immediately after the last existing ``(net N "name")`` line.
    2. Else immediately before the first top-level ``(footprint ...)`` block.
       (KiCad 10 omits the ``(net 0 "")`` sentinel in fresh boards, so there is
       no net line to append after.)
    3. Else immediately before the file's final closing paren.

    Returns ``None`` when no insertion point can be found, so the caller can
    surface an explicit error rather than silently dropping the nets.
    """
    last_net_match = None
    for m in _NET_LINE_RE.finditer(pcb_content):
        last_net_match = m
    if last_net_match is not None:
        return last_net_match.end()

    fp_match = _FOOTPRINT_RE.search(pcb_content)
    if fp_match is not None:
        return fp_match.start()

    pos = pcb_content.rfind('\n)')
    if pos == -1:
        # All three resolution strategies failed -- collapsing this into a
        # bare None gave the caller (and its own caller's generic "Could not
        # find insertion point" error) zero signal about WHICH strategies
        # were tried and why none worked. Logged here (not returned/changed
        # in shape) so existing callers' None-checking contract is
        # unchanged, but the detail isn't lost. finding #81.
        logger.warning(
            "find_net_insert_pos: no existing (net ...) line, no top-level "
            "(footprint ...) block, and no closing paren found -- content is "
            "likely empty, truncated, or not a .kicad_pcb S-expression at all "
            "(%d bytes)", len(pcb_content),
        )
        return None
    return pos


def escape_net_name(net_name: str) -> str:
    """Encode a net name for an S-expression quoted string.

    Backslash first (so it doesn't double-escape the quote's introducer), then
    the double-quote. Inverse of :func:`unescape_net_name`. This is what makes
    every injector safe by construction: an externally-authored label like
    ``BUS"A`` (from a kicad-cli netlist) is written as ``BUS\\"A`` instead of
    corrupting the ``.kicad_pcb`` with an unbalanced quote.
    """
    return net_name.replace("\\", "\\\\").replace('"', '\\"')


def unescape_net_name(raw: str) -> str:
    """Decode the body of an S-expression quoted string back to a raw name.

    Single left-to-right pass (``\\X`` -> ``X``) so adjacent escapes like
    ``\\\\\\"`` decode correctly; the exact inverse of :func:`escape_net_name`.
    """
    return re.sub(r"\\(.)", r"\1", raw)


def format_net_line(net_code: int, net_name: str) -> str:
    """Format one net definition exactly as it is written into a ``.kicad_pcb``.

    The leading ``\\n\\t`` makes the inserted line stand on its own, indented to
    match the top-level net block. ``net_name`` is S-expression-escaped here so
    callers never have to — a name needing no escaping is written unchanged.
    """
    return f'\n\t(net {net_code} "{escape_net_name(net_name)}")'


def inject_net_definitions(
    pcb_content: str,
    new_nets: Sequence[Tuple[int, str]],
) -> Optional[str]:
    """Insert formatted net lines for ``new_nets`` into ``pcb_content``.

    ``new_nets`` is an ordered sequence of ``(net_code, net_name)`` pairs; the
    lines are emitted in that order at the single canonical insertion point.
    Returns the rewritten content, the original unchanged content when
    ``new_nets`` is empty, or ``None`` when no insertion point exists.
    """
    if not new_nets:
        return pcb_content

    insert_pos = find_net_insert_pos(pcb_content)
    if insert_pos is None:
        return None

    new_lines = "".join(format_net_line(code, name) for code, name in new_nets)
    return pcb_content[:insert_pos] + new_lines + pcb_content[insert_pos:]


def existing_net_codes(pcb_content: str) -> List[Tuple[int, str]]:
    """Return ``(code, name)`` for every existing ``(net N "name")`` line.

    Uses _NET_LINE_RE -- the SAME compiled pattern find_net_insert_pos
    matches against -- so the two can never disagree about what counts as a
    net line (previously each held its own independently-written copy of
    the identical regex; finding #80).
    """
    out: List[Tuple[int, str]] = []
    for m in _NET_LINE_RE.finditer(pcb_content):
        # Decode S-expression escapes so the in-memory name round-trips with
        # what format_net_line wrote (raw representation everywhere in memory).
        out.append((int(m.group(1)), unescape_net_name(m.group(2))))

    # A line that LOOKS like a net definition (starts "(net <digits>") but
    # doesn't match the full escape-aware pattern (malformed quote escaping,
    # an unexpected format) was previously skipped with no counter at all --
    # "this net doesn't exist" was indistinguishable from "it was malformed
    # and silently dropped". finding #82.
    candidate_count = len(_NET_CANDIDATE_RE.findall(pcb_content))
    if candidate_count > len(out):
        logger.warning(
            "existing_net_codes: %d net-shaped line(s) did not match the "
            "expected (net N \"name\") format and were skipped (%d matched "
            "of %d candidates)",
            candidate_count - len(out), len(out), candidate_count,
        )
    return out
