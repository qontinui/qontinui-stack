#!/usr/bin/env bash
# Preserve the current coord :latest image under a distinct rollback tag
# BEFORE a coord rebuild retags :latest and prunes the old layers.
#
# Usage:   ./coord-preserve-before-rebuild.sh <deploy-name>
# Example: ./coord-preserve-before-rebuild.sh wave-8
#
# Why this exists
# ---------------
# Every coord image rebuild (`docker compose build coord`) retags
# `qontinui-canonical-coord:latest` to the freshly-built image. Docker then
# prunes the *previous* :latest's underlying layers — they are no longer
# referenced by any tag. The live `qontinui-canonical-coord` container keeps
# running on its already-extracted rootfs, but the image it was created from
# is GONE from Docker's content store. Consequences:
#
#   - Any `docker compose up -d coord` rolls the NEW (possibly
#     DB-incompatible) binary forward with no way back.
#   - Any Docker daemon / host restart cannot recreate the container —
#     there is no image to recreate it from.
#   - `docker commit` on the live container fails with
#     "content digest ... not found" (orphaned parent layers). Recovery
#     then requires the heavier `docker export | docker import` flatten.
#     See memory feedback_docker_commit_export_import_recovery.
#
# Running this script before the rebuild gives the previous :latest a
# durable name (`:pre-<deploy-name>-pinned`) so the compose `coord.image`
# can be repinned to it for an instant rollback, and a host restart can
# always recreate the container from a real image.
#
# This is the codification of Gap 4 in memory
# proj_deployment_config_gaps_2026-05-rollout — the pattern was empirically
# validated three times (:pre-phase5-pinned 2026-05-16,
# :pre-config-session-pinned 2026-05-17) before being scripted here.
#
# Safety
# ------
# This script is non-destructive by construction:
#   - It only ADDS a tag (an alias) to an existing image; it never deletes,
#     prunes, rebuilds, or restarts anything.
#   - It REFUSES to overwrite an existing preservation tag (a stale tag for
#     the same deploy-name would otherwise silently lose the real rollback
#     target). Pick a different deploy-name or explicitly delete the
#     conflicting tag first.
#   - If there is no local :latest to preserve, it warns and exits 0 (a
#     fresh machine with nothing to lose is not an error).
#
# Portability: pure bash + docker CLI. Works on Linux and on Windows Git
# Bash (the only `docker` invocations are tag / image inspect / images).

set -euo pipefail

IMAGE="qontinui-canonical-coord"

DEPLOY_NAME="${1:?usage: $0 <deploy-name>   (e.g. $0 wave-8)}"

# Normalise: strip whitespace; reject anything that isn't a safe docker tag
# component (alnum, dash, underscore, dot). Keeps the resulting tag valid
# and predictable for the rollback runbook step.
if ! printf '%s' "$DEPLOY_NAME" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._-]*$'; then
  echo "ERROR: deploy-name '${DEPLOY_NAME}' is not a valid tag component" >&2
  echo "       allowed: letters, digits, dot, dash, underscore; must start alnum" >&2
  exit 1
fi

TAG="pre-${DEPLOY_NAME}-pinned"

if ! docker image inspect "${IMAGE}:latest" >/dev/null 2>&1; then
  echo "WARN: ${IMAGE}:latest not present locally; nothing to preserve." >&2
  echo "      (Fresh machine / image only ever pulled by tag — no rollback" >&2
  echo "       target to lose. Safe to proceed with the rebuild.)" >&2
  exit 0
fi

if docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
  echo "ERROR: ${IMAGE}:${TAG} already exists; refusing to overwrite." >&2
  echo "       Overwriting would discard the real rollback target this tag" >&2
  echo "       currently points at. Either:" >&2
  echo "         - choose a different <deploy-name>, or" >&2
  echo "         - explicitly delete the stale tag:" >&2
  echo "             docker rmi ${IMAGE}:${TAG}" >&2
  echo "           (verify it is not the live container's image first:" >&2
  echo "             docker inspect ${IMAGE} --format '{{.Image}}')" >&2
  exit 1
fi

LATEST_ID="$(docker image inspect "${IMAGE}:latest" --format '{{.Id}}')"

docker tag "${IMAGE}:latest" "${IMAGE}:${TAG}"

echo "OK: ${IMAGE}:latest (${LATEST_ID}) preserved as :${TAG}"
echo ""
echo "Rollback (if the upcoming deploy goes bad):"
echo "  1. Edit qontinui-stack/docker-compose.yml — coord.image:"
echo "       ${IMAGE}:latest  ->  ${IMAGE}:${TAG}"
echo "  2. docker compose up -d coord"
echo ""
docker images "${IMAGE}" --format "table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}"
