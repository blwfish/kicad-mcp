# SPEC — Out-of-Band Events Subsystem

**Status:** Approved for implementation
**Author:** Brian + Claude
**Date:** 2026-05-27
**Targets:** A shared `mcp_events.py` module vendored into MCP server projects; per-MCP integration patterns. First adopters: kicad-mcp and freecad-mcp.
**Informed by:** Audit of freecad-mcp's existing error handling (`/Volumes/Files/claude/freecad-mcp/`, 2026-05-27).

## Motivation

A coherent channel for events that don't belong in a tool's main response but also can't be silently swallowed — errors that the caller LLM should see, warnings that surface a degradation, info-level signals that need to survive between calls.

The freecad-mcp audit found three patterns of out-of-band handling that need a unified channel:

1. **Silent-pass sites.** `except Exception: pass` blocks that discard real failures — file `base.py:178-179` (`save_before_risky_op` silently fails), `spreadsheet_ops.py:348/361/376` (multiple fallback paths silently swallow errors), `base.py:291-294` (per-geometry-element failures).
2. **Visibility loss to the LLM.** `boolean_ops.py:36` writes a complexity warning to `FreeCAD.Console.PrintWarning()` — invisible to the calling LLM. The warning is constructed and immediately discarded from the MCP channel.
3. **Inconsistent error envelopes.** Some tools return `{"result": "Error doing X: e"}` (error embedded in success envelope as string); others return `{"error": "...", "error_id": "..."}` (structured dict). The LLM can't reliably parse status across the tool surface.

Same patterns will appear in kicad-mcp as it grows — telemetry write failures (the immediate need from the [Feedback Infrastructure SPEC](SPEC_Feedback_Infrastructure.md)), schema-drift warnings, stale jlcparts-snapshot warnings (from the upcoming component-intelligence work), and similar.

Without a coherent channel:
- Errors get swallowed → never fixed
- Warnings buried where the LLM can't see → never acted on
- Inconsistent envelopes → LLM can't reliably parse status

This SPEC defines that channel.

## Non-goals

- **Not a general-purpose logging framework.** Use Python's `logging` for debug/info messages that don't need to surface to the LLM. This subsystem is for events that the *calling LLM* should see or that need to *persist between calls*.
- **Not a metrics or tracing platform.** No counters, no timers, no distributed-trace IDs.
- **Not a persistence layer.** The shared module accumulates events in-process and flushes them to the response envelope. Persistence (durable storage of events between sessions) is each MCP's local concern, layered on top.
- **Not a published PyPI release yet** (until ~0.1.0 stabilizes). But it IS a proper Python package from day one — installed via editable path-dependency in the workspace; published to PyPI once stable. **Copy-paste vendoring was considered and rejected**: in a single-maintainer multi-repo workspace, copy-paste drift is a real, predictable failure mode that proper packaging eliminates. See the "Distribution" section below.

## Design principles

- **Synchronous at call boundary.** Event accumulation happens inline during a tool call. No daemon, no background thread, no async write queue. See `feedback_synchronous_at_call_boundary.md` in memory.
- **Context-frugal.** The default response envelope omits `events` entirely when nothing notable happened. Warnings and errors are included; info-level events are accumulated but not surfaced by default (caller can opt in). See `feedback_context_frugality.md`.
- **Never silent.** Every recorded event has a path to surface. If no event context is active when something is recorded, the event falls back to stderr (visible at process level) rather than being dropped.
- **Same API surface across MCPs.** Copying the file should "just work." No project-specific configuration, no class hierarchies to subclass.

## Architecture

### The shared module: `mcp-events` (Python package, module name `mcp_events`)

A small Python package, roughly 150-200 lines of code plus tests. No external dependencies beyond the Python standard library. Lives in its own repo / directory so both consumers depend on a single source of truth.

### Location

```
/Volumes/Files/claude/
├── kicad-mcp/         ← consumer; depends on mcp-events
├── freecad-mcp/       ← consumer; depends on mcp-events
└── mcp-events/        ← canonical package (separate directory, eventually its own git repo)
    ├── pyproject.toml
    ├── src/mcp_events/
    │   ├── __init__.py    # the public API
    │   └── _impl.py       # implementation if it grows; v1 can be one file
    └── tests/
        └── test_mcp_events.py
```

