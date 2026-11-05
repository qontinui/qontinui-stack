#!/usr/bin/env python3
"""Resolve a plan's ``Depends-On:`` field to upstream-plan status.

Phase 3.2 of the 2026-05-21 coordination-improvements plan. Implements the
parser + lookup procedure described inline in
``qontinui-claude-config/.claude/commands/implement-plan.md`` Step 0.4 (and
``vet-plan.md`` Step 4 / ``verify-plan-status.md``). Centralising it here
keeps the rule in one place so the skill consumers can shell out instead of
re-implementing the parse inline.

Usage
-----

    $ python scripts/resolve-plan-deps.py <plan-path>           # JSON
    $ python scripts/resolve-plan-deps.py <plan-path> --json    # JSON (explicit)
    $ python scripts/resolve-plan-deps.py <plan-path> --human   # text summary

Exit codes:
    0   all declared deps are SHIPPED (or no Depends-On field)
    1   at least one dep is unsatisfied (missing, not-yet-shipped, terminal)
    2   input error (path missing, not a file, plans dir missing, etc.)

Environment overrides:
    QONTINUI_PLANS_DIR          active plans dir
                                (fallback: ``<workspace-root>/plans``)
    QONTINUI_PLANS_ARCHIVE_DIR  optional archive directory — a real
                                destination for archived plans; unset means
                                single-directory layout (no default)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The scripts live at ``<workspace-root>/qontinui-stack/scripts/``, so the
# workspace root is two parents up from this file's directory. The fallback
# plans dir matches the coord ``plan-discipline`` policy: ``QONTINUI_PLANS_DIR``
# falls back to ``<workspace-root>/plans`` when unset.
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PLANS_DIR = WORKSPACE_ROOT / "plans"

# The stamp parse owner. A byte-identical copy of qontinui-claude-config
# ``scripts/lib/plan_stamp.py`` sits in ``scripts/lib/`` beside this file (its
# provenance is ``scripts/lib/plan_stamp.SOURCE``; ``tests/test_resolve_plan_deps.py``
# compares the bytes when the sibling checkout is present). Vendored rather than
# imported across repos because ``/plan-graph`` runs this file from a
# qontinui-stack checkout that may have no qontinui-claude-config beside it.
# Plan 2026-10-30-four-stamp-readers-left-out-of-scope-still-miss-plain-stamps
# Phase 3: every stamp shape the owner reads (blockquote, plain, list-form,
# run-on, lower-case) reads the same here.
_PLAN_STAMP_PATH = Path(__file__).resolve().parent / "lib" / "plan_stamp.py"


def _load_plan_stamp():
    spec = importlib.util.spec_from_file_location("plan_stamp", _PLAN_STAMP_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the stamp parse owner at {_PLAN_STAMP_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plan_stamp = _load_plan_stamp()

UNSATISFIED_NOT_SHIPPED: frozenset[str] = frozenset(
    {"DRAFT", "VETTED", "IN PROGRESS", "PARTIAL", "NOT STARTED"}
)
UNSATISFIED_TERMINAL: frozenset[str] = frozenset({"SUPERSEDED", "OBSOLETE"})


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class DepResolution:
    stem: str
    status: str | None  # lifecycle word, or "MISSING"
    location: str | None
    summary: str | None


@dataclass
class Resolution:
    plan_stem: str
    depends_on: list[DepResolution] = field(default_factory=list)
    all_satisfied: bool = True
    unsatisfied: list[dict[str, str]] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "plan_stem": self.plan_stem,
            "depends_on": [
                {
                    "stem": d.stem,
                    "status": d.status,
                    "location": d.location,
                    "summary": d.summary,
                }
                for d in self.depends_on
            ],
            "all_satisfied": self.all_satisfied,
            "unsatisfied": list(self.unsatisfied),
        }


class InputError(Exception):
    """Raised for non-recoverable input problems → exit code 2."""


# ---------------------------------------------------------------------------
# Directory resolution (shared with plan-graph.py)
# ---------------------------------------------------------------------------


def resolve_plans_dir(cli_value: str | None) -> Path:
    """Resolve the active plans dir: CLI flag → env → workspace fallback.

    Precedence: ``cli_value`` → ``QONTINUI_PLANS_DIR`` →
    ``<workspace-root>/plans``. Raises ``InputError`` (→ exit code 2) when
    the resolved directory does not exist — never silently render an empty
    result against a phantom directory.
    """
    raw = (
        cli_value
        or os.environ.get("QONTINUI_PLANS_DIR")
        or str(DEFAULT_PLANS_DIR)
    )
    plans_dir = Path(raw).expanduser().resolve()
    if not plans_dir.is_dir():
        # ASCII-only message: this prints to stderr BEFORE the UTF-8
        # reconfigure, so an em-dash would mojibake on cp1252 consoles.
        raise InputError(
            f"plans dir does not exist: {plans_dir} - set QONTINUI_PLANS_DIR "
            "(or --plans-dir) to your active plans directory"
        )
    return plans_dir


def resolve_archive_dir(cli_value: str | None) -> Path | None:
    """Resolve the optional archive directory — a real destination for
    archived plans; unset means single-directory layout.

    Precedence: ``cli_value`` → ``QONTINUI_PLANS_ARCHIVE_DIR`` → ``None``
    (no default). An explicitly-empty env var counts as unset.
    """
    raw = cli_value or os.environ.get("QONTINUI_PLANS_ARCHIVE_DIR")
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc


def find_status_block(text: str) -> str | None:
    """The plan's status block as the parse owner locates it, or ``None``.

    Marker-stripped and space-joined (``plan_stamp.status_block``). Any stamp
    shape the owner reads counts -- a plain ``**Status:** SHIPPED`` stamp is a
    block here too, where the old ``> **Status:``-only scan returned ``None``
    and the plan read as DRAFT.
    """
    return plan_stamp.status_block(text, body_first=True)


def parse_depends_on(status_block: str) -> list[str]:
    """``Depends-On:`` stems on the physical lines of ``status_block``.

    The shared rule (``plan_stamp.depends_on_from_lines``): every
    ``Depends-On:`` occurrence, only the rest of its own physical line, only
    date-prefixed stem tokens, unioned in order. A whole plan is read with
    ``read_plan_stamp`` instead, which also covers a ``Depends-On:`` line
    below the stamp's own block.
    """
    return plan_stamp.depends_on_from_lines(status_block.splitlines())


def _summary_after(block: str, word: str) -> str | None:
    idx = block.upper().find(word.upper()) if word else -1
    after = block[idx + len(word):] if idx >= 0 else block
    summary = " ".join(after.split())
    # Remove a leading date stamp like "2026-05-21." that often follows the
    # lifecycle word, then the bold close and punctuation noise.
    summary = re.sub(r"^\d{4}-\d{2}-\d{2}\.?\s*", "", summary)
    summary = re.sub(r"^\*+\s*", "", summary)
    summary = summary.lstrip(".:- ").strip()
    return summary or None


def extract_lifecycle(status_block: str) -> tuple[str | None, str | None]:
    """Return ``(lifecycle_word, summary)`` for a status block.

    The word is the parse owner's TOKEN, upper-cased (``SHIPPED``,
    ``IN PROGRESS``, ``OBSOLETE`` for a lower-case ``obsolete`` stamp,
    ``COMPLETE`` for a ``COMPLETE`` stamp). It is read from the stamp line
    itself, never searched for anywhere in the block: the old vocabulary
    search returned a later ``IN PROGRESS`` history line for a ``COMPLETE``
    stamp. ``None`` when the block holds no stamp the owner reads.
    """
    stamp = plan_stamp.read_stamp(status_block)
    if stamp is None or not stamp[0]:
        return None, None
    word = stamp[0].upper()
    return word, _summary_after(stamp[1], word)


def read_plan_stamp(text: str) -> tuple[str | None, str | None, list[str]] | None:
    """``(lifecycle_word, summary, depends_on)`` for a whole plan, or ``None``.

    ``None`` means the owner found no stamp in its window. ``lifecycle_word``
    is ``None`` when a stamp line carries no word after its key.
    """
    stamp = plan_stamp.read_stamp(text)
    if stamp is None:
        return None
    word = stamp[0].upper() or None
    deps = plan_stamp.read_depends_on(text) or []
    return word, (_summary_after(stamp[1], word) if word else None), deps


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _candidate_paths(
    stem: str, plans_dir: Path, archive_dir: Path | None
) -> list[Path]:
    # The optional archive directory is a real destination for archived
    # plans: a dep lookup falls back to it only when it is configured.
    # Unset means single-directory layout. Dedup so an archive that equals
    # the plans dir isn't stat'd twice.
    dirs = [plans_dir] if archive_dir is None else [plans_dir, archive_dir]
    seen: list[Path] = []
    for d in dirs:
        p = d / f"{stem}.md"
        if p not in seen:
            seen.append(p)
    return seen


def resolve_dep(
    stem: str,
    plans_dir: Path,
    archive_dir: Path | None,
) -> DepResolution:
    """Resolve a single dep stem to a status + location."""
    for candidate in _candidate_paths(stem, plans_dir, archive_dir):
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                # Treat unreadable-but-existing as missing for status-gate
                # purposes — caller will mark unsatisfied.
                return DepResolution(
                    stem=stem,
                    status="MISSING",
                    location=str(candidate).replace("\\", "/"),
                    summary=None,
                )
            stamp = read_plan_stamp(text)
            if stamp is None:
                # Per skill spec: "A plan with no status blockquote at all
                # is treated as DRAFT."
                return DepResolution(
                    stem=stem,
                    status="DRAFT",
                    location=str(candidate).replace("\\", "/"),
                    summary=None,
                )
            lifecycle, summary, _deps = stamp
            return DepResolution(
                stem=stem,
                status=lifecycle or "DRAFT",
                location=str(candidate).replace("\\", "/"),
                summary=summary,
            )
    return DepResolution(stem=stem, status="MISSING", location=None, summary=None)


# ---------------------------------------------------------------------------
# Top-level resolve
# ---------------------------------------------------------------------------


def resolve_plan(
    plan_path: Path,
    plans_dir: Path,
    archive_dir: Path | None,
) -> Resolution:
    if not plan_path.exists():
        raise InputError(f"plan file does not exist: {plan_path}")
    if not plan_path.is_file():
        raise InputError(f"not a file: {plan_path}")

    text = _read_text(plan_path)
    stamp = read_plan_stamp(text)
    plan_stem = plan_path.stem

    if stamp is None:
        # An input plan with no status block is malformed for the purposes
        # of dependency resolution — but we should still tolerate plans
        # that legitimately have no status (e.g. design notes). Treat as
        # "no deps declared" and exit 0. The skill consumer can choose to
        # enforce status-block presence separately.
        return Resolution(plan_stem=plan_stem)

    stems = stamp[2]
    resolution = Resolution(plan_stem=plan_stem)
    if not stems:
        return resolution

    for stem in stems:
        dep = resolve_dep(stem, plans_dir, archive_dir)
        resolution.depends_on.append(dep)
        if dep.status == "SHIPPED":
            continue
        # Anything else is unsatisfied.
        if dep.status == "MISSING":
            reason = "missing_file"
        elif dep.status in UNSATISFIED_TERMINAL:
            reason = "terminal_blocker"
        elif dep.status in UNSATISFIED_NOT_SHIPPED:
            reason = "not_yet_shipped"
        else:
            # Unknown lifecycle word — be conservative, treat as not-shipped.
            reason = "not_yet_shipped"
        resolution.unsatisfied.append({"stem": dep.stem, "reason": reason})

    resolution.all_satisfied = not resolution.unsatisfied
    return resolution


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _format_human(resolution: Resolution) -> str:
    out: list[str] = []
    out.append(f"Plan: {resolution.plan_stem}")
    if not resolution.depends_on:
        out.append("  (no Depends-On declared)")
        out.append("Result: all_satisfied = true")
        return "\n".join(out)
    out.append(f"  Depends-On ({len(resolution.depends_on)} dep(s)):")
    for dep in resolution.depends_on:
        loc = dep.location or "<not found>"
        status = dep.status or "?"
        summary = f" — {dep.summary}" if dep.summary else ""
        out.append(f"    - {dep.stem} [{status}] @ {loc}{summary}")
    out.append(f"Result: all_satisfied = {str(resolution.all_satisfied).lower()}")
    if resolution.unsatisfied:
        out.append("Unsatisfied:")
        for entry in resolution.unsatisfied:
            out.append(f"    - {entry['stem']}: {entry['reason']}")
    return "\n".join(out)


def _resolve_dirs(args: argparse.Namespace) -> tuple[Path, Path | None]:
    return resolve_plans_dir(args.plans_dir), resolve_archive_dir(
        args.archive_dir
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve a plan's Depends-On stems to upstream-plan status.",
    )
    parser.add_argument(
        "plan_path",
        help="Absolute path to the plan .md file to inspect.",
    )
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--json",
        dest="format",
        action="store_const",
        const="json",
        help="Emit JSON (default).",
    )
    fmt.add_argument(
        "--human",
        dest="format",
        action="store_const",
        const="human",
        help="Emit a human-readable text summary.",
    )
    parser.add_argument(
        "--plans-dir",
        default=None,
        help=(
            "Override the active plans dir (env: QONTINUI_PLANS_DIR; "
            "fallback: <workspace-root>/plans)."
        ),
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help=(
            "Optional archive directory — a real destination for archived "
            "plans; unset means single-directory layout "
            "(env: QONTINUI_PLANS_ARCHIVE_DIR)."
        ),
    )
    parser.set_defaults(format="json")

    args = parser.parse_args(list(argv) if argv is not None else None)

    plan_path = Path(args.plan_path).expanduser()
    if not plan_path.is_absolute():
        plan_path = plan_path.resolve()

    try:
        plans_dir, archive_dir = _resolve_dirs(args)
        resolution = resolve_plan(plan_path, plans_dir, archive_dir)
    except InputError as exc:
        # Print a structured error so callers can still parse stdout/stderr.
        msg = f"resolve-plan-deps: {exc}"
        if args.format == "json":
            print(json.dumps({"error": str(exc)}, indent=2))
        print(msg, file=sys.stderr)
        return 2

    # Reconfigure stdout to UTF-8 so plan summaries with `→`, `—`, etc. don't
    # explode on Windows consoles whose default code page is cp1252. Available
    # on Python >=3.7.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    if args.format == "json":
        print(json.dumps(resolution.to_json(), indent=2))
    else:
        print(_format_human(resolution))

    return 0 if resolution.all_satisfied else 1


if __name__ == "__main__":
    raise SystemExit(main())
