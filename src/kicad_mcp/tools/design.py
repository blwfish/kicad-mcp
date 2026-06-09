"""``design`` router — firmware-spec front end.

Turns a firmware spec — an ESP32 ``config.h`` or an Arduino ``.ino`` sketch, with
pins as ``#define`` or ``const``/``constexpr`` — into a canonical design-intent
doc, and generates a partial ``.kicad_sch`` from it. Recovers the MCU-side signal
nets deterministically; everything firmware can't know (power, passives,
connectors, parts) is emitted as an explicit gap manifest — never invented.

Operations:
  import_firmware(firmware_path, out_path?) -> {status, intent_path, summary, gaps}
      Parse firmware -> design-intent YAML. firmware_path is a config.h or .ino
      file, or a directory containing one (config.h, or an Arduino sketch folder
      whose .ino tabs are concatenated). The MCU is detected from platformio.ini,
      or declared via a board.yaml ``board_id`` (the escape hatch for sketches).
  intent_template() -> {valid_values, recognized_*, honesty_rules, example, how_to}
      Publish the DesignIntent contract — for firmware the deterministic parser
      CAN'T read (MicroPython, Rust, pinMode-literal C). An AI reads the firmware,
      authors an intent YAML matching `example`, then ingests it via import_intent.
  import_intent(intent_path, out_path?) -> {status, intent_path, summary, gaps}
      Ingest + VALIDATE an externally-authored intent (the second producer) to the
      same bar a parsed one meets; rejects on any honesty/structural error rather
      than letting a malformed intent become a wrong board.
  expand_templates(intent_path, out_path?) -> {status, components_added, gaps_resolved}
      Expand recognized components into support circuitry (power tree, decoupling,
      pull-ups, address strapping). Writes the richer intent (in place unless
      out_path given). Run between import_firmware and generate_schematic.
  generate_schematic(intent_path, schematic_path) -> {status, ...}
      Materialize MCU + recognized peripherals + signal nets into a .kicad_sch.
  show_intent(intent_path) -> {status, summary, gaps}
      Validate + summarize an intent doc (facts vs gaps).
  suggest_cards(firmware_path) -> {status, drafts, count}
      Offline auto-draft: propose device cards for the firmware's UNCARDED
      peripherals (I2C-address identity + WHO_AM_I hints + local symbol-name
      matching). Drafts are returned for review — never placed, no network used.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, NamedTuple, Optional

logger = logging.getLogger(__name__)

import yaml
from fastmcp import FastMCP

from kicad_mcp.utils.firmware.autodraft import draft_card, extract_whoami
from kicad_mcp.utils.firmware.bus_part_resolver import resolve_bus_parts
from kicad_mcp.utils.firmware.cards import (
    load_cards,
    part_serves_map,
    recognized_part_names,
)
from kicad_mcp.utils.firmware.generate import generate_schematic as _generate
from kicad_mcp.utils.firmware.intent import (
    MCU_REF,
    DesignIntent,
    Gap,
    build_intent,
    candidate_devices,
    contract_value_sets,
    example_intent,
    find_board_id,
    load_intent,
    save_intent,
    to_dict,
    validate_intent,
)
from kicad_mcp.utils.firmware.part_extractor import (
    collect_corpus,
    extract_part_names,
)
from kicad_mcp.utils.firmware.knowledge import resolve_peripheral
from kicad_mcp.utils.firmware.sidecar import (
    BoardSidecar,
    SidecarError,
    advise_unspecified_placement,
    apply_sidecar,
    find_sidecar,
    load_sidecar,
)
from kicad_mcp.utils.firmware.parse import (
    idf_target_defines,
    parse_macros,
    partition,
    select_active_branches,
)
from kicad_mcp.utils.firmware.templates import expand_intent


class FirmwareSource(NamedTuple):
    """A located firmware source: an ``anchor`` path (used for sidecar/board lookup,
    naming and the corpus scan) and the ``text`` to parse. For an Arduino sketch the
    text is all the folder's ``.ino`` tabs concatenated the way the IDE builds them."""
    anchor: Path
    text: str


