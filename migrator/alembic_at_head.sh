#!/bin/sh
# Healthcheck script: exits 0 iff alembic_version in the canonical DB
# matches the single head of the alembic chain shipped in this image.
#
# Used by the `alembic-status` sidecar service in docker-compose.yml as
# its docker healthcheck. External observability (docker inspect,
# Prometheus container-health exporters, supervisor scripts, CI smoke
# probes) can poll this service's health to learn whether the canonical
# DB is at chain head — a strictly stronger signal than postgres's
# pg_isready, which stays green even when the schema is stale.
#
# The script reuses the migrator image's existing alembic install +
# embedded `alembic/versions/` chain. `alembic current` prints the
# DB's stamped revision; `alembic heads` prints the chain head(s).
# The script fails (non-zero exit, with a diagnostic on stderr that
# docker captures into the healthcheck log) on any of:
#
#   - UNDETERMINED: `alembic current` failed, so nothing at all is known
#     about the DB's stamp. Two shapes:
#       * the DB is stamped at a revision this image's chain does not
#         contain (alembic says "Can't locate revision identified by
#         '<rev>'") — reported as a distinct, named diagnostic, because
#         the usual cause is a stale image rather than a broken DB;
#       * anything else — reported generically, quoting alembic's own
#         message and its exit status.
#   - UNDETERMINED: `alembic heads` failed, so the chain head is unknown.
#     `heads` reads only the filesystem, so this means a broken chain
#     (missing down_revision, import error), never a DB condition.
#   - UNHEALTHY: alembic current is empty (DB never stamped). This may
#     ONLY be claimed when `alembic current` SUCCEEDED and printed no
#     revision token — an empty result from a FAILED command is UNKNOWN,
#     not evidence that the DB is unstamped.
#   - UNHEALTHY: alembic heads count != 1 (multi-head divergence — the
#     same condition that broke the migrator on 2026-05-07).
#   - UNHEALTHY: `alembic current` printed more than one revision (the DB
#     is branched — stamped at several revisions at once). Reading only
#     the first row would let a branched DB whose first row happens to
#     equal the chain head report OK, i.e. a FALSE HEALTHY from the one
#     surface whose whole purpose is to be believed.
#   - UNHEALTHY: alembic current != chain head (DB is at an old
#     revision; the migrator must run, or did run and silently failed).
#   - FATAL: DATABASE_URL is not set, or the alembic project root cannot
#     be entered (exit 2). Both are "this script cannot run at all",
#     distinct from any verdict about the DB.
#
# Docker's healthcheck has three states (starting/healthy/unhealthy) and
# there is nowhere to map UNDETERMINED except non-zero — we cannot
# assert health. So UNDETERMINED still exits 1; what it must not do is
# assert a cause that was never established.
#
# Idempotent on the happy path (no DB writes; just two `alembic` CLI
# invocations that read filesystem + alembic_version table).
#
# Why a separate script vs. inline in the healthcheck: docker's
# `CMD-SHELL` healthcheck runs in /bin/sh -c and can't easily express
# multi-step logic with diagnostic output. A standalone script is
# also easier to test manually (`docker exec
# qontinui-canonical-alembic-status /alembic_at_head.sh`).
#
# Regression test: scripts/tests/test_alembic_at_head.py, run by CI's
# `python -m pytest scripts/tests/ -q` job.

set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[alembic-status] FATAL: DATABASE_URL is not set" >&2
  exit 2
fi

# The alembic project root inside the image. Overridable ONLY so the
# regression test can run this exact script outside the container; in
# the image nothing sets it and the default is used.
#
# `if ! cd` rather than a bare `cd`: under `set -e` a failed `cd` exits 1
# carrying only sh's own message — an unlabelled unhealthy verdict from
# the one script whose entire purpose is to name what it established.
app_dir="${ALEMBIC_STATUS_APP_DIR:-/app}"
if ! cd "$app_dir"; then
  echo "[alembic-status] FATAL: cannot enter alembic project root '${app_dir}'" >&2
  exit 2
fi

# Scratch dir for alembic's two streams. Each invocation's stdout and
# stderr are captured to files rather than discarded, so a failure's own
# message can be quoted back in the diagnostic.
work="$(mktemp -d 2>/dev/null || true)"
if [ -z "$work" ] || [ ! -d "$work" ]; then
  work="${TMPDIR:-/tmp}/alembic-status.$$"
  mkdir -p "$work"
fi
trap 'rm -rf "$work"' EXIT

# Echo alembic's own words back, minus the INFO preamble, indented under
# the headline. Bounded so a traceback cannot flood the healthcheck log.
emit_alembic_diagnostics() {
  cat "$1" "$2" \
    | sed -e 's/^FAILED: //' -e 's/^ERROR  *\[[^]]*\] //' \
    | grep -v '^INFO ' \
    | grep -v '^[[:space:]]*$' \
    | awk '!seen[$0]++' \
    | head -n 5 \
    | sed 's/^/  alembic: /' >&2
}

