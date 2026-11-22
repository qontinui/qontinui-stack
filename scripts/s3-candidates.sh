#!/usr/bin/env bash
# s3-candidates.sh — run the D8 object-store candidates through the D7
# conformance subset and the stack's two real clients, then apply D8's rule.
#
# Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 1.
#
# For each candidate in scripts/s3-candidates.pins.toml it:
#   1. refuses an entry inside the 14-day release-age cooldown (D5);
#   2. fetches the release by URL into a FRESH, EMPTY directory and checks
#      sha256 before anything is unpacked or run (curl + `sha256sum -c`, until
#      Phase 4's fetch-pinned.sh lands);
#   3. reads the LICENSE at the pinned tag and screens it (source-available
#      licences are excluded; the LICENSE sha256 is pinned too);
#   4. starts the candidate as a plain user process on a temp dir and free
#      loopback ports, with its one-time setup (Garage: layout assign + apply,
#      key import, bucket create + allow);
#   5. records idle RSS from /proc/<pid>/status after --idle-secs (default 30);
#   6. runs scripts/s3-conformance.py against it;
#   7. runs the two client round trips:
#        bazel-remote  — the release binary with --s3.* flags: a CAS PUT on one
#                        instance, the blob read back by a SECOND instance with
#                        an empty disk cache, so the GET can only come from S3;
#        sccache       — the release binary with an S3 bucket: one cargo build
#                        of a hello-world library crate (bin crates are not
#                        cacheable by sccache), then the target dir emptied and
#                        the server restarted, which must report a cache hit.
# and finally writes decision.json with D8's rule applied in order.
#
# No Docker and no root: everything runs as the invoking user under --work-dir.
# Credentials are generated per run and passed by environment variable name.
# They are throwaway and are never printed. Nothing under --out-dir carries them:
# a failure record names the log under --work-dir instead of copying from it.
# --work-dir itself does hold them (store configs written 0600, store logs), so
# treat it as throwaway and delete it after the run.
#
# Usage:
#   scripts/s3-candidates.sh --out-dir DIR [--work-dir DIR] [--bin-cache DIR]
#                            [--candidate NAME]... [--idle-secs N] [--skip-clients]
#
# Exit: 0 a pick was made; 4 no candidate passes all seven (premise falsified,
#       D8 — halt); 2 usage / preflight; 3 a pin failed verification (sha256
#       mismatch or cooldown); 5 a download failed (network, not a mismatch);
#       1 the controls were void, or a step of the run itself failed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PINS="${SCRIPT_DIR}/s3-candidates.pins.toml"
LIB="${SCRIPT_DIR}/s3_candidates_lib.py"
CONFORMANCE="${SCRIPT_DIR}/s3-conformance.py"
BOTO3_PIN="1.43.126"   # released 2026-11-06, outside the 14-day cooldown on 2026-11-22
BUCKET="conformance"
REGION="us-east-1"

OUT_DIR=""
WORK_DIR=""
BIN_CACHE=""
IDLE_SECS=30
SKIP_CLIENTS=0
CANDIDATES=()

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        --work-dir) WORK_DIR="$2"; shift 2 ;;
        --bin-cache) BIN_CACHE="$2"; shift 2 ;;
        --candidate) CANDIDATES+=("$2"); shift 2 ;;
        --idle-secs) IDLE_SECS="$2"; shift 2 ;;
        --skip-clients) SKIP_CLIENTS=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "s3-candidates: unknown argument $1" >&2; usage >&2; exit 2 ;;
    esac
done

[ -n "$OUT_DIR" ] || { echo "s3-candidates: --out-dir is required" >&2; exit 2; }
for tool in curl sha256sum tar python3 uv openssl; do
    command -v "$tool" >/dev/null 2>&1 || { echo "s3-candidates: $tool is required" >&2; exit 2; }
done
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "s3-candidates: python3 >= 3.11 (tomllib) is required" >&2; exit 2; }

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
if [ -z "$WORK_DIR" ]; then WORK_DIR="$(mktemp -d -t s3-candidates.XXXXXX)"; fi
mkdir -p "$WORK_DIR"; WORK_DIR="$(cd "$WORK_DIR" && pwd)"
BIN_CACHE="${BIN_CACHE:-${WORK_DIR}/bin-cache}"
mkdir -p "$BIN_CACHE"

