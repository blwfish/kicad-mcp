"""Export router — manufacturing and output files for KiCad projects.

See docs/SPEC_Tool_Consolidation.md.
"""
import asyncio
import logging
import os
import subprocess
import time
import zipfile
from typing import Any, Dict, Optional

from fastmcp import FastMCP, Context

from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import KiCadCLIError, format_cli_error, get_kicad_cli_path
from kicad_mcp.utils.path_validation import validate_project_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# gerbers
# ---------------------------------------------------------------------------

def _op_gerbers(
    pcb_path: str,
    output_dir: str = "",
    create_zip: bool = True,
) -> Dict[str, Any]:
    _pv_err = validate_project_path(pcb_path)
    if _pv_err:
        return {"error": _pv_err}

    try:
        kicad_cli = get_kicad_cli_path(required=True)
    except Exception as e:
        return {"error": str(e)}
    assert kicad_cli is not None  # required=True raises above if CLI not found

    if not output_dir:
        pcb_dir = os.path.dirname(os.path.abspath(pcb_path))
        output_dir = os.path.join(pcb_dir, "gerbers")

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as e:
        return {"error": f"Failed to create output directory {output_dir}: {e}"}

    # output_dir is reused across runs (exist_ok=True); a stale file left
    # over from a PREVIOUS export -- e.g. a different board's gerber that
    # doesn't share a filename with anything this run writes -- used to be
    # silently swept into "all_files" below (whose comment assumed the
    # directory "is freshly created per export and contains only kicad-cli
    # output") and shipped into the fab-package ZIP. Record the start time
    # so only files this run actually wrote/touched are treated as output.
    export_start_time = time.time()

    errors = []

    gerber_cmd = [
        kicad_cli, "pcb", "export", "gerbers",
        "--output", output_dir + "/",
        pcb_path,
    ]

    try:
        _gerber_run = subprocess.run(
            gerber_cmd, capture_output=True, text=True,
            check=True, timeout=30,
        )
        if _gerber_run.stdout.strip():
            logger.info("Gerber export: %s", _gerber_run.stdout.strip())
    except subprocess.CalledProcessError as e:
        errors.append(f"Gerber export failed: {format_cli_error(e)}")
    except subprocess.TimeoutExpired:
        errors.append("Gerber export timed out after 30s")

    drill_cmd = [
        kicad_cli, "pcb", "export", "drill",
        "--output", output_dir + "/",
        "--format", "excellon",
        "--excellon-units", "mm",
        pcb_path,
    ]

    try:
        _drill_run = subprocess.run(
            drill_cmd, capture_output=True, text=True,
            check=True, timeout=30,
        )
        if _drill_run.stdout.strip():
            logger.info("Drill export: %s", _drill_run.stdout.strip())
    except subprocess.CalledProcessError as e:
        errors.append(f"Drill export failed: {format_cli_error(e)}")
    except subprocess.TimeoutExpired:
        errors.append("Drill export timed out after 30s")

    if errors:
        # Structured error list with count — caller can programmatically
        # retry per-step instead of parsing a joined string.
        return {"error": f"{len(errors)} export step(s) failed", "errors": errors, "error_count": len(errors)}

    # Glob everything in output_dir — picks up *.gbr (modern KiCad),
    # *.gtl/.gbl/.gto/etc. (RS-274X layer extensions), *.drl/.xln (drill),
    # *.gbrjob (fab job manifest), and anything else kicad-cli produces (the
    # earlier *.gbr-only glob silently shipped fab packages missing layers).
    # output_dir is reused across runs (exist_ok=True above), so filter to
    # files this run actually wrote/touched -- a small tolerance covers
    # coarse filesystem mtime resolution (e.g. FAT32's 2s granularity).
    try:
        all_paths = [os.path.join(output_dir, f) for f in os.listdir(output_dir)]
    except OSError as e:
        return {"error": f"Failed to list output directory {output_dir}: {e}"}
    all_files = sorted(
        p for p in all_paths
        if os.path.isfile(p) and os.path.getmtime(p) >= export_start_time - 1.0
    )
    stale_files = sorted(
        os.path.basename(p) for p in all_paths
        if os.path.isfile(p) and os.path.getmtime(p) < export_start_time - 1.0
    )
    if stale_files:
        logger.warning("Ignoring %d stale file(s) in %s left over from a "
                        "previous export: %s", len(stale_files), output_dir, stale_files)

    # Explicit allow-lists rather than "not drill = gerber": the prior binary
    # split put ANY unrecognized extension (a stray .log/.txt/.rpt kicad-cli
    # or a future version might emit) into the gerber bucket silently, with
    # no way for a caller to tell "27 real gerbers" from "26 gerbers + 1
    # unrelated file". Drill extensions listed first since drill files are
    # never also gerber files.
    _DRILL_EXTS = (".drl", ".xln")
    _GERBER_EXTS = (
        ".gbr", ".gtl", ".gbl", ".gto", ".gbo", ".gts", ".gbs",
        ".gko", ".gm1", ".gm2", ".gbrjob",
    )
    drill_files = [f for f in all_files if f.endswith(_DRILL_EXTS)]
    gerber_files = [f for f in all_files if f.endswith(_GERBER_EXTS)]
    other_files = [
        f for f in all_files
        if not f.endswith(_DRILL_EXTS) and not f.endswith(_GERBER_EXTS)
    ]
    if other_files:
        logger.warning(
            "Export produced %d file(s) with an unrecognized extension, not "
            "classified as gerber or drill: %s", len(other_files), other_files,
        )

    if not all_files:
        return {"error": "No output files generated — PCB may be empty"}

    # A 0-byte gerber/drill file (kicad-cli can write an empty placeholder on
    # a layer with nothing to export, or on a truncated write) previously
    # passed the existence-only check above and was silently zipped into the
    # fab package as if it were real data — report it instead of shipping it
    # silently as a successful export.
    empty_files = [os.path.basename(f) for f in all_files if os.path.getsize(f) == 0]
    if empty_files:
        return {"error": f"{len(empty_files)} exported file(s) are 0 bytes: {empty_files}",
                "empty_files": empty_files}

    result = {
        "status": "ok",
        "output_dir": output_dir,
        "gerber_files": [os.path.basename(f) for f in gerber_files],
        "drill_files": [os.path.basename(f) for f in drill_files],
        "gerber_count": len(gerber_files),
        "drill_count": len(drill_files),
        "total_files": len(all_files),
    }
    if stale_files:
        result["ignored_stale_files"] = stale_files
    if other_files:
        result["other_files"] = [os.path.basename(f) for f in other_files]

    if create_zip:
        pcb_name = os.path.splitext(os.path.basename(pcb_path))[0]
        zip_path = os.path.join(
            os.path.dirname(output_dir), f"{pcb_name}-gerbers.zip"
        )
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for f in all_files:
                    zf.write(f, os.path.basename(f))
        except (OSError, zipfile.BadZipFile) as e:
            # The individual gerber/drill files are already on disk and
            # correctly reported above -- zipping is a convenience step, not
            # the export itself, so a failure here (disk full, permission)
            # must not look like the whole export failed nor silently
            # produce a truncated/absent zip under status="ok".
            return {**result, "status": "error",
                    "error": f"Gerber files exported but ZIP creation failed: {e}"}
        result["zip_path"] = zip_path
        result["zip_size_bytes"] = os.path.getsize(zip_path)

    return result


