"""Generate a partial ``.kicad_sch`` from a DesignIntent.

Packs the MCU + recognized peripherals onto a compact table sized to each
symbol's extent and wires each net by label-at-pin (same net name on every
resolved endpoint → KiCad connectivity), the same mechanism the schematic-build
tools use. Endpoints that don't resolve to a real symbol pin are recorded (not
silently skipped). Orphan nets get only their MCU pin labeled — the far end is
left open, matching the intent's gap.

Three disposition buckets in the result keep an ``ok`` status honest about what
was NOT wired and why: ``component_errors`` (a chip couldn't be placed) and
``unresolved_endpoints`` (a wire that should have been made failed) are FAILURES
and gate the status off ``ok``; ``deferred_endpoints`` are off-board endpoints
(remote/terminal) intentionally not wired here — realized at the expand step or
field-wired through a connector — and do NOT affect status (deferral isn't loss).
"""
from __future__ import annotations

from typing import Any

from kicad_mcp.utils.firmware.intent import DesignIntent, is_remote
from kicad_mcp.utils.firmware.knowledge import (
    is_terminal_card_type,
    resolve_mcu_by_part,
    resolve_symbol,
    role_to_pin_name,
)
from kicad_mcp.utils.firmware.mcu_pinmap import (
    gpio_to_pin_number,
    resolve_pin_token,
)

# --- layout geometry -------------------------------------------------------
# Components are laid out on a *table* grid: each column is as wide as its
# widest symbol and each row as tall as its tallest, plus a gutter that leaves
# room for the pin-stub + net-label text that ``wire_pin`` adds AFTER placement
# (those labels aren't in the symbol bounding box, so the gutter must cover
# them). A fixed-pitch grid wasted ~80% of the sheet on 12 mm-tall passives and
# pushed tall designs into a >600 mm strip that overran the page; this packs
# to the actual symbol extents and the page is then sized to fit exactly (see
# ``_finalize_page``). Connectivity is net-label-by-name, so position is free.
_COL_GUTTER_MM = 16.0     # horizontal room for stub + net-label text
_ROW_GUTTER_MM = 11.0     # vertical room for stub + net-label text
_MARKER_PITCH_MM = 20.32  # power-marker spacing in the band below the grid
_PAGE_MARGIN_MM = 12.7    # blank border kept around content on the sheet
_TARGET_ASPECT = 1.6      # width:height the column count aims for (landscape)
_GRID_MM = 1.27           # snap component origins to this grid

# KiCad's default worksheet renders the title block anchored to the page's
# bottom-right corner at a FIXED offset — measured on a blank sheet at 108.0 x
# 34.0mm, positioned 120.1mm from the right edge and 44.2mm from the bottom —
# independent of page size. A page sized tight to content packs components
# straight into that reserved corner, so ``_finalize_page`` pads the bottom
# margin and enforces a minimum width to keep it clear (padded above the
# measured footprint for safety margin, not tuned to the exact box).
_TITLEBLOCK_W_MM = 125.0
_TITLEBLOCK_H_MM = 45.0


def _component_extent(comp: Any) -> tuple[float, float, float, float]:
    """``(rel_min_x, rel_min_y, width, height)`` of a placed symbol's bounding
    box, expressed relative to its ``position`` anchor. Translation-invariant,
    so it is valid before the final placement pass moves the symbol."""
    from kicad_sch_api.core.component_bounds import get_component_bounding_box
    bb = get_component_bounding_box(comp, include_properties=False)
    return (bb.min_x - comp.position.x, bb.min_y - comp.position.y,
            bb.width, bb.height)


