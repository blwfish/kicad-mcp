#!/usr/bin/env python3
"""Sync the tool count across all docs.

Run before committing when tools are added or removed:
    python scripts/sync_tool_count.py

CI runs this then checks `git diff --exit-code`.

Markers kept in sync
--------------------
Markdown files:   <!-- tool-count -->N<!-- /tool-count -->
test_server.py:   EXPECTED_TOOL_COUNT = N
"""
import asyncio
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MARKDOWN_MARKER = re.compile(r"<!-- tool-count -->\d+<!-- /tool-count -->")
TEST_CONSTANT = re.compile(r"^(EXPECTED_TOOL_COUNT = )\d+", re.MULTILINE)

MARKDOWN_FILES = [
    ROOT / "README.md",
    ROOT / "TOOLS.md",
    ROOT / "AGENT-INSTALL.md",
]
TEST_FILE = ROOT / "tests" / "test_server.py"


def get_tool_count() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from kicad_mcp.server import create_server  # noqa: PLC0415

    s = create_server()
    tools = asyncio.run(s.list_tools())
    return len(tools)


def sync_file(path: Path, pattern: re.Pattern, replacement: str) -> bool:
    """Apply pattern replacement. Returns True if the file changed."""
    text = path.read_text(encoding="utf-8")
    new = pattern.sub(replacement, text)
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def main() -> None:
    count = get_tool_count()
    print(f"Server reports {count} tools")

    md_replacement = f"<!-- tool-count -->{count}<!-- /tool-count -->"
    test_replacement = f"\\g<1>{count}"

    changed: list[str] = []

    for md in MARKDOWN_FILES:
        if sync_file(md, MARKDOWN_MARKER, md_replacement):
            changed.append(md.name)
            print(f"  Updated {md.name}")
        else:
            # Warn if the file has no markers at all — likely forgotten.
            text = md.read_text(encoding="utf-8")
            if "<!-- tool-count -->" not in text:
                print(f"  WARNING: {md.name} has no <!-- tool-count --> markers")

    if sync_file(TEST_FILE, TEST_CONSTANT, test_replacement):
        changed.append(TEST_FILE.name)
        print(f"  Updated {TEST_FILE.name}")

    if changed:
        print(f"\nUpdated {len(changed)} file(s): {', '.join(changed)}")
        print("Review the changes and commit them.")
    else:
        print(f"\nAll files already show {count} — nothing to update.")


if __name__ == "__main__":
    main()
