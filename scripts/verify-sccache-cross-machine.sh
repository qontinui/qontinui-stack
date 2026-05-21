#!/usr/bin/env bash
# Verify the cross-machine sccache shared-S3 cache actually pays off.
#
# Phase 2.3 of plans/2026-05-21-coordination-improvements.md. Phase 2.1 stood
# up the shared bucket (qontinui-sccache-shared); Phase 2.2 pointed the runner
# / coord .cargo/config.toml at it. This script is the repeatable MECHANISM
# that proves the cross-machine cycle-time win instead of eyeballing it once
# (memory: feedback_build_verification_over_manual_observation).
#
# Two roles, run on two different machines against the SAME source state:
#
#   machine A (producer):  ./verify-sccache-cross-machine.sh --role producer
#       cargo clean -> cold build -> uploads compile units to S3, records writes.
#       Prints the exact command to run on machine B.
#
#   machine B (consumer):  ./verify-sccache-cross-machine.sh --role consumer
#       fresh build -> SHOULD pull A's artifacts from S3 -> asserts the
#       cache-hit rate clears the plan's >80% termination predicate.
#       Exits 0 if the predicate holds, 1 otherwise.
#
# Single-machine substrate proof (no second machine needed): run producer
# then consumer on the SAME machine. Because the consumer builds into a fresh
# empty target dir, every cacheable compile unit is a miss locally and can
# only be satisfied from S3 — so consumer HITS prove the round-trip works.
# This is what Phase 2.3 measured on spaceship while MSI was offline.
#
# Usage:
#   ./verify-sccache-cross-machine.sh [--role producer|consumer] [--crate PATH]
#                                     [--threshold PCT] [--dry-run]
#
#   --role        producer (default) or consumer.
#   --crate       Path to a cargo crate to build. Default: a synthetic probe
#                 crate generated under $TMPDIR that pulls serde + tokio so
#                 there are real compile units to cache. Pass a real workspace
#                 member (e.g. ../../qontinui-coord) for a heavier signal.
#   --threshold   Min cache-hit rate (%) the consumer must clear. Default 80
#                 (the plan's Stream-2 termination predicate).
#   --dry-run     Self-check only: validate env + tooling + arg parsing and
#                 print what WOULD run. No cargo, no S3 writes. Exits 0/1 on
#                 whether the machine is correctly configured.
#
# Required env (the same vars qontinui-stack/docs/sccache-cross-machine.md
# prescribes; the script fails fast with an actionable message if missing):
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
#   SCCACHE_BUCKET   (expected: qontinui-sccache-shared)
#   SCCACHE_REGION   (expected: us-east-1)
#
set -euo pipefail

ROLE="producer"
CRATE=""
THRESHOLD="80"
DRY_RUN="0"

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo ">> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --role)      ROLE="${2:-}"; shift 2 ;;
    --crate)     CRATE="${2:-}"; shift 2 ;;
    --threshold) THRESHOLD="${2:-}"; shift 2 ;;
    --dry-run)   DRY_RUN="1"; shift ;;
    -h|--help)   sed -n '2,55p' "$0"; exit 0 ;;
    *)           die "unknown arg: $1 (try --help)" ;;
  esac
done

case "$ROLE" in
  producer|consumer) ;;
  *) die "--role must be 'producer' or 'consumer', got '$ROLE'" ;;
esac

# ---------------------------------------------------------------------------
# Preflight: tooling + env. Fail fast with actionable guidance.
# ---------------------------------------------------------------------------
command -v sccache >/dev/null 2>&1 || die "sccache not on PATH. Install it (cargo install sccache) and retry."
command -v cargo   >/dev/null 2>&1 || die "cargo not on PATH."

missing=""
[ -n "${AWS_ACCESS_KEY_ID:-}" ]     || missing="$missing AWS_ACCESS_KEY_ID"
[ -n "${AWS_SECRET_ACCESS_KEY:-}" ] || missing="$missing AWS_SECRET_ACCESS_KEY"
[ -n "${SCCACHE_BUCKET:-}" ]        || missing="$missing SCCACHE_BUCKET"
[ -n "${SCCACHE_REGION:-}" ]        || missing="$missing SCCACHE_REGION"
if [ -n "$missing" ]; then
  die "missing required env:$missing
  See qontinui-stack/docs/sccache-cross-machine.md 'Per-machine setup'.
  Quickstart:
    export AWS_ACCESS_KEY_ID=<key>
    export AWS_SECRET_ACCESS_KEY=<secret>
    export SCCACHE_BUCKET=qontinui-sccache-shared
    export SCCACHE_REGION=us-east-1
    unset SCCACHE_ENDPOINT SCCACHE_S3_USE_SSL SCCACHE_MINIO_HOST"