def _grid_dims(extents: list[tuple[float, float, float, float]], cols: int):
    """Column widths, row heights and overall (W, H) for a ``cols``-wide table
    grid over ``extents`` (in placement order). Width/height include gutters."""
    rows = (len(extents) + cols - 1) // cols
    col_w = [0.0] * cols
    row_h = [0.0] * rows
    for i, (_rx, _ry, w, h) in enumerate(extents):
        col_w[i % cols] = max(col_w[i % cols], w)
        row_h[i // cols] = max(row_h[i // cols], h)
    total_w = sum(col_w) + max(cols - 1, 0) * _COL_GUTTER_MM
    total_h = sum(row_h) + max(rows - 1, 0) * _ROW_GUTTER_MM
    return col_w, row_h, total_w, total_h


def _choose_cols(extents: list[tuple[float, float, float, float]]) -> int:
    """Pick the column count whose resulting grid aspect is closest to
    ``_TARGET_ASPECT`` — keeps the sheet landscape instead of a tall strip."""
    n = len(extents)
    if n <= 1:
        return max(n, 1)
    best_cols, best_dev = 1, None
    for cols in range(1, n + 1):
        _cw, _rh, w, h = _grid_dims(extents, cols)
        if h <= 0:
            continue
        dev = abs((w / h) - _TARGET_ASPECT)
        if best_dev is None or dev < best_dev:
            best_cols, best_dev = cols, dev
    return best_cols


def _snap(v: float) -> float:
    return round(v / _GRID_MM) * _GRID_MM


def _layout_table(comps: list[Any],
                  extents: list[tuple[float, float, float, float]],
                  cols: int) -> tuple[float, float]:
    """Position ``comps`` (in order) on a table grid; return the grid's
    bottom-left ``(x, bottom_y)`` so the power-marker band can sit below it."""
    from kicad_sch_api.core.types import Point
    col_w, row_h, _w, _h = _grid_dims(extents, cols)
    col_x = [0.0] * cols
    for c in range(1, cols):
        col_x[c] = col_x[c - 1] + col_w[c - 1] + _COL_GUTTER_MM
    rows = len(row_h)
    row_y = [0.0] * rows
    for r in range(1, rows):
        row_y[r] = row_y[r - 1] + row_h[r - 1] + _ROW_GUTTER_MM
    for i, comp in enumerate(comps):
        c, r = i % cols, i // cols
        rx, ry, w, _h2 = extents[i]
        # centre horizontally within the (possibly wider) column; top-align row.
        cx = col_x[c] + (col_w[c] - w) / 2.0
        comp.position = Point(_snap(cx - rx), _snap(row_y[r] - ry))
    bottom = (row_y[-1] + row_h[-1]) if rows else 0.0
    return 0.0, bottom


def _finalize_page(sch: Any, margin: float = _PAGE_MARGIN_MM) -> tuple[float, float]:
    """Translate all content to positive coordinates (a ``margin`` border) and
    return the ``(width, height)`` mm needed to contain it — the page size.

    Components can extend above/left of their anchor (negative coords) and the
    grid can be taller than any standard sheet; both clip silently on SVG/PDF
    export. We measure every element's true extent — symbol bounding boxes, wire
    endpoints, and net-label text — shift so the top-left sits at ``margin``,
    and hand back the exact sheet size so nothing falls outside the viewBox."""
    from kicad_sch_api.core.component_bounds import get_component_bounding_box
    from kicad_sch_api.core.types import Point

    inf = float("inf")
    min_x = min_y = inf
    max_x = max_y = -inf

    def grow(x: float, y: float) -> None:
        nonlocal min_x, min_y, max_x, max_y
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x)
        max_y = max(max_y, y)

    for comp in sch.components:
        try:
            bb = get_component_bounding_box(comp, include_properties=False)
        except Exception:  # noqa: BLE001 - a missing symbol must not lose the page fit
            grow(comp.position.x, comp.position.y)
            continue
        grow(bb.min_x, bb.min_y)
        grow(bb.max_x, bb.max_y)
    for wire in sch.wires:
        for p in wire.points:
            grow(p.x, p.y)
    for label in sch.labels:
        p = label.position
        # Net-label text renders to the +x side of its anchor (rotation 0); pad
        # so a label at the right edge isn't clipped (~0.95 mm/char at size 1.27).
        text_w = len(getattr(label, "text", "") or "") * 0.95
        grow(p.x, p.y)
        grow(p.x + text_w, p.y + 1.27)

    if min_x is inf:  # nothing placed — leave the default sheet untouched
        return 0.0, 0.0

    dx, dy = margin - min_x, margin - min_y
    for comp in sch.components:
        comp.position = Point(comp.position.x + dx, comp.position.y + dy)
    for wire in sch.wires:
        wire.points = [Point(p.x + dx, p.y + dy) for p in wire.points]
    for label in sch.labels:
        label.translate(dx, dy)

    # Bottom margin must clear the title block's reserved corner (see
    # _TITLEBLOCK_*_MM above) even though content only got the plain ``margin``
    # on every other side; width is floored the same way so the block's own
    # left edge doesn't run off a page narrower than its footprint.
    width = max((max_x - min_x) + 2 * margin, _TITLEBLOCK_W_MM + margin)
    height = (max_y - min_y) + margin + max(margin, _TITLEBLOCK_H_MM)
    return width, height


