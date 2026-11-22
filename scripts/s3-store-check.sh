#!/usr/bin/env bash
# s3-store-check.sh — prove a running stack object store does what the stack
# needs, the way the CI job `s3 store conformance` runs it.
#
# Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 2. Two verbs:
#
#   wait-writable   block (<= 90 s) until the store accepts a bucket create and
#                   a PUT. A store's health route can answer before its write
#                   path is ready (SeaweedFS does); without this the first
#                   real write races the warm-up.
#   check <label>   with the stack's buckets already created by
#                   `object-store-init`:
#                     1. scripts/s3-conformance.py — the D7 subset, 7/7 or fail;
#                     2. the three init buckets exist; `qontinui-dev` serves an
#                        anonymous GET; `qontinui-session-output-cold` refuses
#                        one (403) and `qontinui-dev` refuses an anonymous LIST;
#                     3. scripts/tests/test_sccache_stats_server.py's live arm
#                        against this store, REQUIRED (a skip fails).
#
# Environment: S3_ACCESS_KEY, S3_SECRET_KEY (the compose credentials),
# S3_ENDPOINT (default http://127.0.0.1:${S3_API_PORT:-9100}), OUT_DIR for the
# conformance JSON (default .). Needs the `aws` CLI, curl, python3 with boto3
# and pytest. Exits non-zero on the first failed check.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ENDPOINT="${S3_ENDPOINT:-http://127.0.0.1:${S3_API_PORT:-9100}}"
OUT_DIR="${OUT_DIR:-.}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY must be set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY must be set}"

export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
export AWS_DEFAULT_REGION="${S3_REGION:-us-east-1}"
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
s3api() { aws --endpoint-url "$ENDPOINT" s3api "$@"; }
log() { printf '[s3-store-check] %s\n' "$*"; }
die() { printf '[s3-store-check] FAIL: %s\n' "$*" >&2; exit 1; }

wait_writable() {
    local probe body i
    probe="ready-probe-$(date +%s)-$$"
    body="$(mktemp)"; echo ready >"$body"
    for i in $(seq 1 45); do
        if s3api create-bucket --bucket "$probe" >/dev/null 2>&1 \
            && s3api put-object --bucket "$probe" --key probe --body "$body" >/dev/null 2>&1; then
            s3api delete-object --bucket "$probe" --key probe >/dev/null 2>&1 || true
            s3api delete-bucket --bucket "$probe" >/dev/null 2>&1 || true
            rm -f "$body"
            log "store at $ENDPOINT accepts writes (attempt $i)"
            return 0
        fi
        sleep 2
    done
    rm -f "$body"
    die "store at $ENDPOINT did not accept a bucket create + PUT within 90 s"
}

http_code() { curl -s -o /dev/null -w '%{http_code}' "$1"; }

check() {
    local label="$1" body code
    mkdir -p "$OUT_DIR"
    log "[$label] D7 conformance subset"
    python3 "$HERE/s3-conformance.py" --endpoint "$ENDPOINT" \
        --access-key-env S3_ACCESS_KEY --secret-key-env S3_SECRET_KEY \
        --bucket "conformance-$label" --label "$label" \
        --json-out "$OUT_DIR/s3-conformance-$label.json" \
        || die "[$label] conformance subset did not pass 7/7 (see $OUT_DIR/s3-conformance-$label.json)"

    log "[$label] init buckets and their access"
    for b in qontinui-dev "${BAZEL_REMOTE_BUCKET:-qontinui-bazel-remote}" \
             "${SESSION_OUTPUT_COLD_BUCKET:-qontinui-session-output-cold}"; do
        s3api head-bucket --bucket "$b" >/dev/null || die "[$label] bucket $b missing (object-store-init did not create it)"
    done
    body="$(mktemp)"; echo "anonymous-access probe" >"$body"
    s3api put-object --bucket qontinui-dev --key ci/anon-probe.txt --body "$body" >/dev/null
    s3api put-object --bucket "${SESSION_OUTPUT_COLD_BUCKET:-qontinui-session-output-cold}" \
        --key ci/anon-probe.txt --body "$body" >/dev/null
    rm -f "$body"
    code="$(http_code "$ENDPOINT/qontinui-dev/ci/anon-probe.txt")"
    [ "$code" = 200 ] || die "[$label] anonymous GET on qontinui-dev answered $code, want 200"
    code="$(http_code "$ENDPOINT/qontinui-dev?list-type=2")"
    [ "$code" = 403 ] || die "[$label] anonymous LIST on qontinui-dev answered $code, want 403"
    code="$(http_code "$ENDPOINT/${SESSION_OUTPUT_COLD_BUCKET:-qontinui-session-output-cold}/ci/anon-probe.txt")"
    [ "$code" = 403 ] || die "[$label] anonymous GET on the cold-tier bucket answered $code, want 403"

    log "[$label] sccache stats server against this store"
    QONTINUI_S3_TEST_ENDPOINT="$ENDPOINT" QONTINUI_S3_TEST_REQUIRED=1 \
        python3 -m pytest "$HERE/tests/test_sccache_stats_server.py" -q -rs \
        || die "[$label] sccache stats test failed"
    log "[$label] PASS"
}

case "${1:-}" in
    wait-writable) wait_writable ;;
    check) [ -n "${2:-}" ] || die "usage: $0 check <label>"; check "$2" ;;
    *) echo "usage: $0 wait-writable | check <label>" >&2; exit 2 ;;
esac
