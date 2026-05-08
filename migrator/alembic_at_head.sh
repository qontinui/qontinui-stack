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
# The script fails (non-zero exit, with a one-line diagnostic on
# stderr that docker captures into the healthcheck log) on any of:
#
#   - alembic current is empty (DB never stamped).
#   - alembic heads count != 1 (multi-head divergence — the same
#     condition that broke the migrator on 2026-05-07).
#   - alembic current != chain head (DB is at an old revision; the
#     migrator must run, or did run and silently failed).
#
# Idempotent on the happy path (no DB writes; just two `alembic` CLI
# invocations that read filesystem + alembic_version table).
#
# Why a separate script vs. inline in the healthcheck: docker's
# `CMD-SHELL` healthcheck runs in /bin/sh -c and can't easily express
# multi-step logic with diagnostic output. A standalone script is
# also easier to test manually (`docker exec
# qontinui-canonical-alembic-status /alembic_at_head.sh`).

set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[alembic-status] FATAL: DATABASE_URL is not set" >&2
  exit 2
fi

cd /app

# alembic current — first non-INFO line is the rev (or empty if DB
# isn't stamped). The output format on a stamped DB is e.g.:
#   INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
#   INFO  [alembic.runtime.migration] Will assume transactional DDL.
#   a6f606408ecb (head)
# We want the trailing rev token, head marker stripped.
cur="$(alembic current 2>/dev/null \
  | awk '/^[a-z0-9_]+/{print $1; exit}' \
  || true)"

if [ -z "$cur" ]; then
  echo "[alembic-status] UNHEALTHY: alembic_version is empty (DB never stamped)" >&2
  exit 1
fi

# alembic heads — count how many head lines appear. >1 means multi-head
# divergence. The chain head is the single head's rev id (first column
# of the matching line).
heads_lines="$(alembic heads 2>/dev/null \
  | awk '/^[a-z0-9_]+/{print $1}' \
  || true)"

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