def _write_user_paper(schematic_path: str, width: float, height: float) -> None:
    """Rewrite the saved sheet's single-token ``(paper "...")`` into a KiCad
    ``User`` size with explicit mm dimensions. The kicad_sch_api serializer only
    emits a one-token paper, so an exact custom sheet must be patched in here."""
    import re
    with open(schematic_path, encoding="utf-8") as fh:
        text = fh.read()
    replacement = f'(paper "User" {width:.4f} {height:.4f})'
    patched, n = re.subn(r'\(paper\s+"[^"]*"\)', replacement, text, count=1)
    if n:
        with open(schematic_path, "w", encoding="utf-8") as fh:
            fh.write(patched)


def _build_status(component_errors: list, unresolved: list, any_placed: bool) -> str:
    """Three-state status for a (partial) schematic build.

    ``ok`` only when nothing was dropped and every endpoint that should have
    wired did. A dropped component or an unresolved endpoint means the saved
    schematic is incomplete: ``partial`` if at least something was placed,
    ``error`` if nothing was. Mirrors pcb_nets' ok/partial/error so callers see
    one consistent contract — and so a version-renamed/typo'd lib_id surfaces
    instead of silently reporting ``ok`` on a board missing a chip.

    Deferred endpoints (off-board/terminal, see ``deferred_endpoints``) are NOT
    failures and are intentionally excluded here — deferral is not loss.
    """
    if not component_errors and not unresolved:
        return "ok"
    return "partial" if any_placed else "error"

# Power-rail name -> KiCad power symbol lib_id (verified to resolve).
_RAIL_LIB = {
    "+3V3": "power:+3V3", "+5V": "power:+5V",
    "GND": "power:GND", "VBUS": "power:VBUS",
}