def _gather_sketch(sketch_dir: Path) -> Optional[FirmwareSource]:
    """Build a FirmwareSource from a folder's ``.ino`` tabs, or None if it has none.
    The Arduino IDE compiles one translation unit: the folder-named main sketch
    first, then the remaining tabs alphabetically — replicated here so pins defined
    in a secondary tab (``pins.ino``) are seen."""
    inos = sorted(sketch_dir.glob("*.ino"))
    if not inos:
        return None
    main = sketch_dir / f"{sketch_dir.name}.ino"
    ordered = ([main] if main in inos else []) + [p for p in inos if p != main]
    text = "\n".join(p.read_text(errors="replace") for p in ordered)
    return FirmwareSource(anchor=(main if main in inos else ordered[0]), text=text)


def _find_firmware(firmware_path: str) -> Optional[FirmwareSource]:
    """Locate the firmware to parse from a file or directory path. Supports a
    PlatformIO/ESP-IDF ``config.h`` and an Arduino ``.ino`` sketch (multi-tab).
    Returns the source text plus an anchor path for sidecar/board lookup."""
    p = Path(firmware_path)
    if p.is_file():
        if p.suffix == ".ino":
            # Build the whole sketch from its folder's tabs, but anchor on the FILE
            # the user named (provenance/sidecar lookup), not the folder-named main.
            sketch = _gather_sketch(p.parent)
            text = sketch.text if sketch is not None else p.read_text(errors="replace")
            return FirmwareSource(p, text)
        return FirmwareSource(p, p.read_text(errors="replace"))
    if p.is_dir():
        for direct in (p / "include" / "config.h", p / "config.h"):
            if direct.exists():
                return FirmwareSource(direct, direct.read_text(errors="replace"))
        # A top-level .ino sketch wins over a config.h buried DEEPER in the tree —
        # otherwise a vendored library's nested config.h would hijack the import.
        sketch = _gather_sketch(p)
        if sketch is not None:
            return sketch
        matches = sorted(p.rglob("config.h"))
        if matches:
            if len(matches) > 1:
                # Several projects each carrying a config.h, none at the top level —
                # we pick the alphabetically-first, which may be the wrong firmware.
                # Warn so a silently-wrong import is diagnosable; pass a specific path.
                logger.warning(
                    "Multiple config.h found under %r; using %s. Pass a specific "
                    "config.h path to disambiguate. Candidates: %s",
                    firmware_path, matches[0], ", ".join(str(m) for m in matches),
                )
            return FirmwareSource(matches[0], matches[0].read_text(errors="replace"))
        return None
    return None


def _peripheral_entry(intent: DesignIntent, p: Any) -> dict[str, Any]:
    """One manifest row. ``locus`` is carried only when a peripheral is NOT a
    plain on-board part, so the BOM distinguishes placed parts, documented
    off-board devices, and the terminals they land on (§8)."""
    pl = intent.placements.get(p.ref)
    locus = pl.locus if pl is not None else p.locus
    entry: dict[str, Any] = {"ref": p.ref, "type": p.type}
    if locus != "on_board":
        entry["locus"] = locus
    return entry


def _summary(intent: DesignIntent) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for n in intent.nets:
        by_kind[n.kind] = by_kind.get(n.kind, 0) + 1
    # Field-wired devices: documented off-board, with the terminal they land on
    # (from the remote_device gaps the templates emit) — honest BOM (§8).
    remote_devices = [
        {"detail": g.detail,
         "terminal": g.resolved_components[0] if g.resolved_components else None}
        for g in intent.gaps if g.kind == "remote_device"
    ]
    out: dict[str, Any] = {
        "mcu": intent.mcu.part if intent.mcu else None,
        "peripherals": [_peripheral_entry(intent, p) for p in intent.peripherals],
        "net_count": len(intent.nets),
        "nets_by_kind": by_kind,
        "gap_count": len(intent.gaps),
        "unmodeled_macros": intent.provenance.get("unparsed_count", 0),
    }
    if remote_devices:
        out["remote_devices"] = remote_devices
    if intent.connector_legends:
        out["terminals"] = [
            {"ref": L.ref, "device": L.device, "positions": L.positions}
            for L in intent.connector_legends
        ]
    return out


