"""Schematic auto-placement subsystem.

Per ``docs/SPEC_Schematic_Placement.md``. The package is structured as:

- ``state``    — PlacementState dataclass + serialization helpers
- ``topology`` — Layer 1: graph construction + Louvain community detection
- ``labeling`` — Layers 2/3/4: pattern_recognition + LCSC + caller hints  (Slice 2)
- ``rank``     — Phase 2: directed-BFS tier assignment                     (Slice 3)
- ``pack``     — Phase 3: barycentric ordering + coordinate assignment     (Slice 3)
- ``conventions`` — v1 convention rules                                    (Slice 4)
- ``cache``    — stateful server-side cache for iteration                  (Slice 5)

The public router lives at ``kicad_mcp.tools.schematic_layout``.
"""