def generate_schematic(intent: DesignIntent, schematic_path: str) -> dict[str, Any]:
    import kicad_sch_api as ksa
    from kicad_sch_api.library.cache import get_symbol_cache

    from kicad_mcp.tools.schematic_impl import _kicad_pin_position, _pin_wire_offset

    if intent.mcu is None:
        return {
            "status": "error", "code": "no_mcu",
            "message": "Design intent has no MCU (unresolved board); cannot generate.",
        }

    cache = get_symbol_cache()
    sch = ksa.create_schematic("firmware_gen")

    # MCU card facts (resolved once): the GPIO pin-name prefix (ESP32 "IO", Pico
    # "GPIO") for gpio resolution below, and any cross-version symbol alternates.
    _mcu_info = resolve_mcu_by_part(intent.mcu.part)
    gpio_prefix = _mcu_info.get("gpio_pin_prefix", "IO") if _mcu_info else "IO"
    mcu_alts = list(_mcu_info.get("alt_lib_ids", []) or []) if _mcu_info else []

    # --- place components (MCU first, then peripherals) ---
    # Tuple: (ref, lib_id, alt_lib_ids, value, footprint). alt_lib_ids carries
    # older-KiCad names for a renamed symbol (from the card, when present).
    to_place: list[tuple[str, str | None, list[str], str, str | None]] = [
        (intent.mcu.ref, intent.mcu.lib_id, mcu_alts, intent.mcu.part, intent.mcu.footprint)
    ]
    # Off-board peripherals are field-wired (a terminal carries their nets), so
    # they are documented in the BOM but never placed as a symbol — their original
    # pin endpoints then drop out of the wiring loop (ref not in ``placed``),
    # leaving only the synthesized terminal wired (see remote_peripherals). A
    # peripheral is off-board if it's declared remote OR its card is
    # ``realize: terminal`` — the latter is intrinsically remote even on a raw
    # (non-expanded) intent, before _inject_terminal_loci has run.
    # ref -> why it's off-board (the reason carried into deferred_endpoints, so an
    # ``ok`` build still says which declared connections it deferred and why).
    offboard_reason: dict[str, str] = {}
    for p in intent.peripherals:
        if is_remote(intent, p.ref):
            offboard_reason[p.ref] = "remote: off-board, field-wired through a connector"
        elif is_terminal_card_type(p.type):
            offboard_reason[p.ref] = "terminal: realized as a screw terminal at the expand step"
    offboard_refs = set(offboard_reason)
    to_place += [
        (p.ref, p.lib_id, list(p.alt_lib_ids), p.value or p.type, p.footprint)
        for p in intent.peripherals if p.ref not in offboard_refs
    ]

    placed: dict[str, Any] = {}
    symbols: dict[str, Any] = {}
    component_errors: list[dict[str, Any]] = []
    # Add at a throwaway spread (final positions come from the table-layout pass
    # below; symbol extents are translation-invariant so the temp pos is moot).
    for i, (ref, lib_id, alts, value, footprint) in enumerate(to_place):
        if not lib_id:
            component_errors.append({"ref": ref, "error": "no lib_id in intent"})
            continue
        # Try the primary lib_id then any cross-version alternates; use whichever
        # name actually exists in the running library for the placement.
        resolved_lib_id, sym = resolve_symbol(cache, lib_id, alts)
        if sym is None:
            tried = ", ".join(repr(c) for c in (lib_id, *alts))
            component_errors.append({"ref": ref, "error": f"no symbol found (tried {tried})"})
            continue
        assert resolved_lib_id is not None  # sym present => a candidate resolved
        try:
            comp = sch.components.add(lib_id=resolved_lib_id, reference=ref, value=value,
                                      position=(25.4, 25.4 + i * 25.4), footprint=footprint)
        except Exception as e:  # noqa: BLE001 - record + continue, don't abort the build
            component_errors.append({"ref": ref, "error": f"add failed: {e}"})
            continue
        placed[ref] = comp
        symbols[ref] = sym

    # Pack the placed symbols onto a compact table grid (chosen column count
    # keeps the sheet landscape). Done before wiring, which reads final pin
    # positions. ``grid_bottom`` anchors the power-marker band below the grid.
    placed_comps = list(placed.values())
    extents = [_component_extent(c) for c in placed_comps]
    if placed_comps:
        grid_left, grid_bottom = _layout_table(
            placed_comps, extents, _choose_cols(extents))
    else:
        grid_left, grid_bottom = 0.0, 0.0

    type_by_ref = {p.ref: p.type for p in intent.peripherals}

    # gpio_prefix (resolved above) covers every gpio resolution below — an endpoint
    # carrying a gpio is always MCU-side.
    def resolve_pin(ref: str, gpio: Any, role: Any, pin: Any) -> Any:
        sym = symbols.get(ref)
        if sym is None:
            return None
        if pin is not None:                        # direct pin NAME or number
            return resolve_pin_token(sym, pin)
        if gpio is not None:                       # MCU side
            return gpio_to_pin_number(sym, int(gpio), gpio_prefix)
        ptype = type_by_ref.get(ref)               # peripheral side
        # The roles map may point at a pin NAME (MCP23017 ``SDA``) or a pin
        # NUMBER (a module-header device, ``SDA`` -> ``4``) — handle both.
        pin_name = role_to_pin_name(ptype, role) if ptype else None
        if pin_name is not None:
            num = resolve_pin_token(sym, pin_name)
            if num is not None:
                return num
        if role:                                   # last resort: role == pin name
            return resolve_pin_token(sym, role)
        return None

    def wire_pin(comp: Any, pin_num: str, net_name: str) -> bool:
        """Add a wire stub + net label at a pin (the connectivity primitive)."""
        pin_pos = _kicad_pin_position(comp, pin_num)
        if pin_pos is None:
            return False
        dx, dy = _pin_wire_offset(comp, pin_num, 2.54)
        lx, ly = pin_pos.x + dx, pin_pos.y + dy
        sch.add_wire(start=(pin_pos.x, pin_pos.y), end=(lx, ly))
        sch.add_label(net_name, (lx, ly))
        return True

    # --- wire nets ---
    nets_by_kind: dict[str, int] = {}
    endpoints_wired = 0
    unresolved: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for net in intent.nets:
        for ep in net.endpoints:
            if ep.ref not in placed:
                # Off-board peripherals are intentionally excluded from placement
                # (a terminal/connector handles their signals). Not a failure —
                # record them in their own bucket so a later ``ok`` is transparent
                # about which declared connections were deferred, rather than the
                # connection silently vanishing from the schematic.
                if ep.ref in offboard_refs:
                    deferred.append({"net": net.name, "ref": ep.ref,
                                     "role": ep.role,
                                     "reason": offboard_reason[ep.ref]})
                    continue
                unresolved.append({"net": net.name, "ref": ep.ref,
                                   "reason": "component not placed"})
                continue
            pin_num = resolve_pin(ep.ref, ep.gpio, ep.role, ep.pin)
            if pin_num is None:
                unresolved.append({"net": net.name, "ref": ep.ref, "gpio": ep.gpio,
                                   "role": ep.role, "pin": ep.pin,
                                   "reason": "pin unresolved"})
                continue
            if wire_pin(placed[ep.ref], pin_num, net.name):
                endpoints_wired += 1
            else:
                unresolved.append({"net": net.name, "ref": ep.ref, "pin": pin_num,
                                   "reason": "pin position unavailable"})
        nets_by_kind[net.kind] = nets_by_kind.get(net.kind, 0) + 1

    # --- idiomatic power-rail markers (best-effort) ---
    # Rail connectivity is already established by the rail-named labels above;
    # these add KiCad's conventional power symbol + PWR_FLAG per rail so the net
    # is recognized as a power rail (and ERC has a driver). Failure never aborts.
    # Markers go in a clear band BELOW all placed components. Position is
    # irrelevant to connectivity (labels join by name), but a marker dropped
    # onto a component pin would merge that pin's net into the rail — so keep
    # them well clear of the grid.
    marker_y = grid_bottom + _ROW_GUTTER_MM + _MARKER_PITCH_MM
    rail_markers = 0
    pwr_idx = 0
    for rail in sorted({n.name for n in intent.nets if n.kind == "power"}):
        rail_lib = _RAIL_LIB.get(rail)
        if rail_lib is None:
            continue
        for mlib in (rail_lib, "power:PWR_FLAG"):
            if cache.get_symbol(mlib) is None:
                continue
            try:
                mcomp = sch.components.add(
                    lib_id=mlib, reference=f"#PWR{pwr_idx:02d}", value=rail,
                    position=(grid_left + pwr_idx * _MARKER_PITCH_MM, marker_y))
            except Exception:  # noqa: BLE001 - markers are best-effort
                continue
            pwr_idx += 1
            if wire_pin(mcomp, "1", rail):
                rail_markers += 1

    # Shift everything to positive coords and size the sheet to fit exactly;
    # otherwise negative/over-tall content silently clips on SVG/PDF export.
    page_w, page_h = _finalize_page(sch)
    sch.save(schematic_path)
    if page_w > 0:
        _write_user_paper(schematic_path, page_w, page_h)

    return {
        "status": _build_status(component_errors, unresolved, bool(placed)),
        "schematic_path": schematic_path,
        "components_placed": len(placed),
        "component_errors": component_errors,
        "nets_total": len(intent.nets),
        "nets_by_kind": nets_by_kind,
        "endpoints_wired": endpoints_wired,
        "rail_markers": rail_markers,
        "unresolved_endpoints": unresolved,
        "deferred_endpoints": deferred,
        "gaps": len(intent.gaps),
        # Export honesty: the exact sheet size the schematic was written at, so
        # a caller can tell what viewBox/paper an SVG/PDF export will use. The
        # page is always sized to content, so nothing can fall outside it.
        "page_size_mm": [round(page_w, 1), round(page_h, 1)],
    }
