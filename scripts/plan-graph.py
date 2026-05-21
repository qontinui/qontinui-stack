#!/usr/bin/env python3
"""Render the dependency DAG across all plans.

Phase 3.3 of the 2026-05-21 coordination-improvements plan. Walks every
``*.md`` plan in ``plans/`` + ``qontinui-dev-notes/plans/`` (env-overridable
per ``resolve-plan-deps.py``), builds a directed graph of ``Depends-On:``
edges, and renders it as a text-tree, JSON, or Mermaid graph.

Edges
-----
An edge ``A -> B`` means "A depends on B" (B must ship before A). Plans
named in another plan's ``Depends-On:`` field but not present on disk are
rendered as ``[MISSING]`` leaf nodes and surfaced in the JSON ``missing``
field. Cycles (``a -> b -> a``) are flagged in the JSON ``cycles`` field
but do not crash the renderer.

Usage
-----

    $ python scripts/plan-graph.py [--root <stem>] [--format text|json|mermaid]
                                   [--include-shipped]

Output formats:
    text    Indented tree, one node per line (default).
    json    Node + edge list with cycle/missing metadata for tooling.
    mermaid ``graph TD;`` syntax suitable for pasting into mermaid.live.

Subgraph selection:
    --root <stem>  Restrict output to the subgraph reachable from <stem>
                   (downward = its deps + transitive deps).

Hiding terminal nodes:
    By default, SHIPPED + SUPERSEDED + OBSOLETE leaves are hidden so the
    operator sees only what's still in motion. Use ``--include-shipped`` to
    show everything. Non-leaf shipped nodes are always rendered because
    they're structurally on the path to a non-shipped descendant.

Environment overrides (shared with ``resolve-plan-deps.py``):
    QONTINUI_PLANS_DIR          (default ``D:/qontinui-root/plans``)
    QONTINUI_PLANS_ARCHIVE_DIR  (default ``D:/qontinui-root/qontinui-dev-notes/plans``)

Re-uses parser primitives (``find_status_block``, ``parse_depends_on``,
``extract_lifecycle``) from ``resolve-plan-deps.py`` (Phase 3.2) via direct
spec-from-file-location import — the sibling script lives in the same dir.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PLANS_DIR = "D:/qontinui-root/plans"
DEFAULT_ARCHIVE_DIR = "D:/qontinui-root/qontinui-dev-notes/plans"

# Lifecycle words that mean the node is "done" for rendering-purposes. When
# such a node is also a leaf (no in-graph descendants), the default render
# hides it unless ``--include-shipped`` was passed.
HIDDEN_LEAF_STATUSES: frozenset[str] = frozenset(
    {"SHIPPED", "SUPERSEDED", "OBSOLETE"}
)

# Status placeholder for nodes whose blockquote was malformed.
UNKNOWN_STATUS = "?"

# Status placeholder for declared-but-missing dep stems.
MISSING_STATUS = "MISSING"


# ---------------------------------------------------------------------------
# Load the Phase 3.2 parser as a module
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_RPD_PATH = _THIS_DIR / "resolve-plan-deps.py"


def _load_rpd():
    """Import ``resolve-plan-deps.py`` (hyphenated → spec_from_file)."""
    spec = importlib.util.spec_from_file_location(
        "resolve_plan_deps", _RPD_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load resolver module at {_RPD_PATH}")
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so ``@dataclass``'s sys.modules lookup succeeds
    # under ``from __future__ import annotations``.
    sys.modules["resolve_plan_deps"] = mod
    spec.loader.exec_module(mod)
    return mod


_rpd = _load_rpd()
find_status_block = _rpd.find_status_block
parse_depends_on = _rpd.parse_depends_on
extract_lifecycle = _rpd.extract_lifecycle


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Node:
    stem: str
    status: str  # lifecycle word, ``MISSING``, or ``?``
    location: str | None  # plan file path (forward-slashes) or None for missing


@dataclass
class Graph:
    nodes: dict[str, Node] = field(default_factory=dict)
    # ``edges[a]`` = set of stems b such that "a depends on b".
    edges: dict[str, set[str]] = field(default_factory=dict)
    # Stems that other plans depend on but which have no file on disk.
    missing: set[str] = field(default_factory=set)
    # Detected cycles. Each cycle is a list of stems ending where it started:
    # e.g. ``[a, b, c, a]``.
    cycles: list[list[str]] = field(default_factory=list)

    def ensure_node(
        self,
        stem: str,
        status: str,
        location: str | None,
    ) -> None:
        if stem not in self.nodes:
            self.nodes[stem] = Node(stem=stem, status=status, location=location)
            self.edges.setdefault(stem, set())

    def add_edge(self, from_stem: str, to_stem: str) -> None:
        self.edges.setdefault(from_stem, set()).add(to_stem)

    def predecessors(self, stem: str) -> set[str]:
        """Return stems that depend on ``stem``."""
        return {a for a, deps in self.edges.items() if stem in deps}


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


def _iter_plan_files(plans_dir: Path, archive_dir: Path) -> Iterable[Path]:
    seen_stems: set[str] = set()
    # Walk in-progress plans first so they take precedence on stem collisions
    # (matches resolve-plan-deps.py lookup order).
    for d in (plans_dir, archive_dir):
        if not d.exists():
            continue
        for path in sorted(d.glob("*.md")):
            stem = path.stem
            if stem in seen_stems:
                continue
            seen_stems.add(stem)
            yield path


def _parse_plan(path: Path) -> tuple[str, str, list[str]]:
    """Return (stem, status, depends_on_stems) for a plan file.

    Malformed status block → status = ``UNKNOWN_STATUS``, deps = ``[]``.
    """
    stem = path.stem
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return stem, UNKNOWN_STATUS, []
    block = find_status_block(text)
    if block is None:
        # No status block at all — treat as DRAFT-equivalent. Matches the
        # resolve-plan-deps.py convention.
        return stem, "DRAFT", []
    lifecycle, _summary = extract_lifecycle(block)
    status = lifecycle or UNKNOWN_STATUS
    deps = parse_depends_on(block)
    return stem, status, deps


def build_graph(plans_dir: Path, archive_dir: Path) -> Graph:
    """Walk both plan dirs, build a Graph with edges, missing, and cycles."""
    graph = Graph()

    # First pass: register every on-disk plan as a node.
    plan_paths: dict[str, Path] = {}
    for path in _iter_plan_files(plans_dir, archive_dir):
        plan_paths[path.stem] = path

    # Second pass: parse each plan, register the node + edges. Missing deps
    # become MISSING placeholder nodes.
    for stem, path in plan_paths.items():
        plan_stem, status, deps = _parse_plan(path)
        graph.ensure_node(
            plan_stem,
            status,
            location=str(path).replace("\\", "/"),
        )
        for dep_stem in deps:
            if dep_stem in plan_paths:
                # Dep is itself a plan we'll register in this loop, but it
                # might not be visited yet — pre-register it as a stub. The
                # later iteration's ensure_node won't overwrite (existing-key
                # check), so we have to populate properly here OR rely on the
                # later visit. We rely on the later visit; for now, just add
                # the edge.
                graph.add_edge(plan_stem, dep_stem)
            else:
                # Missing dep — register a MISSING placeholder node.
                graph.ensure_node(dep_stem, MISSING_STATUS, location=None)
                graph.missing.add(dep_stem)
                graph.add_edge(plan_stem, dep_stem)

    # If a plan's stem appears as a dep BEFORE its file was visited in pass 2,
    # the placeholder ensure_node above may have stored it as MISSING. Fix
    # this by re-running ensure_node for every plan_paths entry — ensure_node
    # only inserts on missing keys, so we replace stubs here.
    for stem, path in plan_paths.items():
        plan_stem, status, _deps = _parse_plan(path)
        if (
            plan_stem in graph.nodes
            and graph.nodes[plan_stem].status == MISSING_STATUS
        ):
            # This shouldn't happen given the second-pass order above (we
            # always ensure_node BEFORE we add edges), but be defensive.
            graph.nodes[plan_stem] = Node(
                stem=plan_stem,
                status=status,
                location=str(path).replace("\\", "/"),
            )

    # Strip cycle-only "missing" entries: a stem is only "missing" if it has
    # no on-disk file. The above logic already excludes plan_paths members
    # via the if/else. Sanity-check.
    graph.missing = {s for s in graph.missing if s not in plan_paths}

    # Cycle detection: DFS with a visited+stack tracking.
    graph.cycles = _detect_cycles(graph)
    return graph


def _detect_cycles(graph: Graph) -> list[list[str]]:
    """DFS-based cycle detection.

    Returns a list of cycles. Each cycle is a path ``[a, b, c, a]`` such
    that a -> b -> c -> a are all edges in ``graph``. Cycles are deduped on
    their rotation-invariant canonical form (smallest-stem-first).
    """
    cycles: list[list[str]] = []
    seen_signatures: set[tuple[str, ...]] = set()

    def canonical(cycle: list[str]) -> tuple[str, ...]:
        # cycle is [..., start] where start == cycle[0]. Rotate so smallest
        # stem leads, drop the closing duplicate.
        loop = cycle[:-1]
        if not loop:
            return tuple(cycle)
        min_i = loop.index(min(loop))
        rotated = loop[min_i:] + loop[:min_i]
        return tuple(rotated)

    stack: list[str] = []
    in_stack: set[str] = set()
    visited: set[str] = set()

    def dfs(node: str) -> None:
        stack.append(node)
        in_stack.add(node)
        for nbr in sorted(graph.edges.get(node, ())):
            if nbr in in_stack:
                # Found a back-edge. Extract the cycle from the stack.
                start_idx = stack.index(nbr)
                cycle = stack[start_idx:] + [nbr]
                sig = canonical(cycle)
                if sig not in seen_signatures:
                    seen_signatures.add(sig)
                    cycles.append(cycle)
            elif nbr not in visited:
                dfs(nbr)
        stack.pop()
        in_stack.discard(node)
        visited.add(node)

    for stem in sorted(graph.nodes):
        if stem not in visited:
            dfs(stem)
    return cycles


# ---------------------------------------------------------------------------
# Subgraph selection
# ---------------------------------------------------------------------------


def restrict_to_root(graph: Graph, root: str) -> Graph:
    """Return a graph containing only ``root`` + its transitive deps.

    If ``root`` is not in the graph, returns an empty graph plus a MISSING
    placeholder for the requested root so the renderer can surface the typo.
    """
    sub = Graph()
    if root not in graph.nodes:
        sub.ensure_node(root, MISSING_STATUS, location=None)
        sub.missing.add(root)
        return sub

    # BFS downward (dependencies).
    queue: list[str] = [root]
    seen: set[str] = set()
    while queue:
        cur = queue.pop()
        if cur in seen:
            continue
        seen.add(cur)
        src_node = graph.nodes.get(cur)
        if src_node is not None:
            sub.ensure_node(src_node.stem, src_node.status, src_node.location)
        else:
            sub.ensure_node(cur, MISSING_STATUS, location=None)
            sub.missing.add(cur)
        for nbr in graph.edges.get(cur, ()):
            sub.add_edge(cur, nbr)
            queue.append(nbr)

    # Carry over any cycles fully contained inside the subgraph.
    sub.cycles = [
        c for c in graph.cycles if all(stem in seen for stem in c[:-1])
    ]
    return sub


# ---------------------------------------------------------------------------
# Rendering: filtering for default --include-shipped=false
# ---------------------------------------------------------------------------


def _filter_visible(graph: Graph, include_shipped: bool) -> set[str]:
    """Return the set of stems that should appear in the default render.

    Hide rule: a node is hidden if its status is in ``HIDDEN_LEAF_STATUSES``
    AND it has no descendant that is itself visible. Equivalently: a node is
    visible if it has a non-shipped descendant (transitively) OR it is itself
    non-shipped/missing.

    With ``--include-shipped=True``, return every node.
    """
    if include_shipped:
        return set(graph.nodes.keys())

    # Walk the graph from "anchor" nodes (non-hidden status) upward. Any node
    # that is an ancestor of an anchor stays visible. A node with no path to
    # an anchor is dropped.
    anchors: set[str] = {
        stem
        for stem, node in graph.nodes.items()
        if node.status not in HIDDEN_LEAF_STATUSES
    }

    # Pre-compute reverse edges so we can walk ancestors.
    reverse: dict[str, set[str]] = {stem: set() for stem in graph.nodes}
    for src, dests in graph.edges.items():
        for d in dests:
            reverse.setdefault(d, set()).add(src)

    # We want to keep every node that is either an anchor itself or has any
    # descendant that's an anchor. Walk DOWN from each node and check if it
    # reaches an anchor — but that's O(N^2). Faster: do reverse BFS from
    # anchors to mark every ancestor as visible, AND also mark every
    # descendant of an anchor as visible (downward BFS), so SHIPPED nodes
    # on the path *from* an anchor *to* a leaf still render if they're a
    # transitive dep of an anchor.
    visible: set[str] = set()

    # All anchors themselves visible.
    visible.update(anchors)

    # Reverse-BFS (ancestors).
    queue: list[str] = list(anchors)
    while queue:
        cur = queue.pop()
        for parent in reverse.get(cur, ()):
            if parent not in visible:
                visible.add(parent)
                queue.append(parent)

    # Forward-BFS (descendants of anchors). This catches the case where an
    # anchor depends on a SHIPPED node — the SHIPPED dep is part of the chain
    # the operator wants to see ("X is blocked on Y which is already shipped,
    # so X is unblocked-pending-no-other-deps"). Without this, the render
    # would show the anchor with "(no deps)" even though its SHIPPED dep is
    # right there.
    queue = list(anchors)
    while queue:
        cur = queue.pop()
        for child in graph.edges.get(cur, ()):
            if child not in visible:
                visible.add(child)
                queue.append(child)

    return visible


# ---------------------------------------------------------------------------
# Rendering: text tree
# ---------------------------------------------------------------------------


def _supports_unicode() -> bool:
    """Heuristic for whether stdout can render Unicode box-drawing.

    Per ``feedback_native_git_stderr_ps51`` + Phase 3.2's cp1252 mojibake
    note, Windows consoles default to cp1252 which can't encode ``├``. We
    upgrade stdout to UTF-8 in ``main()`` below, so this returns True after
    that reconfigure. But if reconfigure failed (very old Python), fall back
    to ASCII.
    """
    enc = getattr(sys.stdout, "encoding", None) or ""
    return enc.lower().replace("-", "") in {"utf8", "utf16le", "utf16"}


def _render_text(
    graph: Graph,
    *,
    include_shipped: bool,
    unicode_ok: bool | None = None,
) -> str:
    visible = _filter_visible(graph, include_shipped)
    if unicode_ok is None:
        unicode_ok = _supports_unicode()

    if unicode_ok:
        tee, elbow, pipe, blank = "├── ", "└── ", "│   ", "    "
    else:
        tee, elbow, pipe, blank = "+-- ", "`-- ", "|   ", "    "

    # Roots = visible nodes with no visible predecessors.
    visible_nodes = [n for n in graph.nodes.values() if n.stem in visible]
    roots = sorted(
        [
            n.stem
            for n in visible_nodes
            if not any(p in visible for p in graph.predecessors(n.stem))
        ]
    )

    if not roots and not visible_nodes:
        return "(no plans matched)"
    if not roots:
        # Every node has a predecessor → everything is part of a cycle.
        # Pick the lexicographically smallest stem as the entry point so the
        # operator can still see the graph.
        roots = sorted(visible)[:1]

    out_lines: list[str] = []
    rendered_nodes: set[str] = set()

    def render_node(stem: str, prefix: str, is_last: bool, depth: int) -> None:
        connector = "" if depth == 0 else (elbow if is_last else tee)
        label = _node_label(graph, stem)
        out_lines.append(f"{prefix}{connector}{label}")

        # Cycle/already-seen short-circuit: if we've already expanded this
        # node's children somewhere, don't recurse again (prevents infinite
        # loops on cycles + reduces duplication on diamonds).
        if stem in rendered_nodes:
            if graph.edges.get(stem):
                cont = "" if depth == 0 else (blank if is_last else pipe)
                out_lines.append(f"{prefix}{cont}{elbow}(see above)")
            return
        rendered_nodes.add(stem)

        children = sorted(
            d for d in graph.edges.get(stem, ()) if d in visible
        )
        if not children:
            if graph.nodes.get(stem) and graph.nodes[stem].status != MISSING_STATUS:
                cont = "" if depth == 0 else (blank if is_last else pipe)
                out_lines.append(f"{prefix}{cont}{elbow}(no deps)")
            return

        child_prefix = prefix + ("" if depth == 0 else (blank if is_last else pipe))
        for i, child in enumerate(children):
            last = i == len(children) - 1
            render_node(child, child_prefix, last, depth + 1)

    for i, root in enumerate(roots):
        render_node(root, prefix="", is_last=(i == len(roots) - 1), depth=0)

    if graph.cycles:
        out_lines.append("")
        out_lines.append(f"WARN: {len(graph.cycles)} cycle(s) detected:")
        for cycle in graph.cycles:
            out_lines.append("  " + " -> ".join(cycle))

    if graph.missing:
        out_lines.append("")
        out_lines.append(
            f"WARN: {len(graph.missing)} declared dep(s) have no plan file:"
        )
        for stem in sorted(graph.missing):
            out_lines.append(f"  {stem}")

    return "\n".join(out_lines)


def _node_label(graph: Graph, stem: str) -> str:
    node = graph.nodes.get(stem)
    if node is None:
        return f"{stem}  [{MISSING_STATUS}]"
    return f"{stem}  [{node.status}]"


# ---------------------------------------------------------------------------
# Rendering: JSON
# ---------------------------------------------------------------------------


def _render_json(graph: Graph) -> str:
    payload = {
        "nodes": [
            {
                "stem": n.stem,
                "status": n.status,
                "location": n.location,
            }
            for n in sorted(graph.nodes.values(), key=lambda x: x.stem)
        ],
        "edges": sorted(
            (
                {"from": src, "to": dst}
                for src, dests in graph.edges.items()
                for dst in dests
            ),
            key=lambda e: (e["from"], e["to"]),
        ),
        "missing": sorted(graph.missing),
        "cycles": [list(c) for c in graph.cycles],
    }
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Rendering: Mermaid
# ---------------------------------------------------------------------------


_MERMAID_SAFE_PREFIX = "n_"


def _mermaid_id(stem: str) -> str:
    """Mermaid node ids must be alphanumeric + underscore.

    Plan stems contain hyphens (``2026-05-21-foo``), so we replace
    non-alphanumeric chars with underscores and prefix to avoid leading-digit
    issues.
    """
    safe = "".join(c if c.isalnum() else "_" for c in stem)
    return f"{_MERMAID_SAFE_PREFIX}{safe}"


def _render_mermaid(graph: Graph) -> str:
    lines: list[str] = ["graph TD;"]
    for stem in sorted(graph.nodes):
        node = graph.nodes[stem]
        label = f"{stem}<br/>{node.status}"
        # Wrap label in quoted-string syntax so HTML-style <br/> renders.
        lines.append(f'  {_mermaid_id(stem)}["{label}"];')
    for src in sorted(graph.edges):
        for dst in sorted(graph.edges[src]):
            lines.append(f"  {_mermaid_id(src)} --> {_mermaid_id(dst)};")
    if graph.cycles:
        lines.append("")
        lines.append("%% CYCLES DETECTED:")
        for cycle in graph.cycles:
            lines.append("%%   " + " -> ".join(cycle))
    if graph.missing:
        lines.append("")
        lines.append("%% MISSING DEPS:")
        for stem in sorted(graph.missing):
            lines.append(f"%%   {stem}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _resolve_dirs(args: argparse.Namespace) -> tuple[Path, Path]:
    plans_dir_raw = (
        args.plans_dir
        or os.environ.get("QONTINUI_PLANS_DIR")
        or DEFAULT_PLANS_DIR
    )
    archive_dir_raw = (
        args.archive_dir
        or os.environ.get("QONTINUI_PLANS_ARCHIVE_DIR")
        or DEFAULT_ARCHIVE_DIR
    )
    plans_dir = Path(plans_dir_raw).expanduser().resolve()
    archive_dir = Path(archive_dir_raw).expanduser().resolve()
    return plans_dir, archive_dir


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build + render the Depends-On DAG across all plans."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help=(
            "Restrict to the subgraph reachable from <stem> (downward = its "
            "transitive deps). Default: full graph."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json", "mermaid"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--include-shipped",
        action="store_true",
        default=False,
        help=(
            "Include SHIPPED + SUPERSEDED + OBSOLETE leaves. By default, "
            "these are hidden so the render focuses on what's still in "
            "motion."
        ),
    )
    parser.add_argument(
        "--plans-dir",
        default=None,
        help="Override in-progress plans dir (env: QONTINUI_PLANS_DIR).",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="Override shipped archive dir (env: QONTINUI_PLANS_ARCHIVE_DIR).",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        default=False,
        help=(
            "Force ASCII box-drawing for text output (default: Unicode "
            "when stdout supports it). Useful for piping to non-UTF8 sinks."
        ),
    )

    args = parser.parse_args(list(argv) if argv is not None else None)

    plans_dir, archive_dir = _resolve_dirs(args)

    # Reconfigure stdout to UTF-8 BEFORE building the graph so Unicode
    # detection in _supports_unicode picks up the upgraded encoding.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    graph = build_graph(plans_dir, archive_dir)
    if args.root:
        graph = restrict_to_root(graph, args.root)

    if args.format == "json":
        print(_render_json(graph))
    elif args.format == "mermaid":
        print(_render_mermaid(graph))
    else:
        unicode_ok = None if not args.ascii else False
        print(
            _render_text(
                graph,
                include_shipped=args.include_shipped,
                unicode_ok=unicode_ok,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
