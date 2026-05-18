# syntax=docker/dockerfile:1.7
#
# qontinui-web production overlay — layers proprietary qontinui-cloud-control
# on top of the OSS slim image so the AWS staging/prod web service has the
# cloud-only models (Subscription, AdminNotificationSettings, …) and routes
# (billing, admin, organizations) registered at boot.
#
# Why a separate overlay (vs modifying qontinui-web/backend/Dockerfile):
#   - Keeps qontinui-web OSS-clean (no PAT-clone of a private repo in the
#     public Dockerfile). Per architectural-decisions.md § "Open-core posture":
#     OSS deployments must build without proprietary credentials.
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
#   # 2. Fetch CC_TOKEN to a tmp file (BuildKit secret source — never in layers).
#   #    cc_token lives in us-east-1 alongside the other staging Secrets Manager
#   #    entries (migrated 2026-05-17 from eu-central-1 for region colocation).
#   #    Setting `$env:AWS_DEFAULT_REGION = "us-east-1"` once in your shell
#   #    profile is the cleaner alternative to passing --region on every call.
#   #    The RUN below strips \r\n from the secret with `tr -d`, so it's safe
#   #    to fetch from either a POSIX or Windows host (Windows-side `> file`
#   #    redirects add CRLF; Linux-side adds LF only).
#   aws secretsmanager get-secret-value --secret-id qontinui/staging/cc_token \
#     --region us-east-1 --query SecretString --output text > /tmp/cc_token
#
#   # 3. Build overlay:
#   docker build \
#     --build-arg SLIM_IMAGE=qontinui-web-backend:slim-<sha> \
#     --secret id=cc_token,src=/tmp/cc_token \
#     -t qontinui-web-backend:staging-prod-<sha> \
#     qontinui-stack/web-prod/
#
#   # 4. Delete the tmp token file.
#   rm /tmp/cc_token

ARG SLIM_IMAGE=qontinui-web-backend:slim-latest
FROM ${SLIM_IMAGE}

# git is needed to clone the private cloud-control repo. The slim runtime
# stripped it; restore for this one RUN, then leave installed for runtime
# debugging ergonomics (~30 MB delta). Remove if image footprint matters
# more than ad-hoc debug capability.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Clone + editable-install cloud-control. Token comes from BuildKit's tmpfs
# secret mount — it appears only at /run/secrets/cc_token during this RUN
# and is discarded immediately after, never persisted into any image layer.
# After clone, .git/ is removed so the credentialed remote URL doesn't
# survive into the runtime image (a defense-in-depth measure even though
# .git/ wouldn't be reachable from a running container without exec).
RUN --mount=type=secret,id=cc_token,required=true \
    CC_TOKEN="$(tr -d '\r\n' < /run/secrets/cc_token)" \
    && git clone --depth 1 \
        "https://x-access-token:${CC_TOKEN}@github.com/jspinak/qontinui-cloud-control.git" \
        /qontinui-cloud-control \
    && unset CC_TOKEN \
    && find /qontinui-cloud-control -name ".git" -type d -exec rm -rf {} + 2>/dev/null || true \
    && pip install --no-cache-dir -e /qontinui-cloud-control

# Sanity probe — verifies the side-effect import works (registers extension
# hooks). Fails the build if cloud-control's __init__ raises.
RUN python -c "import qontinui_cloud_control; print('cloud-control loaded:', qontinui_cloud_control.__file__)"

# Inherits ENTRYPOINT + CMD from the slim image (tini + uvicorn :8000).
