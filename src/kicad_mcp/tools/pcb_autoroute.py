"""PCB autorouting via FreeRouter (Specctra DSN/SES pipeline).

Provides both synchronous (autoroute_pcb) and async (autoroute_pcb_async +
poll_autoroute + cancel_autoroute) tools.  The async path avoids MCP timeouts
by running FreeRouter in a background thread and letting the caller poll for
completion.
"""

import glob
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from kicad_mcp.utils.pcbnew_bridge import run_pcbnew_script
from kicad_mcp.utils.keepout_helpers import (
    KEEPOUT_HELPER,
    COURTYARD_BBOX_HELPER,
    COURTYARD_BBOX_TUPLE_HELPER,
    NUDGE_PLACEMENT_HELPER,
)

logger = logging.getLogger(__name__)

# KiCad internal class names for track/via discrimination.
# Single source of truth — referenced in both _export_dsn and _import_ses scripts.
_PCB_TRACK_CLASS = "PCB_TRACK"
_PCB_VIA_CLASS = "PCB_VIA"

_EDGE_CUTS_LAYER = "Edge.Cuts"


def _jar_version_key(path: str) -> tuple:
    """Return a (major, minor, patch) tuple for sorting freerouting JAR paths."""
    name = os.path.basename(path)
    m = re.search(r"freerouting[^0-9]*(\d+)\.(\d+)(?:\.(\d+))?", name)
    if m:
        return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
    return (0, 0, 0)

# Known locations for the FreeRouter JAR (exact-name fallbacks before the glob search below)
_FREEROUTER_SEARCH_PATHS = [
    os.path.expanduser("~/freerouting.jar"),
    os.path.expanduser("~/Downloads/freerouting.jar"),
]

# Async job tracking: job_id -> {status, started, result, error, ...}
_autoroute_jobs: Dict[str, Dict[str, Any]] = {}
_autoroute_lock = threading.Lock()

# Maximum number of concurrent async autoroute jobs
MAX_CONCURRENT_JOBS = 3
# Completed jobs older than this (seconds) are removed during cleanup
_JOB_TTL_SECONDS = 30 * 60  # 30 minutes


def _cleanup_stale_jobs() -> None:
    """Remove completed/errored jobs older than _JOB_TTL_SECONDS.

    Must be called while holding _autoroute_lock.
    """
    now = time.time()
    stale = [
        jid
        for jid, j in _autoroute_jobs.items()
        if j["status"] in ("done", "error", "cancelled")
        and (now - j["started"]) > _JOB_TTL_SECONDS
    ]
    for jid in stale:
        del _autoroute_jobs[jid]


def _find_freerouter_jar(explicit_path: Optional[str] = None) -> Optional[str]:
    """Find the FreeRouter JAR file."""
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path

    # Check environment variable
    env_path = os.environ.get("FREEROUTER_JAR")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Search known locations
    for path in _FREEROUTER_SEARCH_PATHS:
        if os.path.isfile(path):
            return path

    # Glob search: look for freerouting*.jar in common directories
    for search_dir in [
        os.path.expanduser("~"),
        os.path.expanduser("~/Downloads"),
        # Sibling directories of this repo (common dev layout)
        os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."),
    ]:
        search_dir = os.path.realpath(search_dir)
        for pattern in [
            os.path.join(search_dir, "freerouting*.jar"),
            os.path.join(search_dir, "*", "freerouting*.jar"),
        ]:
            matches = sorted(glob.glob(pattern), key=_jar_version_key, reverse=True)
            if matches:
                return matches[0]

    # Search in PATH for 'freerouting' command
    which = shutil.which("freerouting")
    if which:
        return which

    return None


def _find_java() -> Optional[str]:
    """Find a Java 17+ runtime."""
    java = shutil.which("java")
    if java:
        return java
    # macOS: check common install locations
    if platform.system() == "Darwin":
        candidates = [
            "/usr/bin/java",
            "/Library/Java/JavaVirtualMachines/amazon-corretto-21.jdk"
            "/Contents/Home/bin/java",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


def _select_best_pass(
    pass_meas: list[tuple[str, Optional[int]]],
) -> tuple[Optional[str], Optional[int]]:
    """Pick the SES with the fewest KiCad-MEASURED unconnected nets (pure).

    ``pass_meas`` is ``(ses_path, measured_unconnected)`` per ok pass, where the
    count comes from KiCad's ratsnest after importing that pass (the source of
    truth — we measure the artifact we'd ship, not FreeRouter's prose log).

    Among passes we could measure, fewest unconnected wins. If NONE could be
    measured (the measurement subprocess failed) but passes produced output, we
    still import the first SES so a transient measurement failure doesn't throw
    away a routed board. Returns ``(ses_path | None, best_measured | None)``.
    """
    measured = [(s, m) for s, m in pass_meas if m is not None]
    if measured:
        return min(measured, key=lambda sm: sm[1])
    if pass_meas:
        return pass_meas[0][0], None
    return None, None


def _export_dsn(
    pcb_path: str, dsn_path: str, remove_zones: bool
) -> Dict[str, Any]:
    """Export a PCB to Specctra DSN format (step 1 of the pipeline)."""
    export_script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])