Eventual GitHub home: `blwfish/mcp-events` (not in scope for this SPEC — local dir works for v1).

### Consumer integration

Each consuming MCP adds `mcp-events` as a normal Python dependency. In dev, the path source overrides PyPI:

```toml
# kicad-mcp/pyproject.toml (and freecad-mcp/pyproject.toml, identical pattern)
[project]
dependencies = [
    "mcp-events>=0.1.0",   # PyPI version for downstream users
    # ... other deps ...
]

[tool.uv.sources]
mcp-events = { path = "../mcp-events", editable = true }
```

The `[tool.uv.sources]` block tells uv (in dev) to install from the local path rather than PyPI. Downstream users installing kicad-mcp per `AGENT-INSTALL.md` run `uv sync` against the cloned repo; their sync resolves `mcp-events>=0.1.0` from PyPI (the path source resolution applies only when the local path exists, which it does for Brian and not for them). Note: kicad-mcp itself is NOT distributed via PyPI (per [`feedback_ai_first_distribution.md`](../../../../Users/blw/.claude/projects/-Volumes-Files-claude-kicad-mcp/memory/feedback_ai_first_distribution.md)) — it uses `AGENT-INSTALL.md` as the distribution model. `mcp-events` is the exception because it's a library dependency that package managers must resolve.

Editable install means a fix in `/Volumes/Files/claude/mcp-events/src/mcp_events/__init__.py` is immediately visible to both consumers. No copy, no sync, no drift.

### Versioning policy

- Aggressive bumping while early: 0.1.0 → 0.1.1 → 0.1.2 as the API settles
- Consumers pin to `>=0.1.0,<0.2.0` (or similar tight range) until 0.1 stabilizes
- 0.2 onward = stable API; consumers can use `>=0.2.0` and benefit from patches without version pinning
- 1.0 once it's been stable for ~6 months and there's no foreseeable need for breaking changes

### Publication to PyPI

Publish `mcp-events` to PyPI BEFORE merging any kicad-mcp PR that depends on it. kicad-mcp's CI uses `uv sync --frozen` and the lockfile must resolve `mcp-events>=0.1.0` from PyPI — a local-path-only resolution would fail in CI and fail for downstream users following `AGENT-INSTALL.md`. (Note: kicad-mcp itself is NOT published to PyPI — `AGENT-INSTALL.md` is its distribution model. `mcp-events` is the exception because it's a runtime library dependency.)

### Public API

```python
# Event data
@dataclass
class Event:
    severity: str       # "info" | "warn" | "error"
    code: str           # short stable identifier, e.g. "stale_cache_fallback"
    message: str        # human-readable
    context: dict       # optional structured data; default {}

# The accumulator — one per tool call
class EventAccumulator:
    def emit(self, severity: str, code: str, message: str, context: dict | None = None) -> None: ...
    def info(self, code: str, message: str, context: dict | None = None) -> None: ...
    def warn(self, code: str, message: str, context: dict | None = None) -> None: ...
    def error(self, code: str, message: str, context: dict | None = None) -> None: ...
    def soft_failure(self, code: str, message: str, context: dict | None = None) -> None:
        # Convenience: alias for warn() with the soft-failure semantic.
        # Use this at sites that were previously `except Exception: pass`.
        ...
    def has_any(self, min_severity: str = "info") -> bool: ...
    def to_envelope(self, min_severity: str = "warn") -> list[dict]:
        # Render as the JSON-serializable envelope shape.
        # Default threshold "warn" excludes info events; pass min_severity="info"
        # to include them.
        ...
    @property
    def events(self) -> list[Event]:
        # Read-only access to all collected events (any severity).
        # For local persistence layers to iterate.
        ...

# Context management
class event_context:
    """Context manager establishing an EventAccumulator for the current call.
    Use at the top of each tool:

        @mcp.tool()
        def my_tool(args):
            with event_context() as events:
                # ... tool logic ...
                response = {"status": "ok", "data": ...}
                if events.has_any("warn"):
                    response["events"] = events.to_envelope()
                return response
    """
    def __enter__(self) -> EventAccumulator: ...
    def __exit__(self, *exc) -> bool: ...

def get_current() -> EventAccumulator | None:
    """Returns the active EventAccumulator if inside an event_context, else None.
    Used by helper functions that don't receive the accumulator directly."""
    ...

# Top-level shortcuts (find the current accumulator via ContextVar)
def soft_failure(code: str, message: str, context: dict | None = None) -> None: ...
def emit_event(severity: str, code: str, message: str, context: dict | None = None) -> None: ...

# Decorator convenience
def with_events(min_severity: str = "warn"):
    """Decorator that wraps a tool function with an event_context and auto-
    augments the response with the envelope.

        @with_events()
        @mcp.tool()
        def my_tool(args):
            soft_failure("foo", "bar")  # automatically captured
            return {"status": "ok", "data": ...}
    """
    ...
```