# ---------------------------------------------------------------------------
# thumbnail
# ---------------------------------------------------------------------------

async def _op_thumbnail(
    project_path: str, ctx: Context | None
) -> Dict[str, Any]:
    try:
        logger.debug("Generating thumbnail via CLI for project: %s", project_path)

        if not os.path.exists(project_path):
            logger.warning("Project not found: %s", project_path)
            if ctx:
                await ctx.info(f"Project not found: {project_path}")
            return {"error": f"Project not found: {project_path}"}

        files = get_project_files(project_path)
        if "pcb" not in files:
            logger.warning("PCB file not found in project")
            if ctx:
                await ctx.info("PCB file not found in project")
            return {"error": "PCB file not found in project"}

        pcb_file = files["pcb"]
        logger.debug("Found PCB file: %s", pcb_file)

        if ctx:
            await ctx.report_progress(10, 100)
            await ctx.info(
                f"Generating thumbnail for {os.path.basename(pcb_file)} using kicad-cli"
            )

        try:
            result = await _generate_thumbnail_with_cli(pcb_file, ctx)
            # Explicit success check — `result and "error" not in result`
            # would falsely accept {} as success; require "status": "ok"
            # so we can't confuse "no error key" with "success".
            if isinstance(result, dict) and result.get("status") == "ok":
                logger.info("Thumbnail generated successfully via CLI.")
                return result
            else:
                logger.warning("_generate_thumbnail_with_cli returned error or empty result")
                if ctx:
                    await ctx.info(
                        "Failed to generate thumbnail using kicad-cli."
                    )
                if isinstance(result, dict) and result:
                    return result
                return {"error": "Failed to generate thumbnail using kicad-cli"}
        except Exception as e:
            logger.error("Error calling _generate_thumbnail_with_cli: %s", e)
            if ctx:
                await ctx.info(
                    f"Error generating thumbnail with kicad-cli: {e}"
                )
            return {"error": f"Error generating thumbnail with kicad-cli: {e}"}

    except asyncio.CancelledError:
        logger.debug("Thumbnail generation cancelled")
        raise
    except Exception as e:
        logger.error("Unexpected error in thumbnail generation: %s", e)
        if ctx:
            await ctx.info(f"Error: {e}")
        return {"error": f"Unexpected error in thumbnail generation: {e}"}


