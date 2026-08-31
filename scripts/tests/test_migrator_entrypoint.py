"""Tests for ``migrator/entrypoint.sh``.

Runnable via either::

    python -m pytest qontinui-stack/scripts/tests/test_migrator_entrypoint.py
    python -m unittest qontinui-stack.scripts.tests.test_migrator_entrypoint

Sibling of :mod:`test_alembic_at_head`, and written the same way: stdlib only,
``unittest.TestCase`` so both runners work, and the shipped ``.sh`` driven as a
subprocess against a fake ``alembic`` on ``PATH`` so what is exercised is the
file byte-for-byte rather than a reimplementation of its logic in Python.

**Why this file exists.** ``entrypoint.sh`` received the same
stderr-suppression fix as ``alembic_at_head.sh`` in the same commit (9763836,
PR #76) and shipped with no coverage at all, because it hardcoded ``cd /app``
and so could not be driven outside the container — the ``ALEMBIC_STATUS_APP_DIR``
override that made its sibling testable was not extended to it. It is the
ENTRYPOINT of both the compose ``migrator`` one-shot and the ECS migrator task
(``aws/modules/migrator/main.tf``), so its log lines are what an operator reads
in CloudWatch when a production migration goes wrong.

Two properties are load-bearing here and are asserted throughout:

* **Log honesty.** A failed probe must say it failed, name alembic's exit
  status, and quote alembic's own words. It must never present the failure as
  a revision id — the measured pre-fix symptom was the literal line
  ``[migrator] alembic current: FAILED:``, because ``FAILED:`` arrives on
  *stdout* and the un-anchored ``awk`` read it as ``$1``.
* **Control flow is unchanged by any of that.** The probes are logging only.
  Whatever they report, a DSN that is not provably at head still reaches
  ``exec alembic upgrade head``, which fails loudly on its own. A test that
  let a failed probe suppress the upgrade would be asserting the opposite of
  what this script is for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent.parent
_SCRIPT = _REPO_ROOT / "migrator" / "entrypoint.sh"

# The revision the canonical dev DB is stamped at, and which the 2026-05-25
# migrator image's 219-file chain does not contain.
_STAMPED_REV = "coord_specq_01_speculative_ci_status"
# The single head of that stale image's embedded chain.
_IMAGE_HEAD = "coord_handoff_requests"

_DSN = "postgresql+psycopg2://qontinui_user:s3cr3t_pw@postgres:5432/qontinui_db"

_SH = shutil.which("sh") or shutil.which("bash")


# ---------------------------------------------------------------------------
# Fake `alembic` shims
#
# Every shim appends its argv to $FAKE_ALEMBIC_LOG before doing anything else,
# so a test can assert whether `upgrade head` was reached. `upgrade` succeeds
# unless the shim says otherwise.
# ---------------------------------------------------------------------------

_PREAMBLE = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_ALEMBIC_LOG"
"""

_UPGRADE_OK = """
if [ "$1" = "upgrade" ]; then
  echo "INFO  [alembic.runtime.migration] Running upgrade -> head" >&2
  exit 0
fi
exit 0
"""

_UPGRADE_FAILS = """
if [ "$1" = "upgrade" ]; then
  echo "FAILED: Can't locate revision identified by 'coord_specq_01_speculative_ci_status'"
  exit 255
fi
exit 0
"""

# Today's measured state. Note which stream each line goes to: the INFO
# preamble and the ERROR line are stderr, the `FAILED:` line is STDOUT.
_CURRENT_CANNOT_LOCATE = f"""
if [ "$1" = "current" ]; then
  echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
  echo "FAILED: Can't locate revision identified by '{_STAMPED_REV}'"
  echo "ERROR  [alembic.util.messaging] Can't locate revision identified by '{_STAMPED_REV}'" >&2
  exit 255
fi
"""

_CURRENT_DB_DOWN = """
if [ "$1" = "current" ]; then
  echo "FAILED: (psycopg2.OperationalError) connection to server at \\"postgres\\" failed" >&2
  exit 1
fi
"""

