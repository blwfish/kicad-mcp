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
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.firmware.generate import generate_schematic as _generate
from kicad_mcp.utils.firmware.intent import (
    DesignIntent,
    build_intent,
    find_board_id,
    load_intent,
    save_intent,
)
from kicad_mcp.utils.firmware.parse import parse_defines, partition
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
        return {
            "status": "error", "code": "unknown_operation",
            "message": (f"Unknown operation: {operation!r}. Valid: import_firmware, "
                        "expand_templates, generate_schematic, show_intent."),
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
    parsed = partition(parse_defines(cfg.read_text(errors="replace")))
    intent = build_intent(parsed, firmware_path=str(cfg), board_id=board)

    dest = Path(out_path) if out_path else cfg.parent / "design_intent.yaml"
    try:
        save_intent(intent, str(dest))
    except OSError as e:
        return {"status": "error", "code": "write_failed",
                "message": f"Could not write intent doc: {e}"}

    return {
        "status": "ok", "intent_path": str(dest),
        "board": board, "summary": _summary(intent), "gaps": _gaps_list(intent),
    }


def _op_expand(*, intent_path: Optional[str], out_path: Optional[str]) -> dict:
    if not intent_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "intent_path is required."}
    if not Path(intent_path).exists():
        return {"status": "error", "code": "intent_not_found",
                "message": f"Intent doc not found: {intent_path}"}
    intent = load_intent(intent_path)
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
    intent = load_intent(intent_path)
    return _generate(intent, schematic_path)


def _op_show(*, intent_path: Optional[str]) -> dict:
    if not intent_path:
        return {"status": "error", "code": "missing_parameter",
                "message": "intent_path is required."}
    if not Path(intent_path).exists():
        return {"status": "error", "code": "intent_not_found",
                "message": f"Intent doc not found: {intent_path}"}
    intent = load_intent(intent_path)
    return {"status": "ok", "summary": _summary(intent), "gaps": _gaps_list(intent)}
