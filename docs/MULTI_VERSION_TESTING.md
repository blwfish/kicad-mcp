# Multi-Version KiCad Testing

How to install side-by-side KiCad versions and run integration tests against each without disturbing the daily-driver install at `/Applications/KiCad/KiCad.app`.

## Install procedure

```bash
# Download KiCad 9.0.x and 10.0.x .dmg installers from kicad.org/downloads/
mkdir -p /Volumes/Files/claude/kicad-versions/9.0 /Volumes/Files/claude/kicad-versions/10.0

# Mount each .dmg, copy KiCad.app to the version dir — do NOT drag to /Applications
cp -R /Volumes/KiCad/KiCad.app /Volumes/Files/claude/kicad-versions/9.0/

# Remove Gatekeeper quarantine (one-time per install)
xattr -dr com.apple.quarantine /Volumes/Files/claude/kicad-versions/9.0/KiCad.app

# Verify the bridge can find the Python interpreter
KICAD_APP_PATH=/Volumes/Files/claude/kicad-versions/9.0/KiCad.app uv run python -c \
    "from kicad_mcp.utils.pcbnew_bridge import _get_kicad_python; print(_get_kicad_python())"
```

`/Applications/KiCad/KiCad.app` (currently 10.0) is the daily driver and is not touched by integration tests unless `KICAD_APP_PATH` is unset.

## Running integration tests

```bash
# Against the daily-driver install (10.0)
KICAD_INTEGRATION=1 uv run pytest tests/integration/ -v

# Against a specific version
KICAD_INTEGRATION=1 KICAD_APP_PATH=/Volumes/Files/claude/kicad-versions/9.0/KiCad.app \
    uv run pytest tests/integration/ -v
```

The `KICAD_INTEGRATION=1` guard prevents `pytest` (no args) from collecting the integration module. The fast mocked suite (`uv run pytest`) is unaffected.

## CI (self-hosted runner)

The integration workflow runs on a self-hosted macOS runner with both KiCad versions pre-installed at `/Volumes/Files/claude/kicad-versions/{9.0,10.0}/KiCad.app`. See `.github/workflows/integration.yml`.

The 10.0 matrix cell is `continue-on-error: false` (blocks PRs). The 9.0 cell is `continue-on-error: true` (visible but non-blocking).
