"""PCB domain router — all PCB editing operations.

See docs/SPEC_Tool_Consolidation.md §2.
"""

import logging
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Op imports — each source module exposes module-level _op_* helpers
# ---------------------------------------------------------------------------

from kicad_mcp.tools.pcb_board import (
    _op_create,
    _op_load,
    _op_set_outline,
    _op_set_design_rules,
    _op_finalize,
)
from kicad_mcp.tools.pcb_footprints import (
    _op_place_footprint,
    _op_move_footprint,
    _op_list_footprints,
    _op_get_pad_positions,
    _op_get_footprint_dimensions,
)
from kicad_mcp.tools.pcb_nets import (
    _op_add_net,
    _op_rename_net,
    _op_list_nets,
    _op_set_net_class,
    _op_assign_pad_net,
    _op_bulk_assign_pad_nets,
)
from kicad_mcp.tools.pcb_routing import (
    _op_add_trace,
    _op_add_via,
    _op_clear_routing,
    _op_edit_trace_width,
)
from kicad_mcp.tools.pcb_zones import (
    _op_add_zone,
    _op_fill_zones,
)
from kicad_mcp.tools.pcb_silkscreen import (
    _op_add_text,
    _op_list_silkscreen,
    _op_update_silkscreen,
    _op_auto_fix_silkscreen,
    _op_check_silkscreen_overlaps,
)
# get_constraints shares impl with audit router (single source of truth)
from kicad_mcp.tools.pcb_keepout import _op_constraints as _op_get_constraints


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_pcb_tools(mcp: FastMCP) -> None:
    """Register the pcb domain router."""

    @mcp.tool()
    def pcb(
        operation: str,
        *,
        # ── Lifecycle / board ─────────────────────────────────────────────
        pcb_path: Optional[str] = None,
        x_mm: Optional[float] = None,
        y_mm: Optional[float] = None,
        width_mm: Optional[float] = None,
        height_mm: Optional[float] = None,
        min_track_width_mm: float = 0.2,
        min_clearance_mm: float = 0.2,
        min_via_diameter_mm: float = 0.6,
        min_via_drill_mm: float = 0.3,
        min_hole_to_hole_mm: float = 0.25,
        min_through_hole_diameter_mm: float = 0.3,
        min_copper_edge_clearance_mm: float = 0.5,
        fix_silkscreen: bool = True,
        fill_zones_flag: bool = True,
        # ── Footprints ────────────────────────────────────────────────────
        library: Optional[str] = None,
        footprint_name: Optional[str] = None,
        reference: Optional[str] = None,
        value: Optional[str] = None,
        rotation_deg: Optional[float] = None,
        layer: str = "F.Cu",
        check_keepouts: bool = True,
        # ── Nets ──────────────────────────────────────────────────────────
        net_name: str = "",
        old_name: Optional[str] = None,
        new_name: Optional[str] = None,
        class_name: Optional[str] = None,
        nets: Optional[List[str]] = None,
        track_width_mm: float = 0.25,
        clearance_mm: float = 0.2,
        via_diameter_mm: float = 0.6,
        via_drill_mm: float = 0.3,
        pad_number: Optional[str] = None,
        assignments: Optional[List[Dict[str, str]]] = None,
        # ── Routing ───────────────────────────────────────────────────────
        start_x_mm: Optional[float] = None,
        start_y_mm: Optional[float] = None,
        end_x_mm: Optional[float] = None,
        end_y_mm: Optional[float] = None,
        trace_width_mm: float = 0.25,
        drill_mm: float = 0.3,
        size_mm: float = 0.6,
        via_type: str = "through",
        clear_tracks: bool = True,
        clear_vias: bool = True,
        clear_zones_flag: bool = False,
        new_width_mm: Optional[float] = None,
        net_filter: str = "",
        layer_filter: str = "",
        # ── Zones ─────────────────────────────────────────────────────────
        corners: Optional[List[List[float]]] = None,
        zone_clearance_mm: float = 0.3,
        min_width_mm: float = 0.2,
        connect_pads: str = "thermal",
        priority: int = 0,
        # ── Silkscreen / text ─────────────────────────────────────────────
        text: Optional[str] = None,
        text_size_mm: float = 1.0,
        thickness_mm: float = 0.15,
        silk_field: str = "reference",
        visible: Optional[bool] = None,
        rel_x_mm: Optional[float] = None,
        rel_y_mm: Optional[float] = None,
        silk_size_mm: Optional[float] = None,
        silk_thickness_mm: Optional[float] = None,
        angle_deg: Optional[float] = None,
        silk_layer: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PCB domain router — board lifecycle, footprints, nets, routing, zones, silkscreen.

        Operations:

        LIFECYCLE
          create(pcb_path)
              -> {status, file}   Create a new empty .kicad_pcb file.

          load(pcb_path)
              -> {status, file, footprint_count, track_count, footprints}
                 Load a PCB file and return a summary.

          finalize(pcb_path, fix_silkscreen=True, fill_zones_flag=True)
              -> {status, silkscreen, zones}
                 Fix silkscreen overlaps and fill copper zones in one step.

        BOARD
          set_outline(pcb_path, x_mm, y_mm, width_mm, height_mm)
              -> {status, previous_edge_cuts_removed, outline}
                 Set a rectangular board outline on Edge.Cuts (replaces existing).

          set_design_rules(pcb_path, min_track_width_mm=0.2, min_clearance_mm=0.2,
                           min_via_diameter_mm=0.6, min_via_drill_mm=0.3,
                           min_hole_to_hole_mm=0.25, min_through_hole_diameter_mm=0.3,
                           min_copper_edge_clearance_mm=0.5)
              -> {status, design_rules, project_rules_updated}
                 Set PCB design rules in the PCB file and .kicad_pro.

          get_constraints(pcb_path)
              -> {status, board_outline, keepout_zones, design_rules,
                  existing_footprints_count, total_keepout_area_mm2}
                 Board outline + keepouts + design rules. Shared with audit.constraints.

        FOOTPRINTS
          place_footprint(pcb_path, library, footprint_name, reference, value,
                          x_mm, y_mm, rotation_deg=0, layer="F.Cu",
                          check_keepouts=True)
              -> {status, placed, bounding_box, placement_warnings?}

          move_footprint(pcb_path, reference, x_mm, y_mm, rotation_deg=None)
              -> {status, reference, x_mm, y_mm, rotation}

          list_footprints(pcb_path)
              -> {status, footprint_count, footprints}

          get_pad_positions(pcb_path, reference)
              -> {status, reference, pad_count, pads}

          get_footprint_dimensions(library, footprint_name, rotation_deg=0)
              -> {status, body_bbox, courtyard?, pad_span, keepout_zones?}
                 Query footprint dimensions WITHOUT placing it on a PCB.

        NETS
          add_net(pcb_path, net_name)
              -> {status, net, net_code}

          rename_net(pcb_path, old_name, new_name)
              -> {status, old_name, new_name, net_code, replacements}

          list_nets(pcb_path)
              -> {status, net_count, nets}

          set_net_class(pcb_path, class_name, nets, track_width_mm=0.25,
                        clearance_mm=0.2, via_diameter_mm=0.6, via_drill_mm=0.3)
              -> {status, class_name, action, nets_assigned}

          assign_pad_net(pcb_path, reference, pad_number, net_name)
              -> {status, reference, pad, net}

          bulk_assign_pad_nets(pcb_path, assignments)
              -> {status, assigned, error_count, nets_created, errors}

        ROUTING
          add_trace(pcb_path, start_x_mm, start_y_mm, end_x_mm, end_y_mm,
                    trace_width_mm=0.25, layer="F.Cu", net_name="")
              -> {status, trace}

          add_via(pcb_path, x_mm, y_mm, drill_mm=0.3, size_mm=0.6,
                  net_name="", via_type="through")
              -> {status, via}

          clear_routing(pcb_path, clear_tracks=True, clear_vias=True,
                        clear_zones_flag=False)
              -> {status, tracks_removed, vias_removed, zones_removed}

          edit_trace_width(pcb_path, new_width_mm, net_filter="", layer_filter="")
              -> {status, updated, skipped}

        ZONES
          add_zone(pcb_path, net_name, layer="F.Cu", corners=[], clearance_mm=0.3,
                   min_width_mm=0.2, connect_pads="thermal", priority=0)
              -> {status, zone}   corners=[] auto-derives from board outline.

          fill_zones(pcb_path)
              -> {status, fill_success, zones_filled, zones}

        SILKSCREEN + TEXT
          add_text(pcb_path, text, x_mm, y_mm, layer="F.SilkS",
                   text_size_mm=1.0, thickness_mm=0.15, rotation_deg=0)
              -> {status, text, x_mm, y_mm, layer}

          list_silkscreen(pcb_path)
              -> {status, item_count, items}

          update_silkscreen(pcb_path, reference, silk_field="reference",
                            visible=None, x_mm=None, y_mm=None, rel_x_mm=None,
                            rel_y_mm=None, silk_size_mm=None, silk_thickness_mm=None,
                            angle_deg=None, silk_layer=None)
              -> {status, reference, field, text, visible, ...}

          auto_fix_silkscreen(pcb_path)
              -> {status, already_ok, moved, hidden, fixes}

          check_silkscreen_overlaps(pcb_path)
              -> {status, silk_items_checked, pads_checked, overlap_count, overlaps,
                  text_overlap_count, text_overlaps}
                 Shared impl with audit.check_silkscreen_overlaps.
        """
        # ── Lifecycle ──────────────────────────────────────────────────────

        if operation == "create":
            if pcb_path is None:
                return {"error": "operation='create' requires 'pcb_path'"}
            return _op_create(pcb_path)

        if operation == "load":
            if pcb_path is None:
                return {"error": "operation='load' requires 'pcb_path'"}
            return _op_load(pcb_path)

        if operation == "finalize":
            if pcb_path is None:
                return {"error": "operation='finalize' requires 'pcb_path'"}
            return _op_finalize(pcb_path, fix_silkscreen=fix_silkscreen, fill_zones=fill_zones_flag)

        # ── Board ──────────────────────────────────────────────────────────

        if operation == "set_outline":
            if pcb_path is None:
                return {"error": "operation='set_outline' requires 'pcb_path'"}
            if x_mm is None:
                return {"error": "operation='set_outline' requires 'x_mm'"}
            if y_mm is None:
                return {"error": "operation='set_outline' requires 'y_mm'"}
            if width_mm is None:
                return {"error": "operation='set_outline' requires 'width_mm'"}
            if height_mm is None:
                return {"error": "operation='set_outline' requires 'height_mm'"}
            return _op_set_outline(pcb_path, x_mm, y_mm, width_mm, height_mm)

        if operation == "set_design_rules":
            if pcb_path is None:
                return {"error": "operation='set_design_rules' requires 'pcb_path'"}
            return _op_set_design_rules(
                pcb_path,
                min_track_width_mm=min_track_width_mm,
                min_clearance_mm=min_clearance_mm,
                min_via_diameter_mm=min_via_diameter_mm,
                min_via_drill_mm=min_via_drill_mm,
                min_hole_to_hole_mm=min_hole_to_hole_mm,
                min_through_hole_diameter_mm=min_through_hole_diameter_mm,
                min_copper_edge_clearance_mm=min_copper_edge_clearance_mm,
            )

        if operation == "get_constraints":
            if pcb_path is None:
                return {"error": "operation='get_constraints' requires 'pcb_path'"}
            return _op_get_constraints(pcb_path)

        # ── Footprints ─────────────────────────────────────────────────────

        if operation == "place_footprint":
            if pcb_path is None:
                return {"error": "operation='place_footprint' requires 'pcb_path'"}
            if library is None:
                return {"error": "operation='place_footprint' requires 'library'"}
            if footprint_name is None:
                return {"error": "operation='place_footprint' requires 'footprint_name'"}
            if reference is None:
                return {"error": "operation='place_footprint' requires 'reference'"}
            if value is None:
                return {"error": "operation='place_footprint' requires 'value'"}
            if x_mm is None:
                return {"error": "operation='place_footprint' requires 'x_mm'"}
            if y_mm is None:
                return {"error": "operation='place_footprint' requires 'y_mm'"}
            return _op_place_footprint(
                pcb_path, library, footprint_name, reference, value,
                x_mm, y_mm, rotation_deg=rotation_deg or 0.0, layer=layer,
                check_keepouts=check_keepouts,
            )

        if operation == "move_footprint":
            if pcb_path is None:
                return {"error": "operation='move_footprint' requires 'pcb_path'"}
            if reference is None:
                return {"error": "operation='move_footprint' requires 'reference'"}
            if x_mm is None:
                return {"error": "operation='move_footprint' requires 'x_mm'"}
            if y_mm is None:
                return {"error": "operation='move_footprint' requires 'y_mm'"}
            return _op_move_footprint(
                pcb_path, reference, x_mm, y_mm,
                rotation_deg=rotation_deg,
            )

        if operation == "list_footprints":
            if pcb_path is None:
                return {"error": "operation='list_footprints' requires 'pcb_path'"}
            return _op_list_footprints(pcb_path)

        if operation == "get_pad_positions":
            if pcb_path is None:
                return {"error": "operation='get_pad_positions' requires 'pcb_path'"}
            if reference is None:
                return {"error": "operation='get_pad_positions' requires 'reference'"}
            return _op_get_pad_positions(pcb_path, reference)

        if operation == "get_footprint_dimensions":
            if library is None:
                return {"error": "operation='get_footprint_dimensions' requires 'library'"}
            if footprint_name is None:
                return {"error": "operation='get_footprint_dimensions' requires 'footprint_name'"}
            return _op_get_footprint_dimensions(library, footprint_name, rotation_deg=rotation_deg or 0.0)

        # ── Nets ───────────────────────────────────────────────────────────

        if operation == "add_net":
            if pcb_path is None:
                return {"error": "operation='add_net' requires 'pcb_path'"}
            if not net_name:
                return {"error": "operation='add_net' requires 'net_name'"}
            return _op_add_net(pcb_path, net_name)

        if operation == "rename_net":
            if pcb_path is None:
                return {"error": "operation='rename_net' requires 'pcb_path'"}
            if old_name is None:
                return {"error": "operation='rename_net' requires 'old_name'"}
            if new_name is None:
                return {"error": "operation='rename_net' requires 'new_name'"}
            return _op_rename_net(pcb_path, old_name, new_name)

        if operation == "list_nets":
            if pcb_path is None:
                return {"error": "operation='list_nets' requires 'pcb_path'"}
            return _op_list_nets(pcb_path)

        if operation == "set_net_class":
            if pcb_path is None:
                return {"error": "operation='set_net_class' requires 'pcb_path'"}
            if class_name is None:
                return {"error": "operation='set_net_class' requires 'class_name'"}
            if nets is None:
                return {"error": "operation='set_net_class' requires 'nets'"}
            return _op_set_net_class(
                pcb_path, class_name, nets,
                track_width_mm=track_width_mm,
                clearance_mm=clearance_mm,
                via_diameter_mm=via_diameter_mm,
                via_drill_mm=via_drill_mm,
            )

        if operation == "assign_pad_net":
            if pcb_path is None:
                return {"error": "operation='assign_pad_net' requires 'pcb_path'"}
            if reference is None:
                return {"error": "operation='assign_pad_net' requires 'reference'"}
            if pad_number is None:
                return {"error": "operation='assign_pad_net' requires 'pad_number'"}
            if not net_name:
                return {"error": "operation='assign_pad_net' requires 'net_name'"}
            return _op_assign_pad_net(pcb_path, reference, pad_number, net_name)

        if operation == "bulk_assign_pad_nets":
            if pcb_path is None:
                return {"error": "operation='bulk_assign_pad_nets' requires 'pcb_path'"}
            if not assignments:
                return {"error": "operation='bulk_assign_pad_nets' requires 'assignments'"}
            return _op_bulk_assign_pad_nets(pcb_path, assignments)

        # ── Routing ────────────────────────────────────────────────────────

        if operation == "add_trace":
            if pcb_path is None:
                return {"error": "operation='add_trace' requires 'pcb_path'"}
            if start_x_mm is None:
                return {"error": "operation='add_trace' requires 'start_x_mm'"}
            if start_y_mm is None:
                return {"error": "operation='add_trace' requires 'start_y_mm'"}
            if end_x_mm is None:
                return {"error": "operation='add_trace' requires 'end_x_mm'"}
            if end_y_mm is None:
                return {"error": "operation='add_trace' requires 'end_y_mm'"}
            return _op_add_trace(
                pcb_path, start_x_mm, start_y_mm, end_x_mm, end_y_mm,
                width_mm=trace_width_mm, layer=layer, net_name=net_name,
            )

        if operation == "add_via":
            if pcb_path is None:
                return {"error": "operation='add_via' requires 'pcb_path'"}
            if x_mm is None:
                return {"error": "operation='add_via' requires 'x_mm'"}
            if y_mm is None:
                return {"error": "operation='add_via' requires 'y_mm'"}
            return _op_add_via(
                pcb_path, x_mm, y_mm,
                drill_mm=drill_mm, size_mm=size_mm,
                net_name=net_name, via_type=via_type,
            )

        if operation == "clear_routing":
            if pcb_path is None:
                return {"error": "operation='clear_routing' requires 'pcb_path'"}
            return _op_clear_routing(
                pcb_path,
                clear_tracks=clear_tracks,
                clear_vias=clear_vias,
                clear_zones=clear_zones_flag,
            )

        if operation == "edit_trace_width":
            if pcb_path is None:
                return {"error": "operation='edit_trace_width' requires 'pcb_path'"}
            if new_width_mm is None:
                return {"error": "operation='edit_trace_width' requires 'new_width_mm'"}
            return _op_edit_trace_width(
                pcb_path, new_width_mm, net_name=net_filter, layer=layer_filter
            )

        # ── Zones ──────────────────────────────────────────────────────────

        if operation == "add_zone":
            if pcb_path is None:
                return {"error": "operation='add_zone' requires 'pcb_path'"}
            if not net_name:
                return {"error": "operation='add_zone' requires 'net_name'"}
            return _op_add_zone(
                pcb_path, net_name,
                layer=layer,
                corners=corners if corners is not None else [],
                clearance_mm=zone_clearance_mm,
                min_width_mm=min_width_mm,
                connect_pads=connect_pads,
                priority=priority,
            )

        if operation == "fill_zones":
            if pcb_path is None:
                return {"error": "operation='fill_zones' requires 'pcb_path'"}
            return _op_fill_zones(pcb_path)

        # ── Silkscreen / text ──────────────────────────────────────────────

        if operation == "add_text":
            if pcb_path is None:
                return {"error": "operation='add_text' requires 'pcb_path'"}
            if text is None:
                return {"error": "operation='add_text' requires 'text'"}
            if x_mm is None:
                return {"error": "operation='add_text' requires 'x_mm'"}
            if y_mm is None:
                return {"error": "operation='add_text' requires 'y_mm'"}
            return _op_add_text(
                pcb_path, text, x_mm, y_mm,
                layer=layer, size_mm=text_size_mm,
                thickness_mm=thickness_mm, rotation_deg=rotation_deg or 0.0,
            )

        if operation == "list_silkscreen":
            if pcb_path is None:
                return {"error": "operation='list_silkscreen' requires 'pcb_path'"}
            return _op_list_silkscreen(pcb_path)

        if operation == "update_silkscreen":
            if pcb_path is None:
                return {"error": "operation='update_silkscreen' requires 'pcb_path'"}
            if reference is None:
                return {"error": "operation='update_silkscreen' requires 'reference'"}
            return _op_update_silkscreen(
                pcb_path, reference,
                field=silk_field,
                visible=visible,
                x_mm=x_mm, y_mm=y_mm,
                rel_x_mm=rel_x_mm, rel_y_mm=rel_y_mm,
                size_mm=silk_size_mm,
                thickness_mm=silk_thickness_mm,
                angle_deg=angle_deg,
                layer=silk_layer,
            )

        if operation == "auto_fix_silkscreen":
            if pcb_path is None:
                return {"error": "operation='auto_fix_silkscreen' requires 'pcb_path'"}
            return _op_auto_fix_silkscreen(pcb_path)

        if operation == "check_silkscreen_overlaps":
            if pcb_path is None:
                return {"error": "operation='check_silkscreen_overlaps' requires 'pcb_path'"}
            return _op_check_silkscreen_overlaps(pcb_path)

        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: create|load|finalize|"
                f"set_outline|set_design_rules|get_constraints|"
                f"place_footprint|move_footprint|list_footprints|"
                f"get_pad_positions|get_footprint_dimensions|"
                f"add_net|rename_net|list_nets|set_net_class|"
                f"assign_pad_net|bulk_assign_pad_nets|"
                f"add_trace|add_via|clear_routing|edit_trace_width|"
                f"add_zone|fill_zones|"
                f"add_text|list_silkscreen|update_silkscreen|"
                f"auto_fix_silkscreen|check_silkscreen_overlaps"
            )
        }
