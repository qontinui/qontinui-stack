#!/usr/bin/env bash
# s3-migrate-from-minio.sh — copy the stack's buckets from a RUNNING old MinIO
# into the object store that replaced it, then verify the copy by listing.
#
# Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 2. The swap
# (docker-compose.yml `object-store`) starts the new store on a NEW volume and
# leaves the old `qontinui_canonical_minio_data` volume declared and untouched,
# so nothing is lost by the swap itself; this script is how data crosses over.
# The caches (qontinui-sccache, qontinui-bazel-remote) are rebuildable, so
# copying them is optional; qontinui-dev and the cold-tier mirror are not.
#
# It never deletes anything on either side and never stops or starts a
# container. Bring the old MinIO up yourself, on a spare port, against its
# kept volume, for example:
#
#   docker run -d --name minio-migrate-source -p 127.0.0.1:9190:9000 \
#     -v qontinui_canonical_minio_data:/data \
#     -e MINIO_ROOT_USER=minioadmin -e MINIO_ROOT_PASSWORD=minioadmin \
#     minio/minio:<the tag or digest you last ran> server /data
#
# (Whatever MinIO build last wrote the volume; MinIO is archived, so no build
# is recommended here.) Then:
#
#   SRC_ACCESS_KEY=minioadmin SRC_SECRET_KEY=minioadmin \
#   S3_ACCESS_KEY=... S3_SECRET_KEY=... \
#   bash scripts/s3-migrate-from-minio.sh --source-endpoint http://127.0.0.1:9190
#
# and `docker rm -fv minio-migrate-source` when it reports PASS (-v drops only
# anonymous volumes; the named MinIO volume is kept).
#
# How the copy works. The aws CLI cannot sync between two endpoints in one
# command, so each bucket is synced source -> a local staging dir -> dest
# (`aws s3 sync` both legs). Uploads to the destination are forced single-part
# (a multipart threshold above any object here) so every destination ETag is
# the plain MD5 of the object.
#
# How it verifies, per bucket, from fresh ListObjectsV2 walks of BOTH stores:
#   * the key sets are equal (the count, and every key);
#   * every key's size is equal;
#   * ETags: where the source ETag is a plain MD5 (no "-N" suffix), it must
#     equal the destination ETag; where the source object was a multipart
#     upload (ETag "<md5-of-md5s>-N", which no re-upload reproduces), the
#     destination ETag must equal the MD5 of the staged bytes that came from
#     the source. Each such case is counted in the report, never skipped.
# Any mismatch exits 1 and names the key. A bucket that is ABSENT on the source
# is reported as absent (exit 1 unless --allow-missing names it), never as
# "copied 0 objects".
#
# Options:
#   --source-endpoint URL   the running old MinIO (required)
#   --dest-endpoint URL     default http://127.0.0.1:${S3_API_PORT:-9100}
#   --buckets "a b c"       default: qontinui-dev qontinui-session-output-cold
#                           qontinui-bazel-remote qontinui-sccache
#   --allow-missing "a b"   buckets that may be absent on the source
#   --staging-dir DIR       default: a fresh mktemp dir, removed on success
#   --verify-only           skip the copy; only compare the two stores. A
#                           multipart-source object then has no staged bytes
#                           to hash and FAILS (unverifiable is not verified);
#                           point --staging-dir at a kept staging dir to check it
# Credentials come from the environment, never argv: SRC_ACCESS_KEY /
# SRC_SECRET_KEY for the source, S3_ACCESS_KEY / S3_SECRET_KEY for the dest.
# Needs: aws (v2), python3.
set -euo pipefail

SRC=""
DST="http://127.0.0.1:${S3_API_PORT:-9100}"
BUCKETS="qontinui-dev qontinui-session-output-cold qontinui-bazel-remote qontinui-sccache"
ALLOW_MISSING=""
STAGING=""
VERIFY_ONLY=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//;$d'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --source-endpoint) SRC="$2"; shift 2 ;;
        --dest-endpoint) DST="$2"; shift 2 ;;
        --buckets) BUCKETS="$2"; shift 2 ;;
        --allow-missing) ALLOW_MISSING="$2"; shift 2 ;;
        --staging-dir) STAGING="$2"; shift 2 ;;
        --verify-only) VERIFY_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
done
[ -n "$SRC" ] || { echo "--source-endpoint is required (see --help)" >&2; exit 2; }
: "${SRC_ACCESS_KEY:?SRC_ACCESS_KEY (the access key of the old MinIO) must be set}"
: "${SRC_SECRET_KEY:?SRC_SECRET_KEY must be set}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY (the access key of the object store) must be set}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY must be set}"
command -v aws >/dev/null || { echo "the aws CLI (v2) is required" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 2; }