lib() { python3 -I "$LIB" "$@"; }
uvpy() { uv run --quiet --no-project --with "boto3==${BOTO3_PIN}" python "$@"; }
log() { printf '[s3-candidates] %s\n' "$*" >&2; }

if [ "${#CANDIDATES[@]}" -eq 0 ]; then
    mapfile -t CANDIDATES < <(lib names --pins "$PINS" --section candidate)
fi

# ── process bookkeeping: only PIDs this script started are ever signalled ──
PIDS=()
forget_pid() {
    local pid="$1" p kept=()
    for p in "${PIDS[@]}"; do [ "$p" != "$pid" ] && kept+=("$p"); done
    PIDS=("${kept[@]}")
}
# True only while PID is still a live child of THIS shell: a PID that exited and
# was reused by some other process is never signalled.
is_our_child() {
    local pid="$1" ppid
    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" || return 1
    [ "$ppid" = "$$" ]
}
stop_pid() {
    local pid="$1"
    forget_pid "$pid"
    is_our_child "$pid" || return 0
    kill -TERM "$pid" 2>/dev/null || return 0
    for _ in $(seq 1 40); do
        kill -0 "$pid" 2>/dev/null || { wait "$pid" 2>/dev/null || true; return 0; }
        sleep 0.25
    done
    is_our_child "$pid" && kill -KILL "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null || true
}
SCCACHE_BIN=""
SCCACHE_PORT_STARTED=""   # the port of an sccache server this script started, while it may be up
cleanup() {
    local pid
    for pid in "${PIDS[@]}"; do stop_pid "$pid"; done
    if [ -n "$SCCACHE_BIN" ] && [ -n "$SCCACHE_PORT_STARTED" ]; then
        # The port must be in the environment, or --stop-server addresses the
        # default 4226 server, which this script did not start.
        SCCACHE_SERVER_PORT="$SCCACHE_PORT_STARTED" "$SCCACHE_BIN" --stop-server >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

# ── fetch a pinned binary: fresh empty download dir, sha256 before unpack ──
# Prints the absolute path of the verified binary.
fetch_pinned() {
    local section="$1" name="$2"
    local PIN_VERSION PIN_URL PIN_SHA256 PIN_ARCHIVE PIN_BINARY PIN_ASSET
    eval "$(lib pin-env --pins "$PINS" --section "$section" --name "$name")"
    local why
    if ! why="$(lib cooldown --pins "$PINS" --section "$section" --name "$name")"; then
        log "REFUSING $name $PIN_VERSION: inside the release-age cooldown ($why)"
        return 3
    fi
    # The cache saves only the download. The cached asset is re-hashed against
    # the pin on every use and the binary is unpacked afresh from it, so nothing
    # in the cache is trusted on the strength of a file saying it was verified.
    # (This runs in a command substitution under `||`, where set -e is off, so
    # every step is checked explicitly.)
    local dir="${BIN_CACHE}/${name}-${PIN_VERSION}"
    local asset="${dir}/dl/${PIN_ASSET}"
    local bin="${dir}/bin/${PIN_BINARY}"
    if [ -f "$asset" ] && printf '%s  %s\n' "$PIN_SHA256" "$asset" | sha256sum -c --quiet - >/dev/null 2>&1; then
        log "reusing cached $name $PIN_VERSION download (sha256 re-checked against the pin)"
    else
        rm -rf "$dir"
        mkdir -p "${dir}/dl" || return 1
        log "fetching $name $PIN_VERSION ($why)"
        if ! curl -fsSL --retry 2 --max-time 600 -o "$asset" "$PIN_URL"; then
            log "DOWNLOAD FAILED for $name $PIN_VERSION ($PIN_URL): a network or upstream failure, not a mismatch"
            rm -rf "$dir"; return 5
        fi
        if ! printf '%s  %s\n' "$PIN_SHA256" "$asset" | sha256sum -c --quiet - >&2; then
            log "REFUSING $name $PIN_VERSION: sha256 mismatch against the pin"
            rm -rf "$dir"; return 3
        fi
    fi
    rm -rf "${dir}/bin"
    mkdir -p "${dir}/bin" || return 1
    case "$PIN_ARCHIVE" in
        none) install -m 0755 "$asset" "$bin" || return 1 ;;
        tar.gz) tar -xzf "$asset" -C "${dir}/bin" --no-same-owner || { log "$name: unpack failed"; return 1; } ;;
        *) log "unknown archive type $PIN_ARCHIVE"; return 3 ;;
    esac
    [ -x "$bin" ] || { log "$name: $PIN_BINARY not found after unpack"; return 3; }
    printf '%s\n' "$bin"
}

