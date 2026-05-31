# syntax=docker/dockerfile:1.7
#
# qontinui-web production overlay — layers qontinui-cloud-control (AGPL, public:
# github.com/qontinui/qontinui-cloud-control) on top of the OSS slim image so the AWS staging/prod web service has the
# cloud-only models (Subscription, AdminNotificationSettings, …) and routes
# (billing, admin, organizations) registered at boot.
#
# Why a separate overlay (vs modifying qontinui-web/backend/Dockerfile):
#   - Keeps the OSS qontinui-web image buildable standalone without cloning a
#     sibling repo; the deploy-time composition lives here instead. (cloud-control
#     is itself AGPL/public now, so this is composition hygiene, not secrecy.)
#   - Lets the deploy concern live alongside other deploy concerns in
#     qontinui-stack (mirroring the qontinui-stack/migrator/ pattern).
#   - Matches the carve-out design in tmp_cloud_control_carve_out.md §6.1:
#     cloud-control is editable-installed on top of qontinui-web, side-effect
#     registers its routes + models via main.py:18's try/except ImportError.
#
# Build:
#   # 1. Build the OSS slim image first (from qontinui-web/backend/):
#   docker build -t qontinui-web-backend:slim-<sha> qontinui-web/backend/
#
#   # 2. Build overlay (no secret needed — cloud-control is a public repo):
#   docker build \
#     --build-arg SLIM_IMAGE=qontinui-web-backend:slim-<sha> \
#     -t qontinui-web-backend:staging-prod-<sha> \
#     qontinui-stack/web-prod/

ARG SLIM_IMAGE=qontinui-web-backend:slim-latest
FROM ${SLIM_IMAGE}

# git is needed to clone the cloud-control repo. The slim runtime
# stripped it; restore for this one RUN, then leave installed for runtime
# debugging ergonomics (~30 MB delta). Remove if image footprint matters
# more than ad-hoc debug capability.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Clone + editable-install cloud-control (public, AGPL — no credentials needed).
# After clone, .git/ is removed to keep the runtime image lean.
RUN git clone --depth 1 \
        https://github.com/qontinui/qontinui-cloud-control.git \
        /qontinui-cloud-control \
    && find /qontinui-cloud-control -name ".git" -type d -exec rm -rf {} + 2>/dev/null || true \
    && pip install --no-cache-dir -e /qontinui-cloud-control

# Sanity probe — verifies the side-effect import works (registers extension
# hooks). Fails the build if cloud-control's __init__ raises.
RUN python -c "import qontinui_cloud_control; print('cloud-control loaded:', qontinui_cloud_control.__file__)"

# Inherits ENTRYPOINT + CMD from the slim image (tini + uvicorn :8000).
