"""Firmware-spec front end: parse a firmware spec into a design-intent doc and
generate a partial schematic from it.

See docs/ and the plan: firmware code (#defines) -> design-intent doc
(facts + flagged gaps) -> partial .kicad_sch (MCU + recognized peripherals +
signal nets; power/passives/connectors left as explicit gaps).
"""