# One field of a pin entry (e.g. version), for records written by this script.
pin_field() {
    local section="$1" name="$2" field="$3"
    lib pin-env --pins "$PINS" --section "$section" --name "$name" \
        | sed -n "s/^PIN_${field}=//p" | tr -d "'"
}

licence_screen() {
    local name="$1" PIN_LICENSE_URL
    eval "$(lib pin-env --pins "$PINS" --section candidate --name "$name" | grep '^PIN_LICENSE_URL=')"
    local d="${WORK_DIR}/licence-${name}"
    rm -rf "$d"; mkdir -p "$d" || return 1
    if ! curl -fsSL --retry 2 --max-time 60 -o "${d}/LICENSE" "$PIN_LICENSE_URL"; then
        log "DOWNLOAD FAILED for $name's LICENSE ($PIN_LICENSE_URL)"
        return 5
    fi
    # exit 3 = screened out (recorded, LICENCE_EXCLUDED_EXIT); 0 = passed;
    # anything else (1 is an uncaught Python exception) is a failure of the step.
    lib licence --pins "$PINS" --name "$name" --file "${d}/LICENSE" --out "${OUT_DIR}/${name}.licence.json"
}

new_credentials() {
    # Garage requires its own key-id shape (GK + 24 hex); the other stores accept any.
    local ak sk
    ak="$(openssl rand -hex 12)" && sk="$(openssl rand -hex 32)" || return 1
    [ "${#ak}" -eq 24 ] && [ "${#sk}" -eq 64 ] || return 1
    CAND_ACCESS_KEY="GK${ak}"
    CAND_SECRET_KEY="${sk}"
    export CAND_ACCESS_KEY CAND_SECRET_KEY
}

# ── candidate start functions: set CAND_PID and ENDPOINT, leave $BUCKET ready ──
start_garage() {
    local bin="$1" d="$2" s3 rpc admin
    read -r s3 rpc admin < <(lib ports 3)
    mkdir -p "$d/meta" "$d/data"
    local old_umask; old_umask="$(umask)"
    umask 077
    cat >"$d/garage.toml" <<EOF
metadata_dir = "$d/meta"
data_dir = "$d/data"
db_engine = "lmdb"
replication_factor = 1
rpc_bind_addr = "127.0.0.1:$rpc"
rpc_public_addr = "127.0.0.1:$rpc"
rpc_secret = "$(openssl rand -hex 32)"

[s3_api]
s3_region = "$REGION"
api_bind_addr = "127.0.0.1:$s3"
root_domain = ".s3.garage.localhost"

[admin]
api_bind_addr = "127.0.0.1:$admin"
EOF
    umask "$old_umask"
    "$bin" -c "$d/garage.toml" server >"$d/server.log" 2>&1 &
    CAND_PID=$!; PIDS+=("$CAND_PID")
    lib wait-port 127.0.0.1 "$rpc" 30 || { log "garage rpc did not open"; return 1; }
    local node=""
    for _ in $(seq 1 40); do
        node="$("$bin" -c "$d/garage.toml" node id -q 2>/dev/null | cut -d@ -f1)" && [ -n "$node" ] && break
        sleep 0.5
    done
    [ -n "$node" ] || { log "garage node id unavailable"; return 1; }
    "$bin" -c "$d/garage.toml" layout assign -z dc1 -c 10G "$node" >>"$d/setup.log" 2>&1 || return 1
    "$bin" -c "$d/garage.toml" layout apply --version 1 >>"$d/setup.log" 2>&1 || return 1
    # The secret reaches garage's argv here: a throwaway per-run test key on a
    # loopback-only node, which `key import` accepts no other way.
    "$bin" -c "$d/garage.toml" key import --yes -n conformance "$CAND_ACCESS_KEY" "$CAND_SECRET_KEY" >>"$d/setup.log" 2>&1 || return 1
    "$bin" -c "$d/garage.toml" bucket create "$BUCKET" >>"$d/setup.log" 2>&1 || return 1
    "$bin" -c "$d/garage.toml" bucket allow --read --write --owner "$BUCKET" --key conformance >>"$d/setup.log" 2>&1 || return 1
    ENDPOINT="http://127.0.0.1:$s3"
    lib wait-port 127.0.0.1 "$s3" 30
}

