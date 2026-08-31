"""Tests for the ``on:`` trigger block of ``.github/workflows/qontinui-ci.yml``.

Runnable via either::

    python -m pytest qontinui-stack/scripts/tests/test_ci_workflow_triggers.py
    python -m unittest qontinui-stack.scripts.tests.test_ci_workflow_triggers

Written against ``unittest.TestCase`` so both runners work, matching the sibling
``test_alembic_at_head.py`` / ``test_terraform_sensitive_lint.py`` convention.
Stdlib only — the CI job that picks this up (``.github/workflows/qontinui-ci.yml``,
*"python script unit tests"*) installs nothing but pytest and runs
``python -m pytest scripts/tests/ -q`` over the whole directory, so this file
needs no wiring beyond existing. That constraint is why the ``on:`` block is read
by the small subset parser below rather than by PyYAML: PyYAML is not installed
on that job, and adding it would put a third-party dependency on a
deliberately dependency-free path.

WHY THIS FILE EXISTS
====================
PR #77 (``537611aa``, 2026-08-31) replaced ``on: [push, pull_request]`` with a
``push:`` trigger scoped to four branch patterns. That turned a value nothing
validates into a **load-bearing configuration with two opposite failure modes**,
and the only thing defending either one is a comment in the file asking people
not to break it:

1. **Widening it back.** An unfiltered ``push:`` also fires on every PR head
   branch, so every PR runs this workflow TWICE against the same head SHA and
   GitHub rolls the commit up to non-passing until the slower duplicate drains —
   the PR sits at ``mergeStateStatus=UNSTABLE`` (measured: PR #76 held ~20
   minutes for jobs that execute in 13-16 s). This is the defect PR #77 fixed.

2. **Narrowing it too far.** Dropping or mistyping one of the three
   ``merge-candidate*`` globs means coord's merge train pushes a candidate ref
   that produces no check run, waits ``awaiting-ci`` until
   ``COORD_MERGE_CI_TIMEOUT`` (1800 s), requeues forever, and permanently
   consumes one of ``COORD_MERGE_SLOTS`` (default 3) — the runner #566 livelock.

**Neither failure is caught anywhere else, and the second is caught nowhere at
all.** Direction 1 is (as of 2026-08-31) in review as a coord-side detector,
``qontinui-coord`` PR #1765, and even when it lands it is an *info-level,
non-paging observation*. Direction 2 is invisible to coord by design:
``ci_baseline.rs::is_candidate_check_producing_trigger`` judges "can this repo
produce candidate CI?" on the mere PRESENCE of a ``push:`` key and is
deliberately branch-filter-blind — a recorded fail-safe choice, pinned by its
own test ``a_branch_filtered_push_still_counts_as_a_candidate_producer``. So
coord will keep reporting this repo as a candidate-CI producer while the globs
are wrong, and the symptom surfaces as a wedged merge slot whose cause points at
innocent repos.

This is the same class of gap, and the same remedy, as
``scripts/terraform-sensitive-lint.py``: a value that ``fmt``, ``validate`` and a
green test suite all pass straight over, given a check that can actually see it.

Plan: 2026-08-31-devops-unfiltered-push-trigger-duplicates-every-pr-ci-run.

WHAT IT CANNOT DEFEND
=====================
The workflow under test is also the workflow that runs this test, so the guard
covers edits to the file but not its deletion — delete ``qontinui-ci.yml`` and
nothing in this repo runs at all. That gap is coord's
(``AlertKind::MergeCandidateNoCiProducer``), not this file's, and no in-repo
check can close it.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_WORKFLOW_DIR = _REPO_ROOT / ".github" / "workflows"
_CI_WORKFLOW = _WORKFLOW_DIR / "qontinui-ci.yml"

# ---------------------------------------------------------------------------
# The invariants, named once.
# ---------------------------------------------------------------------------

#: This repo's default branch. coord's head-baseline axis needs a ``push``
#: trigger that CAN fire on it (``ci_baseline.rs``).
TRUNK = "main"

#: coord's authoritative candidate scratch-ref prefixes, verbatim from
#: ``qontinui-coord/crates/coord/src/ci_observer.rs``::
#:
#:     pub const CANDIDATE_BRANCH_PREFIXES_EXACT: &[&str] = &[
#:         "merge-candidate/",
#:         "merge-candidate-batch/",
#:         "merge-candidate-spec/",
#:     ];
#:
#: Exactly three. Each needs a matching glob in the ``push:`` branch filter or
#: the merge train wedges on that shape of candidate ref.
CANDIDATE_BRANCH_GLOBS = (
    "merge-candidate/**",
    "merge-candidate-batch/**",
    "merge-candidate-spec/**",
)

#: The workflow ``name:`` and the job ``name:`` values ARE the check contexts
#: coord's merge engine matches on. Renaming one silently stops satisfying the
#: required check rather than failing loudly, so they are pinned here too.
WORKFLOW_NAME = "qontinui CI"
JOB_NAMES = {
    "terraform": "terraform fmt + validate",
    "drift-classifier": "python script unit tests",
}


class WorkflowParseError(Exception):
    """The subset parser met a construct it does not model.

    Raised rather than guessed. Every silent-pass failure mode this file exists
    to prevent is one where a plausible-looking answer was produced from an
    input nobody actually parsed, so an unmodelled construct must surface as a
    loud test error and not as a green run.
    """


# ---------------------------------------------------------------------------
# A deliberately small YAML subset parser.
#
# It models exactly what a GitHub workflow's TOP-LEVEL keys and ``on:`` block
# use: block mappings, block sequences, flow sequences, plain and quoted
# scalars, and empty (null) values. It never descends into ``jobs: -> steps:``,
# so block scalars (``run: |``), anchors and the rest of YAML are out of reach
# by construction rather than by luck.
#
# One incidental benefit over PyYAML: in YAML 1.1 the bare key ``on`` parses as
# the boolean ``True``, so a PyYAML-based check has to remember to look up
# ``True`` rather than ``"on"``. Reading the literal key text sidesteps that.
# ---------------------------------------------------------------------------

_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][\w.-]*)[ ]*:(?P<rest>.*)$")
_SEQ_RE = re.compile(r"^(?P<indent>[ ]*)-[ ]+(?P<item>.*)$")


def strip_comment(text: str) -> str:
    """Drop a trailing ``#`` comment, honouring quotes.

    A ``#`` only opens a comment at the start of the line or after whitespace,
    which is YAML's own rule and is what keeps a value such as ``'a#b'`` intact.
    Escape sequences inside double quotes are not modelled — no value in this
    repo's workflows uses one, and one appearing would surface as a parse
    mismatch rather than a silent misread.
    """
    out: list[str] = []
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (i == 0 or text[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out)


def _is_skippable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_flow_sequence(value: str) -> list[str]:
    """``[push, pull_request]`` -> ``['push', 'pull_request']``."""
    inner = value.strip()[1:-1].strip()
    if not inner:
        return []
    return [_unquote(item) for item in inner.split(",")]


def _parse_block(lines: list[str]):
    """Parse an indented block into ``dict`` / ``list`` / ``None``."""
    content = [ln for ln in lines if not _is_skippable(ln)]
    if not content:
        return None

    base = min(len(ln) - len(ln.lstrip(" ")) for ln in content)

    if _SEQ_RE.match(content[0]) and (len(content[0]) - len(content[0].lstrip(" "))) == base:
        items: list[str] = []
        for line in content:
            match = _SEQ_RE.match(line)
            if not match or len(match.group("indent")) != base:
                raise WorkflowParseError(f"unmodelled sequence entry: {line!r}")
            item = strip_comment(match.group("item")).strip()
            if not item or item[0] in "[{" or item.endswith(":"):
                raise WorkflowParseError(f"unmodelled sequence item: {line!r}")
            items.append(_unquote(item))
        return items

    mapping: dict[str, object] = {}
    index = 0
    while index < len(content):
        line = content[index]
        match = _KEY_RE.match(line)
        if not match or len(match.group("indent")) != base:
            raise WorkflowParseError(f"unmodelled mapping entry: {line!r}")
        key = match.group("key")
        inline = strip_comment(match.group("rest")).strip()

        index += 1
        child: list[str] = []
        while index < len(content):
            following = content[index]
            if len(following) - len(following.lstrip(" ")) <= base:
                break
            child.append(following)
            index += 1

        if inline and child:
            raise WorkflowParseError(f"key {key!r} has both an inline value and a block")
        if inline.startswith("["):
            mapping[key] = _split_flow_sequence(inline)
        elif inline:
            mapping[key] = _unquote(inline)
        else:
            mapping[key] = _parse_block(child)
    return mapping


def top_level(text: str) -> dict[str, list[str] | str]:
    """Split a document into top-level keys and the raw lines beneath each.

    Values come back as either the inline scalar text or the list of body lines,
    so a caller can parse only the blocks it actually models.
    """
    lines = text.splitlines()
    out: dict[str, list[str] | str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_skippable(line):
            index += 1
            continue
        match = _KEY_RE.match(line)
        if not match or match.group("indent"):
            raise WorkflowParseError(f"line {index + 1}: not a top-level key: {line!r}")
        key = match.group("key")
        inline = strip_comment(match.group("rest")).strip()

        index += 1
        body: list[str] = []
        while index < len(lines):
            following = lines[index]
            # A column-0 comment does NOT end the block — it is a comment, not a
            # key — otherwise a comment written between two nested entries would
            # silently truncate the body being collected.
            if not _is_skippable(following) and not following[:1].isspace():
                break
            body.append(following)
            index += 1

        if inline and any(not _is_skippable(ln) for ln in body):
            raise WorkflowParseError(
                f"line {index}: top-level key {key!r} has both an inline value and an "
                "indented block; refusing to guess which one is the value"
            )
        out[key] = inline if inline else body
    return out


def parse_triggers(text: str) -> dict[str, object]:
    """Return the workflow's ``on:`` block as ``{event: config-or-None}``.

    All three spellings GitHub accepts are normalised to the same shape:
    ``on: push``, ``on: [push, pull_request]`` and the block-mapping form.
    """
    blocks = top_level(text)
    if "on" not in blocks:
        raise WorkflowParseError("workflow declares no top-level `on:` key")
    value = blocks["on"]

    if isinstance(value, str):
        if value.startswith("["):
            return _as_events(_split_flow_sequence(value))
        return _as_events([_unquote(value)])

    parsed = _parse_block(value)
    if parsed is None:
        raise WorkflowParseError("`on:` is declared but empty")
    if isinstance(parsed, list):
        return _as_events(parsed)
    return parsed


def _as_events(names: list[str]) -> dict[str, object]:
    """Turn a bare event list into ``{event: None}``, validating the names.

    A flow sequence is split on commas without regard for quoting, which is
    right for event names (always bare identifiers) and would be wrong for an
    arbitrary scalar. Validating here keeps a mis-split from becoming a
    confident wrong answer instead of an error.
    """
    events: dict[str, object] = {}
    for name in names:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise WorkflowParseError(f"not a GitHub event name: {name!r}")
        events[name] = None
    return events


def push_is_unfiltered(triggers: dict[str, object]) -> bool:
    """True when ``push:`` is declared but carries no branch filter.

    Matches the predicate coord PR #1765 adds fleet-wide: a ``push`` trigger is
    unfiltered when it declares neither ``branches:`` nor ``branches-ignore:``.
    The inline-sequence and scalar spellings are unfiltered by construction.
    """
    if "push" not in triggers:
        return False
    config = triggers["push"]
    if config is None:
        return True
    if not isinstance(config, dict):
        raise WorkflowParseError(f"unmodelled `push:` configuration: {config!r}")
    return not ({"branches", "branches-ignore"} & set(config))


def job_names(text: str) -> dict[str, str | None]:
    """Map job id -> its ``name:``, without descending into ``steps:``."""
    blocks = top_level(text)
    body = blocks.get("jobs")
    if not isinstance(body, list):
        raise WorkflowParseError("workflow declares no `jobs:` block")
    content = [ln for ln in body if not _is_skippable(ln)]
    if not content:
        raise WorkflowParseError("`jobs:` is empty")

    base = min(len(ln) - len(ln.lstrip(" ")) for ln in content)
    out: dict[str, str | None] = {}
    index = 0
    while index < len(content):
        match = _KEY_RE.match(content[index])
        if not match or len(match.group("indent")) != base:
            raise WorkflowParseError(f"unmodelled job entry: {content[index]!r}")
        job_id = match.group("key")
        index += 1

        inner: list[str] = []
        while index < len(content):
            following = content[index]
            if len(following) - len(following.lstrip(" ")) <= base:
                break
            inner.append(following)
            index += 1

        # Only a `name:` at the job's own indent is the job name; the `- name:`
        # entries under `steps:` sit deeper and start with a dash.
        name = None
        if inner:
            inner_base = min(len(ln) - len(ln.lstrip(" ")) for ln in inner)
            for line in inner:
                candidate = _KEY_RE.match(line)
                if (
                    candidate
                    and len(candidate.group("indent")) == inner_base
                    and candidate.group("key") == "name"
                ):
                    name = _unquote(strip_comment(candidate.group("rest")).strip())
                    break
        out[job_id] = name
    return out


def _ci_workflow_text() -> str:
    return _CI_WORKFLOW.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The invariants PR #77 made load-bearing.
# ---------------------------------------------------------------------------


class PushTriggerTests(unittest.TestCase):
    """``push:`` must exist, must be filtered, and must list the right branches."""

    def setUp(self) -> None:
        self.triggers = parse_triggers(_ci_workflow_text())

    def _push_branches(self) -> list:
        """The ``push:`` branch list, or a readable failure saying why there is none.

        Without this the branch-content tests subscript ``None`` and report a
        ``TypeError``, which buries the real finding under a stack trace. The
        two defects that get here first — no ``push:`` at all, and an
        unfiltered one — are each named by their own test.
        """
        config = self.triggers.get("push")
        self.assertIsInstance(
            config,
            dict,
            "`push:` is absent or carries no configuration at all, so there is no "
            "branch filter to inspect. See test_push_trigger_is_declared and "
            "test_push_trigger_is_filtered for the actual defect.",
        )
        branches = config.get("branches")
        self.assertIsInstance(
            branches,
            list,
            "`push:` declares no `branches:` list. See test_push_trigger_is_filtered.",
        )
        return branches

    def test_push_trigger_is_declared(self) -> None:
        self.assertIn(
            "push",
            self.triggers,
            "`push:` was removed from qontinui-ci.yml. coord's merge train pushes "
            "merge-candidate scratch refs and waits `awaiting-ci` for check runs on "
            "them; a pull_request-only workflow can never satisfy that, so the train "
            "times out at COORD_MERGE_CI_TIMEOUT and permanently consumes a merge "
            "slot. coord also judges candidate-CI capability on the mere PRESENCE of "
            "this key, so removing it trips AlertKind::MergeCandidateNoCiProducer.",
        )

    def test_push_trigger_is_filtered(self) -> None:
        self.assertFalse(
            push_is_unfiltered(self.triggers),
            "`push:` carries no `branches:` filter. An unfiltered push trigger also "
            "fires on every PR head branch, so every PR runs this workflow twice "
            "against the same head SHA and sits at mergeStateStatus=UNSTABLE until "
            "the slower duplicate drains. This is the exact defect PR #77 fixed; a "
            "`concurrency:` block does NOT substitute, because github.ref differs by "
            "event and the two runs land in different groups.",
        )

    def test_push_branches_include_the_trunk(self) -> None:
        branches = self._push_branches()
        self.assertIn(
            TRUNK,
            branches,
            f"`push:` no longer fires on {TRUNK!r}. coord's head-baseline axis needs a "
            "push trigger that can fire on the default branch.",
        )

    def test_push_branches_include_all_three_candidate_globs(self) -> None:
        branches = self._push_branches()
        missing = [glob for glob in CANDIDATE_BRANCH_GLOBS if glob not in branches]
        self.assertEqual(
            [],
            missing,
            f"`push:` is missing candidate branch glob(s) {missing}. These mirror "
            "coord's CANDIDATE_BRANCH_PREFIXES_EXACT (ci_observer.rs) and every one "
            "of the three is load-bearing: a candidate ref of a shape not listed here "
            "produces no check run, so the merge train waits awaiting-ci until it "
            "times out and requeues forever. coord cannot detect this — "
            "is_candidate_check_producing_trigger is branch-filter-blind by design.",
        )


class PullRequestTriggerTests(unittest.TestCase):
    """``pull_request:`` must stay UNFILTERED."""

    def setUp(self) -> None:
        self.triggers = parse_triggers(_ci_workflow_text())

    def test_pull_request_trigger_is_declared(self) -> None:
        self.assertIn("pull_request", self.triggers)

    def test_pull_request_trigger_is_unfiltered(self) -> None:
        self.assertIsNone(
            self.triggers["pull_request"],
            "`pull_request:` acquired a filter. It is deliberately unfiltered so that "
            "a stacked PR based on a long-lived integration branch still gets CI; "
            "adding a `branches:` list here silently drops those PRs (runner "
            "#651/#652).",
        )

    def test_pull_request_target_is_never_used(self) -> None:
        self.assertNotIn(
            "pull_request_target",
            self.triggers,
            "`pull_request_target` runs fork-authored code with repository secrets in "
            "scope. This repo is PUBLIC and its terraform state holds 12 plaintext "
            "secret values, so the trigger is forbidden outright "
            "(docs/terraform-state-secret-inventory.md).",
        )


class CheckContextTests(unittest.TestCase):
    """The names coord's merge engine matches on.

    These are pinned, not merely observed: a rename here is not wrong in itself,
    but it changes the required-check contexts and must be done together with
    the coord-side update rather than discovered afterwards from a merge train
    that stopped seeing its checks.
    """

    def setUp(self) -> None:
        self.text = _ci_workflow_text()

    def test_workflow_name_is_the_check_context_coord_reads(self) -> None:
        blocks = top_level(self.text)
        self.assertEqual(
            WORKFLOW_NAME,
            blocks.get("name"),
            "The workflow `name:` changed. It is the check context coord's merge "
            "engine reads; update coord's required checks in the same change.",
        )

    def test_job_ids_and_names_are_unchanged(self) -> None:
        self.assertEqual(
            JOB_NAMES,
            job_names(self.text),
            "A job id or job `name:` changed. Job names are check contexts too; "
            "update coord's required checks in the same change.",
        )


class EveryWorkflowInThisRepoTests(unittest.TestCase):
    """The invariant generalises to the directory, not just to one file.

    PR #77 closed the *event* axis for ``qontinui-ci.yml``: one workflow can no
    longer emit both a ``push`` and a ``pull_request`` run at one head SHA. The
    *workflow* axis stays open — a second workflow added later with an
    unfiltered ``push:`` re-creates the same duplicate runs at the same SHA.
    That is not hypothetical: the fleet-wide sweep this plan drove found exactly
    that residue in ``qontinui-workflow-ui`` and ``qontinui-demo-stage``, where a
    second ``ci.yml`` ran effectively the same job.

    ``qontinui-stack`` carries one workflow today, so this test is a tripwire
    for the next one rather than a check on the present state.
    """

    def test_workflow_directory_is_readable(self) -> None:
        self.assertTrue(_WORKFLOW_DIR.is_dir(), f"missing {_WORKFLOW_DIR}")
        self.assertTrue(
            sorted(_WORKFLOW_DIR.glob("*.y*ml")),
            "no workflows found — this test would otherwise pass vacuously",
        )

    def test_no_workflow_declares_an_unfiltered_push_trigger(self) -> None:
        offenders = []
        for path in sorted(_WORKFLOW_DIR.glob("*.y*ml")):
            try:
                triggers = parse_triggers(path.read_text(encoding="utf-8"))
            except WorkflowParseError as exc:
                # Naming the file matters: this loop is the only place that
                # reads a workflow this test file has never seen, so an
                # unmodelled construct must say WHICH file to look at.
                raise WorkflowParseError(f"{path.name}: {exc}") from exc
            if push_is_unfiltered(triggers):
                offenders.append(path.name)
        self.assertEqual(
            [],
            offenders,
            f"workflow(s) {offenders} declare an unfiltered `push:`. Every push/PR "
            "workflow in this repo must scope `push:` to the trunk plus the three "
            "merge-candidate globs, or its runs duplicate at every PR head SHA.",
        )


# ---------------------------------------------------------------------------
# Self-tests for the subset parser.
#
# The parser is the part of this file that could be wrong in a way that makes
# every assertion above pass vacuously, so it is exercised against synthetic
# documents — including the pre-PR-#77 spelling, which MUST read as unfiltered.
# ---------------------------------------------------------------------------


class ParserTests(unittest.TestCase):
    def test_flow_sequence_form_is_unfiltered(self) -> None:
        triggers = parse_triggers("name: x\non: [push, pull_request]\njobs:\n  a:\n    name: A\n")
        self.assertEqual({"push": None, "pull_request": None}, triggers)
        self.assertTrue(push_is_unfiltered(triggers))

    def test_scalar_form_is_unfiltered(self) -> None:
        triggers = parse_triggers("on: push\n")
        self.assertEqual({"push": None}, triggers)
        self.assertTrue(push_is_unfiltered(triggers))

    def test_mapping_without_branches_is_unfiltered(self) -> None:
        triggers = parse_triggers("on:\n  push:\n  pull_request:\n")
        self.assertTrue(push_is_unfiltered(triggers))

    def test_mapping_with_only_paths_is_still_unfiltered(self) -> None:
        triggers = parse_triggers("on:\n  push:\n    paths:\n      - 'src/**'\n")
        self.assertTrue(push_is_unfiltered(triggers))

    def test_branches_ignore_counts_as_filtered(self) -> None:
        triggers = parse_triggers("on:\n  push:\n    branches-ignore:\n      - gh-pages\n")
        self.assertFalse(push_is_unfiltered(triggers))

    def test_absent_push_is_not_reported_as_unfiltered(self) -> None:
        self.assertFalse(push_is_unfiltered(parse_triggers("on:\n  pull_request:\n")))

    def test_block_mapping_with_branches_round_trips(self) -> None:
        triggers = parse_triggers(
            "on:\n"
            "  push:\n"
            "    branches:\n"
            "      - main\n"
            "      - 'merge-candidate/**'\n"
            "  pull_request:\n"
        )
        self.assertEqual(
            {"push": {"branches": ["main", "merge-candidate/**"]}, "pull_request": None},
            triggers,
        )

    def test_comments_and_blank_lines_are_ignored(self) -> None:
        triggers = parse_triggers(
            "# leading comment\n"
            "\n"
            "on:\n"
            "  # why push is scoped\n"
            "  push:\n"
            "    branches:\n"
            "      - main  # the trunk\n"
            "\n"
            "  pull_request:\n"
            "# trailing comment at column 0\n"
            "permissions:\n"
            "  contents: read\n"
        )
        self.assertEqual({"push": {"branches": ["main"]}, "pull_request": None}, triggers)

    def test_hash_inside_a_quoted_scalar_survives(self) -> None:
        self.assertEqual("a#b", strip_comment("'a#b'  # trailing").strip().strip("'"))

    def test_job_names_ignore_step_names(self) -> None:
        text = (
            "name: demo\n"
            "on:\n"
            "  pull_request:\n"
            "jobs:\n"
            "  build:\n"
            "    name: the build job\n"
            "    steps:\n"
            "      - name: a step name\n"
            "        run: echo hi\n"
            "  lint:\n"
            "    runs-on: ubuntu-latest\n"
        )
        self.assertEqual({"build": "the build job", "lint": None}, job_names(text))

    def test_missing_on_key_raises_rather_than_passing(self) -> None:
        with self.assertRaises(WorkflowParseError):
            parse_triggers("name: x\njobs:\n  a:\n    name: A\n")

    def test_unmodelled_push_scalar_raises_rather_than_guessing(self) -> None:
        with self.assertRaises(WorkflowParseError):
            push_is_unfiltered(parse_triggers("on:\n  push: something-unexpected\n"))

    def test_inline_value_plus_block_raises_rather_than_dropping_one(self) -> None:
        with self.assertRaises(WorkflowParseError):
            top_level("on: push\n  push:\n    branches: [main]\n")

    def test_a_non_event_name_raises_rather_than_becoming_an_event(self) -> None:
        with self.assertRaises(WorkflowParseError):
            parse_triggers("on: ['not an event']\n")

    def test_an_indented_first_line_raises(self) -> None:
        with self.assertRaises(WorkflowParseError):
            top_level("  name: indented\n")

    def test_the_shipped_workflow_parses(self) -> None:
        """The real file, end to end — the parser must model what actually ships.

        Reaching an answer is all this asserts. The *shape* of that answer
        belongs to the invariant tests above; duplicating it here would report
        one defect twice and file half of it under the parser.
        """
        text = _ci_workflow_text()
        self.assertTrue(parse_triggers(text), "parsed no triggers from the shipped file")
        self.assertTrue(job_names(text), "parsed no jobs from the shipped file")


if __name__ == "__main__":  # pragma: no cover - convenience for direct runs
    unittest.main()