def _gaps_list(intent: DesignIntent) -> list[dict[str, str]]:
    return [{"kind": g.kind, "detail": g.detail} for g in intent.gaps]


def register_design_tools(mcp: FastMCP) -> None:
    """Register the ``design`` router."""

    @mcp.tool()
    def design(
        operation: str,
        firmware_path: str | None = None,
        intent_path: str | None = None,
        schematic_path: str | None = None,
        out_path: str | None = None,
    ) -> dict:
        """Firmware-spec front end: firmware -> design-intent -> partial schematic.

        See the module docstring for operations and arguments.
        """
        if operation == "import_firmware":
            return _op_import(firmware_path=firmware_path, out_path=out_path)
        if operation == "import_intent":
            return _op_import_intent(intent_path=intent_path, out_path=out_path)
        if operation == "expand_templates":
            return _op_expand(intent_path=intent_path, out_path=out_path)
        if operation == "generate_schematic":
            return _op_generate(intent_path=intent_path, schematic_path=schematic_path)
        if operation == "show_intent":
            return _op_show(intent_path=intent_path)
        if operation == "suggest_cards":
            return _op_suggest_cards(firmware_path=firmware_path)
        if operation == "intent_template":
            return _op_intent_template()
        return {
            "status": "error", "code": "unknown_operation",
            "message": (f"Unknown operation: {operation!r}. Valid: import_firmware, "
                        "import_intent, expand_templates, generate_schematic, "
                        "show_intent, suggest_cards, intent_template."),
        }


def _load_sidecar_for(cfg: Path) -> tuple[Optional[BoardSidecar], Optional[str], Optional[dict]]:
    """Locate + load the board.yaml sidecar (if any), EARLY. Returns
    ``(sidecar, path, error)`` — ``error`` is a ready-to-return dict on malformed
    YAML, else None. Loaded up front (not post-build) so its ``board_id`` escape
    hatch can reach #if branch-selection and MCU resolution, not just the post-build
    keys."""
    sidecar_path = find_sidecar(str(cfg))
    if sidecar_path is None:
        return None, None, None
    try:
        return load_sidecar(sidecar_path), sidecar_path, None
    except SidecarError as e:
        return None, sidecar_path, {"status": "error", "code": "invalid_sidecar",
                                    "message": f"Malformed {sidecar_path}: {e}"}


def _resolve_board(cfg: Path, sidecar: Optional[BoardSidecar]) -> tuple[Optional[str], str]:
    """Board id for branch-selection + MCU resolution, with its source. A sidecar
    ``board_id`` (the escape hatch for projects with no/unrecognized platformio.ini,
    e.g. an Arduino-IDE sketch) overrides platformio detection."""
    if sidecar is not None and sidecar.board_id:
        return sidecar.board_id, "sidecar"
    board = find_board_id(str(cfg))
    return board, "platformio" if board else "none"