start_seaweedfs() {
    local bin="$1" d="$2" m mg v vg f fg s sg ic ln
    read -r m mg v vg f fg s sg ic ln < <(lib ports 10)
    mkdir -p "$d/data"
    local old_umask; old_umask="$(umask)"
    umask 077
    cat >"$d/s3.json" <<EOF
{"identities": [{"name": "conformance",
  "credentials": [{"accessKey": "$CAND_ACCESS_KEY", "secretKey": "$CAND_SECRET_KEY"}],
  "actions": ["Admin", "Read", "List", "Tagging", "Write"]}]}
EOF
    umask "$old_umask"
    "$bin" server -dir="$d/data" -ip=127.0.0.1 -ip.bind=127.0.0.1 \
        -master.port="$m" -master.port.grpc="$mg" \
        -volume.port="$v" -volume.port.grpc="$vg" -volume.max=0 \
        -filer -filer.port="$f" -filer.port.grpc="$fg" \
        -s3 -s3.port="$s" -s3.port.grpc="$sg" -s3.port.iceberg="$ic" -s3.port.lance="$ln" -s3.config="$d/s3.json" \
        >"$d/server.log" 2>&1 &
    CAND_PID=$!; PIDS+=("$CAND_PID")
    ENDPOINT="http://127.0.0.1:$s"
    lib wait-port 127.0.0.1 "$s" 90 || { log "seaweedfs s3 did not open"; return 1; }
    # The S3 gateway opens before the filer/volume path can take a write; wait
    # until a bucket create succeeds rather than sleeping a guessed interval.
    local i
    for i in $(seq 1 60); do
        if uvpy - "$ENDPOINT" "$BUCKET" "$REGION" >/dev/null 2>&1 <<'PY'
import os, sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=sys.argv[1], region_name=sys.argv[3],
                 aws_access_key_id=os.environ["CAND_ACCESS_KEY"], aws_secret_access_key=os.environ["CAND_SECRET_KEY"],
                 config=Config(s3={"addressing_style": "path"}))
try:
    c.create_bucket(Bucket=sys.argv[2])
except c.exceptions.BucketAlreadyOwnedByYou:
    pass
c.put_object(Bucket=sys.argv[2], Key="ready-probe", Body=b"x")
c.delete_object(Bucket=sys.argv[2], Key="ready-probe")
PY
        then return 0; fi
        sleep 1
    done
    log "seaweedfs never accepted a write"; return 1
}

start_versitygw() {
    local bin="$1" d="$2" s
    read -r s < <(lib ports 1)
    mkdir -p "$d/posix" "$d/iam"
    ROOT_ACCESS_KEY="$CAND_ACCESS_KEY" ROOT_SECRET_KEY="$CAND_SECRET_KEY" \
        "$bin" --port "127.0.0.1:$s" --region "$REGION" --iam-dir "$d/iam" posix "$d/posix" \
        >"$d/server.log" 2>&1 &
    CAND_PID=$!; PIDS+=("$CAND_PID")
    ENDPOINT="http://127.0.0.1:$s"
    lib wait-port 127.0.0.1 "$s" 30 || { log "versitygw did not open"; return 1; }
    # posix backend: a bucket is a directory the gateway creates via CreateBucket.
    uvpy - "$ENDPOINT" "$BUCKET" "$REGION" >>"$d/setup.log" 2>&1 <<'PY'
import os, sys, boto3
from botocore.config import Config
c = boto3.client("s3", endpoint_url=sys.argv[1], region_name=sys.argv[3],
                 aws_access_key_id=os.environ["CAND_ACCESS_KEY"], aws_secret_access_key=os.environ["CAND_SECRET_KEY"],
                 config=Config(s3={"addressing_style": "path"}))
c.create_bucket(Bucket=sys.argv[2])
PY
}

