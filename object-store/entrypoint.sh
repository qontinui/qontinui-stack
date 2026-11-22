#!/bin/sh
# qontinui canonical object store entrypoint — versitygw, posix backend.
#
# The posix backend stores each object as a file under /data/<bucket>/<key>
# and its metadata (ETag, content type, user metadata, bucket policy) in
# EXTENDED ATTRIBUTES on those files. A volume whose filesystem refuses user
# xattrs would accept the first PUT and then lose every object's metadata, so
# this probes before serving and refuses with the remedy instead.
#
# Docker's `local` volume driver keeps a named volume as a host directory under
# the daemon's data root (ext4, xfs, or the WSL2 VM's ext4 on Docker Desktop),
# all of which support `user.*` xattrs. The probe is for the case that does
# not: a bind mount or volume driver on a filesystem without them. The remedy
# is versitygw's own sidecar mode — set VGW_META_SIDECAR=/meta (compose passes
# it through and mounts a dedicated volume there) and metadata is kept there as
# files instead. Empty means unset.
#
# Pick ONE mode per volume and keep it. Switching an existing volume between
# xattrs and VGW_META_SIDECAR does not move metadata: objects written in the
# old mode lose their ETag, content type and user metadata in the new one, and
# the bucket policy goes with them. To switch, copy the data into a fresh
# store in the new mode (scripts/s3-migrate-from-minio.sh works between any
# two endpoints).
#
# Environment (all set by docker-compose.yml):
#   ROOT_ACCESS_KEY / ROOT_SECRET_KEY  root credentials (from S3_ACCESS_KEY /
#                                      S3_SECRET_KEY); read by versitygw itself
#   VGW_META_SIDECAR                   optional (empty = xattrs); disables the probe
set -eu

DATA_DIR="${OBJECT_STORE_DATA_DIR:-/data}"

if [ -z "${ROOT_ACCESS_KEY:-}" ] || [ -z "${ROOT_SECRET_KEY:-}" ]; then
    echo "[object-store] FATAL: ROOT_ACCESS_KEY and ROOT_SECRET_KEY must be set (compose maps them from S3_ACCESS_KEY / S3_SECRET_KEY)" >&2
    exit 1
fi

mkdir -p "${DATA_DIR}"

if [ -n "${VGW_META_SIDECAR:-}" ]; then
    mkdir -p "${VGW_META_SIDECAR}"
    echo "[object-store] metadata in sidecar dir ${VGW_META_SIDECAR}; xattr probe skipped"
else
    # Compose passes an empty VGW_META_SIDECAR by default; drop it so
    # versitygw sees the option as absent, not as an empty path.
    unset VGW_META_SIDECAR
    probe="${DATA_DIR}/.qontinui-xattr-probe.$$"
    : >"${probe}"
    if setfattr -n user.qontinui.probe -v ok "${probe}" 2>/dev/null \
        && [ "$(getfattr --only-values -n user.qontinui.probe "${probe}" 2>/dev/null)" = "ok" ]; then
        rm -f "${probe}"
        echo "[object-store] ${DATA_DIR} supports user xattrs"
    else
        rm -f "${probe}"
        echo "[object-store] FATAL: ${DATA_DIR} does not support user extended attributes, which versitygw's posix backend needs for object metadata." >&2
        echo "[object-store] Put the volume on ext4/xfs/btrfs, or set VGW_META_SIDECAR=/meta (docker-compose.yml mounts the canonical_object_store_meta volume there) to keep metadata as sidecar files." >&2
        exit 1
    fi
fi

# :9000 inside the container; compose publishes it as ${S3_API_PORT:-9100}.
# /health is the unauthenticated liveness route the compose healthcheck reads.
exec versitygw --port :9000 --health /health posix "${DATA_DIR}"
