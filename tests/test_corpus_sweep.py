"""Mechanics tests for scripts/corpus_sweep.py (the Phase 3 nightly harness).

The sweep's VALUE claim (1,319 real sketches, zero crashes) is measured nightly
against live machine state; these tests pin the harness PLUMBING against a
synthetic corpus so a refactor can't quietly turn "all passed" into "swept
nothing": sketch discovery filters, per-sketch row shape, the loud missing-root
exit code, and the per-project (not per-example-file) backlog census.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "corpus_sweep.py"

spec = importlib.util.spec_from_file_location("corpus_sweep", SCRIPT)
cs = importlib.util.module_from_spec(spec)
sys.modules["corpus_sweep"] = cs   # dataclass introspection needs the registry
spec.loader.exec_module(cs)


def _mk_sketch(d: Path, name: str, text: str) -> Path:
    folder = d / name
    folder.mkdir(parents=True)
    (folder / f"{name}.ino").write_text(text)
    return folder


def test_sweep_one_imports_a_pin_bearing_sketch(tmp_path):
    d = _mk_sketch(tmp_path, "blinky",
                   "#define LED_PIN 13\nvoid setup() {}\nvoid loop() {}\n")
    row = cs.sweep_one(d, "esp32dev")
    assert "crash" not in row and "skip" not in row
    assert row["pins"] == 1 and row["nets"] >= 1


def test_sweep_one_never_drops_a_row_on_empty_sketch(tmp_path):
    d = _mk_sketch(tmp_path, "empty", "void setup() {}\nvoid loop() {}\n")
    row = cs.sweep_one(d, "esp32dev")
    assert row["pins"] == 0 and "crash" not in row   # degraded, not dead


def test_sketch_dirs_skips_own_firmware_in_project_tree(tmp_path):
    # in ANY project tree (detected structurally by libdeps presence, NOT by
    # directory name — the name check was a cold-review catch) only libdeps
    # examples are corpus
    root = tmp_path / "some-other-project"
    own = _mk_sketch(root / "myproj/src", "main", "void loop(){}")
    dep = _mk_sketch(root / "myproj/.pio/libdeps/esp32dev/SomeLib/examples",
                     "Demo", "void loop(){}")
    found = cs.sketch_dirs(root)
    assert dep in found and own not in found


def test_missing_root_exits_loudly(tmp_path):
    # a vanished corpus root must NOT read as a 0-sketch green run
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--root",
         f"gone=esp32dev={tmp_path}/does-not-exist"],
        capture_output=True, text=True)
    assert r.returncode == 2
    assert "MISSING/EMPTY CORPUS ROOT" in r.stdout


def test_backlog_counts_per_project_not_per_example(tmp_path):
    # one project depending on a lib with MANY examples counts once
    root = tmp_path / "tree"
    lib1 = root / "proj1/.pio/libdeps/esp32dev/ESP32Encoder"
    for ex in ("A", "B", "C"):
        _mk_sketch(lib1 / "examples", ex, "void loop(){}")
    _mk_sketch(root / "proj2/.pio/libdeps/esp32dev/ESP32Encoder/examples",
               "D", "void loop(){}")
    devices, _excluded, _unmapped = cs.libdeps_backlog(root)
    assert devices == {"rotary encoder": 2}   # 2 projects, not 4 examples


def test_unmapped_lib_is_reported_not_dropped(tmp_path):
    root = tmp_path / "tree"
    _mk_sketch(root / "p/.pio/libdeps/esp32dev/BrandNewSensorLib/examples",
               "X", "void loop(){}")
    devices, excluded, unmapped = cs.libdeps_backlog(root)
    assert unmapped == {"BrandNewSensorLib": 1} and not devices


def test_lib_to_device_excludes_have_no_silent_fourth_label():
    # every value in the table is a device NAME or an explicit None (excluded
    # with the not-a-device reason); the unmapped bucket covers the rest. No
    # entry can be "didn't notice".
    for lib, dev in cs.LIB_TO_DEVICE.items():
        assert dev is None or (isinstance(dev, str) and dev.strip()), lib


def test_present_but_empty_root_exits_loudly(tmp_path):
    # cold-review catch: an EXISTING root with zero sketches (wiped examples,
    # remounted-empty volume, regressed glob) must not read as a green run.
    empty = tmp_path / "corpus"
    empty.mkdir()
    r = subprocess.run([sys.executable, str(SCRIPT), "--root",
                        f"e=esp32dev={empty}"], capture_output=True, text=True)
    assert r.returncode == 2
    assert "no_sketches_found" in r.stdout


def test_malformed_root_arg_is_a_clean_usage_error(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "--root", "oops-no-equals"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "name=board_id=path" in r.stderr
    assert "Traceback" not in r.stderr   # usage error, not a fake crash signal


def test_duplicate_root_paths_rejected(tmp_path):
    d = _mk_sketch(tmp_path, "s", "#define LED_PIN 2\nvoid loop(){}")
    r = subprocess.run([sys.executable, str(SCRIPT),
                        "--root", f"a=esp32dev={tmp_path}",
                        "--root", f"b=esp32dev={tmp_path}"],
                       capture_output=True, text=True)
    assert r.returncode == 2 and "double-count" in r.stderr


def test_backlog_ignores_platformio_bookkeeping_dotdirs(tmp_path):
    # a .cache/ dir inside a libdeps env is tooling, not a library — it must
    # not appear in the unmapped census (cold-review catch).
    root = tmp_path / "tree"
    _mk_sketch(root / "p/.pio/libdeps/esp32dev/ESP32Servo/examples", "X",
               "void loop(){}")
    (root / "p/.pio/libdeps/esp32dev/.cache").mkdir(parents=True)
    devices, _excluded, unmapped = cs.libdeps_backlog(root)
    assert not unmapped and devices == {"servo header": 1}
