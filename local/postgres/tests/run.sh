#!/usr/bin/env bash
# run.sh — the local/postgres test suite against real PostgreSQL 16 + pgvector.
#
# Starts a PUBLISHER and a SUBSCRIBER server (pgvector/pgvector:pg16,
# wal_level=logical, data on tmpfs, no host port published) and a tools
# container that runs tests/inside.sh: every test green on the real tree, then
# every mutation in tests/mutations.py against a mutated copy, each of which
# must turn its named tests RED. Everything is removed on exit.
#
# Networking: a dedicated docker network ("pub"/"sub" aliases). If the docker
# daemon cannot allocate another network (its address pools are exhausted —
# this happens on busy CI hosts), the three containers share the publisher's
# network namespace instead (publisher :5432, subscriber :5433). Either way no
# host port is used.
#
# Usage: tests/run.sh [--no-mutations] [--only <unittest id>]... [--keep]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(dirname "$HERE")"
PG_IMAGE="${QPG_PG_IMAGE:-pgvector/pgvector:pg16}"
TOOLS_IMAGE="${QPG_TOOLS_IMAGE:-qontinui-pg-tests:local}"
ID="qpgt-$$-$RANDOM"
KEEP=0
INSIDE_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-mutations) INSIDE_ARGS+=(--no-mutations) ;;
    --only) INSIDE_ARGS+=(--only "$2"); shift ;;
    --keep) KEEP=1 ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done

TMP="$(mktemp -d)"
NET=""
cleanup() {
  if [ "$KEEP" = 0 ]; then
    docker rm -fv "$ID-tools" "$ID-sub" "$ID-pub" >/dev/null 2>&1 || true
    if [ -n "$NET" ]; then docker network rm "$NET" >/dev/null 2>&1 || true; fi
  else
    echo "kept containers $ID-pub $ID-sub (remove with: docker rm -fv $ID-pub $ID-sub)"
  fi
  rm -rf "$TMP"
}
trap cleanup EXIT

echo "==> building $TOOLS_IMAGE"
docker build -q -t "$TOOLS_IMAGE" -f "$HERE/Dockerfile" "$HERE" >/dev/null

pw() { python3 -c 'import secrets; print(secrets.token_hex(16))'; }
SUPER_PW="$(pw)"; APP_PW="$(pw)"; REPL_PW="$(pw)"
{
  echo "*:*:*:postgres:$SUPER_PW"
  echo "*:*:*:qontinui_user:$APP_PW"
  echo "*:*:*:qontinui_replication:$REPL_PW"
} > "$TMP/pgpass"
chmod 0600 "$TMP/pgpass"

SERVER_ARGS=(-c wal_level=logical -c max_replication_slots=20 -c max_wal_senders=20
             -c max_logical_replication_workers=8 -c wal_retrieve_retry_interval=1s)
# shellcheck disable=SC2054  # the comma is inside one postgres setting value
SUB_LOG_ARGS=(-c logging_collector=on -c "log_destination=stderr,jsonlog")

if docker network create "$ID" >/dev/null 2>&1; then
  NET="$ID"
  echo "==> network mode: docker network $NET"
  docker run -d --name "$ID-pub" --network "$NET" --network-alias pub --tmpfs /var/lib/postgresql/data:rw \
    -e POSTGRES_PASSWORD="$SUPER_PW" "$PG_IMAGE" "${SERVER_ARGS[@]}" >/dev/null
  docker run -d --name "$ID-sub" --network "$NET" --network-alias sub --tmpfs /var/lib/postgresql/data:rw \
    -e POSTGRES_PASSWORD="$SUPER_PW" "$PG_IMAGE" "${SERVER_ARGS[@]}" "${SUB_LOG_ARGS[@]}" >/dev/null
  TOOLS_NET=(--network "$NET")
  ENV=(-e PUB_HOST=pub -e PUB_PORT=5432 -e SUB_HOST=sub -e SUB_PORT=5432
       -e PUB_FROM_SUB_HOST=pub -e PUB_FROM_SUB_PORT=5432)
  SUB_PORT=5432
else
  echo "==> network mode: shared namespace (docker could not create a network)"
  docker run -d --name "$ID-pub" --tmpfs /var/lib/postgresql/data:rw \
    -e POSTGRES_PASSWORD="$SUPER_PW" "$PG_IMAGE" "${SERVER_ARGS[@]}" >/dev/null
  docker run -d --name "$ID-sub" --network "container:$ID-pub" --tmpfs /var/lib/postgresql/data:rw \
    -e POSTGRES_PASSWORD="$SUPER_PW" -e PGPORT=5433 "$PG_IMAGE" -c port=5433 "${SERVER_ARGS[@]}" "${SUB_LOG_ARGS[@]}" >/dev/null
  TOOLS_NET=(--network "container:$ID-pub")
  ENV=(-e PUB_HOST=127.0.0.1 -e PUB_PORT=5432 -e SUB_HOST=127.0.0.1 -e SUB_PORT=5433
       -e PUB_FROM_SUB_HOST=127.0.0.1 -e PUB_FROM_SUB_PORT=5432)
  SUB_PORT=5433
fi

wait_ready() {  # container port
  local _
  for _ in $(seq 1 120); do
    # The image's init phase runs a socket-only server; TCP answers only once
    # the real server is up.
    if docker exec "$1" pg_isready -q -h 127.0.0.1 -p "$2" 2>/dev/null; then return 0; fi
    sleep 1
  done
  docker logs "$1" | tail -20
  echo "server $1 did not become ready" >&2
  return 1
}
wait_ready "$ID-pub" 5432
wait_ready "$ID-sub" "$SUB_PORT"

for c in "$ID-pub:5432" "$ID-sub:$SUB_PORT"; do
  docker exec -i -e PGPORT="${c##*:}" "${c%%:*}" psql -X -q -v ON_ERROR_STOP=1 -U postgres >/dev/null <<SQL
CREATE ROLE qontinui_user LOGIN CREATEROLE NOBYPASSRLS NOSUPERUSER PASSWORD '$APP_PW';
CREATE ROLE qontinui_replication LOGIN REPLICATION PASSWORD '$REPL_PW';
SQL
done

echo "==> running tests in $ID-tools"
docker run --name "$ID-tools" "${TOOLS_NET[@]}" "${ENV[@]}" \
  -e PGPASSFILE=/run/qpg/pgpass -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$SRC:/src:ro" -v "$TMP:/run/qpg:ro" \
  "$TOOLS_IMAGE" bash /src/tests/inside.sh "${INSIDE_ARGS[@]}"
