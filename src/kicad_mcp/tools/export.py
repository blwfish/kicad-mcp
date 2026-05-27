"""Export router — manufacturing and output files for KiCad projects.

See docs/SPEC_Tool_Consolidation.md.
"""
import asyncio
import logging
import os
import shutil
import subprocess
import zipfile
from typing import Any, Dict, Optional

from fastmcp import FastMCP, Context

from kicad_mcp.config import KICAD_APP_PATH, system
from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# gerbers
# ---------------------------------------------------------------------------

def _op_gerbers(
    pcb_path: str,
    output_dir: str = "",
    create_zip: bool = True,
) -> Dict[str, Any]:
    if not os.path.exists(pcb_path):
        return {"error": f"PCB file not found: {pcb_path}"}

    try:
        kicad_cli = get_kicad_cli_path(required=True)
    except Exception as e:
        return {"error": str(e)}
    assert kicad_cli is not None  # required=True raises above if CLI not found

    if not output_dir:
        pcb_dir = os.path.dirname(os.path.abspath(pcb_path))
        output_dir = os.path.join(pcb_dir, "gerbers")

    os.makedirs(output_dir, exist_ok=True)

    errors = []

    gerber_cmd = [
        kicad_cli, "pcb", "export", "gerbers",
        "--output", output_dir + "/",
        pcb_path,
    ]

    def _cli_error_text(e: subprocess.CalledProcessError) -> str:
        # Concatenate both streams — `or` would drop stderr entirely
        # when empty, hiding stdout-only error reports (kicad-cli
        # writes errors to stdout on some builds).
        parts = [s.strip() for s in (e.stderr, e.stdout) if s and s.strip()]
        return "\n".join(parts) or f"(no output; exit code {e.returncode})"

    try:
        _gerber_run = subprocess.run(
            gerber_cmd, capture_output=True, text=True,
            check=True, timeout=30,
        )
        if _gerber_run.stdout.strip():
            logger.info("Gerber export: %s", _gerber_run.stdout.strip())
    except subprocess.CalledProcessError as e:
        errors.append(f"Gerber export failed: {_cli_error_text(e)}")
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
        errors.append(f"Drill export failed: {_cli_error_text(e)}")
    except subprocess.TimeoutExpired:
        errors.append("Drill export timed out after 30s")

    if errors:
        # Structured error list with count — caller can programmatically
        # retry per-step instead of parsing a joined string.
        return {"error": f"{len(errors)} export step(s) failed", "errors": errors, "error_count": len(errors)}

    # output_dir is freshly created per export and contains only kicad-cli
    # output, so globbing everything is correct — picks up *.gbr (modern
    # KiCad), *.gtl/.gbl/.gto/etc. (RS-274X layer extensions), *.drl/.xln
    # (drill), *.gbrjob (fab job manifest), and anything else kicad-cli
    # produces. The earlier *.gbr-only glob silently shipped fab packages
    # missing layers.
    all_files = sorted(
        os.path.join(output_dir, f)
        for f in os.listdir(output_dir)
        if os.path.isfile(os.path.join(output_dir, f))
    )
    gerber_files = [f for f in all_files if not (f.endswith(".drl") or f.endswith(".xln"))]
    drill_files = [f for f in all_files if f.endswith(".drl") or f.endswith(".xln")]

    if not all_files:
        return {"error": "No output files generated — PCB may be empty"}

    result = {
        "status": "ok",
        "output_dir": output_dir,
        "gerber_files": [os.path.basename(f) for f in gerber_files],
        "drill_files": [os.path.basename(f) for f in drill_files],
        "gerber_count": len(gerber_files),
        "drill_count": len(drill_files),
        "total_files": len(all_files),
    }

    if create_zip:
        pcb_name = os.path.splitext(os.path.basename(pcb_path))[0]
        zip_path = os.path.join(
            os.path.dirname(output_dir), f"{pcb_name}-gerbers.zip"
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in all_files:
                zf.write(f, os.path.basename(f))
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

        kicad_cli = None
        if system == "Darwin":
            kicad_cli_path = os.path.join(
                KICAD_APP_PATH, "Contents/MacOS/kicad-cli"
            )
            if os.path.exists(kicad_cli_path):
                kicad_cli = kicad_cli_path
            elif shutil.which("kicad-cli") is not None:
                kicad_cli = "kicad-cli"
            else:
                logger.warning("kicad-cli not found at %s or in PATH", kicad_cli_path)
                return {"error": f"kicad-cli not found at {kicad_cli_path} or in PATH"}
        elif system == "Windows":
            kicad_cli_path = os.path.join(KICAD_APP_PATH, "bin", "kicad-cli.exe")
            if os.path.exists(kicad_cli_path):
                kicad_cli = kicad_cli_path
            elif shutil.which("kicad-cli.exe") is not None:
                kicad_cli = "kicad-cli.exe"
            elif shutil.which("kicad-cli") is not None:
                kicad_cli = "kicad-cli"
            else:
                logger.warning("kicad-cli not found at %s or in PATH", kicad_cli_path)
                return {"error": f"kicad-cli not found at {kicad_cli_path} or in PATH"}
        elif system == "Linux":
            kicad_cli = shutil.which("kicad-cli")
            if not kicad_cli:
                logger.warning("kicad-cli not found in PATH")
                return {"error": "kicad-cli not found in PATH"}
        else:
            logger.warning("Unsupported operating system: %s", system)
            return {"error": f"Unsupported operating system: {system}"}

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

    @mcp.tool()
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
              -> {success, output_file, file_size, ...}
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
