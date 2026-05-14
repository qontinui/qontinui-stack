# syntax=docker/dockerfile:1.7
#
# qontinui canonical sccache — LAN-shared rustc compile-unit cache.
#
# Row 2 Phase 2 of the fleet topology design:
#   D:/qontinui-root/plans/2026-05-14-fleet-topology-and-build-pool-design.md
#
# The actual cache lives in MinIO (bucket `qontinui-sccache`). Each agent /
# build machine runs its own local `sccache` daemon configured to talk S3
# to that bucket — the wire-level shared component is MinIO, not this
# container. This container has two jobs:
#
#   1. Ensure the `qontinui-sccache` MinIO bucket exists at first boot
#      (idempotent via `mc mb --ignore-existing`).
#   2. Expose an HTTP stats endpoint that aggregates bucket-side telemetry
#      (object count, total size) for the coord `/metrics` scraper. Coord
#      sums these with per-machine local sccache stats it pulls from each
#      runner / supervisor at scrape time.
#
# Phase 6 (cloud-burst CI workers) will additionally consume sccache as a
# daemon inside containerized build slots — at that point this container
# also runs `sccache --start-server` and CI containers point at it via
# SCCACHE_ENDPOINT. For Phase 2 the daemon role is dormant; we still
# expose the port so the wiring is in place when Phase 6 lands.

FROM alpine:3.20

ARG SCCACHE_VERSION=0.8.2
ARG TARGETARCH=amd64

# sccache binary (statically linked musl build from upstream release) +
# `mc` (MinIO client) for bucket init + a python runtime for the stats
# HTTP shim. tini gives PID-1 signal handling so `docker stop` doesn't
# leave a zombie sccache server.
RUN apk add --no-cache \
        ca-certificates \
        curl \
        tini \
        python3 \
    && curl -fSL "https://github.com/mozilla/sccache/releases/download/v${SCCACHE_VERSION}/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        | tar -xz -C /tmp \
    && install -m 0755 "/tmp/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl/sccache" /usr/local/bin/sccache \
    && rm -rf "/tmp/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl" \
    && curl -fSL "https://dl.min.io/client/mc/release/linux-amd64/mc" -o /usr/local/bin/mc \
    && chmod +x /usr/local/bin/mc \
    && sccache --version \
    && mc --version

COPY entrypoint.sh /entrypoint.sh
COPY stats_server.py /usr/local/bin/sccache-stats-server
RUN chmod +x /entrypoint.sh /usr/local/bin/sccache-stats-server

# Stats HTTP port. Local sccache server port stays 4226 (sccache default);
# stats sidecar listens on 4227. Both are exposed; coord scrapes 4227.
EXPOSE 4226 4227

ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
