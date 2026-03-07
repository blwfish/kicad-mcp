"""
Export tools for KiCad projects.
"""
import asyncio
import glob
import logging
import os
import shutil
import subprocess
import zipfile
from typing import Any, Dict

from fastmcp import FastMCP, Context

from kicad_mcp.config import KICAD_APP_PATH, system
from kicad_mcp.utils.file_utils import get_project_files
from kicad_mcp.utils.kicad_cli import get_kicad_cli_path

logger = logging.getLogger(__name__)


def register_export_tools(mcp: FastMCP) -> None:
    """Register export tools with the MCP server.

    Args:
        mcp: The FastMCP server instance
    """

    @mcp.tool()
    def export_gerbers(
        pcb_path: str,
        output_dir: str = "",
        create_zip: bool = True,
    ) -> Dict[str, Any]:
        """Export Gerber fabrication files + Excellon drill files from a PCB.

        Generates all files needed for PCB fabrication (e.g., JLCPCB upload).
        Exports all copper, mask, silkscreen, paste, and edge layers as
        Gerber files, plus drill files in Excellon format with mm units.

        Optionally creates a ZIP file containing all outputs, ready for
        direct upload to JLCPCB, PCBWay, OSH Park, etc.

        Args:
            pcb_path: Path to the .kicad_pcb file.
            output_dir: Directory for output files. Default: "gerbers/"
                subdirectory next to the PCB file.
            create_zip: Create a ZIP file of all outputs (default True).

        Returns:
            Dictionary with output paths, file counts, and ZIP path.
        """
        if not os.path.exists(pcb_path):
            return {"error": f"PCB file not found: {pcb_path}"}

        try:
            kicad_cli = get_kicad_cli_path(required=True)
        except Exception as e:
            return {"error": str(e)}

        # Default output directory
        if not output_dir:
            pcb_dir = os.path.dirname(os.path.abspath(pcb_path))
            output_dir = os.path.join(pcb_dir, "gerbers")

        os.makedirs(output_dir, exist_ok=True)

        errors = []

        # Export Gerber files (all standard fab layers)
        gerber_cmd = [
            kicad_cli, "pcb", "export", "gerbers",
            "--output", output_dir + "/",
            pcb_path,
        ]

        try:
            result = subprocess.run(
                gerber_cmd, capture_output=True, text=True,
                check=True, timeout=30,
            )
            logger.info("Gerber export: %s", result.stdout.strip())
        except subprocess.CalledProcessError as e:
            errors.append(f"Gerber export failed: {e.stderr or e.stdout}")
        except subprocess.TimeoutExpired:
            errors.append("Gerber export timed out after 30s")

        # Export drill files (Excellon format, mm units)
        drill_cmd = [
            kicad_cli, "pcb", "export", "drill",
            "--output", output_dir + "/",
            "--format", "excellon",
            "--excellon-units", "mm",
            pcb_path,
        ]

        try:
            result = subprocess.run(
                drill_cmd, capture_output=True, text=True,
                check=True, timeout=30,
            )
            logger.info("Drill export: %s", result.stdout.strip())
        except subprocess.CalledProcessError as e:
            errors.append(f"Drill export failed: {e.stderr or e.stdout}")
        except subprocess.TimeoutExpired:
            errors.append("Drill export timed out after 30s")

        if errors:
            return {"error": "; ".join(errors)}

        # Collect output files
        gerber_files = sorted(glob.glob(os.path.join(output_dir, "*.gbr")))
        drill_files = sorted(
            glob.glob(os.path.join(output_dir, "*.drl"))
            + glob.glob(os.path.join(output_dir, "*.xln"))
        )
        all_files = gerber_files + drill_files

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

        # Create ZIP
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

    @mcp.tool()
    async def generate_pcb_thumbnail(
        project_path: str, ctx: Context | None
    ):
        """Generate a thumbnail image of a KiCad PCB layout using kicad-cli.

        Args:
            project_path: Path to the KiCad project file (.kicad_pro)
            ctx: Context for MCP communication

        Returns:
            Dictionary with thumbnail path and size, or error information
        """
        try:
            print(f"Generating thumbnail via CLI for project: {project_path}")

            if not os.path.exists(project_path):
                print(f"Project not found: {project_path}")
                if ctx:
                    await ctx.info(f"Project not found: {project_path}")
                return {"error": f"Project not found: {project_path}"}

            # Get PCB file from project
            files = get_project_files(project_path)
            if "pcb" not in files:
                print("PCB file not found in project")
                if ctx:
                    await ctx.info("PCB file not found in project")
                return {"error": "PCB file not found in project"}

            pcb_file = files["pcb"]
            print(f"Found PCB file: {pcb_file}")

            if ctx:
                await ctx.report_progress(10, 100)
                await ctx.info(
                    f"Generating thumbnail for {os.path.basename(pcb_file)} using kicad-cli"
                )

            try:
                result = await _generate_thumbnail_with_cli(pcb_file, ctx)
                if result and "error" not in result:
                    print("Thumbnail generated successfully via CLI.")
                    return result
                else:
                    print("_generate_thumbnail_with_cli returned error or empty result")
                    if ctx:
                        await ctx.info(
                            "Failed to generate thumbnail using kicad-cli."
                        )
                    return result or {"error": "Failed to generate thumbnail using kicad-cli"}
            except Exception as e:
                print(f"Error calling _generate_thumbnail_with_cli: {e}")
                if ctx:
                    await ctx.info(
                        f"Error generating thumbnail with kicad-cli: {e}"
                    )
                return {"error": f"Error generating thumbnail with kicad-cli: {e}"}

        except asyncio.CancelledError:
            print("Thumbnail generation cancelled")
            raise
        except Exception as e:
            print(f"Unexpected error in thumbnail generation: {e}")
            if ctx:
                await ctx.info(f"Error: {e}")
            return {"error": f"Unexpected error in thumbnail generation: {e}"}

    @mcp.tool()
    async def generate_project_thumbnail(
        project_path: str, ctx: Context | None
    ):
        """Generate a thumbnail of a KiCad project's PCB layout (Alias for generate_pcb_thumbnail)."""
        print(
            f"generate_project_thumbnail called, redirecting to "
            f"generate_pcb_thumbnail for {project_path}"
        )
        return await generate_pcb_thumbnail(project_path, ctx)