# ── client round trips; each writes $OUT_DIR/<cand>.client-<name>.json ──
bazel_remote_round_trip() {
    local cand="$1" d="$2" out="$3"
    local br br_rc=0; br="$(fetch_pinned client bazel-remote)" || br_rc=$?
    if [ "$br_rc" -ne 0 ]; then
        write_unknown "$out" "bazel-remote binary unobtainable (fetch_pinned exit $br_rc: 3 pin refused, 5 download failed)"
        return 0
    fi
    local br_version; br_version="$(pin_field client bazel-remote VERSION)"
    local hostport="${ENDPOINT#http://}" p1 g1 p2 g2
    read -r p1 g1 p2 g2 < <(lib ports 4)
    mkdir -p "$d/br-a" "$d/br-b"
    head -c 200000 /dev/urandom >"$d/blob.bin"
    local sha; sha="$(sha256sum "$d/blob.bin" | cut -c1-64)"
    # bazel-remote reads the S3 secret from these env vars as well as flags,
    # which keeps the secret off argv.
    local -a s3flags=(--s3.endpoint "$hostport" --s3.bucket "$BUCKET" --s3.prefix "bazel-remote"
        --s3.auth_method access_key --s3.disable_ssl --s3.region "$REGION")
    BAZEL_REMOTE_S3_ACCESS_KEY_ID="$CAND_ACCESS_KEY" BAZEL_REMOTE_S3_SECRET_ACCESS_KEY="$CAND_SECRET_KEY" \
        "$br" --dir "$d/br-a" --max_size 1 --port "$p1" --grpc_port "$g1" "${s3flags[@]}" >"$d/br-a.log" 2>&1 &
    local pa=$!; PIDS+=("$pa")
    lib wait-port 127.0.0.1 "$p1" 30 || { write_fail "$out" "bazel-remote A did not open (see br-a.log)" "$d/br-a.log"; stop_pid "$pa"; return 0; }
    local put_status; put_status="$(lib http-put "http://127.0.0.1:$p1/cas/$sha" "$d/blob.bin")"
    local key=""
    key="$(uvpy "$LIB" s3-wait-key --endpoint "$ENDPOINT" --access-key-env CAND_ACCESS_KEY --secret-key-env CAND_SECRET_KEY \
            --region "$REGION" --bucket "$BUCKET" --prefix bazel-remote --contains "$sha" --secs 30 2>/dev/null)" || key=""
    stop_pid "$pa"
    BAZEL_REMOTE_S3_ACCESS_KEY_ID="$CAND_ACCESS_KEY" BAZEL_REMOTE_S3_SECRET_ACCESS_KEY="$CAND_SECRET_KEY" \
        "$br" --dir "$d/br-b" --max_size 1 --port "$p2" --grpc_port "$g2" "${s3flags[@]}" >"$d/br-b.log" 2>&1 &
    local pb=$!; PIDS+=("$pb")
    lib wait-port 127.0.0.1 "$p2" 30 || { write_fail "$out" "bazel-remote B did not open (see br-b.log)" "$d/br-b.log"; stop_pid "$pb"; return 0; }
    local get_status; get_status="$(lib http-get "http://127.0.0.1:$p2/cas/$sha" "$d/blob.back")"
    stop_pid "$pb"
    local equal=false
    if [ -f "$d/blob.back" ] && cmp -s "$d/blob.bin" "$d/blob.back"; then equal=true; fi
    local verdict=FAIL
    if [ "$put_status" = 200 ] && [ -n "$key" ] && [ "$get_status" = 200 ] && [ "$equal" = true ]; then verdict=PASS; fi
    python3 -I - "$out" "$verdict" "$put_status" "$key" "$get_status" "$equal" "$sha" "$br_version" <<'PY'
import json, sys
out, verdict, put, key, get, equal, sha, version = sys.argv[1:]
json.dump({"client": "bazel-remote", "version": version, "verdict": verdict,
           "cas_put_status_instance_a": put, "s3_object_key": key or None,
           "cas_get_status_instance_b_empty_disk": get, "bytes_equal": equal == "true",
           "blob_sha256": sha, "blob_bytes": 200000,
           "method": "PUT /cas/<sha256> on instance A; poll the bucket until the object lands under s3.prefix; "
                     "GET /cas/<sha256> on instance B whose --dir is empty, so the bytes can only come from S3"},
          open(out, "w"), indent=2)
PY
    log "$cand bazel-remote round trip: $verdict (put=$put_status key=${key:+present} get=$get_status equal=$equal)"
}