async def _generate_thumbnail_with_cli(
    pcb_file: str, ctx: Context | None
):
    """Generate PCB thumbnail using command line tools."""
    try:
        logger.debug("Attempting to generate thumbnail using KiCad CLI tools")
        if ctx:
            await ctx.report_progress(20, 100)

        project_dir = os.path.dirname(pcb_file)
        project_name = os.path.splitext(os.path.basename(pcb_file))[0]
        output_file = os.path.join(project_dir, f"{project_name}_thumbnail.svg")

        # Was a hand-rolled platform branch (its own Darwin/Windows hardcoded
        # app-bundle paths + shutil.which fallback, Linux shutil.which only) --
        # an independent, less capable copy of kicad_cli.py's KiCadCLIManager
        # (env var override, per-OS common-path fallbacks including Homebrew/
        # snap, actual `--version` validation, caching) that _op_gerbers in
        # this same file already delegates to. finding #22 of the 2026-09-23
        # full review.
        try:
            kicad_cli = get_kicad_cli_path(required=True)
        except KiCadCLIError as e:
            logger.warning("%s", e)
            return {"error": str(e)}
        assert kicad_cli is not None  # required=True raises above if CLI not found

        if ctx:
            await ctx.report_progress(30, 100)
            await ctx.info(
                "Using KiCad command line tools for thumbnail generation"
            )

        cmd = [
            kicad_cli,
            "pcb",
            "export",
            "svg",
            "--output",
            output_file,
            "--layers",
            "F.Cu,B.Cu,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts",
            pcb_file,
        ]

        logger.debug("Running command: %s", " ".join(cmd))
        if ctx:
            await ctx.report_progress(50, 100)

        try:
            process = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=30
            )
            logger.debug("Command successful: %s", process.stdout)

            if ctx:
                await ctx.report_progress(70, 100)

            if not os.path.exists(output_file):
                logger.warning("Output file not created: %s", output_file)
                return {"error": f"Output file not created: {output_file}"}

            file_size = os.path.getsize(output_file)
            if file_size == 0:
                logger.warning("Thumbnail output file is 0 bytes: %s", output_file)
                return {"error": f"Thumbnail generation produced a 0-byte file: {output_file}"}

            logger.info("Successfully generated thumbnail with CLI, size: %d bytes", file_size)
            if ctx:
                await ctx.report_progress(90, 100)
                await ctx.info(f"Thumbnail generated ({file_size} bytes)")
            return {
                "status": "ok",
                "thumbnail_path": output_file,
                "size_bytes": file_size,
            }

        except subprocess.CalledProcessError as e:
            logger.error("Command '%s' failed with code %d", " ".join(e.cmd), e.returncode)
            logger.error("Stderr: %s", e.stderr)
            logger.error("Stdout: %s", e.stdout)
            # Concatenate both streams — kicad-cli writes errors to stdout
            # on some builds; `or` would silently drop the real error.
            parts = [s.strip() for s in (e.stderr, e.stdout) if s and s.strip()]
            err_msg = "\n".join(parts) or f"(no output; exit code {e.returncode})"
            if ctx:
                await ctx.info(f"KiCad CLI command failed: {err_msg}")
            return {"error": f"KiCad CLI command failed: {err_msg}"}
        except subprocess.TimeoutExpired:
            logger.warning("Command timed out after 30 seconds: %s", " ".join(cmd))
            if ctx:
                await ctx.info("KiCad CLI command timed out")
            return {"error": "KiCad CLI command timed out after 30 seconds"}
        except Exception as e:
            logger.error("Error running CLI command: %s", e)
            if ctx:
                await ctx.info(f"Error running KiCad CLI: {e}")
            return {"error": f"Error running KiCad CLI: {e}"}

    except asyncio.CancelledError:
        logger.debug("CLI thumbnail generation cancelled")
        raise
    except Exception as e:
        logger.error("Unexpected error in CLI thumbnail generation: %s", e)
        if ctx:
            await ctx.info(f"Unexpected error: {e}")
        return {"error": f"Unexpected error in CLI thumbnail generation: {e}"}


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