fi

EXPECTED_BUCKET="qontinui-sccache-shared"
if [ "$SCCACHE_BUCKET" != "$EXPECTED_BUCKET" ]; then
  note "WARNING: SCCACHE_BUCKET='$SCCACHE_BUCKET' (expected '$EXPECTED_BUCKET'). Continuing, but cross-machine sharing only works if both machines use the same bucket."
fi

# These would silently divert sccache to the in-stack MinIO lane and break the
# cross-machine premise. Unset them for this process so the daemon (re)starts
# clean against real AWS S3.
unset SCCACHE_ENDPOINT SCCACHE_S3_USE_SSL SCCACHE_MINIO_HOST SCCACHE_MINIO_USER SCCACHE_MINIO_PASS 2>/dev/null || true

export RUSTC_WRAPPER=sccache

# ---------------------------------------------------------------------------
# Resolve crate to build (generate synthetic probe if none given).
# ---------------------------------------------------------------------------
GENERATED_PROBE="0"
if [ -z "$CRATE" ]; then
  PROBE_ROOT="${TMPDIR:-/tmp}/sccache_xmachine_probe"
  CRATE="$PROBE_ROOT"
  GENERATED_PROBE="1"
fi

gen_probe() {
  # Deterministic synthetic crate: identical Cargo.toml + src on both machines
  # so the compile-unit cache keys match. serde + tokio give a few dozen real
  # rustc invocations to cache.
  rm -rf "$CRATE"
  mkdir -p "$CRATE/src"
  cat > "$CRATE/Cargo.toml" <<'TOML'
[package]
name = "sccache_xmachine_probe"
version = "0.1.0"
edition = "2021"

[dependencies]
serde = { version = "1", features = ["derive"] }
tokio = { version = "1", features = ["full"] }
TOML
  cat > "$CRATE/src/main.rs" <<'RS'
use serde::{Deserialize, Serialize};

#[derive(Serialize, Deserialize, Debug)]
struct Probe { name: String, n: u64 }

#[tokio::main]
async fn main() {
    let p = Probe { name: "sccache".into(), n: 42 };
    let s = serde_json::to_string(&p).unwrap_or_default();
    let _ = tokio::time::sleep(std::time::Duration::from_millis(0)).await;
    println!("{s}");
}
RS
  # serde_json needed by main.rs; add it without `cargo add` to stay offline-safe.
  cat >> "$CRATE/Cargo.toml" <<'TOML'
serde_json = "1"
TOML
}

# ---------------------------------------------------------------------------
# Dry-run: validate config + print plan, then exit. No build, no S3 writes.
# ---------------------------------------------------------------------------
if [ "$DRY_RUN" = "1" ]; then
  note "DRY RUN — environment self-check"
  note "role        = $ROLE"
  note "crate       = $CRATE$( [ "$GENERATED_PROBE" = "1" ] && echo ' (would be generated)')"
  note "threshold   = ${THRESHOLD}%"
  note "bucket      = $SCCACHE_BUCKET"
  note "region      = $SCCACHE_REGION"
  note "RUSTC_WRAPPER = $RUSTC_WRAPPER"
  note "sccache     = $(sccache --version)"
  note "cargo       = $(cargo --version)"
  note "OK: all required env present and tooling found. Re-run without --dry-run to measure."
  exit 0
fi

[ "$GENERATED_PROBE" = "1" ] && { note "generating synthetic probe crate at $CRATE"; gen_probe; }
[ -f "$CRATE/Cargo.toml" ] || die "no Cargo.toml at '$CRATE'"

# ---------------------------------------------------------------------------
# Bounce the daemon so it forks with the S3 env (memory:
# feedback_sccache_daemon_env_inheritance). Then confirm it actually points
# at S3 — robust to sccache 0.15's "Cache location  s3, name: <bucket>" and
# older "S3, bucket: Bucket(name=<bucket>)" formats.
# ---------------------------------------------------------------------------
note "bouncing sccache daemon to inherit S3 env"
sccache --stop-server >/dev/null 2>&1 || true
sccache --start-server >/dev/null 2>&1 || die "sccache --start-server failed"