sccache_round_trip() {
    local cand="$1" d="$2" out="$3"
    local sc_rc=0; SCCACHE_BIN="$(fetch_pinned client sccache)" || sc_rc=$?
    if [ "$sc_rc" -ne 0 ]; then
        SCCACHE_BIN=""
        write_unknown "$out" "sccache binary unobtainable (fetch_pinned exit $sc_rc: 3 pin refused, 5 download failed)"
        return 0
    fi
    local sc_version; sc_version="$(pin_field client sccache VERSION)"
    command -v cargo >/dev/null 2>&1 || { write_unknown "$out" "cargo/rustc not on PATH, so no compile can be cached"; return 0; }
    local crate="$d/hello-crate"
    mkdir -p "$crate/src" "$d/sccache-local" "$d/xdg"
    cat >"$crate/Cargo.toml" <<'EOF'
[package]
name = "hello_sccache"
version = "0.1.0"
edition = "2021"

[lib]
path = "src/lib.rs"
EOF
    printf 'pub fn hello() -> &%sstatic str {\n    "hello, world"\n}\n' "'" >"$crate/src/lib.rs"
    local port; read -r port < <(lib ports 1)
    SCCACHE_PORT_STARTED="$port"
    local -a env=(
        SCCACHE_BUCKET="$BUCKET" SCCACHE_ENDPOINT="${ENDPOINT#http://}" SCCACHE_S3_USE_SSL=off
        SCCACHE_REGION="$REGION" SCCACHE_S3_KEY_PREFIX="sccache" SCCACHE_SERVER_PORT="$port"
        SCCACHE_DIR="$d/sccache-local" SCCACHE_IDLE_TIMEOUT=0 SCCACHE_CONF="$d/xdg/none.toml"
        SCCACHE_ERROR_LOG="$d/sccache-error.log" SCCACHE_LOG=info
        XDG_CONFIG_HOME="$d/xdg" AWS_ACCESS_KEY_ID="$CAND_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$CAND_SECRET_KEY"
        AWS_DEFAULT_REGION="$REGION" AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null
        RUSTC_WRAPPER="$SCCACHE_BIN" CARGO_INCREMENTAL=0
    )
    : >"$d/xdg/none.toml"
    local build_rc=0
    # Chained with &&: under `|| build_rc=$?` set -e is off, so a plain list
    # would report only its last command's status.
    {
        env "${env[@]}" "$SCCACHE_BIN" --start-server &&
        env "${env[@]}" "$SCCACHE_BIN" --zero-stats >/dev/null &&
        env "${env[@]}" CARGO_TARGET_DIR="$d/target-1" cargo build --manifest-path "$crate/Cargo.toml" --offline -q &&
        env "${env[@]}" "$SCCACHE_BIN" --show-stats --stats-format json >"$d/stats-1.json" &&
        env "${env[@]}" "$SCCACHE_BIN" --stop-server >/dev/null &&
        # Same target path, emptied: rustc's -L dependency=<target> argument is
        # part of sccache's hash key, so a different path would miss by design.
        rm -rf "$d/target-1" &&
        env "${env[@]}" "$SCCACHE_BIN" --start-server &&
        env "${env[@]}" "$SCCACHE_BIN" --zero-stats >/dev/null &&
        env "${env[@]}" CARGO_TARGET_DIR="$d/target-1" cargo build --manifest-path "$crate/Cargo.toml" --offline -q &&
        env "${env[@]}" "$SCCACHE_BIN" --show-stats --stats-format json >"$d/stats-2.json" &&
        env "${env[@]}" "$SCCACHE_BIN" --stop-server >/dev/null
    } >"$d/sccache-run.log" 2>&1 || build_rc=$?
    # A failed step above may have left the server up: stop it by its own port.
    SCCACHE_SERVER_PORT="$port" "$SCCACHE_BIN" --stop-server >/dev/null 2>&1 || true
    SCCACHE_PORT_STARTED=""
    local count; count="$(uvpy "$LIB" s3-count --endpoint "$ENDPOINT" --access-key-env CAND_ACCESS_KEY \
        --secret-key-env CAND_SECRET_KEY --region "$REGION" --bucket "$BUCKET" --prefix sccache 2>/dev/null || echo '{}')"
    python3 -I - "$out" "$d/stats-1.json" "$d/stats-2.json" "$build_rc" "$count" "$sc_version" <<'PY'
import json, sys
out, s1p, s2p, rc, count, version = sys.argv[1:]
def load(p):
    try:
        return json.load(open(p))
    except Exception:
        return {}
def tot(stats, field):
    v = stats.get("stats", {}).get(field, {})
    if isinstance(v, dict):
        return sum(x for x in v.get("counts", {}).values() if isinstance(x, int))
    return v if isinstance(v, int) else 0
s1, s2 = load(s1p), load(s2p)
writes1 = s1.get("stats", {}).get("cache_writes", 0)
werr1 = s1.get("stats", {}).get("cache_write_errors", 0)
hits2 = tot(s2, "cache_hits")
miss2 = tot(s2, "cache_misses")
rerr2 = s2.get("stats", {}).get("cache_errors", {})
rerr2 = sum(rerr2.get("counts", {}).values()) if isinstance(rerr2, dict) else 0
location = s1.get("cache_location", "")
objects = json.loads(count or "{}").get("objects", 0)
ok = rc == "0" and writes1 >= 1 and werr1 == 0 and hits2 >= 1 and objects >= 1 and "s3" in location.lower()
json.dump({"client": "sccache", "version": version, "verdict": "PASS" if ok else "FAIL",
           "build_exit": int(rc), "cache_location": location,
           "build_1_cache_writes": writes1, "build_1_cache_write_errors": werr1,
           "build_2_cache_hits": hits2, "build_2_cache_misses": miss2, "build_2_cache_errors": rerr2,
           "bucket_objects_under_prefix": objects,
           "method": "cargo build of a hello-world LIBRARY crate with RUSTC_WRAPPER=sccache (bin crates are not cacheable); "
                     "server restarted and the same CARGO_TARGET_DIR emptied for build 2, which must hit (served from S3: the s3 backend keeps no local cache)"},
          open(out, "w"), indent=2)
print("PASS" if ok else "FAIL")
PY
    log "$cand sccache round trip written to $(basename "$out")"
}

