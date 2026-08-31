"""Tests for ``migrator/alembic_at_head.sh``.

Runnable via either::

    python -m pytest qontinui-stack/scripts/tests/test_alembic_at_head.py
    python -m unittest qontinui-stack.scripts.tests.test_alembic_at_head

Written against ``unittest.TestCase`` so both runners work, matching the sibling
``test_terraform_sensitive_lint.py`` / ``test_symbol_claims_by_machine.py``
convention. Stdlib only — the CI job that picks this up
(``.github/workflows/qontinui-ci.yml``, *"python script unit tests"*) installs
nothing but pytest and runs ``python -m pytest scripts/tests/ -q`` over the
whole directory, so this file needs no wiring beyond existing.

**What is under test is the MESSAGE, not the exit code.** Docker healthchecks
have three states (starting/healthy/unhealthy): exit 0 is healthy and *any*
non-zero is unhealthy. Both the pre-fix false claim and the post-fix honest one
exit 1, so the exit code cannot distinguish them and asserting on it would pass
against the very bug this file exists to catch. Exit status is still checked
where it carries independent meaning (0 on the happy path, 2 on the
DATABASE_URL fatal).

The defect reproduced by :class:`StaleImageTests` is the measured state of
``qontinui-canonical-alembic-status`` on 2026-08-31: three days unhealthy
(FailingStreak 9733) emitting ``alembic_version is empty (DB never stamped)``
against a DB that was stamped, reachable and carrying 52 live connections. The
real condition was ``alembic current`` exiting **255** with *"Can't locate
revision identified by 'coord_specq_01_speculative_ci_status'"* — thrown away
three times over: by ``2>/dev/null``, by an ``awk`` filter anchored to lowercase
(``FAILED:`` arrives on *stdout* and survived the redirect), and by a pipeline
whose exit status is ``awk``'s rather than alembic's.

The script is driven as a subprocess against a fake ``alembic`` on ``PATH``, so
what is exercised is the shipped file byte-for-byte rather than a reimplementation
of its logic in Python.
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
_SCRIPT = _REPO_ROOT / "migrator" / "alembic_at_head.sh"

# The revision the canonical dev DB is stamped at, and which the 2026-05-25
# migrator image's 219-file chain does not contain.
_STAMPED_REV = "coord_specq_01_speculative_ci_status"
# The single head of that stale image's embedded chain.
_IMAGE_HEAD = "coord_handoff_requests"

_SH = shutil.which("sh") or shutil.which("bash")


# ---------------------------------------------------------------------------
# Fake `alembic` shims
# ---------------------------------------------------------------------------

# Today's measured state. Note precisely which stream each line goes to: the
# INFO preamble and the ERROR line are stderr, the `FAILED:` line is STDOUT.
# That split is the whole point — the old awk filter read the stream carrying
# the diagnosis and dropped it on a character-class detail.
_SHIM_CURRENT_CANNOT_LOCATE = f"""#!/bin/sh
if [ "$1" = "current" ]; then
  echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
  echo "INFO  [alembic.runtime.migration] Will assume transactional DDL." >&2
  echo "FAILED: Can't locate revision identified by '{_STAMPED_REV}'"
  echo "ERROR  [alembic.util.messaging] Can't locate revision identified by '{_STAMPED_REV}'" >&2
  exit 255