# Remove copper pour zones if requested (FreeRouter doesn't understand them)
zones_removed = 0
if params["remove_zones"]:
    zones_to_remove = []
    for z in board.Zones():
        if not z.GetIsRuleArea():
            zones_to_remove.append(z)
    for z in zones_to_remove:
        board.Remove(z)
        zones_removed += 1

# Export Specctra DSN
pcbnew.ExportSpecctraDSN(board, params["dsn_path"])

# Save board (with zones removed if applicable)
board.Save(params["pcb_path"])

# Count tracks/vias before routing
tracks = sum(1 for t in board.GetTracks() if t.GetClass() == params["track_class"])
vias = sum(1 for t in board.GetTracks() if t.GetClass() == params["via_class"])

print(json.dumps({
    "status": "ok",
    "dsn_exported": True,
    "dsn_path": params["dsn_path"],
    "zones_removed": zones_removed,
    "existing_tracks": tracks,
    "existing_vias": vias,
}))
"""
    return run_pcbnew_script(export_script, params={
        "pcb_path": pcb_path,
        "dsn_path": dsn_path,
        "remove_zones": remove_zones,
        "track_class": _PCB_TRACK_CLASS,
        "via_class": _PCB_VIA_CLASS,
    }, timeout=30.0)


# Ground-truth unconnected check — independent of FreeRouter's output. FreeRouter
# can report "0 unrouted" while leaving pads unreachable (e.g. a pad on the board
# edge), and its prose log format drifts between versions. Rebuild the ratsnest
# from actual copper and ask KiCad's own connectivity engine for an integer.
# Single source of truth shared by _import_ses (final) and _measure_ses_unconnected
# (per-pass selection). Requires `board` in scope; leaves `unconnected` set.
# RecalcNet() was removed in KiCad 10; BuildConnectivity() is the replacement.
# KiCad 10 also added a required aVisibleOnly argument; KiCad 9 takes none.
_RATSNEST_COUNT_SNIPPET = """
connectivity = board.GetConnectivity()
if hasattr(connectivity, 'RecalcNet'):
    connectivity.RecalcNet()
else:
    board.BuildConnectivity()
    connectivity = board.GetConnectivity()
try:
    unconnected = connectivity.GetUnconnectedCount(False)
except TypeError:
    unconnected = connectivity.GetUnconnectedCount()
"""


def _import_ses(pcb_path: str, ses_path: str) -> Dict[str, Any]:
    """Import a Specctra SES file back into the PCB (step 3 of the pipeline)."""
    import_script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])

# Import Specctra SES
pcbnew.ImportSpecctraSES(board, params["ses_path"])
board.Save(params["pcb_path"])

# Count results
tracks = sum(1 for t in board.GetTracks() if t.GetClass() == params["track_class"])
vias = sum(1 for t in board.GetTracks() if t.GetClass() == params["via_class"])

# Count nets
netinfo = board.GetNetInfo()
net_count = netinfo.GetNetCount()
""" + _RATSNEST_COUNT_SNIPPET + """
print(json.dumps({
    "status": "ok",
    "ses_imported": True,
    "tracks": tracks,
    "vias": vias,
    "net_count": net_count,
    "unconnected_after_routing": unconnected,
}))
"""
    return run_pcbnew_script(import_script, params={
        "pcb_path": pcb_path,
        "ses_path": ses_path,
        "track_class": _PCB_TRACK_CLASS,
        "via_class": _PCB_VIA_CLASS,
    }, timeout=30.0)


def _measure_ses_unconnected(pcb_path: str, ses_path: str) -> Optional[int]:
    """Measure how many nets a pass's SES leaves unconnected — WITHOUT saving.

    Imports ``ses_path`` into the pristine pre-route board in memory and asks
    KiCad's ratsnest for the unconnected count. Because it never calls
    ``board.Save``, ``pcb_path`` on disk is untouched, so each pass is measured
    against the same starting board and the real final import still happens once,
    later, via :func:`_import_ses`. Returns the count, or ``None`` if the
    measuring subprocess failed (caller treats that as "unranked").
    """
    measure_script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