write_unknown() {
    python3 -I - "$1" "$2" <<'PY'
import json, sys
json.dump({"verdict": "UNKNOWN", "reason": sys.argv[2]}, open(sys.argv[1], "w"), indent=2)
PY
}
# A failure record names the log; it never copies from it. Store logs live under
# --work-dir and may echo the run's credentials, which must not reach --out-dir.
write_fail() {
    python3 -I - "$1" "$2" "$3" <<'PY'
import json, sys
json.dump({"verdict": "FAIL", "reason": sys.argv[2], "log": sys.argv[3],
           "log_note": "under --work-dir; not copied here because a store log may echo the run's credentials"},
          open(sys.argv[1], "w"), indent=2)
PY
}

# ── main loop ──────────────────────────────────────────────────────────────
run_candidate() {
    local name="$1"
    local d="${WORK_DIR}/run-${name}"
    # run_candidate is called under `if`, where set -e is off, so every step is
    # checked here. A recorded RESULT (licence screened out, store failed to
    # start, conformance below 7/7) continues; a failure of the run itself
    # returns non-zero and stops the run.
    rm -rf "$d"; mkdir -p "$d" || return 1
    local bin rc=0
    bin="$(fetch_pinned candidate "$name")" || return $?
    licence_screen "$name" || rc=$?
    case "$rc" in
        0) ;;
        3) log "$name EXCLUDED by the licence screen (recorded)" ;;
        5) return 5 ;;
        *) log "$name licence screen could not run (exit $rc)"; return 1 ;;
    esac
    new_credentials || { log "could not generate credentials"; return 1; }
    CAND_PID=""; ENDPOINT=""
    if ! "start_${name}" "$bin" "$d"; then
        log "$name failed to start; see $d/server.log and $d/setup.log"
        write_fail "${OUT_DIR}/${name}.start.json" "start or setup failed" "$d"
        [ -n "$CAND_PID" ] && stop_pid "$CAND_PID"
        return 0
    fi
    log "$name up at $ENDPOINT (pid $CAND_PID); idling ${IDLE_SECS}s before the RSS read"
    sleep "$IDLE_SECS"
    lib rss --pid "$CAND_PID" --out "${OUT_DIR}/${name}.idle.json" || { log "$name: idle RSS unreadable"; return 1; }
    rc=0
    uvpy "$CONFORMANCE" --endpoint "$ENDPOINT" --access-key-env CAND_ACCESS_KEY --secret-key-env CAND_SECRET_KEY \
        --bucket "$BUCKET" --region "$REGION" --label "$name" --json-out "${OUT_DIR}/${name}.conformance.json" || rc=$?
    case "$rc" in
        0) ;;
        1) log "$name conformance: not 7/7 PASS (recorded)" ;;
        *) log "$name conformance executed nothing or could not run (exit $rc; recorded as such)" ;;
    esac
    [ -f "${OUT_DIR}/${name}.conformance.json" ] || { log "$name: conformance wrote no record"; return 1; }
    if [ "$SKIP_CLIENTS" -eq 0 ]; then
        bazel_remote_round_trip "$name" "$d" "${OUT_DIR}/${name}.client-bazel-remote.json"
        sccache_round_trip "$name" "$d" "${OUT_DIR}/${name}.client-sccache.json"
    else
        write_unknown "${OUT_DIR}/${name}.client-bazel-remote.json" "--skip-clients"
        write_unknown "${OUT_DIR}/${name}.client-sccache.json" "--skip-clients"
    fi
    stop_pid "$CAND_PID"
    # One record per candidate, assembled from the parts (decide reads it).
    python3 -I - "$OUT_DIR" "$name" <<'PY' || { log "$name: could not assemble its record"; return 1; }