fi
if [ "$1" = "heads" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
exit 0
"""

# A failure with no locatable revision id in it — the generic UNDETERMINED arm.
_SHIM_CURRENT_DB_DOWN = f"""#!/bin/sh
if [ "$1" = "current" ]; then
  echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
  echo "FAILED: (psycopg2.OperationalError) connection to server at \\"db\\" failed" >&2
  exit 1
fi
if [ "$1" = "heads" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
exit 0
"""

# A genuinely unstamped DB: `alembic current` SUCCEEDS and prints no revision.
_SHIM_CURRENT_TRULY_EMPTY = f"""#!/bin/sh
if [ "$1" = "current" ]; then
  echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
  echo "INFO  [alembic.runtime.migration] Will assume transactional DDL." >&2
  exit 0
fi
if [ "$1" = "heads" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
exit 0
"""

_SHIM_AT_HEAD = f"""#!/bin/sh
echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl." >&2
if [ "$1" = "current" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
if [ "$1" = "heads" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
exit 0
"""

_SHIM_BEHIND_HEAD = f"""#!/bin/sh
if [ "$1" = "current" ]; then
  echo "oldrev01"
  exit 0
fi
if [ "$1" = "heads" ]; then
  echo "{_IMAGE_HEAD} (head)"
  exit 0
fi
exit 0
"""

# The 2026-05-07 divergence the sidecar was built to catch.
_SHIM_MULTI_HEAD = """#!/bin/sh
if [ "$1" = "current" ]; then
  echo "cr01a2b3c4d5"
  exit 0
fi
if [ "$1" = "heads" ]; then
  echo "cr01a2b3c4d5 (head)"
  echo "7c5e4d3b2a1f (head)"
  exit 0
fi
exit 0
"""

# `heads` reads only the filesystem, so it cannot fail from DB state — but a
# broken chain still fails it, and that must not become "0 heads".
_SHIM_HEADS_BROKEN = """#!/bin/sh
if [ "$1" = "current" ]; then
  echo "oldrev01"
  exit 0
fi
if [ "$1" = "heads" ]; then
  echo "FAILED: Can't locate revision identified by 'missing_down_revision'"
  exit 255
fi
exit 0
"""


class _Result:
    """A completed run of the script, with both streams and the exit status."""

    def __init__(self, proc: subprocess.CompletedProcess) -> None:
        self.returncode = proc.returncode
        self.stdout = proc.stdout
        self.stderr = proc.stderr

    @property
    def output(self) -> str:
        """Everything the healthcheck emitted; docker captures both streams."""
        return self.stdout + self.stderr


# Each shim is deterministic, so a given (shim, DATABASE_URL) pair always
# produces the same run. Several tests assert different properties of the same
# run; memoising keeps this file at one subprocess per distinct scenario rather
# than one per assertion.
_RUN_CACHE: dict[tuple[str, str | None], "_Result"] = {}


@unittest.skipIf(_SH is None, "no POSIX sh on PATH")
class _ScriptCase(unittest.TestCase):
    """Runs the real ``alembic_at_head.sh`` with a fake ``alembic`` on PATH."""

    def run_script(
        self,
        shim: str,
        database_url: str | None = "postgresql://u:p@db:5432/qontinui_db",
    ) -> _Result:
        key = (shim, database_url)
        if key not in _RUN_CACHE:
            _RUN_CACHE[key] = self._run_script_uncached(shim, database_url)
        return _RUN_CACHE[key]

    def _run_script_uncached(self, shim: str, database_url: str | None) -> _Result:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        bindir = root / "bin"
        appdir = root / "app"
        bindir.mkdir()
        appdir.mkdir()

        fake = bindir / "alembic"
        fake.write_text(shim, encoding="utf-8", newline="\n")
        fake.chmod(0o755)

        env = dict(os.environ)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        # The image's alembic project root. Overridden so the shipped script
        # can run verbatim outside the container, where /app does not exist.
        env["ALEMBIC_STATUS_APP_DIR"] = str(appdir)
        if database_url is None:
            env.pop("DATABASE_URL", None)
        else:
            env["DATABASE_URL"] = database_url

        assert _SH is not None
        proc = subprocess.run(
            [_SH, str(_SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return _Result(proc)


class StaleImageTests(_ScriptCase):
    """The measured 2026-08-31 incident: `alembic current` exits 255.

    Every assertion here fails against the pre-fix script, which reported
    ``alembic_version is empty (DB never stamped)`` about a stamped DB.
    """

    def test_does_not_claim_the_db_was_never_stamped(self):
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertNotIn("never stamped", res.output)
        self.assertNotIn("alembic_version is empty", res.output)

    def test_reports_undetermined(self):
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertIn("UNDETERMINED", res.output)

    def test_names_the_revision_the_db_is_stamped_at(self):
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertIn(_STAMPED_REV, res.output)

    def test_says_the_image_chain_does_not_contain_it(self):
        """Phase 3: the stale-image case is its own diagnostic, not the generic one."""
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertIn(
            f"UNDETERMINED: DB is stamped at '{_STAMPED_REV}', "
            "which this image's chain does not contain (image may be stale)",
            res.output,
        )

    def test_surfaces_alembics_own_message(self):
        """`2>/dev/null` used to discard this; it must reach the operator."""
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertIn("Can't locate revision identified by", res.output)

    def test_surfaces_the_real_exit_status(self):
        """The pipeline used to report awk's 0 in place of alembic's 255."""
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertIn("255", res.output)

    def test_still_unhealthy(self):
        """Docker has no UNKNOWN state — honesty must not turn the container green."""
        res = self.run_script(_SHIM_CURRENT_CANNOT_LOCATE)
        self.assertNotEqual(res.returncode, 0)


class GenericUndeterminedTests(_ScriptCase):
    """A failure carrying no revision id falls back to the generic branch."""

    def test_reports_generic_undetermined_with_exit_status(self):
        res = self.run_script(_SHIM_CURRENT_DB_DOWN)
        self.assertIn(
            "UNDETERMINED: `alembic current` failed (exit 1); DB state unknown.",
            res.output,
        )

    def test_does_not_claim_empty(self):
        res = self.run_script(_SHIM_CURRENT_DB_DOWN)
        self.assertNotIn("never stamped", res.output)

    def test_does_not_print_an_empty_revision_id(self):
        """Defensive fallback: no ``stamped at ''`` when nothing could be parsed."""
        res = self.run_script(_SHIM_CURRENT_DB_DOWN)
        self.assertNotIn("stamped at ''", res.output)
        self.assertNotIn("DB is stamped at", res.output)

    def test_surfaces_alembics_own_message(self):
        res = self.run_script(_SHIM_CURRENT_DB_DOWN)
        self.assertIn("psycopg2.OperationalError", res.output)


class TrulyEmptyTests(_ScriptCase):
    """The "empty" claim survives — but only where it is actually earned."""

    def test_successful_and_empty_still_reports_empty(self):
        res = self.run_script(_SHIM_CURRENT_TRULY_EMPTY)
        self.assertIn(
            "UNHEALTHY: alembic_version is empty (DB never stamped)", res.output
        )
        self.assertNotIn("UNDETERMINED", res.output)
        self.assertNotEqual(res.returncode, 0)


class HealthyTests(_ScriptCase):
    def test_at_head_is_healthy(self):
        res = self.run_script(_SHIM_AT_HEAD)
        self.assertEqual(res.returncode, 0, res.output)
        self.assertIn(f"OK: at head {_IMAGE_HEAD}", res.output)
        self.assertNotIn("UNHEALTHY", res.output)
        self.assertNotIn("UNDETERMINED", res.output)


class DivergenceTests(_ScriptCase):
    def test_multi_head_still_reported(self):
        res = self.run_script(_SHIM_MULTI_HEAD)
        self.assertIn("UNHEALTHY: alembic chain has 2 heads (expected 1)", res.output)
        self.assertIn("head: cr01a2b3c4d5", res.output)
        self.assertIn("head: 7c5e4d3b2a1f", res.output)
        self.assertNotEqual(res.returncode, 0)

    def test_behind_head_still_reported(self):
        res = self.run_script(_SHIM_BEHIND_HEAD)
        self.assertIn(f"UNHEALTHY: DB at oldrev01; chain head is {_IMAGE_HEAD}", res.output)
        self.assertNotEqual(res.returncode, 0)


class HeadsFailureTests(_ScriptCase):
    """A failing `alembic heads` must not silently become a 0-head reading."""

    def test_reports_undetermined_rather_than_zero_heads(self):
        res = self.run_script(_SHIM_HEADS_BROKEN)
        self.assertIn(
            "UNDETERMINED: `alembic heads` failed (exit 255); chain head unknown.",
            res.output,
        )
        self.assertNotIn("has 0 heads", res.output)
        self.assertNotEqual(res.returncode, 0)

    def test_surfaces_alembics_own_message(self):
        res = self.run_script(_SHIM_HEADS_BROKEN)
        self.assertIn("missing_down_revision", res.output)


class FatalTests(_ScriptCase):
    def test_missing_database_url_is_exit_2(self):
        res = self.run_script(_SHIM_AT_HEAD, database_url=None)
        self.assertEqual(res.returncode, 2, res.output)
        self.assertIn("FATAL: DATABASE_URL is not set", res.output)


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

    def test_header_documents_the_undetermined_branch(self):
        """The header's failure list must match the branches that exist."""
        header = "\n".join(
            line
            for line in _SCRIPT.read_text(encoding="utf-8").splitlines()
            if line.startswith("#")
        )
        self.assertIn("UNDETERMINED", header)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