def _op_import(*, firmware_path: Optional[str], out_path: Optional[str]) -> dict:
    if not firmware_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "firmware_path is required."}
    src = _find_firmware(firmware_path)
    if src is None:
        return {"status": "error", "code": "config_not_found",
                "message": f"No firmware source (config.h or .ino sketch) found "
                           f"at or under {firmware_path!r}."}
    cfg = src.anchor

    # Load the board.yaml sidecar FIRST: its board_id (if any) is the escape hatch
    # for projects with no/unrecognized platformio.ini, and must reach the steps
    # below — not just the post-build apply.
    sidecar, sidecar_path, sidecar_err = _load_sidecar_for(cfg)
    if sidecar_err is not None:
        return sidecar_err
    board, board_source = _resolve_board(cfg, sidecar)

    # Select the active #if branch for this MCU target before extracting macros
    # (firmware wraps per-target pin maps in `#if CONFIG_IDF_TARGET_*`).
    text = select_active_branches(src.text, idf_target_defines(board))
    parsed = partition(parse_macros(text))
    intent = build_intent(parsed, firmware_path=str(cfg), board_id=board)

    # board.yaml sidecar (Phase 6b): firmware-blind facts (connectors, power
    # source, board size). Applied AFTER build_intent so the importer core stays
    # untouched. The intent-level checks (a placement key must exist in the intent)
    # raise here, so the apply is guarded even though load already succeeded.
    sidecar_applied = None
    if sidecar is not None:
        try:
            intent = apply_sidecar(intent, sidecar)
        except SidecarError as e:
            return {"status": "error", "code": "invalid_sidecar",
                    "message": f"Malformed {sidecar_path}: {e}"}
        sidecar_applied = sidecar_path

    # Part resolution (C4): bind each bus to the SPECIFIC part the firmware NAMES
    # (in the preprocessed config text + sibling source/docs), never invent. Runs
    # after the sidecar so a board.yaml override (provenance "user") is honored.
    # An ambiguous bus (>1 candidate) is disclosed as a gap, never auto-picked.
    peripherals, _ = load_cards()
    corpus = collect_corpus(str(cfg), text)
    evidence = extract_part_names(corpus, recognized_part_names(peripherals))
    for r in resolve_bus_parts(intent.buses, evidence, part_serves_map(peripherals)):
        if r.reason == "ambiguous":
            intent.gaps.append(Gap(
                "part_ambiguous",
                f"Bus {r.bus!r} ({r.type}) names multiple candidate parts "
                f"{r.candidates} in the firmware — not auto-bound (no silent pick). "
                f"Disambiguate with a board.yaml bus_part_overrides entry.",
            ))

    # Nudge (don't assume): list commonly-field-wired buses with no locus set.
    advise_unspecified_placement(intent)

    dest = Path(out_path) if out_path else cfg.parent / "design_intent.yaml"
    try:
        save_intent(intent, str(dest))
    except OSError as e:
        return {"status": "error", "code": "write_failed",
                "message": f"Could not write intent doc: {e}"}

    # power_source / board_size_mm are metadata recorded in the intent and
    # surfaced here. build_pcb_from_schematic reads board_size_mm from the intent
    # to size the board when no explicit dimensions are passed (explicit args
    # still win); power_source remains advisory.
    return {
        "status": "ok", "intent_path": str(dest),
        "board": board, "board_source": board_source,
        "summary": _summary(intent), "gaps": _gaps_list(intent),
        "sidecar": sidecar_applied,
        "board_size_mm": intent.source.get("board_size_mm"),
        "power_source": intent.source.get("power_source"),
        # Which bus bound to which SPECIFIC part, and from where — so the caller
        # sees the resolver isn't inventing (the SPH0645-for-INMP441 fix).
        "resolved_parts": {
            b.name: {"part": b.resolved_part, "via": b.part_provenance}
            for b in intent.buses if b.resolved_part
        },
    }


def _load_intent_or_error(intent_path: str) -> tuple[Optional[DesignIntent], Optional[dict]]:
    """Load an intent doc, returning (intent, None) or (None, error_dict). The
    doc may be hand-edited, truncated, or from a different schema version, so a
    parse failure must surface as a structured error rather than a raw traceback
    to fastmcp (h-design-intent). Mirrors _op_import's care with SidecarError."""
    try:
        return load_intent(intent_path), None
    except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError, ValueError) as e:
        return None, {
            "status": "error", "code": "invalid_intent",
            "message": f"Could not parse intent doc {intent_path}: {type(e).__name__}: {e}",
        }


