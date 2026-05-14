#!/bin/sh
# qontinui canonical sccache entrypoint.
#
# 1. Initialize MinIO bucket `qontinui-sccache` (idempotent).
# 2. Start the sccache daemon (listens on :4226 — dormant until Phase 6
#    cloud-burst CI workers connect; harmless to run unused).
# 3. Start the stats HTTP server on :4227 — coord scrapes this.
#
# All three steps run with the same SCCACHE_* env that local agent / build
# machines also use, so a stats query against this container reflects the
# same bucket every other client reads/writes.

set -eu

MINIO_HOST="${SCCACHE_MINIO_HOST:-minio:9000}"
MINIO_USER="${SCCACHE_MINIO_USER:-minioadmin}"
MINIO_PASS="${SCCACHE_MINIO_PASS:-minioadmin}"
BUCKET="${SCCACHE_BUCKET:-qontinui-sccache}"

echo "[sccache] initializing MinIO bucket ${BUCKET} at ${MINIO_HOST}"
# Retry mc alias because minio may not be reachable for the first second
# or two of cold boot even though depends_on:service_healthy is set —
# alpine's busybox `mc` returns non-zero on network blip. Five tries at
# 2s intervals covers cold start.
i=0
until mc alias set canonical "http://${MINIO_HOST}" "${MINIO_USER}" "${MINIO_PASS}" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "${i}" -ge 5 ]; then
        echo "[sccache] FATAL: could not connect to MinIO at ${MINIO_HOST} after 5 attempts" >&2
        exit 1
    fi
    echo "[sccache] mc alias attempt ${i} failed; retrying in 2s"
    sleep 2
done

mc mb "canonical/${BUCKET}" --ignore-existing >/dev/null
echo "[sccache] bucket ${BUCKET} ready"

# Configure local sccache daemon (used in Phase 6 by in-container builders).
# These are the same vars every client sets; the container is just another
# client from sccache's POV.
export SCCACHE_BUCKET="${BUCKET}"
export SCCACHE_S3_USE_SSL=off
export SCCACHE_S3_KEY_PREFIX="sccache"
export SCCACHE_ENDPOINT="${MINIO_HOST}"
# sccache 0.8 requires SCCACHE_REGION (or AWS_DEFAULT_REGION) even for
# self-hosted S3 like MinIO — opendal's S3 builder refuses to start
# without it. The value is arbitrary as long as it's consistent across
# clients; we use `us-east-1` to match MinIO's default region.
export SCCACHE_REGION="${SCCACHE_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="${SCCACHE_REGION}"
export AWS_ACCESS_KEY_ID="${MINIO_USER}"
export AWS_SECRET_ACCESS_KEY="${MINIO_PASS}"
export SCCACHE_SERVER_PORT="4226"
export SCCACHE_IDLE_TIMEOUT=0
export SCCACHE_LOG="${SCCACHE_LOG:-info}"

echo "[sccache] starting sccache server on :4226"
sccache --start-server

# Wait for the server to bind. `--show-stats` returns 0 once the daemon
# is reachable; before that it returns 1.
i=0
until sccache --show-stats >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "${i}" -ge 10 ]; then
        echo "[sccache] WARN: sccache server didn't come up cleanly after 10s; continuing anyway" >&2
        break
    fi
    sleep 1
done

echo "[sccache] starting stats HTTP server on :4227"
exec python3 /usr/local/bin/sccache-stats-server
