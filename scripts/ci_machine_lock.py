#!/usr/bin/env python3
"""Run a command while holding a machine-wide exclusive lock.

Usage: ci_machine_lock.py [--lock-file PATH] -- COMMAND [ARGS...]

The self-hosted CI runners share one physical machine.  GitHub Actions
`concurrency:` groups cannot serialize across runs without cancelling queued
jobs (one running + one pending per group; a newer pending replaces the older),
and `strategy.max-parallel` only serializes legs *within* one run.  Overlapping
runs (two PRs, or a PR plus a push to main) launch competing FreeRouter JVM
batches and push the autoroute integration tests past their per-test timeout.

This blocks until it holds an exclusive flock on the lock file, then exec()s
the command.  The fd is inherited across exec, so the lock is held until the
command exits; the kernel releases it if the process dies, so a killed job
cannot leave a stale lock behind (unlike a mkdir/touch-based lock).
"""

import argparse
import fcntl
import os
import sys
import time

DEFAULT_LOCK_FILE = "/tmp/kicad-mcp-integration.lock"


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given (usage: ... -- COMMAND [ARGS...])")

    fd = os.open(args.lock_file, os.O_CREAT | os.O_RDWR, 0o666)
    waited_from = time.monotonic()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"ci_machine_lock: waiting for {args.lock_file} ...", flush=True)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print(
            f"ci_machine_lock: acquired after {time.monotonic() - waited_from:.0f}s",
            flush=True,
        )
    os.set_inheritable(fd, True)

    try:
        os.execvp(command[0], command)
    except OSError as e:
        print(f"ci_machine_lock: cannot exec {command[0]!r}: {e}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