def _op_expand(*, intent_path: Optional[str], out_path: Optional[str]) -> dict:
    if not intent_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "intent_path is required."}
    if not Path(intent_path).exists():
        return {"status": "error", "code": "intent_not_found",
                "message": f"Intent doc not found: {intent_path}"}
    intent, err = _load_intent_or_error(intent_path)
    if intent is None:
        assert err is not None
        return err
    n_comp_before = len(intent.peripherals)
    intent = expand_intent(intent)
    dest = out_path or intent_path
    try:
        save_intent(intent, str(dest))
    except OSError as e:
        return {"status": "error", "code": "write_failed",
                "message": f"Could not write expanded intent: {e}"}
    return {
        "status": "ok", "intent_path": str(dest),
        "components_added": len(intent.peripherals) - n_comp_before,
        "gaps_resolved": [g.kind for g in intent.gaps if g.resolved],
        # Open gaps the expansion surfaced (e.g. assumed_part / part_unavailable
        # from part resolution) — disclosure is worthless if the op hides it.
        "gaps": _gaps_list(intent),
        "summary": _summary(intent),
    }


def _op_generate(*, intent_path: Optional[str], schematic_path: Optional[str]) -> dict:
    if not intent_path or not schematic_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "intent_path and schematic_path are both required."}
    if not Path(intent_path).exists():
        return {"status": "error", "code": "intent_not_found",
                "message": f"Intent doc not found: {intent_path}"}
    intent, err = _load_intent_or_error(intent_path)
    if intent is None:
        assert err is not None
        return err
    return _generate(intent, schematic_path)


def _op_suggest_cards(*, firmware_path: Optional[str]) -> dict:
    """Offline auto-draft: propose device cards for the firmware's UNCARDED
    peripherals (Phase 7). Drafts are returned for review — nothing is placed,
    no network is used. Symbol-based high-confidence drafting happens once a
    lib_id is supplied/confirmed; an address-only device gets a flagged skeleton."""
    if not firmware_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "firmware_path is required."}
    src = _find_firmware(firmware_path)
    if src is None:
        return {"status": "error", "code": "config_not_found",
                "message": f"No firmware source (config.h or .ino sketch) found "
                           f"at or under {firmware_path!r}."}
    cfg = src.anchor
    # Honor a sidecar board_id (escape hatch) so suggest_cards resolves the same MCU
    # and #if branches as import would; the sidecar's other keys don't affect drafting.
    sidecar, _, sidecar_err = _load_sidecar_for(cfg)
    if sidecar_err is not None:
        return sidecar_err
    board, _ = _resolve_board(cfg, sidecar)
    text = select_active_branches(src.text, idf_target_defines(board))
    parsed = partition(parse_macros(text))
    intent = build_intent(parsed, firmware_path=str(cfg), board_id=board)
    whoami = extract_whoami(intent.provenance)

    drafts: list[dict] = []
    skipped: list[dict] = []
    for t, address in candidate_devices(parsed):
        if resolve_peripheral(t) is not None:
            continue  # already carded -> placed by import, nothing to draft
        bus = "I2C" if address is not None else None
        roles = {"SDA": "SDA", "SCL": "SCL"} if bus == "I2C" else {}
        d = draft_card(type_name=t, address=address, bus=bus, roles=roles,
                       whoami=whoami)
        if d is not None:
            drafts.append({"confidence": d.confidence, "reasons": d.reasons,
                           "card": d.card})
        else:
            # Auto-draft only handles I2C-addressable identity today. A non-I2C
            # uncarded peripheral (HX711, bit-bang device, …) can't be drafted —
            # record it so the caller knows it was considered and needs a card
            # written by hand, rather than silently omitting it.
            skipped.append({"type": t, "address": address,
                            "reason": "no I2C address — auto-draft heuristics do not apply"})
    return {
        "status": "ok",
        "drafts": drafts,
        "count": len(drafts),
        "skipped": skipped,
        "skipped_count": len(skipped),
        "note": ("Drafts are proposals — review/confirm lib_id, footprint, roles, "
                 "supply/ground pins, then drop the card in a devices dir "
                 "(KICAD_MCP_DEVICE_DIRS) and re-run import_firmware. "
                 "`skipped` peripherals need a device card written by hand."),
    }


def _op_show(*, intent_path: Optional[str]) -> dict:
    if not intent_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "intent_path is required."}
    if not Path(intent_path).exists():
        return {"status": "error", "code": "intent_not_found",
                "message": f"Intent doc not found: {intent_path}"}
    intent, err = _load_intent_or_error(intent_path)
    if intent is None:
        assert err is not None
        return err
    # Report validation alongside the summary (non-fatal here — show is read-only)
    # so an author can see what import_intent would reject without writing anything.
    return {"status": "ok", "summary": _summary(intent), "gaps": _gaps_list(intent),
            "validation": validate_intent(intent)}