_CURRENT_TRULY_EMPTY = """
if [ "$1" = "current" ]; then
  echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
  exit 0
fi
"""

_CURRENT_AT_HEAD = f"""
if [ "$1" = "current" ]; then
  echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
"""

_CURRENT_BEHIND = """
if [ "$1" = "current" ]; then
  echo "oldrev01"
  exit 0
fi
"""

_HEADS_OK = f"""
if [ "$1" = "heads" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
"""

_HEADS_BROKEN = """
if [ "$1" = "heads" ]; then
  echo "FAILED: Can't locate revision identified by 'missing_down_revision'"
  exit 255
fi
"""


def _shim(current: str, heads: str = _HEADS_OK, upgrade: str = _UPGRADE_OK) -> str:
    return _PREAMBLE + current + heads + upgrade


class _Result:
    """A completed run of the script, with both streams and the exit status."""

    def __init__(self, proc: subprocess.CompletedProcess, invocations: list[str]) -> None:
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        #: argv of every `alembic` call the script made, in order.
        self.invocations = invocations

    @property
    def output(self) -> str:
        """Everything the container emitted; compose/CloudWatch capture both."""
        return self.stdout + self.stderr

    @property
    def ran_upgrade(self) -> bool:
        return any(call.startswith("upgrade") for call in self.invocations)


# Each shim is deterministic, so a given (shim, DSN, app_dir) triple always
# produces the same run. Several tests assert different properties of the same
# run; memoising keeps this file at one subprocess per distinct scenario.
_RUN_CACHE: dict[tuple[str, str | None, str | None], "_Result"] = {}


