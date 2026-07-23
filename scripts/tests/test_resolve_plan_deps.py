"""Tests for ``resolve-plan-deps.py``.

Runnable via either:

    python -m pytest qontinui-stack/scripts/tests/test_resolve_plan_deps.py
    python -m unittest qontinui-stack.scripts.tests.test_resolve_plan_deps

The tests are written against ``unittest.TestCase`` so they work with either
runner. pytest discovery picks them up automatically.

All fixtures live under ``scripts/tests/fixtures/`` — the same directory acts
as BOTH the "active plans dir" and the "optional archive dir" via env
overrides on the subprocess invocation, so the tests are hermetic and never
touch the operator's real plans workspace.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


# ---------------------------------------------------------------------------
# Load resolve-plan-deps.py as a module (hyphenated filename → spec_from_file)
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_SCRIPT_PATH = _SCRIPTS_DIR / "resolve-plan-deps.py"
_FIXTURES_DIR = _THIS_DIR / "fixtures"


def _load_module():
    spec = importlib.util.spec_from_file_location("resolve_plan_deps", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so ``@dataclass``'s internal ``sys.modules`` lookup
    # for the module's globals (used to resolve string-form type hints under
    # ``from __future__ import annotations``) succeeds.
    sys.modules["resolve_plan_deps"] = mod
    spec.loader.exec_module(mod)
    return mod


rpd = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_resolver(
    plan_filename: str,
    *,
    fmt: str = "json",
    plans_dir: Path | None = None,
    archive_dir: Path | None = None,
) -> tuple[int, str, str]:
    """Invoke the script as a subprocess; return (exit_code, stdout, stderr)."""
    plan_path = _FIXTURES_DIR / plan_filename
    env = os.environ.copy()
    env["QONTINUI_PLANS_DIR"] = str(plans_dir or _FIXTURES_DIR)
    env["QONTINUI_PLANS_ARCHIVE_DIR"] = str(archive_dir or _FIXTURES_DIR)
    cmd = [sys.executable, str(_SCRIPT_PATH), str(plan_path), f"--{fmt}"]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_in_process(plan_filename: str) -> rpd.Resolution:
    """Call ``resolve_plan`` directly — fast path, no subprocess overhead."""
    return rpd.resolve_plan(
        _FIXTURES_DIR / plan_filename,
        _FIXTURES_DIR,
        _FIXTURES_DIR,
    )


# ---------------------------------------------------------------------------
# Unit tests for the parsing helpers (no subprocess; fast)
# ---------------------------------------------------------------------------


class TestFindStatusBlock(unittest.TestCase):
    def test_single_line_block(self) -> None:
        text = textwrap.dedent(
            """\
            # Plan title

            > **Status: SHIPPED 2026-05-21.** Summary here.

            ## Body
            """
        )
        block = rpd.find_status_block(text)
        self.assertIsNotNone(block)
        assert block is not None  # for type checker
        self.assertIn("SHIPPED", block)

    def test_multi_line_block(self) -> None:
        text = textwrap.dedent(
            """\
            # Plan

            > **Status: IN PROGRESS 2026-05-21.** First line of summary.
            > Second line of summary.
            > Depends-On: foo, bar.

            ## Body
            """
        )
        block = rpd.find_status_block(text)
        self.assertIsNotNone(block)
        assert block is not None
        self.assertIn("Depends-On:", block)
        self.assertIn("Second line", block)

    def test_no_status_block_returns_none(self) -> None:
        text = "# Plan\n\nNo status here.\n"
        self.assertIsNone(rpd.find_status_block(text))


class TestParseDependsOn(unittest.TestCase):
    # Dep references must be date-prefixed plan-stem-shaped tokens
    # (``_STEM_RE``) — bare words like ``foo`` are deliberately rejected.

    def test_no_field(self) -> None:
        block = "**Status: SHIPPED 2026-05-21.** Plain summary."
        self.assertEqual(rpd.parse_depends_on(block), [])

    def test_single_stem(self) -> None:
        block = "**Status: VETTED 2026-05-21.** Depends-On: 2026-05-20-foo-plan."
        self.assertEqual(rpd.parse_depends_on(block), ["2026-05-20-foo-plan"])

    def test_multiple_stems(self) -> None:
        block = (
            "**Status: VETTED 2026-05-21.** Depends-On: 2026-05-20-foo-plan, "
            "2026-05-20-bar-plan , 2026-05-20-baz-plan."
        )
        self.assertEqual(
            rpd.parse_depends_on(block),
            ["2026-05-20-foo-plan", "2026-05-20-bar-plan", "2026-05-20-baz-plan"],
        )

    def test_bare_words_rejected(self) -> None:
        # Prose tokens without the date prefix are not stems.
        block = "**Status: VETTED 2026-05-21.** Depends-On: foo, bar , baz."
        self.assertEqual(rpd.parse_depends_on(block), [])

    def test_trailing_empty_token_dropped(self) -> None:
        block = (
            "**Status: VETTED 2026-05-21.** Depends-On: 2026-05-20-a-plan, "
            "2026-05-20-b-plan , 2026-05-20-c-plan ,"
        )
        self.assertEqual(
            rpd.parse_depends_on(block),
            ["2026-05-20-a-plan", "2026-05-20-b-plan", "2026-05-20-c-plan"],
        )

    def test_token_stops_at_newline(self) -> None:
        # Only the marker's physical line is consumed: a stem named in a
        # later paragraph must not leak into the dep list.
        block = (
            "**Status: VETTED.** Depends-On: 2026-05-20-foo-plan\n\n"
            "Unrelated paragraph mentions 2026-05-22-bar-plan."
        )
        self.assertEqual(rpd.parse_depends_on(block), ["2026-05-20-foo-plan"])


class TestExtractLifecycle(unittest.TestCase):
    def test_single_word(self) -> None:
        block = "**Status: SHIPPED 2026-05-21.** Summary text here."
        lifecycle, summary = rpd.extract_lifecycle(block)
        self.assertEqual(lifecycle, "SHIPPED")
        self.assertIsNotNone(summary)

    def test_two_word_in_progress(self) -> None:
        block = "**Status: IN PROGRESS 2026-05-21.** Phase 1 done."
        lifecycle, _ = rpd.extract_lifecycle(block)
        self.assertEqual(lifecycle, "IN PROGRESS")

    def test_two_word_not_started(self) -> None:
        block = "**Status: NOT STARTED 2026-05-21.** Not yet kicked off."
        lifecycle, _ = rpd.extract_lifecycle(block)
        self.assertEqual(lifecycle, "NOT STARTED")

    def test_superseded(self) -> None:
        block = "**Status: SUPERSEDED 2026-05-21.** Replaced by plan X."
        lifecycle, _ = rpd.extract_lifecycle(block)
        self.assertEqual(lifecycle, "SUPERSEDED")


# ---------------------------------------------------------------------------
# Integration tests: end-to-end via in-process resolve_plan
# ---------------------------------------------------------------------------


class TestResolvePlanInProcess(unittest.TestCase):
    def test_no_depends_on(self) -> None:
        result = _resolve_in_process("2026-05-21-fixture-plan-no-deps.md")
        self.assertEqual(result.depends_on, [])
        self.assertTrue(result.all_satisfied)
        self.assertEqual(result.unsatisfied, [])

    def test_single_shipped_dep(self) -> None:
        result = _resolve_in_process("2026-05-21-fixture-plan-shipped-dep.md")
        self.assertEqual(len(result.depends_on), 1)
        self.assertEqual(result.depends_on[0].status, "SHIPPED")
        self.assertTrue(result.all_satisfied)

    def test_single_draft_dep(self) -> None:
        result = _resolve_in_process("2026-05-21-fixture-plan-draft-dep.md")
        self.assertEqual(len(result.depends_on), 1)
        self.assertEqual(result.depends_on[0].status, "DRAFT")
        self.assertFalse(result.all_satisfied)
        self.assertEqual(len(result.unsatisfied), 1)
        self.assertEqual(result.unsatisfied[0]["reason"], "not_yet_shipped")

    def test_missing_dep(self) -> None:
        result = _resolve_in_process("2026-05-21-fixture-plan-missing-dep.md")
        self.assertEqual(len(result.depends_on), 1)
        self.assertEqual(result.depends_on[0].status, "MISSING")
        self.assertIsNone(result.depends_on[0].location)
        self.assertFalse(result.all_satisfied)
        self.assertEqual(result.unsatisfied[0]["reason"], "missing_file")

    def test_mixed_deps(self) -> None:
        result = _resolve_in_process("2026-05-21-fixture-plan-mixed-deps.md")
        self.assertEqual(len(result.depends_on), 3)
        stems_to_status = {d.stem: d.status for d in result.depends_on}
        self.assertEqual(
            stems_to_status["2026-05-20-fixture-shipped-dep"], "SHIPPED"
        )
        self.assertEqual(
            stems_to_status["2026-05-20-fixture-second-shipped-dep"], "SHIPPED"
        )
        self.assertEqual(
            stems_to_status["2026-05-01-this-does-not-exist"], "MISSING"
        )
        self.assertFalse(result.all_satisfied)
        self.assertEqual(len(result.unsatisfied), 1)
        self.assertEqual(
            result.unsatisfied[0]["stem"], "2026-05-01-this-does-not-exist"
        )

    def test_in_progress_dep_two_word_lifecycle(self) -> None:
        result = _resolve_in_process(
            "2026-05-21-fixture-plan-in-progress-dep.md"
        )
        self.assertEqual(len(result.depends_on), 1)
        self.assertEqual(result.depends_on[0].status, "IN PROGRESS")
        self.assertFalse(result.all_satisfied)
        self.assertEqual(result.unsatisfied[0]["reason"], "not_yet_shipped")

    def test_malformed_extra_commas(self) -> None:
        result = _resolve_in_process(
            "2026-05-21-fixture-plan-malformed-deps.md"
        )
        # 3 real tokens — trailing empty must be dropped.
        self.assertEqual(len(result.depends_on), 3)
        stems = [d.stem for d in result.depends_on]
        self.assertEqual(
            stems,
            [
                "2026-05-20-fixture-shipped-dep",
                "2026-05-20-fixture-second-shipped-dep",
                "2026-05-21-fixture-draft-dep",
            ],
        )


# ---------------------------------------------------------------------------
# Subprocess tests: exit-code surface + CLI flags
# ---------------------------------------------------------------------------


class TestCLIExitCodes(unittest.TestCase):
    def test_exit_zero_when_no_deps(self) -> None:
        rc, stdout, _ = _run_resolver("2026-05-21-fixture-plan-no-deps.md")
        self.assertEqual(rc, 0, msg=stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["depends_on"], [])
        self.assertTrue(payload["all_satisfied"])

    def test_exit_zero_when_shipped(self) -> None:
        rc, stdout, _ = _run_resolver(
            "2026-05-21-fixture-plan-shipped-dep.md"
        )
        self.assertEqual(rc, 0, msg=stdout)
        payload = json.loads(stdout)
        self.assertTrue(payload["all_satisfied"])

    def test_exit_one_when_draft(self) -> None:
        rc, stdout, _ = _run_resolver("2026-05-21-fixture-plan-draft-dep.md")
        self.assertEqual(rc, 1, msg=stdout)
        payload = json.loads(stdout)
        self.assertFalse(payload["all_satisfied"])
        self.assertEqual(payload["unsatisfied"][0]["reason"], "not_yet_shipped")

    def test_exit_one_when_missing(self) -> None:
        rc, stdout, _ = _run_resolver(
            "2026-05-21-fixture-plan-missing-dep.md"
        )
        self.assertEqual(rc, 1, msg=stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["unsatisfied"][0]["reason"], "missing_file")

    def test_exit_one_when_mixed(self) -> None:
        rc, stdout, _ = _run_resolver("2026-05-21-fixture-plan-mixed-deps.md")
        self.assertEqual(rc, 1, msg=stdout)
        payload = json.loads(stdout)
        self.assertEqual(len(payload["depends_on"]), 3)
        self.assertEqual(len(payload["unsatisfied"]), 1)

    def test_exit_two_when_path_invalid(self) -> None:
        env = os.environ.copy()
        env["QONTINUI_PLANS_DIR"] = str(_FIXTURES_DIR)
        env["QONTINUI_PLANS_ARCHIVE_DIR"] = str(_FIXTURES_DIR)
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT_PATH),
                str(_FIXTURES_DIR / "this-plan-does-not-exist.md"),
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stdout + proc.stderr)

    def test_human_output_renders(self) -> None:
        rc, stdout, _ = _run_resolver(
            "2026-05-21-fixture-plan-mixed-deps.md", fmt="human"
        )
        self.assertEqual(rc, 1)
        self.assertIn("Plan:", stdout)
        self.assertIn("Depends-On", stdout)
        self.assertIn("SHIPPED", stdout)
        self.assertIn("MISSING", stdout)
        self.assertIn("all_satisfied = false", stdout)

    def test_human_output_no_deps(self) -> None:
        rc, stdout, _ = _run_resolver(
            "2026-05-21-fixture-plan-no-deps.md", fmt="human"
        )
        self.assertEqual(rc, 0)
        self.assertIn("(no Depends-On declared)", stdout)
        self.assertIn("all_satisfied = true", stdout)


# ---------------------------------------------------------------------------
# Directory resolution: active plans dir + OPTIONAL archive dir (Model B)
# ---------------------------------------------------------------------------


class TestDirectoryResolution(unittest.TestCase):
    def _run_with_env(
        self,
        plan_path: Path,
        env_overrides: dict[str, str],
    ) -> tuple[int, str, str]:
        """Run the script with ONLY the given plan-dir env vars set."""
        env = os.environ.copy()
        env.pop("QONTINUI_PLANS_DIR", None)
        env.pop("QONTINUI_PLANS_ARCHIVE_DIR", None)
        env.update(env_overrides)
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), str(plan_path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _write_depending_plan(self, dir_path: Path) -> Path:
        """Write a plan that depends on a fixture stem living elsewhere."""
        path = dir_path / "2026-05-21-archive-lookup-probe.md"
        path.write_text(
            "# Plan\n\n"
            "> **Status: VETTED 2026-05-21.** Probe. "
            "Depends-On: 2026-05-20-fixture-shipped-dep.\n",
            encoding="utf-8",
        )
        return path

    def test_dep_resolves_from_archive_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plans = Path(tmp)
            plan = self._write_depending_plan(plans)
            rc, stdout, stderr = self._run_with_env(
                plan,
                {
                    "QONTINUI_PLANS_DIR": str(plans),
                    "QONTINUI_PLANS_ARCHIVE_DIR": str(_FIXTURES_DIR),
                },
            )
            self.assertEqual(rc, 0, msg=stdout + stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["depends_on"][0]["status"], "SHIPPED")

    def test_archive_not_consulted_when_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plans = Path(tmp)
            plan = self._write_depending_plan(plans)
            rc, stdout, stderr = self._run_with_env(
                plan,
                {"QONTINUI_PLANS_DIR": str(plans)},
            )
            self.assertEqual(rc, 1, msg=stdout + stderr)
            payload = json.loads(stdout)
            self.assertEqual(payload["depends_on"][0]["status"], "MISSING")

    def test_missing_plans_dir_exits_two_with_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ghost = Path(tmp) / "does-not-exist"
            rc, stdout, stderr = self._run_with_env(
                _FIXTURES_DIR / "2026-05-21-fixture-plan-no-deps.md",
                {"QONTINUI_PLANS_DIR": str(ghost)},
            )
            self.assertEqual(rc, 2, msg=stdout + stderr)
            self.assertIn("QONTINUI_PLANS_DIR", stderr)

    def test_empty_archive_env_counts_as_unset(self) -> None:
        with mock.patch.dict(os.environ, {"QONTINUI_PLANS_ARCHIVE_DIR": ""}):
            self.assertIsNone(rpd.resolve_archive_dir(None))

    def test_in_process_lookup_without_archive(self) -> None:
        # archive_dir=None → single-directory layout; fixture deps resolve.
        result = rpd.resolve_plan(
            _FIXTURES_DIR / "2026-05-21-fixture-plan-shipped-dep.md",
            _FIXTURES_DIR,
            None,
        )
        self.assertTrue(result.all_satisfied)


if __name__ == "__main__":
    unittest.main()
