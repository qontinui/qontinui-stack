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
# That no-op is the ONE place a logging-only probe may SUPPRESS the
# upgrade, so it fires only on an UNAMBIGUOUS reading: exactly one
# stamped revision AND exactly one chain head. Either probe reporting
# more than one leaves its rev empty and falls through to the upgrade,
# for the same reason a failed probe does. Reading only the first row
# would let two real conditions exit 0 having done nothing:
#
#   - a MULTI-HEAD chain (the 2026-05-07 failure mode the sibling
#     healthcheck already guards) whose first head happens to be where
#     the DB is stamped — `alembic upgrade head` would have said
#     "Multiple head revisions are present";
#   - a BRANCHED DB stamped at several revisions, of which the first
#     matches the head.
#
# In ECS the migrator task is the only signal there is, so a silent
# exit 0 there reports a deploy as migrated when it is not.
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

# Collect EVERY revision-shaped row rather than the first, so an
# ambiguous reading is COUNTABLE instead of invisible. A revision id is
# lowercase alnum/underscore, so the anchor also drops alembic's INFO
# preamble now that stderr is merged into these streams.
current_revs=""
current_count=0
if [ "$current_status" -eq 0 ]; then
  current_revs="$(printf '%s\n' "$current_output" | awk '/^[a-z0-9_]+/{print $1}')"
  if [ -n "$current_revs" ]; then
    current_count=$(printf '%s\n' "$current_revs" | wc -l | tr -d ' ')
  fi
fi

# `alembic heads` marks each head with `(head)`; count the marks.
heads_revs=""
heads_count=0
if [ "$heads_status" -eq 0 ]; then
  heads_revs="$(printf '%s\n' "$heads_output" | awk '/\(head\)/{print $1}')"
  if [ -n "$heads_revs" ]; then
    heads_count=$(printf '%s\n' "$heads_revs" | wc -l | tr -d ' ')
  fi
fi

# Only a count of exactly 1 yields a rev to compare. Anything else — a
# failed probe, nothing stamped, or an ambiguous multi-row reading —
# leaves the rev empty, which is what makes the no-op below unreachable.
current_rev=""
if [ "$current_count" -eq 1 ]; then
  current_rev="$current_revs"
fi

head_rev=""
if [ "$heads_count" -eq 1 ]; then
  head_rev="$heads_revs"
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
elif [ "$current_count" -gt 1 ]; then
  echo "[migrator] alembic current: ${current_count} revisions stamped (expected 1) — DB is branched"
  printf '%s\n' "$current_revs" | sed 's/^/[migrator]   current: /'
else
  echo "[migrator] alembic current: ${current_rev:-<none>}"
fi

if [ "$heads_status" -ne 0 ]; then
  echo "[migrator] alembic head:    FAILED (exit ${heads_status}) — chain head unknown"
  log_alembic_failure "$heads_output"
elif [ "$heads_count" -gt 1 ]; then
  echo "[migrator] alembic head:    ${heads_count} heads (expected 1) — chain has diverged"
  printf '%s\n' "$heads_revs" | sed 's/^/[migrator]   head: /'
else
  echo "[migrator] alembic head:    ${head_rev:-<none>}"
fi

if [ -n "$current_rev" ] && [ "$current_rev" = "$head_rev" ]; then
  echo "[migrator] DB already at head — no-op"
  exit 0
fi

echo "[migrator] running: alembic upgrade head"
exec alembic upgrade head