async def _generate_thumbnail_with_cli(
    pcb_file: str, ctx: Context | None
):
    """Generate PCB thumbnail using command line tools.

    Args:
        pcb_file: Path to the PCB file (.kicad_pcb)
        ctx: MCP context for progress reporting

    Returns:
        Dictionary with thumbnail path and size, or error information
    """
    try:
        print("Attempting to generate thumbnail using KiCad CLI tools")
        if ctx:
            await ctx.report_progress(20, 100)

        # Determine output path
        project_dir = os.path.dirname(pcb_file)
        project_name = os.path.splitext(os.path.basename(pcb_file))[0]
        output_file = os.path.join(project_dir, f"{project_name}_thumbnail.svg")

        # Check for required command-line tools based on OS
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
                print(f"kicad-cli not found at {kicad_cli_path} or in PATH")
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
                print(f"kicad-cli not found at {kicad_cli_path} or in PATH")
                return {"error": f"kicad-cli not found at {kicad_cli_path} or in PATH"}
        elif system == "Linux":
            kicad_cli = shutil.which("kicad-cli")
            if not kicad_cli:
                print("kicad-cli not found in PATH")
                return {"error": "kicad-cli not found in PATH"}
        else:
            print(f"Unsupported operating system: {system}")
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

        print(f"Running command: {' '.join(cmd)}")
        if ctx:
            await ctx.report_progress(50, 100)

        try:
            process = subprocess.run(
                cmd, capture_output=True, text=True, check=True, timeout=30
            )
            print(f"Command successful: {process.stdout}")

            if ctx:
                await ctx.report_progress(70, 100)

            if not os.path.exists(output_file):
                print(f"Output file not created: {output_file}")
                return {"error": f"Output file not created: {output_file}"}

            file_size = os.path.getsize(output_file)

            print(
                f"Successfully generated thumbnail with CLI, size: {file_size} bytes"
            )
            if ctx:
                await ctx.report_progress(90, 100)
                await ctx.info(f"Thumbnail generated ({file_size} bytes)")
            return {
                "status": "ok",
                "thumbnail_path": output_file,
                "size_bytes": file_size,
            }

        except subprocess.CalledProcessError as e:
            print(f"Command '{' '.join(e.cmd)}' failed with code {e.returncode}")
            print(f"Stderr: {e.stderr}")
            print(f"Stdout: {e.stdout}")
            if ctx:
                await ctx.info(
                    f"KiCad CLI command failed: {e.stderr or e.stdout}"
                )
            return {"error": f"KiCad CLI command failed: {e.stderr or e.stdout}"}
        except subprocess.TimeoutExpired:
            print(f"Command timed out after 30 seconds: {' '.join(cmd)}")
            if ctx:
                await ctx.info("KiCad CLI command timed out")
            return {"error": "KiCad CLI command timed out after 30 seconds"}
        except Exception as e:
            print(f"Error running CLI command: {e}")
            if ctx:
                await ctx.info(f"Error running KiCad CLI: {e}")
            return {"error": f"Error running KiCad CLI: {e}"}

    except asyncio.CancelledError:
        print("CLI thumbnail generation cancelled")
        raise
    except Exception as e:
        print(f"Unexpected error in CLI thumbnail generation: {e}")
        if ctx:
            await ctx.info(f"Unexpected error: {e}")
        return {"error": f"Unexpected error in CLI thumbnail generation: {e}"}