@unittest.skipIf(_SH is None, "no POSIX sh on PATH")
class _ScriptCase(unittest.TestCase):
    """Runs the real ``entrypoint.sh`` with a fake ``alembic`` on PATH."""

    def run_script(
        self,
        shim: str,
        database_url: str | None = _DSN,
        app_dir: str | None = None,
    ) -> _Result:
        key = (shim, database_url, app_dir)
        if key not in _RUN_CACHE:
            _RUN_CACHE[key] = self._run_script_uncached(shim, database_url, app_dir)
        return _RUN_CACHE[key]

    def _run_script_uncached(
        self, shim: str, database_url: str | None, app_dir: str | None
    ) -> _Result:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bindir = root / "bin"
        default_app = root / "app"
        bindir.mkdir()
        default_app.mkdir()
        log = root / "alembic-calls.log"

        fake = bindir / "alembic"
        fake.write_text(shim, encoding="utf-8", newline="\n")
        fake.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        env["FAKE_ALEMBIC_LOG"] = str(log)
        # The image's alembic project root. Overridden so the shipped script
        # can run verbatim outside the container, where /app does not exist.
        env["MIGRATOR_APP_DIR"] = str(default_app) if app_dir is None else app_dir
        if database_url is None:
            env.pop("DATABASE_URL", None)
        else:
            env["DATABASE_URL"] = database_url

        assert _SH is not None
        # Explicit utf-8 rather than `text=True`: the script's own log lines
        # carry em-dashes, and `text=True` decodes with the locale encoding,
        # which is ASCII under a bare `LC_ALL=C`. Assertions below stay
        # ASCII-only for the same reason, but the decode must not raise.
        proc = subprocess.run(
            [_SH, str(_SCRIPT)],
            env=env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        invocations = (
            log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        )
        return _Result(proc, invocations)


class StaleImageTests(_ScriptCase):
    """`alembic current` exits 255 — the measured 2026-08-31 incident."""

    SHIM = _shim(_CURRENT_CANNOT_LOCATE)

    def test_reports_the_failure_as_a_failure(self):
        res = self.run_script(self.SHIM)
        self.assertIn("[migrator] alembic current: FAILED (exit 255)", res.output)
        self.assertIn("DB revision unknown", res.output)

    def test_never_prints_the_failure_token_as_a_revision(self):
        """The measured pre-fix line was literally `alembic current: FAILED:`."""
        res = self.run_script(self.SHIM)
        self.assertNotIn("[migrator] alembic current: FAILED:", res.output)

    def test_surfaces_alembics_own_message(self):
        """`2>/dev/null` used to discard this; it must reach the operator."""
        res = self.run_script(self.SHIM)
        self.assertIn("Can't locate revision identified by", res.output)
        self.assertIn(_STAMPED_REV, res.output)

    def test_head_probe_is_unaffected(self):
        res = self.run_script(self.SHIM)
        self.assertIn(f"[migrator] alembic head:    {_IMAGE_HEAD}", res.output)

    def test_still_runs_the_upgrade(self):
        """Control flow is deliberately unchanged: the upgrade speaks for itself."""
        res = self.run_script(self.SHIM)
        self.assertTrue(res.ran_upgrade, res.output)
        self.assertIn("[migrator] running: alembic upgrade head", res.output)
        self.assertNotIn("already at head", res.output)

    def test_a_failing_upgrade_still_fails_loudly(self):
        res = self.run_script(_shim(_CURRENT_CANNOT_LOCATE, upgrade=_UPGRADE_FAILS))
        self.assertNotEqual(res.returncode, 0)


class ProbeFailureTests(_ScriptCase):
    """Any other probe failure gets the same treatment."""

    def test_current_failure_names_its_own_exit_status(self):
        res = self.run_script(_shim(_CURRENT_DB_DOWN))
        self.assertIn("[migrator] alembic current: FAILED (exit 1)", res.output)
        self.assertIn("DB revision unknown", res.output)
        self.assertIn("psycopg2.OperationalError", res.output)

    def test_failed_heads_is_not_reported_as_no_head(self):
        res = self.run_script(_shim(_CURRENT_BEHIND, heads=_HEADS_BROKEN))
        self.assertIn("[migrator] alembic head:    FAILED (exit 255)", res.output)
        self.assertIn("chain head unknown", res.output)
        self.assertNotIn("[migrator] alembic head:    <none>", res.output)
        self.assertIn("missing_down_revision", res.output)

    def test_failed_heads_cannot_produce_a_false_at_head_no_op(self):
        """An unknown head must never compare equal to a known current rev."""
        res = self.run_script(_shim(_CURRENT_BEHIND, heads=_HEADS_BROKEN))
        self.assertNotIn("already at head", res.output)
        self.assertTrue(res.ran_upgrade, res.output)


class RevisionReportingTests(_ScriptCase):
    def test_at_head_is_a_no_op(self):
        res = self.run_script(_shim(_CURRENT_AT_HEAD))
        self.assertEqual(res.returncode, 0, res.output)
        self.assertIn(f"[migrator] alembic current: {_IMAGE_HEAD}", res.output)
        self.assertIn("[migrator] DB already at head — no-op", res.output)
        self.assertFalse(res.ran_upgrade, res.output)

    def test_behind_head_runs_the_upgrade(self):
        res = self.run_script(_shim(_CURRENT_BEHIND))
        self.assertIn("[migrator] alembic current: oldrev01", res.output)
        self.assertIn(f"[migrator] alembic head:    {_IMAGE_HEAD}", res.output)
        self.assertTrue(res.ran_upgrade, res.output)
        self.assertEqual(res.returncode, 0, res.output)

    def test_unstamped_db_reports_none_not_a_failure(self):
        """`current` SUCCEEDING with no revision is a real <none>, not FAILED."""
        res = self.run_script(_shim(_CURRENT_TRULY_EMPTY))
        self.assertIn("[migrator] alembic current: <none>", res.output)
        self.assertNotIn("alembic current: FAILED", res.output)
        self.assertTrue(res.ran_upgrade, res.output)


class DsnLoggingTests(_ScriptCase):
    """The DSN carries the password; only host:port may be logged."""

    def test_logs_host_and_port_only(self):
        res = self.run_script(_shim(_CURRENT_AT_HEAD))
        self.assertIn("[migrator] DATABASE_URL host=postgres:5432", res.output)
        self.assertNotIn("s3cr3t_pw", res.output)

    def test_dsn_without_a_database_does_not_leak_the_password(self):
        """The regression: a substitution echoes its input when it cannot match.

        `postgresql://u:pw@host:5432` has credentials but no trailing
        `/<database>`, so the previous `sed -E 's|.*@([^/]+)/.*|\\1|'` printed
        the DSN verbatim — password included — into the compose log and, for
        the ECS migrator sharing this image, into CloudWatch.
        """
        res = self.run_script(
            _shim(_CURRENT_AT_HEAD),
            database_url="postgresql+psycopg2://qontinui_user:s3cr3t_pw@postgres:5432",
        )
        self.assertNotIn("s3cr3t_pw", res.output)
        self.assertIn("[migrator] DATABASE_URL host=postgres:5432", res.output)

    def test_password_containing_an_at_sign_does_not_leak_its_tail(self):
        """The host capture anchors on the LAST `@`, not the first."""
        res = self.run_script(
            _shim(_CURRENT_AT_HEAD),
            database_url="postgresql+psycopg2://qontinui_user:pw@with@at@postgres:5432/qontinui_db",
        )
        self.assertNotIn("pw@with", res.output)
        self.assertIn("[migrator] DATABASE_URL host=postgres:5432", res.output)

    def test_unparseable_dsn_says_so_rather_than_echoing_itself(self):
        res = self.run_script(_shim(_CURRENT_AT_HEAD), database_url="not-a-dsn")
        self.assertIn("[migrator] DATABASE_URL host=<unparsed>", res.output)
        self.assertNotIn("host=not-a-dsn", res.output)


class FatalTests(_ScriptCase):
    def test_missing_database_url_is_exit_2(self):
        res = self.run_script(_shim(_CURRENT_AT_HEAD), database_url=None)
        self.assertEqual(res.returncode, 2, res.output)
        self.assertIn("[migrator] FATAL: DATABASE_URL is not set", res.output)
        self.assertFalse(res.ran_upgrade, res.output)

    def test_unenterable_app_dir_is_a_labelled_fatal(self):
        """Not a bare `set -e` exit carrying only sh's own wording."""
        res = self.run_script(
            _shim(_CURRENT_AT_HEAD), app_dir="/nonexistent/alembic/root"
        )
        self.assertEqual(res.returncode, 2, res.output)
        self.assertIn(
            "[migrator] FATAL: cannot enter alembic project root "
            "'/nonexistent/alembic/root'",
            res.output,
        )
        self.assertFalse(res.ran_upgrade, res.output)


class ShippedScriptTests(unittest.TestCase):
    """Guards on the script's source that no behavioural test can express."""

    def test_script_exists_and_is_posix_sh(self):
        self.assertTrue(_SCRIPT.is_file(), f"missing {_SCRIPT}")
        first = _SCRIPT.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "#!/bin/sh")

    def test_no_stderr_suppression_on_alembic_invocations(self):
        """`2>/dev/null` on an alembic call is discard point #1 of the defect."""
        src = _SCRIPT.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in src.splitlines()
            if "alembic current" in line or "alembic heads" in line
            if "2>/dev/null" in line
        ]
        self.assertEqual(offenders, [])

    def test_dsn_host_extraction_is_not_a_bare_substitution(self):
        """A `sed s|…|` that misses echoes the DSN; only `sed -n …p` fails closed."""
        src = _SCRIPT.read_text(encoding="utf-8")
        # `$DATABASE_URL` un-braced is the expansion that feeds sed; the guard
        # `[ -z "${DATABASE_URL:-}" ]` is braced and so does not match here.
        dsn_lines = [
            line
            for line in src.splitlines()
            if "$DATABASE_URL" in line
            and "sed" in line
            and not line.lstrip().startswith("#")
        ]
        self.assertTrue(dsn_lines, "no DSN-parsing line found")
        for line in dsn_lines:
            self.assertIn("sed -n", line)

    def test_header_documents_the_probes_as_logging_only(self):
        header = "\n".join(
            line
            for line in _SCRIPT.read_text(encoding="utf-8").splitlines()
            if line.startswith("#")
        )
        self.assertIn("LOGGING ONLY", header)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