### Internals (informational)

The current accumulator is tracked via a Python `ContextVar`. This means:
- Each tool call sees its own accumulator
- Async tools that yield don't lose context (ContextVar propagates correctly under asyncio)
- Nested calls (tool calls helper that calls another helper) all share the outer accumulator

The fallback when no context is active: write to stderr in a structured prefix (`[mcp_events] {severity}: {code}: {message}`). This means events are never lost, even if a tool forgets to use `event_context`. The stderr output isn't pretty but it's visible at the process level.

### Response envelope contract

Every tool's response follows this envelope (or uses `with_events` to construct it automatically):

```json
{
    "status": "ok",                       // required: "ok" | "error"
    "data": {...},                        // optional, tool-specific success payload
    "error": "...",                       // required if status=="error"; optional otherwise
    "events": [                           // optional; present when warn/error events accumulated
        {
            "level": "warn",              // "info" | "warn" | "error"
            "code": "stale_cache_fallback",  // short identifier; for programmatic handling
            "message": "Using cached jlcparts snapshot from 5 days ago",  // human-readable
            "context": {"snapshot_age_days": 5, "source_url": "..."}  // optional structured data
        }
    ]
}
```

Rules:
- `events` is omitted entirely when empty. Don't include `"events": []`.
- `events` is always an array, even with one element.
- Each event must have `level`, `code`, `message`. `context` is optional.
- `code` should be short, lowercase, snake_case, and stable across releases (LLMs may pattern-match on it).
- `message` is for humans; can change without breaking anything.
- Tools at `status: "error"` still include events that accumulated before the error occurred.

### Severity policy

| Severity | Surfaced in `events` envelope by default | Typical persistence (per-MCP) | Example |
|---|---|---|---|
| `info` | No (collected but excluded from envelope unless caller lowers threshold) | Yes | "Schema migration applied", "Sweep completed: 12 warnings abandoned" |
| `warn` | Yes | Yes | "Stale cache used as fallback", "kicad-cli netlist schema hash changed", "Soft failure: spreadsheet alias query fell back" |
| `error` | Yes (always — including when otherwise hidden by threshold) | Yes | "Telemetry write failed", "FreeCAD pre-crash save failed" |

