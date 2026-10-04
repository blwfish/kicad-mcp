"""The FreeRouter version banner is still parseable on the INSTALLED jar.

``utils/freerouter_version.py`` identifies the build by the INFO banner FreeRouter
prints at startup (it ships no machine-readable version).  If an upstream release
reformats that line, the probe would quietly degrade to ``status == "unknown"`` and the
"newer than validated" warning would stop firing -- this test turns that silent drift
into a CI failure on the real jar the runners use.
"""

import os
import time

import pytest

from kicad_mcp.tools.pcb_autoroute import _find_freerouter_jar, _find_java, _freerouter_version_info
from kicad_mcp.utils import freerouter_version as fv

pytestmark = pytest.mark.skipif(
    os.environ.get("KICAD_INTEGRATION") != "1",
    reason="Integration tests require KICAD_INTEGRATION=1 and a real install",
)


def test_installed_jar_reports_a_parseable_version():
    jar, java = _find_freerouter_jar(None), _find_java()
    if not (jar and java):
        pytest.skip("FreeRouter jar or Java not available")
    fv._version_cache.clear()

    started = time.monotonic()
    info = _freerouter_version_info(java, jar)
    elapsed = time.monotonic() - started

    assert info["status"] != fv.STATUS_UNKNOWN, (
        f"could not read a version banner from {jar}: the startup line format may have "
        f"changed -- update parse_version_banner"
    )
    assert info["version"] is not None and info["version"].count(".") == 2
    assert elapsed < 20, f"version probe took {elapsed:.1f}s (should be a couple of seconds)"
