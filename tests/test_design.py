"""Tests for the design router's error handling.

h-design-intent: load_intent runs on a possibly hand-edited / truncated /
wrong-schema-version YAML doc. A parse failure must surface as a structured
{status: error, code: invalid_intent} result, not a raw traceback to fastmcp.
Before the fix, _op_expand/_op_generate/_op_show called load_intent unguarded.
"""
from pathlib import Path

from kicad_mcp.tools.design import (
    _find_firmware,
    _gather_sketch,
    _op_expand,
    _op_generate,
    _op_import,
    _op_show,
)

_SKETCH = Path(__file__).parent / "fixtures" / "firmware" / "sketch_blink"


def test_op_expand_malformed_yaml_returns_structured_error(tmp_path):
    p = tmp_path / "intent.yaml"
    p.write_text("{a: 1")  # unclosed flow mapping -> yaml.YAMLError
    result = _op_expand(intent_path=str(p), out_path=None)
    assert result["status"] == "error"
    assert result["code"] == "invalid_intent"


def test_op_show_malformed_yaml_returns_structured_error(tmp_path):
    p = tmp_path / "intent.yaml"
    p.write_text("[1, 2, 3")  # unclosed flow sequence -> yaml.YAMLError
    result = _op_show(intent_path=str(p))
    assert result["status"] == "error"
    assert result["code"] == "invalid_intent"


def test_op_generate_truncated_intent_returns_structured_error(tmp_path):
    # A truncated/empty doc -> yaml.safe_load returns None -> from_dict(None)
    # does None.get(...) -> AttributeError, which the loader must convert.
    p = tmp_path / "intent.yaml"
    p.write_text("")
    result = _op_generate(intent_path=str(p), schematic_path=str(tmp_path / "out.kicad_sch"))
    assert result["status"] == "error"
    assert result["code"] == "invalid_intent"


# --- firmware discovery: config.h + Arduino .ino sketches (Phase 1a) ----------

def test_find_firmware_reads_config_h_file(tmp_path):
    h = tmp_path / "config.h"
    h.write_text("#define LED_PIN 2\n")
    src = _find_firmware(str(h))
    assert src is not None and src.anchor == h and "LED_PIN" in src.text


def test_find_firmware_dir_prefers_config_h_over_ino(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "config.h").write_text("#define A_PIN 5\n")
    (tmp_path / "stray.ino").write_text("const int B_PIN = 6;\n")
    src = _find_firmware(str(tmp_path))
    assert src.anchor == tmp_path / "include" / "config.h"   # PlatformIO layout wins


def test_find_firmware_sketch_folder_concatenates_tabs():
    src = _find_firmware(str(_SKETCH))
    assert src is not None
    assert src.anchor == _SKETCH / "sketch_blink.ino"        # folder-named main is the anchor
    assert "LED_PIN" in src.text and "I2C_SDA" in src.text   # main tab
    assert "BUZZER_PIN" in src.text                          # secondary tab (pins.ino)


def test_find_firmware_single_ino_gathers_sibling_tabs():
    # pointed at one tab -> still builds the whole sketch (the IDE compiles all tabs)
    src = _find_firmware(str(_SKETCH / "pins.ino"))
    assert src is not None and "LED_PIN" in src.text and "BUZZER_PIN" in src.text


def test_find_firmware_none_for_dir_without_firmware(tmp_path):
    assert _find_firmware(str(tmp_path)) is None


def test_gather_sketch_orders_main_first_then_alpha(tmp_path):
    d = tmp_path / "myproj"
    d.mkdir()
    (d / "zeta.ino").write_text("// z\n")
    (d / "myproj.ino").write_text("// main\n")
    (d / "alpha.ino").write_text("// a\n")
    src = _gather_sketch(d)
    assert src is not None and src.anchor == d / "myproj.ino"
    assert src.text.index("// main") < src.text.index("// a") < src.text.index("// z")


def test_op_import_arduino_sketch_end_to_end(tmp_path):
    """The full Arduino-IDE path: discover the multi-tab sketch, parse pins from
    #define AND const across tabs, resolve the MCU from the sidecar board_id."""
    from kicad_mcp.utils.firmware.intent import load_intent
    r = _op_import(firmware_path=str(_SKETCH), out_path=str(tmp_path / "i.yaml"))
    assert r["status"] == "ok"
    assert r["board"] == "esp32dev" and r["board_source"] == "sidecar"
    intent = load_intent(r["intent_path"])
    assert intent.mcu is not None
    assert not any(g.kind == "mcu_unknown" for g in intent.gaps)

    def gpio_of(net_name):
        return next((e.gpio for n in intent.nets if n.name == net_name
                     for e in n.endpoints if e.gpio is not None), None)
    assert gpio_of("LED") == 2        # #define, main tab
    assert gpio_of("BUZZER") == 4     # const int, the pins.ino tab
    # the const/constexpr I2C bare-alias pins were recognized too
    assert any(n.name in ("I2C_SDA", "I2C_SCL") for n in intent.nets)


# --- cold-review fixes: discovery precedence + anchor ------------------------

def test_find_firmware_top_level_ino_beats_nested_config_h(tmp_path):
    # a vendored library's nested config.h must NOT hijack a top-level .ino sketch
    (tmp_path / "sketch.ino").write_text("const int LED_PIN = 2;\n")
    lib = tmp_path / "libraries" / "VendorLib" / "src"
    lib.mkdir(parents=True)
    (lib / "config.h").write_text("#define VENDOR_THING 1\n")
    src = _find_firmware(str(tmp_path))
    assert src is not None and src.anchor == tmp_path / "sketch.ino"
    assert "LED_PIN" in src.text and "VENDOR_THING" not in src.text


def test_find_firmware_top_level_config_h_preferred(tmp_path):
    # a config.h at the top level (PlatformIO without include/) still wins over .ino
    (tmp_path / "config.h").write_text("#define A_PIN 5\n")
    (tmp_path / "sketch.ino").write_text("const int B_PIN = 6;\n")
    assert _find_firmware(str(tmp_path)).anchor == tmp_path / "config.h"


def test_find_firmware_anchors_on_pointed_at_ino(tmp_path):
    # pointing at a specific tab anchors provenance on THAT file, not the alpha-first
    (tmp_path / "aaa.ino").write_text("const int A_PIN = 1;\n")
    (tmp_path / "zzz.ino").write_text("const int Z_PIN = 9;\n")
    src = _find_firmware(str(tmp_path / "zzz.ino"))
    assert src is not None and src.anchor == tmp_path / "zzz.ino"
    assert "A_PIN" in src.text and "Z_PIN" in src.text   # still builds the whole sketch


def test_suggest_cards_works_on_ino_sketch():
    # regression guard: _op_suggest_cards must discover a .ino sketch, not error
    from kicad_mcp.tools.design import _op_suggest_cards
    r = _op_suggest_cards(firmware_path=str(_SKETCH))
    assert r["status"] == "ok"
