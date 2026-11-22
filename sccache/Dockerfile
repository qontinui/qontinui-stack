# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# released: 2024-04-25 pinned: 2026-11-22
#
# qontinui canonical sccache — LAN-shared rustc compile-unit cache.
#
# Row 2 Phase 2 of the fleet topology design:
#   D:/qontinui-root/plans/2026-05-14-fleet-topology-and-build-pool-design.md
#
# The actual cache lives in the stack's object store (bucket
# `qontinui-sccache`). Each agent / build machine runs its own local `sccache`
# daemon configured to talk S3 to that bucket — the wire-level shared
# component is the object store, not this container. This container has two
# jobs:
#
#   1. Ensure the `qontinui-sccache` bucket exists at first boot
#      (idempotent: `aws s3api head-bucket`, else `create-bucket`).
#   2. Expose an HTTP stats endpoint that aggregates bucket-side telemetry
#      (object count, total size, from an S3 ListObjectsV2 walk) for the coord
#      `/metrics` scraper. Coord sums these with per-machine local sccache
#      stats it pulls from each runner / supervisor at scrape time.
#
# Phase 6 (cloud-burst CI workers) will additionally consume sccache as a
# daemon inside containerized build slots — at that point this container
# also runs `sccache --start-server` and CI containers point at it via
# SCCACHE_ENDPOINT. For Phase 2 the daemon role is dormant; we still
# expose the port so the wiring is in place when Phase 6 lands.
#
# Pins (D5 of plan 2026-11-22-fetches-stop-depending-on-upstreams): the base
# image by digest and the sccache tarball by sha256, each with a
# `released:`/`pinned:` line at least 14 days apart; enforced by
# scripts/tests/test_image_pins.py. sccache v0.19.0 is the release whose S3
# round trip against the object store was measured in that plan's Phase 1.

# released: 2026-10-23 pinned: 2026-11-22
FROM alpine:3.23.7@sha256:ff7f0c727dd796aa36787bfcd9a64bf35a6ce72a73451b3359fde849b6703d63

# released: 2026-10-24 pinned: 2026-11-22
ARG SCCACHE_SHA256=aa73c7270c997894038c0d8f0d297865f57569e1687883322bc431713d4bb9dd
ARG SCCACHE_VERSION=0.19.0

# sccache binary (statically linked musl build from upstream release,
# verified by sha256 before it is unpacked) + the AWS CLI for bucket init + a
# python runtime for the stats HTTP shim (stdlib only; it signs its own S3
# requests). tini gives PID-1 signal handling so `docker stop` doesn't leave a
# zombie sccache server.
RUN apk add --no-cache \
        ca-certificates \
        tini \
        python3 \
        aws-cli \
    && apk add --no-cache --virtual .fetch curl \
    && curl -fsSL -o /tmp/sccache.tar.gz \
        "https://github.com/mozilla/sccache/releases/download/v${SCCACHE_VERSION}/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
    && echo "${SCCACHE_SHA256}  /tmp/sccache.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/sccache.tar.gz -C /tmp \
    && install -m 0755 "/tmp/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl/sccache" /usr/local/bin/sccache \
    && rm -rf /tmp/sccache.tar.gz "/tmp/sccache-v${SCCACHE_VERSION}-x86_64-unknown-linux-musl" \
    && apk del .fetch \
    && sccache --version \
    && aws --version

COPY entrypoint.sh /entrypoint.sh
COPY stats_server.py /usr/local/bin/sccache-stats-server
RUN chmod +x /entrypoint.sh /usr/local/bin/sccache-stats-server

# Stats HTTP port. Local sccache server port stays 4226 (sccache default);
# stats sidecar listens on 4227. Both are exposed; coord scrapes 4227.
EXPOSE 4226 4227

ENTRYPOINT ["/sbin/tini", "--", "/entrypoint.sh"]
