#!/bin/sh
# qontinui canonical-DB migrator entrypoint.
#
# Runs `alembic upgrade head` against $DATABASE_URL, then exits. Exit code 0
# on success (including "already at head" no-op); non-zero on any alembic
# error so docker compose surfaces the failure.

set -eu

if [ -z "${DATABASE_URL:-}" ]; then
  echo "[migrator] FATAL: DATABASE_URL is not set" >&2
  exit 2
fi

cd /app

current_rev="$(alembic current 2>/dev/null | awk '/\(head\)/{print $1; exit}{rev=$1} END{if (rev) print rev}')"
head_rev="$(alembic heads 2>/dev/null | awk '/\(head\)/{print $1; exit}')"

echo "[migrator] DATABASE_URL host=$(printf '%s' "$DATABASE_URL" | sed -E 's|.*@([^/]+)/.*|\1|')"
echo "[migrator] alembic current: ${current_rev:-<none>}"
echo "[migrator] alembic head:    ${head_rev:-<none>}"

if [ -n "$current_rev" ] && [ "$current_rev" = "$head_rev" ]; then
  echo "[migrator] DB already at head — no-op"
  exit 0
fi

echo "[migrator] running: alembic upgrade head"
exec alembic upgrade head