export AWS_DEFAULT_REGION="${S3_REGION:-us-east-1}"
export AWS_REQUEST_CHECKSUM_CALCULATION=when_required AWS_RESPONSE_CHECKSUM_VALIDATION=when_required
# A private CLI config: single-part uploads (see the header), nothing from the
# operator's own ~/.aws leaking in.
CFG_DIR="$(mktemp -d)"
trap 'rm -rf "$CFG_DIR"' EXIT
cat >"$CFG_DIR/config" <<'EOF'
[default]
s3 =
  multipart_threshold = 5GB
  multipart_chunksize = 5GB
EOF
export AWS_CONFIG_FILE="$CFG_DIR/config" AWS_SHARED_CREDENTIALS_FILE=/dev/null

src() { AWS_ACCESS_KEY_ID="$SRC_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$SRC_SECRET_KEY" aws --endpoint-url "$SRC" "$@"; }
dst() { AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY" aws --endpoint-url "$DST" "$@"; }
log() { printf '[s3-migrate] %s\n' "$*"; }

KEEP_STAGING=1
if [ -z "$STAGING" ]; then STAGING="$(mktemp -d)"; KEEP_STAGING=0; fi

# list <src|dst> <bucket> <out.tsv>: key \t size \t etag, sorted, from a full
# ListObjectsV2 walk (the CLI paginates).
list() {
    local side="$1" bucket="$2" out="$3"
    "$side" s3api list-objects-v2 --bucket "$bucket" --output json \
        --query 'Contents[].[Key,Size,ETag]' \
        | python3 -c '
import json, sys
rows = json.load(sys.stdin) or []
for key, size, etag in sorted(rows):
    print(f"{key}\t{size}\t{etag.strip(chr(34))}")
' >"$out"
}

FAILED=0
REPORT=()
for bucket in $BUCKETS; do
    if ! src s3api head-bucket --bucket "$bucket" >/dev/null 2>&1; then
        case " $ALLOW_MISSING " in
            *" $bucket "*) log "$bucket: absent on the source (allowed by --allow-missing)"; REPORT+=("$bucket ABSENT-ALLOWED"); continue ;;
            *) log "$bucket: ABSENT on the source (or unreadable with SRC_* credentials)"; REPORT+=("$bucket ABSENT"); FAILED=1; continue ;;
        esac
    fi
    stage="$STAGING/$bucket"
    mkdir -p "$stage"
    if [ "$VERIFY_ONLY" -eq 0 ]; then
        dst s3api head-bucket --bucket "$bucket" >/dev/null 2>&1 || dst s3api create-bucket --bucket "$bucket" >/dev/null
        log "$bucket: source -> staging"
        src s3 sync --only-show-errors "s3://$bucket" "$stage"
        log "$bucket: staging -> destination"
        dst s3 sync --only-show-errors "$stage" "s3://$bucket"
    fi
    list src "$bucket" "$STAGING/$bucket.src.tsv"
    list dst "$bucket" "$STAGING/$bucket.dst.tsv"
    verdict="$(python3 - "$STAGING/$bucket.src.tsv" "$STAGING/$bucket.dst.tsv" "$stage" <<'PY'
import hashlib, os, sys
src_tsv, dst_tsv, stage = sys.argv[1], sys.argv[2], sys.argv[3]
def load(p):
    out = {}
    for line in open(p):
        key, size, etag = line.rstrip("\n").split("\t")
        out[key] = (int(size), etag)
    return out
s, d = load(src_tsv), load(dst_tsv)
problems = []
missing = sorted(set(s) - set(d))
extra = sorted(set(d) - set(s))
problems += [f"missing on destination: {k}" for k in missing[:20]]
problems += [f"only on destination: {k}" for k in extra[:20]]
plain = multipart = 0
for key in sorted(set(s) & set(d)):
    (ssize, setag), (dsize, detag) = s[key], d[key]
    if ssize != dsize:
        problems.append(f"size differs: {key} ({ssize} vs {dsize})")
        continue
    if "-" not in setag:
        plain += 1
        if setag != detag:
            problems.append(f"etag differs: {key} ({setag} vs {detag})")
    else:
        multipart += 1
        path = os.path.join(stage, key)
        if not os.path.isfile(path):
            problems.append(f"multipart source etag and no staged copy to hash: {key}")
            continue
        h = hashlib.md5()  # noqa: S324 — an S3 ETag is an MD5, not a security use
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != detag:
            problems.append(f"multipart object's bytes differ: {key}")
status = "FAIL" if problems else "PASS"
print(f"{status} objects={len(s)} dest_objects={len(d)} etag_plain={plain} etag_multipart_by_md5={multipart}")
for p in problems:
    print("  " + p)
PY
)"
    log "$bucket: $verdict"
    REPORT+=("$bucket ${verdict%%$'\n'*}")
    case "$verdict" in PASS*) ;; *) FAILED=1 ;; esac
done

log "summary:"
for line in "${REPORT[@]}"; do log "  $line"; done
if [ "$FAILED" -ne 0 ]; then
    log "FAIL — staging kept at $STAGING"
    exit 1
fi
[ "$KEEP_STAGING" -eq 1 ] || rm -rf "$STAGING"
log "PASS"