def register_export_tools(mcp: FastMCP) -> None:
    """Register the export domain router."""

    @mcp.tool(
        annotations={
            # All three operations write new files to disk (gerbers+zip,
            # BOM CSV, thumbnail) -- never the input board/schematic, but
            # a real filesystem write, so not readOnlyHint=True.
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def export(
        operation: str,
        ctx: Context | None,
        *,
        pcb_path: Optional[str] = None,
        project_path: Optional[str] = None,
        output_dir: str = "",
        create_zip: bool = True,
    ) -> Dict[str, Any]:
        """Manufacturing and output-file exports.

        Operations:
          gerbers(pcb_path, output_dir="", create_zip=True)
              -> {status, gerber_files, drill_files, zip_path?, ...}
              Export Gerber + Excellon drill files for fabrication. ZIP is
              ready for JLCPCB / PCBWay / OSH Park upload.

          bom_csv(project_path)
              -> {status, output_file, file_size, ...}
              Export a CSV BOM from the project's schematic via kicad-cli.

          thumbnail(project_path)
              -> {status, thumbnail_path, size_bytes}
              Render an SVG thumbnail of the PCB using kicad-cli.
        """
        if operation == "gerbers":
            if pcb_path is None:
                return {"error": "operation='gerbers' requires 'pcb_path'"}
            return _op_gerbers(pcb_path, output_dir=output_dir, create_zip=create_zip)
        if operation == "bom_csv":
            if project_path is None:
                return {"error": "operation='bom_csv' requires 'project_path'"}
            # Imported here to avoid circular import (bom.py imports nothing from us)
            from kicad_mcp.tools.bom import _op_export_bom_csv
            return await _op_export_bom_csv(project_path, ctx)
        if operation == "thumbnail":
            if project_path is None:
                return {"error": "operation='thumbnail' requires 'project_path'"}
            return await _op_thumbnail(project_path, ctx)
        return {
            "error": (
                f"unknown operation {operation!r}; "
                f"valid: gerbers|bom_csv|thumbnail"
            )
        }
