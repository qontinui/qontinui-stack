#!/bin/sh
# qontinui canonical-DB migrator entrypoint.
#
# Runs `alembic upgrade head` against $DATABASE_URL, then exits. Exit code 0
# on success (including "already at head" no-op); non-zero on any alembic
# error so docker compose surfaces the failure.
#
# The two pre-flight probes below are LOGGING ONLY — they never decide
# whether the upgrade runs, beyond the pre-existing "already at head"
# no-op. A probe that fails leaves its rev empty, which falls through to
# `alembic upgrade head`, which then fails loudly. That is deliberate:
# the migrator's job is to run the upgrade and let it speak for itself.
#
# This script is the image ENTRYPOINT for BOTH the local compose
# `migrator` one-shot and the ECS migrator task (aws/modules/migrator),
# so its log lines land in `docker compose logs` and in CloudWatch.
#
# Regression test: scripts/tests/test_migrator_entrypoint.py, run by
# CI's `python -m pytest scripts/tests/ -q` job.

set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[migrator] FATAL: DATABASE_URL is not set" >&2
  exit 2
fi

# The alembic project root inside the image. Overridable ONLY so the
# regression test can drive this exact script outside the container,
# where /app does not exist; nothing in the image or the ECS task
# definition sets it. Mirrors ALEMBIC_STATUS_APP_DIR in the sibling
# healthcheck script, which is the same image's other entry point.
#
# `if ! cd` rather than a bare `cd`: under `set -e` a failed `cd` exits 1
# carrying only sh's own message, i.e. an unlabelled failure from a
# script whose remaining output is uniformly `[migrator]`-tagged.
app_dir="${MIGRATOR_APP_DIR:-/app}"
if ! cd "$app_dir"; then
  echo "[migrator] FATAL: cannot enter alembic project root '${app_dir}'" >&2
  exit 2
fi

# Capture each probe's OWN exit status and its output (stderr merged in,
# rather than discarded to /dev/null). A pipeline reports only its LAST
# command's status, so `alembic current | awk` used to report awk's 0 and
# erase alembic's error — and the un-anchored awk then mis-read the
# `FAILED:` token, which alembic writes to STDOUT, as a revision id.
current_status=0
current_output="$(alembic current 2>&1)" || current_status=$?

heads_status=0
heads_output="$(alembic heads 2>&1)" || heads_status=$?

# A revision id is lowercase alnum/underscore, so the anchor also drops
# alembic's INFO preamble now that stderr is merged into these streams.
current_rev=""
if [ "$current_status" -eq 0 ]; then
  current_rev="$(printf '%s\n' "$current_output" \
    | awk '/\(head\)/{print $1; exit} /^[a-z0-9_]+/{rev=$1} END{if (rev) print rev}')"
fi

head_rev=""
if [ "$heads_status" -eq 0 ]; then
  head_rev="$(printf '%s\n' "$heads_output" | awk '/\(head\)/{print $1; exit}')"
fi

# Echo alembic's own words back, minus the INFO preamble and its
# redundant `FAILED:` / `ERROR [...]` prefixes. Bounded so a traceback
# cannot flood the compose log.
log_alembic_failure() {
  printf '%s\n' "$1" \
    | sed -e 's/^FAILED: //' -e 's/^ERROR  *\[[^]]*\] //' \
    | grep -v '^INFO ' \
    | grep -v '^[[:space:]]*$' \
    | awk '!seen[$0]++' \
    | head -n 5 \
    | sed 's/^/[migrator]   alembic: /'
}

# Only host:port is logged, never the DSN — it carries the password. The
# previous form was `sed -E 's|.*@([^/]+)/.*|\1|'`, a SUBSTITUTION, which
# echoes its input unchanged when the pattern does not match: a DSN with
# credentials but no trailing `/<database>` (e.g.
# `postgresql://u:pw@host:5432`) printed the whole string, password
# included, into the compose log and into CloudWatch. `sed -n …p` prints
# ONLY on a match, so an unparseable DSN now says so instead.
#
# The greedy `.*@` deliberately anchors on the LAST `@`, so a password
# containing `@` cannot leak its tail through the host capture.
db_host="$(printf '%s' "$DATABASE_URL" | sed -n -E 's|.*@([^/?]+).*|\1|p')"
echo "[migrator] DATABASE_URL host=${db_host:-<unparsed>}"

if [ "$current_status" -ne 0 ]; then
  echo "[migrator] alembic current: FAILED (exit ${current_status}) — DB revision unknown"
  log_alembic_failure "$current_output"
else
  echo "[migrator] alembic current: ${current_rev:-<none>}"
fi

if [ "$heads_status" -ne 0 ]; then
  echo "[migrator] alembic head:    FAILED (exit ${heads_status}) — chain head unknown"
  log_alembic_failure "$heads_output"
else
  echo "[migrator] alembic head:    ${head_rev:-<none>}"
fi

if [ -n "$current_rev" ] && [ "$current_rev" = "$head_rev" ]; then
  echo "[migrator] DB already at head — no-op"
  exit 0
fi

echo "[migrator] running: alembic upgrade head"
exec alembic upgrade head
