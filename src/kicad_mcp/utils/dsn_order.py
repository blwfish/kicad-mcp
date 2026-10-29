"""Deterministic ordering for the Specctra DSN handed to FreeRouter.

FreeRouter 2.2.3 is a pure function of its input file: the same DSN gives a
byte-identical SES every time (measured, 8 runs).  The run-to-run variation in
routing outcome comes entirely from the DSN itself: pcbnew's
``ExportSpecctraDSN`` emits components, nets, images and pins in an order that
changes on every export of the *same* board (placements and net membership are
identical; only the order differs), and FreeRouter's result depends on that
order.

:func:`reorder_dsn` removes that accident.  The order becomes a pure function
of (board content, ``seed``):

* ``seed=None`` -- sorted ("canonical") order.
* ``seed=k`` -- a content-keyed permutation of the same groups; the same
  (board, k) always yields the same file.  Best-of-N passes use seeds
  ``None, 1, 2, ...`` so the ensemble is fixed, not luck.

Only the order of *unordered* DSN collections changes: the children of
``placement``/``library``/``network``/``wiring``, the ``place`` entries of a
``component``, the ``pins`` of a net and the net-name atoms of a ``class``.
Everything inside an item (coordinates, rules, padstacks) is preserved.

The helper is pure and has no KiCad dependency.  On any parse problem it
returns ``None`` so the caller can fall back to the unmodified DSN and say so;
it never raises on malformed input.
"""

from __future__ import annotations

import hashlib
import re

Node = str | list["Node"]

# A quoted string, an open/close paren, or a bare atom.  DSN strings carry no
# escapes; the quote character is declared by ``(string_quote ")``.
_TOKEN = re.compile(r'\s*("[^"]*"|\(|\)|[^\s()"]+)')
_QUOTE_DECL = '(string_quote ")'
_QUOTE_PLACEHOLDER = "(string_quote __DSNQUOTE__)"

# Heads whose list children (after the head) form an unordered collection.
_UNORDERED_CHILDREN = frozenset({"placement", "library", "network", "wiring"})


def _parse(text: str) -> Node:
    toks = _TOKEN.findall(text)
    pos = 0

    def node() -> Node:
        nonlocal pos
        tok: str = toks[pos]
        pos += 1
        if tok != "(":
            return tok
        out: list[Node] = []
        while toks[pos] != ")":
            out.append(node())
        pos += 1
        return out

    root: Node = node()
    if pos != len(toks):
        raise ValueError("trailing tokens after the root form")
    return root


def _flat(n: Node) -> str:
    return n if isinstance(n, str) else "(" + " ".join(_flat(c) for c in n) + ")"


def _fmt(n: Node, depth: int = 0) -> str:
    """Flat for deep forms; near the top, leading atoms inline and one sub-list
    per line (readable diffs, and ``(net A`` stays together)."""
    if isinstance(n, str) or depth >= 3 or not any(isinstance(c, list) for c in n):
        return _flat(n)
    lead = 0
    while lead < len(n) and isinstance(n[lead], str):
        lead += 1
    pad = "  " * (depth + 1)
    return "(" + " ".join(_flat(c) for c in n[:lead]) + "".join(
        "\n" + pad + _fmt(c, depth + 1) for c in n[lead:]
    ) + ")"


def _arrange(items: list[Node], seed: int | None) -> list[Node]:
    """Canonical sort, then (for a seed) a content-keyed permutation."""
    ordered = sorted(items, key=_flat)
    if seed is None:
        return ordered

    def rank(item: Node) -> str:
        return hashlib.sha256(f"{seed}:{_flat(item)}".encode()).hexdigest()

    return sorted(ordered, key=rank)


def _reorder(n: Node, parent: str | None, seed: int | None) -> Node:
    if isinstance(n, str):
        return n
    head = n[0] if n and isinstance(n[0], str) else None
    kids = [_reorder(c, head, seed) for c in n]
    if head in _UNORDERED_CHILDREN:
        return kids[:1] + _arrange(kids[1:], seed)
    if head == "component":  # (component NAME (place ...) ...)
        return kids[:2] + _arrange(kids[2:], seed)
    if head == "pins":  # (pins U1-1 U1-2 ...)
        return kids[:1] + _arrange(kids[1:], seed)
    if head == "class" and parent == "network":  # (class NAME net... (rule ...))
        atoms: list[Node] = [k for k in kids[2:] if isinstance(k, str)]
        rest: list[Node] = [k for k in kids[2:] if not isinstance(k, str)]
        return kids[:2] + _arrange(atoms, seed) + rest
    return kids


def reorder_dsn(text: str, seed: int | None = None) -> str | None:
    """Return *text* with its unordered collections arranged deterministically.

    ``None`` means the DSN could not be parsed; the caller keeps the original.
    """
    if not text.startswith("(pcb"):
        return None
    # ``(pcb <path-to-this-file>`` carries a per-export temp path; normalize it.
    body = text.replace(_QUOTE_DECL, _QUOTE_PLACEHOLDER)
    try:
        tree = _parse(body)
    except (ValueError, IndexError):
        return None
    if not isinstance(tree, list) or not tree or tree[0] != "pcb":
        return None
    if len(tree) > 1 and isinstance(tree[1], str):
        tree[1] = "board"
    else:
        tree.insert(1, "board")
    out = _fmt(_reorder(tree, None, seed)).replace("__DSNQUOTE__", '"', 1)
    return out + "\n"
