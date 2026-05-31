"""Unified terminal/connector synthesis — ONE helper for every "place a connector
and route its nets to numbered positions" operation.

Three pre-existing fragments each re-encoded this structural job a different way
(module-card headers, the I2S speaker header, the board.yaml ``extra_connectors``)
— exactly the single-source-of-truth violation CLAUDE.md's Syntactic-Semantic
Seam rule warns about. ``synthesize_connector`` owns the mechanics they share:

  * **symbol sizing** — pick ``Screw_Terminal_01x{N}`` / ``Conn_01x{N}`` for N
    positions (pins numbered ``"1"``..``"N"``),
  * **pin-count validation** — N must fit the symbol (a check none of the three
    fragments did — a latent bug),
  * **ref allocation** — through ONE caller-supplied allocator,
  * **net retargeting** — each position contributes an ``Endpoint`` onto its net,
  * **legend emission** — a ``ConnectorLegend`` so the field-wiring silk legend is
    generated identically for every connector.

What it does NOT conflate: the symbol *family* (a field-wiring screw terminal vs
an on-board socket) stays a caller choice via ``connector_type`` — see §4 of
SPEC_Firmware_Placement_Locus.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from kicad_mcp.utils.firmware.intent import (
    ConnectorLegend,
    Endpoint,
    Peripheral,
)

# --- symbol / footprint families (verified present on KiCad 10) --------------
# Pins are numbered "1".."N"; the "01x{NN}" token encodes the position count.
_SCREW_SYM = "Connector:Screw_Terminal_01x{n:02d}"
_HEADER_SYM = "Connector_Generic:Conn_01x{n:02d}"

# Phoenix MKDS-1,5 5.08 mm screw-terminal block — the field-wiring default
# (Open Decision 4). The "-{n}-" series segment is NOT zero-padded; the
# "1x{n:02d}" footprint segment IS. Present for N = 2..16.
_PHOENIX_FP = ("TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-{n}-5.08_"
               "1x{n:02d}_P5.08mm_Horizontal")
_PHOENIX_RANGE = range(2, 17)
# Universal fallback (lower quality for field wiring, but always present) — used
# when Phoenix has no part for N (e.g. a 1-position terminal).
_PIN_HEADER_FP = "Connector_PinHeader_2.54mm:PinHeader_1x{n:02d}_P2.54mm_Vertical"

# connector_type -> (symbol template, ref prefix, screw?). ``screw`` selects the
# Phoenix terminal-block footprint default; the others use the pin-header default.
_CONNECTOR_TYPES: dict[str, tuple[str, str, bool]] = {
    "screw_terminal": (_SCREW_SYM, "J", True),
    "pin_header":     (_HEADER_SYM, "J", False),
    "pluggable":      (_HEADER_SYM, "J", False),
}

VALID_CONNECTOR_TYPES = frozenset(_CONNECTOR_TYPES)

# Type tag stored on the synthesized Peripheral (informational; not a symbol).
_TYPE_TAG = {
    "screw_terminal": "TERM",
    "pin_header": "HDR",
    "pluggable": "CONN",
}

_SYMBOL_PIN_RE = re.compile(r"_01x(\d+)\b")


def symbol_pin_count(lib_id: str) -> Optional[int]:
    """Position count encoded in a ``…_01x{NN}`` symbol id, or None if the id does
    not encode one (an arbitrary lib_id such as ``Connector:Barrel_Jack``)."""
    m = _SYMBOL_PIN_RE.search(lib_id)
    return int(m.group(1)) if m else None


def default_footprint(connector_type: str, n: int) -> str:
    """The series-default footprint for ``n`` positions of ``connector_type``."""
    spec = _CONNECTOR_TYPES.get(connector_type)
    screw = spec[2] if spec else False
    if screw and n in _PHOENIX_RANGE:
        return _PHOENIX_FP.format(n=n)
    return _PIN_HEADER_FP.format(n=n)


@dataclass
class ConnectorPosition:
    """One boundary-crossing signal landing on a connector position.

    ``net_name`` is the net this position joins (an existing signal/power net, or
    a new one). ``label`` is the short silk-legend text (signal/role/rail name).
    ``pin`` pins the pad number explicitly (an arbitrary-pinout connector such as
    a barrel jack); left None, positions are assigned pads ``1..N`` in order.
    """
    net_name: str
    label: str
    pin: Optional[str] = None


class ConnectorError(ValueError):
    """A connector that cannot be synthesized (too many positions for the symbol,
    duplicate pads, …). Raised loudly — never silently truncated."""


def synthesize_connector(
    positions: list[ConnectorPosition],
    *,
    alloc: Callable[[str], str],
    device: str,
    connector_type: str = "screw_terminal",
    lib_id: Optional[str] = None,
    footprint: Optional[str] = None,
    value: Optional[str] = None,
    origin: str = "template",
) -> tuple[Peripheral, list[tuple[str, Endpoint]], ConnectorLegend]:
    """Synthesize a connector for ``positions`` and return
    ``(connector_peripheral, [(net_name, Endpoint), …], ConnectorLegend)``.

    The caller appends each ``(net_name, Endpoint)`` onto that net (the same
    "join" channel templates already use), adds the Peripheral, and appends the
    legend to ``intent.connector_legends``.

    ``alloc(prefix) -> ref`` is the caller's collision-free ref allocator (the
    ONE allocator the §4 consolidation routes every fragment through).
    """
    if not positions:
        raise ConnectorError(f"connector for {device!r}: needs at least one position")
    if connector_type not in _CONNECTOR_TYPES:
        raise ConnectorError(
            f"connector for {device!r}: unknown connector_type {connector_type!r}; "
            f"valid: {sorted(_CONNECTOR_TYPES)}"
        )
    n = len(positions)
    sym_tmpl, prefix, _ = _CONNECTOR_TYPES[connector_type]
    chosen_lib = lib_id if lib_id is not None else sym_tmpl.format(n=n)

    # Pin-count validation — N must fit the symbol when the symbol encodes a count.
    cap = symbol_pin_count(chosen_lib)
    if cap is not None and n > cap:
        raise ConnectorError(
            f"connector for {device!r}: {n} positions exceed symbol "
            f"{chosen_lib!r} pin count {cap}"
        )

    ref = alloc(prefix)
    chosen_fp = (footprint if footprint is not None
                 else default_footprint(connector_type, n))

    # Assign pad numbers: explicit ``pin`` is honored; the rest fill the lowest
    # free integers 1..N in order. Duplicate pads are an error, not a silent
    # collapse (two signals onto one pad would short them).
    taken: set[str] = {p.pin for p in positions if p.pin is not None}
    if len(taken) != len([p for p in positions if p.pin is not None]):
        raise ConnectorError(f"connector for {device!r}: duplicate explicit pad numbers")
    joins: list[tuple[str, Endpoint]] = []
    assigned: list[tuple[str, str, str]] = []   # (pad, label, net_name)
    auto = 1
    for p in positions:
        if p.pin is not None:
            pad = str(p.pin)
        else:
            while str(auto) in taken:
                auto += 1
            pad = str(auto)
            taken.add(pad)
            auto += 1
        joins.append((p.net_name, Endpoint(ref=ref, pin=pad)))
        assigned.append((pad, p.label, p.net_name))

    if cap is not None:
        for pad, _label, _net in assigned:
            ip = int(pad) if pad.isdigit() else None
            if ip is not None and ip > cap:
                raise ConnectorError(
                    f"connector for {device!r}: pad {pad} exceeds symbol "
                    f"{chosen_lib!r} pin count {cap}"
                )

    legend = _build_legend(ref, device, assigned)

    conn = Peripheral(
        ref=ref,
        type=_TYPE_TAG.get(connector_type, "CONN"),
        lib_id=chosen_lib,
        value=value if value is not None else device,
        footprint=chosen_fp,
        origin=origin,
    )
    return conn, joins, legend


def _build_legend(
    ref: str, device: str, assigned: list[tuple[str, str, str]]
) -> ConnectorLegend:
    """``ConnectorLegend.positions[i]`` is the silk label for pad number ``i+1``
    (the contiguous ``1..N`` case the synthesized terminals always produce). Pads
    that aren't plain integers (an arbitrary-pinout connector) are skipped — the
    legend documents the field-wired terminals, where pads are always 1..N.

    Open Decision 6 dedup: two positions on the SAME net sharing a label is fine
    (two ``GND`` taps → both ``GND``). Two DIFFERENT nets that shorten to the same
    label would be ambiguous on the silk, so the later one gets a ``_2`` suffix."""
    int_pads = [(int(pad), label, net) for pad, label, net in assigned if pad.isdigit()]
    positions: list[str] = []
    if int_pads:
        size = max(p for p, _, _ in int_pads)
        positions = [""] * size
        label_net: dict[str, str] = {}   # label -> first net that claimed it
        used: dict[str, int] = {}
        for pad, label, net in int_pads:
            owner = label_net.get(label)
            if owner is None or owner == net:
                label_net.setdefault(label, net)
                final = label
            else:                         # different net, same label -> suffix
                used[label] = used.get(label, 1) + 1
                final = f"{label}_{used[label]}"
            positions[pad - 1] = final
    return ConnectorLegend(ref=ref, positions=positions, device=device)
