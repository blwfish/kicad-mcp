"""``design`` router — firmware-spec front end.

Turns a firmware spec (ESP32-style ``config.h`` ``#define`` pin map) into a
canonical design-intent doc, and generates a partial ``.kicad_sch`` from it.
Recovers the MCU-side signal nets deterministically; everything firmware can't
know (power, passives, connectors, parts) is emitted as an explicit gap manifest
— never invented.

Operations:
  import_firmware(firmware_path, out_path?) -> {status, intent_path, summary, gaps}
      Parse firmware -> design-intent YAML. firmware_path is a config.h file or a
      directory containing one. Auto-detects the MCU from platformio.ini.
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

from pathlib import Path
from typing import Any, Optional

import yaml
from fastmcp import FastMCP

from kicad_mcp.utils.firmware.autodraft import draft_card, extract_whoami
from kicad_mcp.utils.firmware.generate import generate_schematic as _generate
from kicad_mcp.utils.firmware.intent import (
    DesignIntent,
    build_intent,
    candidate_devices,
    find_board_id,
    load_intent,
    save_intent,
)
from kicad_mcp.utils.firmware.knowledge import resolve_peripheral
from kicad_mcp.utils.firmware.sidecar import (
    SidecarError,
    apply_sidecar,
    find_sidecar,
    load_sidecar,
)
from kicad_mcp.utils.firmware.parse import (
    idf_target_defines,
    parse_defines,
    partition,
    select_active_branches,
)
from kicad_mcp.utils.firmware.templates import expand_intent


def _find_config_header(firmware_path: str) -> Optional[Path]:
    """Resolve the config.h to parse from a file or directory path."""
    p = Path(firmware_path)
    if p.is_file():
        return p
    if p.is_dir():
        prefer = p / "include" / "config.h"
        if prefer.exists():
            return prefer
        matches = sorted(p.rglob("config.h"))
        return matches[0] if matches else None
    return None


def _summary(intent: DesignIntent) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    for n in intent.nets:
        by_kind[n.kind] = by_kind.get(n.kind, 0) + 1
    return {
        "mcu": intent.mcu.part if intent.mcu else None,
        "peripherals": [{"ref": p.ref, "type": p.type} for p in intent.peripherals],
        "net_count": len(intent.nets),
        "nets_by_kind": by_kind,
        "gap_count": len(intent.gaps),
        "unmodeled_macros": intent.provenance.get("unparsed_count", 0),
    }


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
        if operation == "expand_templates":
            return _op_expand(intent_path=intent_path, out_path=out_path)
        if operation == "generate_schematic":
            return _op_generate(intent_path=intent_path, schematic_path=schematic_path)
        if operation == "show_intent":
            return _op_show(intent_path=intent_path)
        if operation == "suggest_cards":
            return _op_suggest_cards(firmware_path=firmware_path)
        return {
            "status": "error", "code": "unknown_operation",
            "message": (f"Unknown operation: {operation!r}. Valid: import_firmware, "
                        "expand_templates, generate_schematic, show_intent, "
                        "suggest_cards."),
        }


def _op_import(*, firmware_path: Optional[str], out_path: Optional[str]) -> dict:
    if not firmware_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "firmware_path is required."}
    cfg = _find_config_header(firmware_path)
    if cfg is None:
        return {"status": "error", "code": "config_not_found",
                "message": f"No config.h found at or under {firmware_path!r}."}

    board = find_board_id(str(cfg))
    # Select the active #if branch for this MCU target before extracting macros
    # (firmware wraps per-target pin maps in `#if CONFIG_IDF_TARGET_*`).
    text = select_active_branches(cfg.read_text(errors="replace"),
                                  idf_target_defines(board))
    parsed = partition(parse_defines(text))
    intent = build_intent(parsed, firmware_path=str(cfg), board_id=board)

    # board.yaml sidecar (Phase 6b): firmware-blind facts (connectors, power
    # source, board size) supplied as data next to config.h. Applied AFTER
    # build_intent so the importer core stays untouched.
    sidecar_path = find_sidecar(str(cfg))
    sidecar_applied = None
    if sidecar_path is not None:
        try:
            intent = apply_sidecar(intent, load_sidecar(sidecar_path))
        except SidecarError as e:
            return {"status": "error", "code": "invalid_sidecar",
                    "message": f"Malformed {sidecar_path}: {e}"}
        sidecar_applied = sidecar_path

    dest = Path(out_path) if out_path else cfg.parent / "design_intent.yaml"
    try:
        save_intent(intent, str(dest))
    except OSError as e:
        return {"status": "error", "code": "write_failed",
                "message": f"Could not write intent doc: {e}"}

    # power_source / board_size_mm are ADVISORY metadata recorded in the intent
    # for the human + the PCB step — surfaced here (build_pcb does not yet apply
    # board_size automatically; pass it explicitly).
    return {
        "status": "ok", "intent_path": str(dest),
        "board": board, "summary": _summary(intent), "gaps": _gaps_list(intent),
        "sidecar": sidecar_applied,
        "board_size_mm": intent.source.get("board_size_mm"),
        "power_source": intent.source.get("power_source"),
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
    cfg = _find_config_header(firmware_path)
    if cfg is None:
        return {"status": "error", "code": "config_not_found",
                "message": f"No config.h found at or under {firmware_path!r}."}
    board = find_board_id(str(cfg))
    text = select_active_branches(cfg.read_text(errors="replace"),
                                  idf_target_defines(board))
    parsed = partition(parse_defines(text))
    intent = build_intent(parsed, firmware_path=str(cfg), board_id=board)
    whoami = extract_whoami(intent.provenance)

    drafts: list[dict] = []
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
    return {
        "status": "ok",
        "drafts": drafts,
        "count": len(drafts),
        "note": ("Drafts are proposals — review/confirm lib_id, footprint, roles, "
                 "supply/ground pins, then drop the card in a devices dir "
                 "(KICAD_MCP_DEVICE_DIRS) and re-run import_firmware."),
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
    return {"status": "ok", "summary": _summary(intent), "gaps": _gaps_list(intent)}