The asymmetry is intentional: errors are unconditional (the LLM must see them); info is opt-in (most callers don't want noise). Warn is the default-visible middle ground.

## Per-MCP integration

The shared module handles in-call accumulation. Each project decides what to do with the accumulated events at the end of each call.

### kicad-mcp integration

- **Dependency**: `mcp-events>=0.1.0` in `pyproject.toml`; `[tool.uv.sources]` points to `../mcp-events` for dev.
- **Import**: `from mcp_events import event_context, soft_failure, emit_event, with_events`.
- **Persistence**: events with severity `warn`/`error` (and optionally `info`) are written to the `system_events` table in the telemetry DB. See `SPEC_Feedback_Infrastructure.md`. The persistence layer iterates `accumulator.events` at end of call and inserts each.
- **Migration target**: every tool should be wrapped with `with_events()`. Start with the heaviest call sites: `suggest_schematic_placement` (when it ships), `run_drc_check`, `analyze_placement_telemetry`. Roll out to the rest as touched.
- **The `analyze_placement_telemetry` system_events query** is the durable view of accumulated events (see the Feedback Infrastructure SPEC).

### freecad-mcp integration

- **Dependency**: same — `mcp-events>=0.1.0` in `pyproject.toml`, path source for dev.
- **Persistence**: freecad-mcp already has fragmented infrastructure (`freecad_debug.py`, `freecad_crash_report.py`, `_last_tracebacks`). Recommendation for v1: don't unify those yet. Just add a write-through to `freecad_debug.py`'s existing jsonl file for `warn`+`error` events.
- **Migration target — known silent-pass sites to retrofit (from the audit)**:
  - `base.py:178-179` (`save_before_risky_op`): replace `except Exception: pass` with `except Exception as e: soft_failure("save_before_risky_op_failed", str(e), {"path": ...})`
  - `boolean_ops.py:34-36` (complexity warning): replace `FreeCAD.Console.PrintWarning(msg)` with `emit_event("warn", "boolean_op_complex", msg, {"face_count": ...})`
  - `spreadsheet_ops.py:348-349, 361-362, 376-377` (XML/dimension/alias fallback): replace each silent `pass` with `soft_failure(...)` plus the existing fallback behavior
  - `base.py:291-294` (per-geometry-element failures): same pattern — capture each one as `info`-severity at minimum so they're not invisible
- **Envelope cleanup**: tools currently inconsistent in error shape. Standardize on the envelope above. Likely a separate PR; not blocking for OOB adoption.

### Future MCPs

Any new MCP adds `mcp-events>=0.1.0` (or whatever current version) to `pyproject.toml` and imports normally. The package is project-agnostic — no kicad-mcp-specific or freecad-mcp-specific code in it.

## Distribution strategy

Standard Python package, distributed via PyPI when ready, consumed via editable path-dependency in the workspace until then. **Copy-paste vendoring is explicitly rejected** for this project because the failure mode (drift between two repos maintained by the same person) is predictable and avoidable.

### Why a package, not copy-paste

The argument for copy-paste vendoring ("API will change as both adopters validate it") applies when two independent teams might disagree on the abstraction. That's not our situation: one maintainer (Brian) owns both consumers and the workspace is the source of truth. Drift between copies is just a foot-gun with no upside in this configuration.

The package approach addresses the "API stability" concern via standard mechanisms — version-pinned dependencies in the consumers, aggressive bumping while early. Same outcome (controlled rollout of changes) with stronger guarantees (impossible to forget to sync).

### What goes in `mcp_events` (the package) vs. per-MCP

In `mcp_events` (shared, lives in `mcp-events/`):
- `Event`, `EventAccumulator`
- `event_context`, `get_current`
- Top-level `soft_failure`, `emit_event`
- `with_events` decorator
- Test suite (one canonical, runs in the `mcp-events` repo)

Per-MCP (local):
- Persistence (system_events table, jsonl file, whatever each project does)
- Hooks that fire on each event (if used)
- The actual `@mcp.tool()` wrapping
- Project-specific codes (`stale_cache_fallback` is kicad-mcp's code for jlcparts; freecad-mcp's equivalent has a different code)

### Drift detection

There's nothing to drift between — both consumers import from the same installed package. The only drift risk is between local `[tool.uv.sources]` path-dependency and the version pinned for downstream users. Run `uv sync` periodically to confirm the path version satisfies the pinned constraint.

## Migration guidance

For retrofitting existing silent-pass sites:

```python
# Before
try:
    risky_thing()
except Exception:
    pass

# After
try:
    risky_thing()
except Exception as e:
    soft_failure("risky_thing_failed", str(e), {"context": ...})
    # Continue with fallback behavior here, unchanged.
```

For surfacing previously-buried warnings:

```python
# Before
FreeCAD.Console.PrintWarning(f"Complex geometry: {n} faces")
# Caller LLM never sees this.

# After
emit_event("warn", "complex_geometry", f"Complex geometry: {n} faces", {"face_count": n})
# Appears in the tool response's events field.
```

For wrapping a tool:

```python
# Before
@mcp.tool()
def my_tool(args):
    # ... work ...
    return {"status": "ok", "data": result}

# After (option 1: decorator)
@with_events()
@mcp.tool()
def my_tool(args):
    # Now any soft_failure / emit_event inside this call surfaces automatically.
    return {"status": "ok", "data": result}

# After (option 2: explicit)
@mcp.tool()
def my_tool(args):
    with event_context() as events:
        response = {"status": "ok", "data": ...}
        if events.has_any("warn"):
            response["events"] = events.to_envelope()
        return response
```

Either works; decorator is less verbose and harder to forget. Use the decorator unless there's a specific reason not to (e.g., the tool needs to inspect events during processing).

## Testing strategy

Tests live ONCE, in the `mcp-events` package itself (`mcp-events/tests/test_mcp_events.py`). Consumers don't re-test the shared module — they just trust it because the package is versioned.

### Test cases (canonical suite)

- **Basic accumulation**: emit info/warn/error → `events` contains all three; `to_envelope("warn")` excludes info
- **`has_any` thresholds**: `has_any("warn")` returns True with only warn, False with only info
- **Context isolation**: nested `event_context` blocks each get their own accumulator (or: confirm they share, depending on design choice — see Open Questions)
- **`soft_failure` shortcut**: equivalent to `warn` with the same arguments
- **Decorator wrapping**: tool wrapped with `@with_events()` automatically includes `events` field when warns occur
- **Decorator omits events when empty**: no warn/error → `events` field absent from response
- **Stderr fallback**: `soft_failure` called outside any context → writes to stderr (capture with capsys), does not raise
- **Async-safety**: under `asyncio.run`, an async tool with `with event_context()` correctly isolates events per coroutine
- **Envelope shape**: every emitted event renders to the documented JSON shape
- **Boundary**: empty string for `code` or `message` — rejected? accepted? Spec choice: accepted (sometimes you have a vague error and want at least a record); but `code=""` and `message=""` together logs a stderr meta-warning

### Boundary tests

- Threshold: `to_envelope("warn")` includes a warn-level event; `to_envelope("error")` excludes it
- `to_envelope()` default is `"warn"`, not `"info"` (check the default)
- `events` array is always a list, never None; absent when no events meet threshold
- `Event.context` defaults to `{}`, not `None`

## v1 scope

Ship the `mcp-events` package:
- Create the directory structure at `/Volumes/Files/claude/mcp-events/`
- `pyproject.toml` with `name = "mcp-events"`, `version = "0.1.0"`, no runtime dependencies
- `src/mcp_events/__init__.py` exporting the public API: `Event`, `EventAccumulator`, `event_context`, `soft_failure`, `emit_event`, `with_events`, `get_current`
- ContextVar-based current-accumulator tracking
- Stderr fallback for events emitted outside any context
- `tests/test_mcp_events.py` with the canonical test suite
- A README explaining the API and the distribution model
- Initialize a git repo in the directory (target: eventual push to `blwfish/mcp-events`)

Concretely for kicad-mcp (same PR or follow-up):
- Add `mcp-events>=0.1.0` to `pyproject.toml` dependencies
- Add `[tool.uv.sources] mcp-events = { path = "../mcp-events", editable = true }` for dev
- Run `uv sync` to update `uv.lock`
- Wire `@with_events()` into `analyze_placement_telemetry` when that tool exists (it doesn't yet; this can wait for the telemetry implementation PR)

Concretely for freecad-mcp (follow-up PR, separate session):
- Same dependency + path-source setup
- Retrofit the named silent-pass sites
- Standardize envelope shape across tools

PyPI publication: ship `mcp-events 0.1.0` to PyPI before merging any kicad-mcp PR that adds `mcp-events` as a dependency (so CI's `uv sync --frozen` resolves it, and so users following `AGENT-INSTALL.md` can `uv sync` successfully). kicad-mcp itself stays off PyPI per [`feedback_ai_first_distribution.md`](../../../../Users/blw/.claude/projects/-Volumes-Files-claude-kicad-mcp/memory/feedback_ai_first_distribution.md).

## Deferred from v1

- **Hooks/observers** (`accumulator.add_hook(callable)`). Useful for tee-ing events to persistence in real time. Not needed in v1 because iterating `accumulator.events` at end of call is simple enough.
- **Tag/category metadata** beyond `code`. If we want hierarchical categorization later, add it.
- **Async-safe stderr fallback**. v1 uses `print(file=sys.stderr)` which is fine in practice. If we hit lock contention under heavy async, switch to a queue.
- **Pip-package promotion.** Revisit after 6+ months of use across kicad-mcp and freecad-mcp.
- **Cross-process event aggregation** (one session's events visible in another). Out of scope; per-MCP persistence layers can do this themselves if needed.
- **Structured `context` schemas.** v1 treats `context` as opaque JSON. v2 could define schemas per `code`.
- **Drift-detection tooling** for vendored copies. Manual sync is fine for v1; automate if drift becomes a recurring problem.

## Open questions (decisions to make at implementation time)

1. **Nested `event_context` semantics.** If a tool wrapped with `@with_events()` calls a helper that also uses `with event_context()`, do the helper's events accumulate into the outer context, or are they isolated? **Proposal: nested contexts accumulate into the outer** (so deeply-nested helpers don't lose their events). The inner context is a no-op if an outer is active. Confirm at implementation.
2. **Event ordering.** Are events emitted in chronological order in the envelope? **Proposal: yes**, list order = emission order. Trivial to maintain.
3. **Decorator + sync-vs-async tools.** FastMCP supports both. `@with_events()` needs to work on both. **Proposal: detect at decoration time** via `asyncio.iscoroutinefunction`; produce a sync or async wrapper accordingly.
4. **`Event` JSON shape stability.** The shape documented above is the wire contract. Adding fields is allowed; renaming/removing is breaking. **Proposal: document this in the file header.**

## Hand-off summary

For an implementer picking this up cold:

1. Read this SPEC end-to-end.
2. Read the two feedback memories (`feedback_context_frugality.md`, `feedback_synchronous_at_call_boundary.md`) for design philosophy.
3. **Create the `mcp-events` package** at `/Volumes/Files/claude/mcp-events/`:
   - `pyproject.toml` with hatchling or uv-build backend; package name `mcp-events`; module name `mcp_events`; version `0.1.0`; no runtime deps; Python `>=3.11`
   - `src/mcp_events/__init__.py` with the full public API per this SPEC
   - `tests/test_mcp_events.py` with the canonical test suite
   - `README.md` explaining what the package is, the API, and the distribution model
   - `git init`, initial commit; ready to push to `blwfish/mcp-events` when the GitHub repo is created (out of scope here)
4. **Wire into kicad-mcp** (same PR or a follow-up, your call):
   - Add `mcp-events>=0.1.0` to `pyproject.toml` dependencies
   - Add the `[tool.uv.sources]` block pointing to the local path with `editable = true`
   - Run `uv sync` to update the lockfile
   - Do NOT wire `@with_events()` into any specific tool yet — telemetry is the first consumer and it doesn't exist yet
5. Do NOT touch freecad-mcp in this PR. That's a follow-up after the canonical package lands.
6. Update `MEMORY.md` and `project_oob_events.md` to mark this implemented when the PR merges; note any deviations.

Tool count after this PR: no change (it's a dependency, not a new tool). Test count target in `mcp-events` repo: ~15-20 tests. kicad-mcp's test count is unchanged.

Implementation order beyond this SPEC:

```
1. OOB Events Subsystem        ← this SPEC
2. Feedback Infrastructure     ← SPEC_Feedback_Infrastructure.md (ready)
3. Component Intelligence      ← memory file's spec needs revision based on jlcparts findings
4. Schematic Auto-Placement    ← spec session ongoing; not started
```