def _op_import_intent(*, intent_path: Optional[str], out_path: Optional[str]) -> dict:
    """Ingest an externally-authored intent (an AI reading non-C firmware, or a hand
    edit), VALIDATING it to the same bar as a parsed one before it enters the
    pipeline. Rejects on any validation error rather than letting a malformed intent
    silently become a wrong board. The canonical, validated intent is written ready
    for expand_templates / generate_schematic."""
    if not intent_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "intent_path is required."}
    if not Path(intent_path).exists():
        return {"status": "error", "code": "intent_not_found",
                "message": f"Intent doc not found: {intent_path}"}
    intent, err = _load_intent_or_error(intent_path)
    if intent is None:
        assert err is not None
        return err
    errors = validate_intent(intent)
    if errors:
        return {"status": "error", "code": "invalid_intent",
                "message": f"Intent failed validation ({len(errors)} issue(s)); "
                           "fix these before it can enter the pipeline.",
                "errors": errors}
    # Mark provenance so an ingested (non-parsed) intent is distinguishable from a
    # deterministically-parsed one (which carries source_file/unparsed, no producer).
    intent.provenance.setdefault("producer", "external")
    intent.provenance["ingested_via"] = "import_intent"
    dest = Path(out_path) if out_path else Path(intent_path).parent / "design_intent.yaml"
    try:
        save_intent(intent, str(dest))
    except OSError as e:
        return {"status": "error", "code": "write_failed",
                "message": f"Could not write intent doc: {e}"}
    return {"status": "ok", "intent_path": str(dest),
            "producer": intent.provenance.get("producer"),
            "summary": _summary(intent), "gaps": _gaps_list(intent)}


def _op_intent_template() -> dict:
    """Publish the DesignIntent contract for a producer building an intent by hand —
    an AI reading firmware the deterministic parser can't (MicroPython, Rust,
    pinMode-literal C). Returns the valid value sets, the recognized parts (with real
    lib_ids), the honesty rules, and a copy-pasteable example. Generated from the
    code, so it can't drift from what import_intent will accept."""
    peris, mcus = load_cards()
    return {
        "status": "ok",
        "how_to": [
            "1. Read the firmware: identify the MCU/board, each peripheral chip (by a "
            "recognized type), and each signal net (an MCU gpio <-> a peripheral role).",
            "2. Write an intent YAML shaped like `example`. Use ONLY a recognized "
            "mcu/peripheral type + its lib_id from `recognized_*`; a lib_id must exist "
            "in KiCad or generate_schematic will drop that part.",
            "3. Emit all of `valid_values.required_gaps` — firmware is structurally "
            "blind to power/decoupling/pullups/connectors/parts.",
            "4. design(operation='import_intent', intent_path=..., out_path=...) — it "
            "validates and rejects with a specific error list if anything is off.",
            "5. Then expand_templates -> generate_schematic on the validated doc.",
        ],
        "valid_values": contract_value_sets(),
        "honesty_rules": [
            "All required_gaps MUST be present (the firmware-blind manifest).",
            "If you can't resolve the MCU: set mcu to null AND add an 'mcu_unknown' gap.",
            f"Every net endpoint ref must be the MCU ('{MCU_REF}') or a declared "
            "peripheral ref.",
            "Anything firmware can't express that you DROPPED goes in "
            "provenance.unparsed — never silently lose it.",
        ],
        "recognized_mcus": [
            {"part": c["part"], "lib_id": c["lib_id"],
             "board_match": list(c.get("board_match", []) or [])}
            for c in mcus
        ],
        "recognized_peripherals": [
            {"type": t, "lib_id": c.get("lib_id"), "bus": c.get("bus")}
            for t, c in sorted(peris.items())
        ],
        "example": to_dict(example_intent()),
    }