board = pcbnew.LoadBoard(params["pcb_path"])
pcbnew.ImportSpecctraSES(board, params["ses_path"])
# Deliberately NO board.Save — measure only.
""" + _RATSNEST_COUNT_SNIPPET + """
print(json.dumps({"status": "ok", "unconnected": unconnected}))
"""
    result = run_pcbnew_script(measure_script, params={
        "pcb_path": pcb_path,
        "ses_path": ses_path,
    }, timeout=30.0)
    val = result.get("unconnected")
    if result.get("status") == "ok" and isinstance(val, int):
        return val
    return None


def _freerouter_cmd(
    java_path: str, jar_path: str, dsn_path: str, ses_path: str,
) -> list[str]:
    """Build the headless FreeRouter (Freerouting v2) invocation.

    ``-Djava.awt.headless=true`` keeps the JVM from initializing AWT on macOS
    (which otherwise creates a dock icon and steals window focus every run);
    ``--gui.enabled=false`` suppresses FreeRouter's own UI.

    ``--usage_and_diagnostic_data.disable_analytics=true`` is mandatory for an
    air-gapped/offline design: Freerouting v2 otherwise POSTs usage analytics to
    ``api.freerouting.app`` on every run. Verified on v2.2.3 — the flag silences
    the analytics call entirely (it also quiets the version-check phone-home).
    """
    return [
        java_path, "-Djava.awt.headless=true", "-jar", jar_path,
        "-de", dsn_path,
        "-do", ses_path,
        "--gui.enabled=false",
        "--usage_and_diagnostic_data.disable_analytics=true",
    ]


def _run_freerouter_pass(
    java_path: str,
    jar_path: str,
    dsn_path: str,
    ses_path: str,
    work_dir: str,
    timeout: float,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run a single FreeRouter pass.  Returns pass result dict.

    If *job_id* is given, the subprocess PID is stored in
    ``_autoroute_jobs[job_id]["pid"]`` so cancel_autoroute can kill it.
    """
    cmd = _freerouter_cmd(java_path, jar_path, dsn_path, ses_path)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=work_dir,
    )

    # Store PID for cancellation
    if job_id:
        with _autoroute_lock:
            job = _autoroute_jobs.get(job_id)
            if job:
                job["pid"] = proc.pid

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return {"status": "timeout"}

    # Check if job was cancelled while running
    if job_id:
        with _autoroute_lock:
            job = _autoroute_jobs.get(job_id)
            if job and job.get("status") == "cancelled":
                return {"status": "cancelled"}

    if proc.returncode != 0:
        # Keep up to 8 kB; FreeRouter error messages can be verbose
        return {"status": "error", "error": stderr[:8192]}

    if not os.path.exists(ses_path):
        return {"status": "no_output"}

    # Routing succeeded and produced a SES. We do NOT parse FreeRouter's stdout
    # for the unconnected count — that prose format drifts between versions and
    # is a second encoding of a number KiCad can measure directly. The caller
    # ranks this pass by importing the SES and measuring the real ratsnest.
    return {"status": "ok"}


