"""In-process, per-resolved-path locking for PCB-mutating tool calls.

Makes AGENT-INSTRUCTIONS.md's "no concurrent PCB writes" rule (previously
caller-enforced discipline only) actually enforced -- for the case this can
genuinely fix. Read the scope limit below before reaching for this in a new
context; it does NOT solve the general concurrent-write problem.

## What this fixes

Two mutating tool calls racing each other WITHIN one running kicad-mcp
server process (one agent session, one server instance, two overlapping
`pcb(operation=...)` calls) -- previously relied entirely on the calling
agent remembering to serialize its own calls. Now a second mutating call
against a PCB path already locked fails immediately with a clear error
instead of racing the first call's load-modify-save cycle.

## What this does NOT fix -- by design, not by oversight

A real, general "no concurrent writes to this file" guarantee would need
OS-level file locking (`flock`/`fcntl` on macOS/Linux, `LockFileEx` on
Windows -- three genuinely different locking models, not variations on one
API) AND would need to work over whatever filesystem `pcb_path` happens to
sit on. Two problems make that impractical to build here:

1. **Two separate kicad-mcp server processes** (e.g. two Claude Code
   sessions, each launching their own stdio-connected server, both pointed
   at the same project) are invisible to each other. This lock lives in
   one process's memory; a lock held by process A does not exist as far as
   process B is concerned. `path_validation.py`'s allowed-roots list
   permits `pcb_path` to be a user's project directory anywhere on disk --
   this in-process lock only ever protects against races within a single
   server, never across two.
2. Even if this were extended to real OS-level locking, that only helps
   for genuinely local files. NFS's locking story is a known mess (NFSv3's
   separate NLM protocol is notorious for stale locks surviving a client
   or server restart; NFSv4's lease-based locking is better but still not
   semantically identical to local `flock`). SMB has its own oplock/lease
   model that doesn't map cleanly onto POSIX locks. And sync tools --
   Dropbox, OneDrive, iCloud Drive -- don't participate in OS-level locking
   at all, so a `.kicad_pcb` in a synced folder can have two writers stomp
   on it with zero lock contention regardless of what's implemented here.

Given that, this module deliberately stays in-process only. If the
multi-process case ever needs solving for real, it needs to be evaluated
on its own terms (a real file lock, accepting the platform/filesystem mess
above, or a different approach entirely -- e.g. a lock file with an
explicit staleness/liveness check) -- not silently assumed to be covered
by this.

## What this does NOT cover, even in-process

Schematic operations (`schematic.py`) mutate an in-memory, module-level
`_current_schematic` object, not a file, until `save` is called explicitly
-- a different concurrency model (races on a Python object across
concurrent async tool calls on one event loop, not races on file I/O).
Out of scope for this module; not currently locked at all.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager

_registry_lock = threading.Lock()
# One threading.Lock per unique resolved path ever locked, for the life of
# this process -- never evicted. Cheap enough not to matter in practice
# (a few dozen small objects for a realistic session's worth of distinct
# boards) and correctness-simpler than any eviction scheme, which would
# need to prove no other thread is about to look up an "evicted" entry.
_locks: dict[str, threading.Lock] = {}


def _lock_for(pcb_path: str) -> threading.Lock:
    key = os.path.realpath(pcb_path)
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


@contextmanager
def pcb_write_lock(pcb_path: str) -> Iterator[bool]:
    """Non-blocking, per-resolved-path lock for a PCB-mutating call.

    Yields True if the lock was acquired -- the caller should proceed.
    Yields False if another mutating call already holds it -- the caller
    MUST return an error immediately, not block waiting: a queued
    autoroute pass can legitimately hold this for up to 30 minutes, and a
    caller silently hanging for that long is worse than an immediate,
    actionable "busy, retry" error (this codebase's own convention favors
    an explicit typed signal over silent blocking -- see AGENTS.md).

    Only wrap MUTATING operations with this -- the ones AGENT-INSTRUCTIONS.md
    already documents as needing serialization. Read-only queries
    (list_nets, get_pad_positions, drc(run), most audit(...) operations)
    must never acquire this; doing so would defeat the "read-only calls
    are safe to run in parallel" design that document promises.
    """
    lock = _lock_for(pcb_path)
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def busy_error(pcb_path: str) -> dict:
    """The standard error shape for a lock-contention rejection -- shared
    so every call site reports this identically."""
    return {
        "status": "error",
        "error": (
            f"Another mutating operation is already in progress on {pcb_path!r} "
            "in this server process. Wait for it to finish, then retry."
        ),
    }
