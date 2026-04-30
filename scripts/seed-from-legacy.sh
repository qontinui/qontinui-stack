#!/usr/bin/env bash
# Seed the canonical PG (qontinui-stack, port 5433) from the legacy PG
# (qontinui-web stack, port 5432). One-shot; run when both stacks are up
# and the canonical PG is empty (post-`docker compose up -d` of qontinui-stack).
#
# This is the "Phase 1 item 4" deliverable from topology plan §9: bring the
# canonical DB into existence with the same row-level state the user has
# been developing against on the PC. After this completes, the runner can
# point at the canonical DB and lose nothing.
#
# Strategy:
#   1. pg_dump from legacy PG (5432) — schema-only, then data-only, both
#      compressed; --schema=runner --schema=public to skip alembic_version
#      meta if needed.
#   2. Restore schema-only into canonical PG (5433). The four canonical
#      schemas (project/coord/agent/auth) already exist via init-scripts;
#      the legacy `runner` schema is restored too — Phase 1 of the
#      consolidation plan will rename runner→project later.
#   3. Restore data-only.
#   4. Sanity check: row counts in legacy.runner.task_runs ==
#      canonical.runner.task_runs (for one anchor table).
#
# Idempotency: re-running this script after a successful seed will fail
# (PG will refuse to recreate existing tables). To re-seed, drop the data
# volume of qontinui-stack first: `docker compose down -v && docker compose up -d`.

set -euo pipefail

LEGACY_HOST="${LEGACY_HOST:-localhost}"
LEGACY_PORT="${LEGACY_PORT:-5432}"
LEGACY_USER="${LEGACY_USER:-qontinui_user}"
LEGACY_DB="${LEGACY_DB:-qontinui_db}"
LEGACY_PASSWORD="${LEGACY_PASSWORD:-qontinui_dev_password}"

CANONICAL_HOST="${CANONICAL_HOST:-localhost}"
CANONICAL_PORT="${CANONICAL_PORT:-5433}"
CANONICAL_USER="${CANONICAL_USER:-qontinui_user}"
CANONICAL_DB="${CANONICAL_DB:-qontinui_db}"
CANONICAL_PASSWORD="${CANONICAL_PASSWORD:-qontinui_dev_password}"

DUMP_FILE="${DUMP_FILE:-./qontinui_legacy_dump_$(date -u +%Y%m%dT%H%M%SZ).sql}"

echo "==> Probing legacy PG at ${LEGACY_HOST}:${LEGACY_PORT}..."
PGPASSWORD="$LEGACY_PASSWORD" pg_isready -h "$LEGACY_HOST" -p "$LEGACY_PORT" -U "$LEGACY_USER" -d "$LEGACY_DB"

echo "==> Probing canonical PG at ${CANONICAL_HOST}:${CANONICAL_PORT}..."
PGPASSWORD="$CANONICAL_PASSWORD" pg_isready -h "$CANONICAL_HOST" -p "$CANONICAL_PORT" -U "$CANONICAL_USER" -d "$CANONICAL_DB"

echo "==> Dumping legacy DB to ${DUMP_FILE}..."
PGPASSWORD="$LEGACY_PASSWORD" pg_dump \
    -h "$LEGACY_HOST" -p "$LEGACY_PORT" -U "$LEGACY_USER" \
    --no-owner --no-privileges \
    "$LEGACY_DB" > "$DUMP_FILE"

DUMP_SIZE=$(wc -c < "$DUMP_FILE" | tr -d ' ')
echo "==> Dump complete: ${DUMP_SIZE} bytes."

echo "==> Restoring into canonical DB..."
PGPASSWORD="$CANONICAL_PASSWORD" psql \
    -h "$CANONICAL_HOST" -p "$CANONICAL_PORT" -U "$CANONICAL_USER" \
    -d "$CANONICAL_DB" \
    -v ON_ERROR_STOP=1 \
    -f "$DUMP_FILE"

echo "==> Sanity check: row counts on anchor tables..."
for tbl in "runner.task_runs" "runner.unified_workflows" "public.alembic_version"; do
    legacy_count=$(PGPASSWORD="$LEGACY_PASSWORD" psql -h "$LEGACY_HOST" -p "$LEGACY_PORT" -U "$LEGACY_USER" -d "$LEGACY_DB" -tAc "SELECT COUNT(*) FROM $tbl" 2>/dev/null || echo "?")
    canonical_count=$(PGPASSWORD="$CANONICAL_PASSWORD" psql -h "$CANONICAL_HOST" -p "$CANONICAL_PORT" -U "$CANONICAL_USER" -d "$CANONICAL_DB" -tAc "SELECT COUNT(*) FROM $tbl" 2>/dev/null || echo "?")
    if [ "$legacy_count" = "$canonical_count" ]; then
        echo "    OK  $tbl: $legacy_count == $canonical_count"
    else
        echo "    MISMATCH  $tbl: legacy=$legacy_count canonical=$canonical_count"
    fi
done

echo ""
echo "==> Seed complete. Dump retained at $DUMP_FILE for rollback."
echo "    To repoint the runner, edit ~/.qontinui/profiles.json (or run 'qontinui_profile use dev')."