def _run_full_autoroute(
    pcb_path: str,
    jar_path: str,
    java_path: str,
    passes: int,
    remove_zones: bool,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Run the complete autoroute pipeline (DSN export → FreeRouter → SES import).

    Used by both the synchronous and async tools.
    """
    pcb_basename = os.path.splitext(os.path.basename(pcb_path))[0]
    work_dir: Optional[str] = None

    # 30 minutes per pass — FreeRouter on complex boards can take 10-20+ min
    per_pass_timeout = 1800.0

    try:
        # mkdtemp goes inside try so the finally block guarantees cleanup
        # even if the job-dict write below raises (otherwise the temp dir
        # would leak).
        work_dir = tempfile.mkdtemp(prefix="kicad_autoroute_")
        dsn_path = os.path.join(work_dir, f"{pcb_basename}.dsn")
        ses_path = os.path.join(work_dir, f"{pcb_basename}.ses")

        # Store work_dir in the job dict so cleanup code can find it
        if job_id:
            with _autoroute_lock:
                job = _autoroute_jobs.get(job_id)
                if job:
                    job["work_dir"] = work_dir

        # Step 1: Export DSN
        logger.info("Exporting DSN from %s", pcb_path)
        export_result = _export_dsn(pcb_path, dsn_path, remove_zones)

        if "error" in export_result:
            return {"error": f"DSN export failed: {export_result['error']}"}
        if not os.path.exists(dsn_path):
            return {"error": f"DSN file was not created at {dsn_path}"}

        # Update job phase
        if job_id:
            with _autoroute_lock:
                job = _autoroute_jobs.get(job_id)
                if job:
                    job["phase"] = "routing"

        # Step 2: Run FreeRouter passes, ranking each by KiCad's MEASURED
        # ratsnest (not FreeRouter's prose log). Each ok pass's SES is imported
        # into a throwaway copy of the pristine board and the unconnected count
        # read from KiCad's own connectivity engine; we keep the lowest.
        pass_meas: list[tuple[str, Optional[int]]] = []
        pass_results: list[dict] = []

        for pass_num in range(1, passes + 1):
            # Check cancellation before each pass
            if job_id:
                with _autoroute_lock:
                    job = _autoroute_jobs.get(job_id)
                    if job and job.get("status") == "cancelled":
                        return {
                            "error": "Cancelled by user",
                            "passes": pass_results,
                        }
                    if job:
                        job["current_pass"] = pass_num

            pass_ses = (
                ses_path if passes == 1
                else os.path.join(work_dir, f"{pcb_basename}_pass{pass_num}.ses")
            )

            logger.info("FreeRouter pass %d/%d", pass_num, passes)
            result = _run_freerouter_pass(
                java_path, jar_path, dsn_path, pass_ses,
                work_dir, per_pass_timeout, job_id,
            )

            if result["status"] == "cancelled":
                return {"error": "Cancelled by user", "passes": pass_results}

            pass_result = {"pass": pass_num, "status": result["status"]}
            if "error" in result:
                pass_result["error"] = result["error"]
            if result["status"] == "ok":
                # Measure this pass against the pristine board (no save).
                measured = _measure_ses_unconnected(pcb_path, pass_ses)
                pass_result["unconnected"] = measured
                pass_meas.append((pass_ses, measured))
            pass_results.append(pass_result)

        best_ses, best_unconnected = _select_best_pass(pass_meas)
        if best_ses is None:
            return {
                "error": "All FreeRouter passes failed",
                "passes": pass_results,
            }

        # Update job phase
        if job_id:
            with _autoroute_lock:
                job = _autoroute_jobs.get(job_id)
                if job:
                    job["phase"] = "importing"

        # Step 3: Import SES
        logger.info("Importing SES into %s", pcb_path)
        import_result = _import_ses(pcb_path, best_ses)

        if "error" in import_result:
            return {"error": f"SES import failed: {import_result['error']}"}

        if "unconnected_after_routing" not in import_result:
            return {"error": "SES import did not report unconnected count — routing result unknown"}
        unconnected = import_result["unconnected_after_routing"]
        if unconnected > 0:
            note = (
                f"WARNING: {unconnected} net(s) still unconnected after routing. "
                "Check for pads outside the board outline or other placement errors. "
                "Run run_drc_check and audit_pcb_placement to investigate."
            )
        elif remove_zones and export_result.get("zones_removed", 0) > 0:
            note = (
                "Copper zones were removed before routing. "
                "Re-add them with add_copper_zone + fill_zones."
            )
        else:
            note = "Routing complete."

        return {
            "status": "ok",
            "pcb_path": pcb_path,
            "freerouter_jar": jar_path,
            "zones_removed": export_result.get("zones_removed", 0),
            "tracks_before": export_result.get("existing_tracks", 0),
            "vias_before": export_result.get("existing_vias", 0),
            "tracks_after": import_result.get("tracks", 0),
            "vias_after": import_result.get("vias", 0),
            "net_count": import_result.get("net_count", 0),
            "unconnected_after_routing": unconnected,
            "passes_run": len(pass_results),
            # Fewest measured unconnected nets across the passes we kept — the
            # selection metric, equal to unconnected_after_routing for the
            # winning pass. None only if no pass could be measured.
            "best_unconnected": best_unconnected,
            "pass_results": pass_results,
            "note": note,
        }

    finally:
        # Clean up temp files (but not if async job still referencing them)
        if not job_id and work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)


def _autoroute_worker(job_id: str, **kwargs: Any) -> None:
    """Background thread worker for async autorouting."""
    try:
        result = _run_full_autoroute(job_id=job_id, **kwargs)
        with _autoroute_lock:
            job = _autoroute_jobs.get(job_id)
            if job and job.get("status") != "cancelled":
                job["status"] = "done" if "error" not in result else "error"
                job["result"] = result
                job["elapsed"] = round(time.time() - job["started"], 1)
    except Exception as exc:
        with _autoroute_lock:
            job = _autoroute_jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["result"] = {"error": str(exc)}
                job["elapsed"] = round(time.time() - job["started"], 1)
    finally:
        # Clean up work_dir
        with _autoroute_lock:
            job = _autoroute_jobs.get(job_id)
            if job and "work_dir" in job:
                try:
                    shutil.rmtree(job["work_dir"], ignore_errors=True)
                except Exception:
                    pass


def _run_pre_route_check(pcb_path: str) -> Dict[str, Any]:
    """Run a lightweight pre-route placement check.

    Returns dict with 'route_ready' boolean and lists of issues found.
    Called internally by autoroute_pcb before launching FreeRouter.
    """
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())
""" + KEEPOUT_HELPER + """
board = pcbnew.LoadBoard(params["pcb_path"])

ds = board.GetDesignSettings()
min_cl = pcbnew.ToMM(ds.m_MinClearance)
if min_cl <= 0:
    min_cl = 0.2

errors = []

# --- Courtyard overlap check ---
""" + COURTYARD_BBOX_HELPER + """

footprints = []
for fp in board.GetFootprints():
    tight_box = get_courtyard_bbox(fp)
    if not tight_box:
        fp_bbox = fp.GetBoundingBox(False, False)
        tight_box = {
            "x_min_mm": round(pcbnew.ToMM(fp_bbox.GetX()), 3),
            "y_min_mm": round(pcbnew.ToMM(fp_bbox.GetY()), 3),
            "x_max_mm": round(pcbnew.ToMM(fp_bbox.GetRight()), 3),
            "y_max_mm": round(pcbnew.ToMM(fp_bbox.GetBottom()), 3),
        }
    footprints.append({"reference": fp.GetReference(), "bbox": tight_box})

courtyard_overlaps = []
for i in range(len(footprints)):
    a = footprints[i]; a_box = a["bbox"]
    for j in range(i + 1, len(footprints)):
        b = footprints[j]; b_box = b["bbox"]
        if rects_overlap(a_box, b_box):
            courtyard_overlaps.append({
                "ref_a": a["reference"], "ref_b": b["reference"],
            })
            errors.append(f"Courtyard overlap: {a['reference']} and {b['reference']}")

# --- Pad clearance check ---
all_pads = []
for fp in board.GetFootprints():
    ref = fp.GetReference()
    for pad in fp.Pads():
        pos = pad.GetPosition(); size = pad.GetSize()
        x = pcbnew.ToMM(pos.x); y = pcbnew.ToMM(pos.y)
        w = pcbnew.ToMM(size.x); h = pcbnew.ToMM(size.y)
        all_pads.append({
            "ref": ref, "pad": pad.GetNumber(),
            "x0": x - w/2, "y0": y - h/2,
            "x1": x + w/2, "y1": y + h/2,
        })

pad_violations = []
n = len(all_pads)
for i in range(n):
    a = all_pads[i]
    ax0 = a["x0"] - min_cl; ay0 = a["y0"] - min_cl
    ax1 = a["x1"] + min_cl; ay1 = a["y1"] + min_cl
    for j in range(i + 1, n):
        b = all_pads[j]
        if a["ref"] == b["ref"]:
            continue
        if ax0 >= b["x1"] or ax1 <= b["x0"] or ay0 >= b["y1"] or ay1 <= b["y0"]:
            continue
        pad_violations.append(f"{a['ref']}:{a['pad']} vs {b['ref']}:{b['pad']}")
        errors.append(f"Pad clearance: {a['ref']}:{a['pad']} and {b['ref']}:{b['pad']}")
        if len(pad_violations) >= 10:
            break
    if len(pad_violations) >= 10:
        break

route_ready = len(errors) == 0
print(json.dumps({
    "status": "ok",
    "route_ready": route_ready,
    "courtyard_overlaps": len(courtyard_overlaps),
    "pad_violations": len(pad_violations),
    "error_count": len(errors),
    "errors": errors[:20],
    "errors_truncated": len(errors) > 20,
}))
"""
    return run_pcbnew_script(script, params={"pcb_path": pcb_path})


def _run_auto_fix_placement(pcb_path: str, spacing_mm: float = 0.5) -> Dict[str, Any]:
    """Nudge overlapping footprints apart.  Lightweight wrapper around pcbnew."""
    script = """
import pcbnew, json, sys

params = json.loads(open(sys.argv[1]).read())

board = pcbnew.LoadBoard(params["pcb_path"])
spacing = params["spacing_mm"]

""" + COURTYARD_BBOX_TUPLE_HELPER + NUDGE_PLACEMENT_HELPER + """

move_count, moved = nudge_overlapping_footprints(board, spacing, 3)
if moved:
    board.Save(params["pcb_path"])

print(json.dumps({
    "status": "ok",
    "components_moved": len(set(moved)),
    "moved": sorted(set(moved)),
}))
"""
    return run_pcbnew_script(script, params={
        "pcb_path": pcb_path,
        "spacing_mm": spacing_mm,
    })


def _op_run(
    pcb_path: str,
    freerouter_jar: str = "",
    passes: int = 1,
    remove_zones: bool = True,
    net_classes: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Synchronous autoroute — runs the full pipeline and waits for completion."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    if not (1 <= passes <= 10):
        return {"error": f"passes must be between 1 and 10, got {passes}"}

    jar_path = _find_freerouter_jar(freerouter_jar or None)
    if not jar_path:
        return {
            "error": (
                "FreeRouter JAR not found. Provide freerouter_jar path, "
                "set FREEROUTER_JAR env var, or place freerouting-2.1.0.jar "
                "in a known location."
            )
        }

    java_path = _find_java()
    if not java_path:
        return {"error": "Java runtime not found. Install Java 17+ (e.g. Amazon Corretto)."}

    # Apply net classes before routing (so DSN export includes them)
    net_class_results = []
    if net_classes:
        from kicad_mcp.tools.pcb_nets import _default_net_class
        stem = os.path.splitext(pcb_path)[0]
        pro_path = stem + ".kicad_pro"
        if not os.path.exists(pro_path):
            return {
                "error": (
                    f"net_classes requires a .kicad_pro file at {pro_path}. "
                    "Create one or use set_net_class separately."
                )
            }

        import json as _json

        with open(pro_path, "r") as f:
            project = _json.load(f)

        if "net_settings" not in project:
            project["net_settings"] = {
                "classes": [_default_net_class()],
                "meta": {"version": 4},
                "net_colors": None,
                "netclass_assignments": None,
                "netclass_patterns": [],
            }

        ns = project["net_settings"]
        classes = ns.get("classes", [])
        _raw_assignments = ns.get("netclass_assignments")
        assignments = _raw_assignments if _raw_assignments is not None else {}

        for cls_name, cls_def in net_classes.items():
            nets = cls_def.get("nets", [])
            tw = cls_def.get("track_width_mm", 0.25)
            cl = cls_def.get("clearance_mm", 0.2)
            vd = cls_def.get("via_diameter_mm", 0.6)
            vr = cls_def.get("via_drill_mm", 0.3)

            # Find or create class
            existing = None
            for c in classes:
                if c.get("name") == cls_name:
                    existing = c
                    break
            if existing:
                existing["track_width"] = tw
                existing["clearance"] = cl
                existing["via_diameter"] = vd
                existing["via_drill"] = vr
            else:
                nc = _default_net_class()
                nc["name"] = cls_name
                nc["track_width"] = tw
                nc["clearance"] = cl
                nc["via_diameter"] = vd
                nc["via_drill"] = vr
                classes.append(nc)

            for net_name in nets:
                assignments[net_name] = cls_name

            net_class_results.append({
                "class": cls_name,
                "track_width_mm": tw,
                "nets_assigned": len(nets),
            })

        ns["classes"] = classes
        ns["netclass_assignments"] = assignments

        with open(pro_path, "w") as f:
            _json.dump(project, f, indent=2)
            f.write("\n")

    # Pre-flight placement check — catch issues before spending time on FreeRouter
    preflight = _run_pre_route_check(pcb_path)
    preflight_info = {}

    if preflight.get("status") == "ok" and not preflight.get("route_ready", True):
        overlaps = preflight.get("courtyard_overlaps", 0)

        if overlaps > 0:
            # Auto-fix courtyard overlaps by nudging footprints apart
            logger.info("Pre-route: %d courtyard overlap(s), auto-fixing", overlaps)
            fix_result = _run_auto_fix_placement(pcb_path)
            preflight_info["auto_fix_applied"] = True
            preflight_info["components_moved"] = fix_result.get("components_moved", 0)

            # Re-check after fix
            recheck = _run_pre_route_check(pcb_path)
            if recheck.get("status") == "ok" and not recheck.get("route_ready", True):
                # Still have issues (likely pad clearance, not just courtyards)
                _errors_raw = recheck.get("errors", [])
                preflight_info["errors_after_fix"] = _errors_raw[:10]
                preflight_info["errors_after_fix_truncated"] = len(_errors_raw) > 10
                preflight_info["route_ready_after_fix"] = False
                # Continue anyway — FreeRouter may still produce a usable result
                logger.warning("Pre-route: still %d error(s) after fix, routing anyway",
                               recheck.get("error_count", 0))
            else:
                preflight_info["route_ready_after_fix"] = True
        else:
            # Pad clearance issues only — can't auto-fix, but warn and continue
            preflight_info["pad_violations"] = preflight.get("pad_violations", 0)
            _errors_raw = preflight.get("errors", [])
            preflight_info["errors"] = _errors_raw[:10]
            preflight_info["errors_truncated"] = len(_errors_raw) > 10
            logger.warning("Pre-route: %d error(s) (no courtyard overlaps to fix)",
                           preflight.get("error_count", 0))

    result = _run_full_autoroute(
        pcb_path=pcb_path,
        jar_path=jar_path,
        java_path=java_path,
        passes=passes,
        remove_zones=remove_zones,
    )

    if net_class_results:
        result["net_classes_applied"] = net_class_results
    if preflight_info:
        result["preflight"] = preflight_info

    return result


def _op_start(
    pcb_path: str,
    freerouter_jar: str = "",
    passes: int = 1,
    remove_zones: bool = True,
) -> Dict[str, Any]:
    """Start autorouting in the background. Returns a job_id immediately."""
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    if not (1 <= passes <= 10):
        return {"error": f"passes must be between 1 and 10, got {passes}"}

    jar_path = _find_freerouter_jar(freerouter_jar or None)
    if not jar_path:
        return {
            "error": (
                "FreeRouter JAR not found. Provide freerouter_jar path, "
                "set FREEROUTER_JAR env var, or place freerouting-2.1.0.jar "
                "in a known location."
            )
        }

    java_path = _find_java()
    if not java_path:
        return {"error": "Java runtime not found. Install Java 17+ (e.g. Amazon Corretto)."}

    # Enforce concurrent job limit and clean up stale jobs
    with _autoroute_lock:
        _cleanup_stale_jobs()
        active = sum(
            1 for j in _autoroute_jobs.values() if j["status"] == "running"
        )
        if active >= MAX_CONCURRENT_JOBS:
            return {
                "error": (
                    f"Too many concurrent autoroute jobs ({active}). "
                    f"Maximum is {MAX_CONCURRENT_JOBS}. "
                    "Wait for a job to finish or cancel one."
                )
            }

    job_id = uuid.uuid4().hex[:8]

    with _autoroute_lock:
        _autoroute_jobs[job_id] = {
            "status": "running",
            "started": time.time(),
            "pcb_path": pcb_path,
            "passes": passes,
            "phase": "exporting",
            "current_pass": 0,
            "pid": None,
        }

    thread = threading.Thread(
        target=_autoroute_worker,
        kwargs={
            "job_id": job_id,
            "pcb_path": pcb_path,
            "jar_path": jar_path,
            "java_path": java_path,
            "passes": passes,
            "remove_zones": remove_zones,
        },
        daemon=True,
    )
    thread.start()

    return {
        "job_id": job_id,
        "status": "submitted",
        "pcb_path": pcb_path,
        "passes": passes,
        "note": "Use autoroute(operation='poll', job_id=...) to check progress.",
    }


def _op_poll(job_id: str) -> Dict[str, Any]:
    """Check the status of an async autoroute job."""
    with _autoroute_lock:
        if job_id not in _autoroute_jobs:
            return {
                "error": (
                    f"Unknown job_id: {job_id!r}. "
                    "Already retrieved or never submitted."
                )
            }

        job = _autoroute_jobs[job_id]
        elapsed = round(time.time() - job["started"], 1)

        if job["status"] == "running":
            return {
                "status": "running",
                "elapsed_s": elapsed,
                "phase": job.get("phase", "unknown"),
                "current_pass": job.get("current_pass", 0),
                "total_passes": job["passes"],
            }

        # Done, error, or cancelled — retrieve and clean up
        result = dict(job)
        del _autoroute_jobs[job_id]

    return {
        "status": result["status"],
        "elapsed_s": result.get("elapsed", elapsed),
        "result": result.get("result", {}),
    }


def _op_cancel(job_id: str) -> Dict[str, Any]:
    """Cancel a running async autoroute job."""
    with _autoroute_lock:
        if job_id not in _autoroute_jobs:
            return {
                "error": (
                    f"Unknown job_id: {job_id!r}. "
                    "Already retrieved or never submitted."
                )
            }

        job = _autoroute_jobs[job_id]
        if job["status"] != "running":
            return {
                "error": (
                    f"Job {job_id} is not running "
                    f"(status: {job['status']})"
                )
            }

        elapsed = round(time.time() - job["started"], 1)
        job["status"] = "cancelled"
        job["elapsed"] = elapsed
        job["result"] = {"error": f"Cancelled by user after {elapsed}s"}

        # Kill the FreeRouter subprocess
        pid = job.get("pid")

    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info("Sent SIGTERM to FreeRouter PID %d", pid)
        except ProcessLookupError:
            pass  # Already exited
        except Exception as exc:
            logger.warning("Failed to kill PID %d: %s", pid, exc)

    return {
        "status": "cancelled",
        "job_id": job_id,
        "elapsed_s": elapsed,
    }


def _op_list_jobs() -> Dict[str, Any]:
    """List all tracked autoroute jobs and their current status."""
    now = time.time()
    with _autoroute_lock:
        jobs = {
            jid: {
                "status": j["status"],
                "elapsed_s": round(now - j["started"], 1),
                "pcb_path": j.get("pcb_path", ""),
                "phase": j.get("phase", ""),
                "current_pass": j.get("current_pass", 0),
                "total_passes": j.get("passes", 1),
            }
            for jid, j in _autoroute_jobs.items()
        }
    return {"jobs": jobs, "count": len(jobs)}


def register_pcb_autoroute_tools(mcp: FastMCP) -> None:
    """Register the autoroute domain router."""

    @mcp.tool()
    def autoroute(
        operation: str,
        *,
        pcb_path: Optional[str] = None,
        freerouter_jar: str = "",
        passes: int = 1,
        remove_zones: bool = True,
        net_classes: Optional[Dict[str, Any]] = None,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PCB autorouting via FreeRouter (Specctra DSN/SES pipeline).

        Operations:
          run(pcb_path, freerouter_jar="", passes=1, remove_zones=True,
              net_classes=None)
              -> {status, pcb_path, tracks_after, vias_after, unconnected_after_routing,
                  passes_run, best_unconnected, note, net_classes_applied?, preflight?}
              Synchronous autoroute — waits for completion. Use for short
              routes. FreeRouter is non-deterministic; set passes > 1 to
              run multiple times and keep the best (fewest unconnected) result.

          start(pcb_path, freerouter_jar="", passes=1, remove_zones=True)
              -> {job_id, status, pcb_path, passes, note}
              Start autorouting in the background. Returns immediately.
              Use poll(job_id) to check progress.

          poll(job_id)
              -> {status: "running"|"done"|"error"|"cancelled",
                  elapsed_s, phase?, result?}
              Check the status of an async autoroute job. Completed/failed
              jobs are removed from tracking after retrieval.

          cancel(job_id)
              -> {status: "cancelled", job_id, elapsed_s}
              Cancel a running async autoroute job. Kills the FreeRouter
              subprocess if it is still running.

          list_jobs()
              -> {jobs: {job_id: {...}}, count}
              List all tracked autoroute jobs and their current status.
        """
        if operation == "run":
            if pcb_path is None:
                return {"error": "operation='run' requires 'pcb_path'"}
            return _op_run(
                pcb_path=pcb_path,
                freerouter_jar=freerouter_jar,
                passes=passes,
                remove_zones=remove_zones,
                net_classes=net_classes,
            )
        if operation == "start":
            if pcb_path is None:
                return {"error": "operation='start' requires 'pcb_path'"}
            return _op_start(
                pcb_path=pcb_path,
                freerouter_jar=freerouter_jar,
                passes=passes,
                remove_zones=remove_zones,
            )
        if operation == "poll":
            if job_id is None:
                return {"error": "operation='poll' requires 'job_id'"}
            return _op_poll(job_id)
        if operation == "cancel":
            if job_id is None:
                return {"error": "operation='cancel' requires 'job_id'"}
            return _op_cancel(job_id)
        if operation == "list_jobs":
            return _op_list_jobs()
        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: run|start|poll|cancel|list_jobs"
            )
        }