LOC="$(sccache --show-stats 2>/dev/null | grep -i 'cache location' || true)"
note "sccache cache location: ${LOC:-<unknown>}"
echo "$LOC" | grep -qi "$SCCACHE_BUCKET" \
  || die "sccache is NOT pointed at bucket '$SCCACHE_BUCKET' (got: ${LOC:-none}).
  Daemon picked up stale env. Re-export the SCCACHE_* / AWS_* vars, then
  stop-server + start-server in THIS shell (order matters)."

# Fresh empty target dir per role: forces every compile unit to be resolved
# from cache (S3) rather than served by a stale local target/.
TARGET_DIR="$CRATE/.xverify_target_${ROLE}"
rm -rf "$TARGET_DIR" 2>/dev/null || true

stat_value() { # $1=label -> integer value from --show-stats, 0 if absent
  sccache --show-stats 2>/dev/null | grep -iE "^$1" | head -1 | grep -oE '[0-9]+' | head -1 || echo 0
}
hit_rate() { # parse "Cache hits rate   85.71 %" -> 85.71 ; falls back to computed
  local r
  r="$(sccache --show-stats 2>/dev/null | grep -iE 'cache hits? rate' | head -1 | grep -oE '[0-9]+(\.[0-9]+)?' | head -1 || true)"
  echo "${r:-0}"
}

run_build() {
  # Build log goes to stderr so the caller's $(run_build) captures ONLY the
  # elapsed-seconds integer on stdout.
  local t0 t1
  sccache --zero-stats >/dev/null 2>&1 || true
  t0=$(date +%s)
  ( cd "$CRATE" && cargo build --target-dir "$TARGET_DIR" ) 2>&1 | tail -2 >&2
  t1=$(date +%s)
  echo $(( t1 - t0 ))
}

if [ "$ROLE" = "producer" ]; then
  note "=== PRODUCER: cold build, uploading compile units to S3 ==="
  ELAPSED="$(run_build)"
  HITS="$(stat_value 'Cache hits')"
  MISSES="$(stat_value 'Cache misses')"
  RATE="$(hit_rate)"
  echo
  note "producer wall-clock: ${ELAPSED}s   hits=$HITS misses=$MISSES rate=${RATE}%"
  note "the $MISSES misses were written to S3 (bucket $SCCACHE_BUCKET)."
  echo
  cat <<EONEXT
============================================================
NEXT: on the OTHER machine, with the SAME source state and
the same SCCACHE_* / AWS_* env exported, run:

    ./verify-sccache-cross-machine.sh --role consumer \\
        ${GENERATED_PROBE:+# (omit --crate to regenerate the identical probe)}\\
        $( [ "$GENERATED_PROBE" = "1" ] || echo "--crate $CRATE" )

It will build fresh and assert the cache-hit rate clears ${THRESHOLD}%
(the plan's Stream-2 termination predicate). That asserted run is the
true cross-machine number.
============================================================
EONEXT
  exit 0
fi

# consumer
note "=== CONSUMER: fresh build, expecting S3 cache HITS from the producer ==="
ELAPSED="$(run_build)"
HITS="$(stat_value 'Cache hits')"
MISSES="$(stat_value 'Cache misses')"
ERRORS="$(stat_value 'Cache errors')"
RATE="$(hit_rate)"
echo
note "consumer wall-clock: ${ELAPSED}s   hits=$HITS misses=$MISSES errors=$ERRORS rate=${RATE}%"

# Integer compare (RATE may be float) without bc: use awk.
PASS="$(awk -v r="$RATE" -v t="$THRESHOLD" 'BEGIN{print (r+0 >= t+0) ? 1 : 0}')"
if [ "$PASS" = "1" ]; then
  note "PASS: cache-hit rate ${RATE}% >= ${THRESHOLD}% — shared cache is paying off."
  exit 0
else
  note "FAIL: cache-hit rate ${RATE}% < ${THRESHOLD}%."
  note "Likely causes: source state differs between machines; --remap-path-prefix"
  note "not matching (worktree path); or the producer never ran. Surface as a"
  note "Stream-2.4 follow-up per the plan."
  exit 1
fi