import json, sys
from pathlib import Path
out, name = Path(sys.argv[1]), sys.argv[2]
def load(suffix):
    try:
        return json.loads((out / f"{name}.{suffix}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
rec = {"name": name, "licence": load("licence"), "idle": load("idle"), "conformance": load("conformance"),
       "clients": {"bazel-remote": load("client-bazel-remote"), "sccache": load("client-sccache")},
       "start_failure": load("start")}
(out / f"{name}.json").write_text(json.dumps(rec, indent=2) + "\n")
PY
}

# ── controls first: the run is VOID unless the broken stub fails exactly the
# two operations it breaks and the conforming stub passes all seven (D8,
# [policy: testing/a-test-must-be-able-to-fail]). The stub is the one the unit
# test uses, so the control and the gate exercise the same code.
run_controls() {
    uvpy - "$SCRIPT_DIR" "$OUT_DIR" <<'PY'
import importlib.util, json, os, sys
from pathlib import Path
script_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
lspec = importlib.util.spec_from_file_location("s3_candidates_lib", script_dir / "s3_candidates_lib.py")
lib = importlib.util.module_from_spec(lspec)
sys.modules[lspec.name] = lib
lspec.loader.exec_module(lib)
spec = importlib.util.spec_from_file_location("test_s3_conformance", script_dir / "tests" / "test_s3_conformance.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
os.environ["CTRL_AK"], os.environ["CTRL_SK"] = mod.ACCESS, mod.SECRET
verdicts = {}
for mode in ("broken", "conforming"):
    out = out_dir / f"control-{mode}.conformance.json"
    with mod._StubServer(mode) as stub:
        code = mod.conformance.main(["--endpoint", stub.endpoint, "--access-key-env", "CTRL_AK",
                                     "--secret-key-env", "CTRL_SK", "--bucket", "conformance",
                                     "--label", f"control-{mode}", "--json-out", str(out)])
    rec = json.loads(out.read_text())
    rec["exit_code"] = code
    out.write_text(json.dumps(rec, indent=2) + "\n")
    verdicts[mode] = {"operations": {o["op"]: o["verdict"] for o in rec["operations"]}, "exit_code": code}
# The validity rule lives in ONE place, the lib (cmd_decide applies it too).
ok = lib.controls_are_valid(verdicts)
failed = sorted(op for op, v in verdicts["broken"]["operations"].items() if v != "PASS")
print(f"controls: broken stub failed {failed} (exit {verdicts['broken']['exit_code']}); "
      f"conforming stub exit {verdicts['conforming']['exit_code']} -> {'VALID' if ok else 'VOID'}")
sys.exit(0 if ok else 1)
PY
}
if ! run_controls; then
    log "control verdicts are wrong: this run is VOID (D8); nothing is decided"
    exit 1
fi

for name in "${CANDIDATES[@]}"; do
    rm -f "${OUT_DIR}/${name}".*.json "${OUT_DIR}/${name}.json"
    rc=0
    run_candidate "$name" || rc=$?
    case "$rc" in
        0) ;;
        3) log "pin verification failed for $name; stopping"; exit 3 ;;
        5) log "a download failed for $name; stopping (retry when the upstream answers)"; exit 5 ;;
        *) log "a step of the run failed for $name (exit $rc); stopping, nothing is decided"; exit 1 ;;
    esac
done

lib decide --pins "$PINS" --results-dir "$OUT_DIR" --out "${OUT_DIR}/decision.json"