cur_out="$work/current.out"
cur_err="$work/current.err"

# Run alembic on its own so its OWN exit status is observable. In a
# pipeline `sh` reports only the LAST command's status, so the previous
# `alembic current | awk` shape reported awk's 0 and erased alembic's
# 255. Nothing is filtered until the status has been captured.
cur_status=0
alembic current >"$cur_out" 2>"$cur_err" || cur_status=$?

if [ "$cur_status" -ne 0 ]; then
  # `alembic current` failed. Nothing is known about alembic_version's
  # contents — in particular this is NEVER evidence that the DB is
  # unstamped, so it must not fall through to the "empty" branch below.
  #
  # One failure has a specific, actionable cause worth naming: the DB is
  # stamped at a revision that is not in this image's embedded chain,
  # which normally means the image predates the revision.
  stale_rev="$(cat "$cur_out" "$cur_err" \
    | sed -n "s/.*Can't locate revision identified by '\([^']*\)'.*/\1/p" \
    | head -n 1)"
  # Defensive: only trust something that actually looks like a revision
  # id; otherwise fall back to the generic branch rather than printing
  # an empty or garbled id.
  case "$stale_rev" in
    '' | *[!A-Za-z0-9_.-]*) stale_rev='' ;;
  esac

  if [ -n "$stale_rev" ]; then
    printf "[alembic-status] UNDETERMINED: DB is stamped at '%s', which this image's chain does not contain (image may be stale)\n" \
      "$stale_rev" >&2
    printf '  alembic current exit: %s\n' "$cur_status" >&2
  else
    printf '[alembic-status] UNDETERMINED: `alembic current` failed (exit %s); DB state unknown.\n' \
      "$cur_status" >&2
  fi
  emit_alembic_diagnostics "$cur_out" "$cur_err"
  exit 1
fi

# alembic current succeeded — the lowercase-anchored lines are the revs
# it is stamped at. The output format on a stamped DB is e.g.:
#   a6f606408ecb (head)
# The lowercase anchor is safe HERE (and only here) because the status
# check above already ruled out the error output: `FAILED:` is written
# to stdout, and would otherwise slip past this filter unseen.
#
# Collect EVERY revision row, not just the first, and count them — the
# same treatment `heads` gets below, and for the same reason: a count
# other than 1 is a real condition, and taking row 1 makes it invisible.
cur_revs="$(awk '/^[a-z0-9_]+/{print $1}' <"$cur_out" || true)"

cur_count=0
if [ -n "$cur_revs" ]; then
  cur_count=$(printf '%s\n' "$cur_revs" | wc -l | tr -d ' ')
fi

if [ "$cur_count" -eq 0 ]; then
  echo "[alembic-status] UNHEALTHY: alembic_version is empty (DB never stamped)" >&2
  exit 1
fi

if [ "$cur_count" -ne 1 ]; then
  echo "[alembic-status] UNHEALTHY: DB is stamped at ${cur_count} revisions (expected 1 — the DB is branched)" >&2
  printf '%s\n' "$cur_revs" | awk '{print "  current: " $0}' >&2
  exit 1
fi

cur="$cur_revs"

heads_out="$work/heads.out"
heads_err="$work/heads.err"

# Same treatment for heads. It reads only the embedded chain, so it
# cannot fail from DB state — but a broken chain can still fail it, and
# that must not silently become "0 heads".
heads_status=0
alembic heads >"$heads_out" 2>"$heads_err" || heads_status=$?

if [ "$heads_status" -ne 0 ]; then
  printf '[alembic-status] UNDETERMINED: `alembic heads` failed (exit %s); chain head unknown.\n' \
    "$heads_status" >&2
  emit_alembic_diagnostics "$heads_out" "$heads_err"
  exit 1
fi

# Count how many head lines appear. >1 means multi-head divergence. The
# chain head is the single head's rev id (first column of the line).
heads_lines="$(awk '/^[a-z0-9_]+/{print $1}' <"$heads_out" || true)"

heads_count=0
if [ -n "$heads_lines" ]; then
  heads_count=$(printf '%s\n' "$heads_lines" | wc -l | tr -d ' ')
fi

if [ "$heads_count" -ne 1 ]; then
  echo "[alembic-status] UNHEALTHY: alembic chain has ${heads_count} heads (expected 1)" >&2
  if [ "$heads_count" -gt 1 ]; then
    printf '%s\n' "$heads_lines" | awk '{print "  head: " $0}' >&2
  fi
  exit 1
fi

head="$heads_lines"

if [ "$cur" != "$head" ]; then
  echo "[alembic-status] UNHEALTHY: DB at ${cur}; chain head is ${head}" >&2
  exit 1
fi

# Healthy: cur == head and chain has exactly one head.
echo "[alembic-status] OK: at head ${head}"
exit 0
