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
    2   input error (path missing, not a file, no status block, etc.)

Environment overrides:
    QONTINUI_PLANS_DIR          (default ``D:/qontinui-root/qontinui-dev-notes/plans``)
    QONTINUI_PLANS_ARCHIVE_DIR  (legacy 2nd lookup path, retired 2026-07-22;
                                same default as above — leave unset)
"""

from __future__ import annotations

import argparse
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

DEFAULT_PLANS_DIR = "D:/qontinui-root/qontinui-dev-notes/plans"
DEFAULT_ARCHIVE_DIR = "D:/qontinui-root/qontinui-dev-notes/plans"

# Lifecycle words recognised inside the status blockquote, in match order.
# Multi-word entries MUST come before any prefix that would shadow them.
LIFECYCLE_WORDS: tuple[str, ...] = (
    "IN PROGRESS",
    "NOT STARTED",
    "SHIPPED",
    "VETTED",
    "DRAFT",
    "PARTIAL",
    "SUPERSEDED",
    "OBSOLETE",
)

# Compiled regex that finds the first lifecycle word — order matters because
# we want "IN PROGRESS" to win over a hypothetical "IN" prefix etc. The
# alternation order maps to leftmost-then-longest semantics in Python's
# default ``re`` engine, so we list two-word forms first.
_LIFECYCLE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in LIFECYCLE_WORDS) + r")\b"
)

# A plan stem is a date-prefixed kebab-case slug, e.g.
# ``2026-06-02-ui-bridge-value-action-focus-lifecycle-plan``. Used to extract
# Depends-On stems while rejecting prose tokens that happen to sit to the
# right of a ``Depends-On:`` marker. The leading date alone (``2026-05-21.``)
# does NOT match — a stem requires at least one ``-word`` segment after the
# date, so bare dates mentioned in prose are not mistaken for deps.
_STEM_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*\b")

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
# Parsing
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc


def find_status_block(text: str) -> str | None:
    """Return the joined text of the first ``> **Status:`` blockquote.

    The blockquote is the contiguous run of ``> ``-prefixed lines starting at
    the line that contains ``**Status:``. The H1 (if any) is ignored — we
    just need the first matching block in the document. Returns the joined
    block text with the ``> `` prefixes stripped, or ``None`` if no status
    block is found.
    """
    lines = text.splitlines()
    block_start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(">") and "**Status:" in line:
            block_start = i
            break
    if block_start is None:
        return None

    block_lines: list[str] = []
    for line in lines[block_start:]:
        if line.startswith(">"):
            # Strip the leading "> " (or just ">") prefix.
            stripped = line[1:]
            if stripped.startswith(" "):
                stripped = stripped[1:]
            block_lines.append(stripped)
        else:
            break
    return "\n".join(block_lines)


def parse_depends_on(status_block: str) -> list[str]:
    """Pull the Depends-On stems out of a status block.

    Refines the loose rule from ``implement-plan.md`` Step 0.4
    (case-sensitive ``Depends-On:`` → split-on-commas) to be robust against
    two real-world shapes that the naive split mis-handled:

    * **Multiple ``Depends-On:`` occurrences.** A status blockquote often
      carries one in the headline sentence and another in a trailing
      ``History:`` / re-vet line. The naive ``str.find`` took only the first
      and then swept everything to its right — including the second marker's
      surrounding prose. We scan *every* occurrence and return the
      order-preserving union (deduped).
    * **Prose to the right of the marker.** Only the remainder of the *same
      physical line* as each marker is considered (never the next blockquote
      paragraph, which may discuss unrelated plans), and within it only
      date-prefixed plan-stem-shaped tokens (``_STEM_RE``) are kept. So
      ``Depends-On: 2026-06-02-foo-plan. This plan widens ^0.8.4 to ...``
      yields exactly ``[2026-06-02-foo-plan]`` instead of comma-splitting the
      trailing sentence into bogus stems.

    Well-formed inputs (a single marker with comma-separated stems on one
    line) resolve identically to the old behavior; malformed-but-real inputs
    no longer produce phantom deps.
    """
    marker = "Depends-On:"
    stems: list[str] = []
    seen: set[str] = set()
    search_from = 0
    while True:
        idx = status_block.find(marker, search_from)
        if idx < 0:
            break
        search_from = idx + len(marker)
        # Restrict to the rest of THIS physical line — never bleed into the
        # next blockquote paragraph (unrelated plan stems may be named there).
        tail = status_block[search_from:]
        line = tail.splitlines()[0] if tail else ""
        for match in _STEM_RE.finditer(line):
            stem = match.group(0)
            if stem not in seen:
                seen.add(stem)
                stems.append(stem)
    return stems


def extract_lifecycle(status_block: str) -> tuple[str | None, str | None]:
    """Return ``(lifecycle_word, summary)``.

    ``lifecycle_word`` is the first match of ``_LIFECYCLE_RE`` inside the
    block (case-sensitive). ``summary`` is the text after the lifecycle
    word up to the next ``> `` boundary or end-of-block, trimmed and with
    common surrounding punctuation removed.

    Both may be ``None`` if the block doesn't conform.
    """
    match = _LIFECYCLE_RE.search(status_block)
    if not match:
        return None, None
    lifecycle = match.group(1)

    # Summary = text after the lifecycle word up to the first blank line
    # (which separates blockquote "paragraphs") or end of block.
    after = status_block[match.end() :]
    # The block has already had ``> `` prefixes stripped, so blank
    # original-blockquote lines appear as "" lines here. Split on those.
    paragraphs = re.split(r"\n\s*\n", after, maxsplit=1)
    summary_raw = paragraphs[0]
    # Collapse whitespace.
    summary = " ".join(summary_raw.split())
    # Remove a leading date stamp like "2026-05-21." that often follows
    # the lifecycle word.
    summary = re.sub(r"^\d{4}-\d{2}-\d{2}\.?\s*", "", summary)
    # Strip the markdown bold close (``**``) that usually follows
    # ``**Status: SHIPPED 2026-05-21.``, plus leading punctuation noise.
    summary = re.sub(r"^\*+\s*", "", summary)
    summary = summary.lstrip(".:- ").strip()
    return lifecycle, (summary or None)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def _candidate_paths(stem: str, plans_dir: Path, archive_dir: Path) -> list[Path]:
    # archive_dir is a retired second location (2026-07-22 consolidation) and
    # now normally equals plans_dir — dedup so it isn't stat'd twice, while
    # still honoring an explicitly-set override.
    seen: list[Path] = []
    for d in (plans_dir, archive_dir):
        p = d / f"{stem}.md"
        if p not in seen:
            seen.append(p)
    return seen


def resolve_dep(
    stem: str,
    plans_dir: Path,
    archive_dir: Path,
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
            block = find_status_block(text)
            if block is None:
                # Per skill spec: "A plan with no status blockquote at all
                # is treated as DRAFT."
                return DepResolution(
                    stem=stem,
                    status="DRAFT",
                    location=str(candidate).replace("\\", "/"),
                    summary=None,
                )
            lifecycle, summary = extract_lifecycle(block)
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
    archive_dir: Path,
) -> Resolution:
    if not plan_path.exists():
        raise InputError(f"plan file does not exist: {plan_path}")
    if not plan_path.is_file():
        raise InputError(f"not a file: {plan_path}")

    text = _read_text(plan_path)
    block = find_status_block(text)
    plan_stem = plan_path.stem

    if block is None:
        # An input plan with no status block is malformed for the purposes
        # of dependency resolution — but we should still tolerate plans
        # that legitimately have no status (e.g. design notes). Treat as
        # "no deps declared" and exit 0. The skill consumer can choose to
        # enforce status-block presence separately.
        return Resolution(plan_stem=plan_stem)

    stems = parse_depends_on(block)
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
        help="Override the plans dir (env: QONTINUI_PLANS_DIR).",
    )
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="Legacy 2nd lookup path, retired 2026-07-22 (env: QONTINUI_PLANS_ARCHIVE_DIR).",
    )
    parser.set_defaults(format="json")

    args = parser.parse_args(list(argv) if argv is not None else None)

    plan_path = Path(args.plan_path).expanduser()
    if not plan_path.is_absolute():
        plan_path = plan_path.resolve()

    plans_dir, archive_dir = _resolve_dirs(args)

    try:
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
