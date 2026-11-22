#!/bin/sh
# qontinui canonical sccache entrypoint.
#
# 1. Ensure the object-store bucket `qontinui-sccache` exists (idempotent).
# 2. Start the sccache daemon (listens on :4226 — dormant until Phase 6
#    cloud-burst CI workers connect; harmless to run unused).
# 3. Start the stats HTTP server on :4227 — coord scrapes this.
#
# All three steps run with the same SCCACHE_* / AWS_* env that local agent /
# build machines also use, so a stats query against this container reflects
# the same bucket every other client reads/writes.
#
# Environment (set by docker-compose.yml):
#   SCCACHE_ENDPOINT       host:port of the S3 store (default object-store:9000)
#   SCCACHE_BUCKET         bucket name (default qontinui-sccache)
#   SCCACHE_REGION         region string (default us-east-1)
#   AWS_ACCESS_KEY_ID /
#   AWS_SECRET_ACCESS_KEY  the store's key pair (compose maps S3_ACCESS_KEY /
#                          S3_SECRET_KEY); read natively by aws, sccache and
#                          the stats server

set -eu

ENDPOINT="${SCCACHE_ENDPOINT:-object-store:9000}"
BUCKET="${SCCACHE_BUCKET:-qontinui-sccache}"
: "${AWS_ACCESS_KEY_ID:?AWS_ACCESS_KEY_ID must be set (compose maps it from S3_ACCESS_KEY)}"
: "${AWS_SECRET_ACCESS_KEY:?AWS_SECRET_ACCESS_KEY must be set (compose maps it from S3_SECRET_KEY)}"

# sccache requires SCCACHE_REGION (or AWS_DEFAULT_REGION) even for a
# self-hosted S3 store — opendal's S3 builder refuses to start without it. The
# value is arbitrary as long as it's consistent across clients; `us-east-1`
# is the store's default region.
export SCCACHE_REGION="${SCCACHE_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="${SCCACHE_REGION}"
# Checksums only where the API requires them (the same setting the store's
# conformance run used).
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required
export AWS_RESPONSE_CHECKSUM_VALIDATION=when_required

echo "[sccache] ensuring bucket ${BUCKET} at http://${ENDPOINT}"
# Retry because the store may not take requests for the first second or two
# of a cold boot even though depends_on:service_healthy is set. head-bucket
# answers 200 for an existing bucket; otherwise create it. Five tries at 2s
# intervals covers cold start.
i=0
until aws --endpoint-url "http://${ENDPOINT}" s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1 \
    || aws --endpoint-url "http://${ENDPOINT}" s3api create-bucket --bucket "${BUCKET}" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "${i}" -ge 5 ]; then
        echo "[sccache] FATAL: could not reach or create bucket ${BUCKET} at http://${ENDPOINT} after 5 attempts" >&2
        exit 1
    fi
    echo "[sccache] bucket attempt ${i} failed; retrying in 2s"
    sleep 2
done
echo "[sccache] bucket ${BUCKET} ready"

# Configure local sccache daemon (used in Phase 6 by in-container builders).
# These are the same vars every client sets; the container is just another
# client from sccache's POV.
export SCCACHE_BUCKET="${BUCKET}"
export SCCACHE_ENDPOINT="${ENDPOINT}"
export SCCACHE_S3_USE_SSL=off
export SCCACHE_S3_KEY_PREFIX="sccache"
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
