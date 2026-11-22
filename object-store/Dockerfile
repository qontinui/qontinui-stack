# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# released: 2024-04-25 pinned: 2026-11-22
#
# qontinui canonical object store — versitygw (posix backend), the S3 store
# that replaced MinIO (whose GitHub repository reports `archived: true`).
#
# Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 2. The store was
# chosen by Phase 1's conformance run (D8): versitygw v1.11.0 passed all seven
# operations of the D7 subset plus both client round trips and had the lowest
# idle RSS of the eligible candidates; SeaweedFS 4.52 is the rehearsed fallback
# (`docker compose --profile fallback-store`). Result:
# qontinui-dev-notes investigation-results/2026-11-19-in-house-substrate-strategy/
# phase1/s3-store-conformance.md.
#
# WHY A LOCAL BUILD AND NOT THE UPSTREAM IMAGE. versity publishes
# `versity/versitygw:v1.11.0` (index digest sha256:8319a530…43c5), but Docker
# Hub records that digest as pushed 2026-11-10 — 12 days before this pin, inside
# D5's 14-day release-age cooldown, so it may not be adopted yet. The RELEASE
# TARBALL below was published 2026-11-06 (16 days), and it is byte-for-byte the
# binary Phase 1 conformance-tested (same URL, same sha256 as
# scripts/s3-candidates.pins.toml). Building it onto a digest-pinned base keeps
# both properties: the tested artifact, and the cooldown. Once the upstream
# image has aged it may replace this file; the CI job `s3 store conformance`
# decides whether whatever replaces it still passes the subset.
#
# linux/amd64 only, like sccache/Dockerfile: the pinned asset is the x86_64
# tarball and every machine that runs this stack is x86_64.
#
# Every third-party reference here is pinned by digest or sha256 with a
# `released:`/`pinned:` line; scripts/tests/test_image_pins.py enforces it.

# released: 2026-10-23 pinned: 2026-11-22
FROM alpine:3.23.7@sha256:ff7f0c727dd796aa36787bfcd9a64bf35a6ce72a73451b3359fde849b6703d63

# released: 2026-11-06 pinned: 2026-11-22
ARG VERSITYGW_SHA256=1f075c9dc92fef232f5b3c7465711ddc5e1b062b2b6df4a3bd246a4be5b35b52
ARG VERSITYGW_VERSION=v1.11.0

# `attr` provides setfattr/getfattr for the entrypoint's xattr probe (the posix
# backend keeps object metadata in extended attributes). curl is used for the
# download only and removed again; busybox wget stays for the healthcheck.
RUN apk add --no-cache ca-certificates attr \
    && apk add --no-cache --virtual .fetch curl \
    && curl -fsSL -o /tmp/versitygw.tar.gz \
        "https://github.com/versity/versitygw/releases/download/${VERSITYGW_VERSION}/versitygw_${VERSITYGW_VERSION}_Linux_x86_64.tar.gz" \
    && echo "${VERSITYGW_SHA256}  /tmp/versitygw.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/versitygw.tar.gz -C /tmp \
    && install -m 0755 "/tmp/versitygw_${VERSITYGW_VERSION}_Linux_x86_64/versitygw" /usr/local/bin/versitygw \
    && rm -rf /tmp/versitygw.tar.gz "/tmp/versitygw_${VERSITYGW_VERSION}_Linux_x86_64" \
    && apk del .fetch \
    && versitygw --version

COPY entrypoint.sh /usr/local/bin/object-store-entrypoint
RUN chmod 0755 /usr/local/bin/object-store-entrypoint

EXPOSE 9000
ENTRYPOINT ["/usr/local/bin/object-store-entrypoint"]
