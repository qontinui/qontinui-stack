#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# pre-checkout-coord-guard.sh
#
# Purpose
#   Block branch-mutating git operations (`git switch`, `git checkout <branch>`,
#   `git reset --hard`) in a working tree the caller does NOT hold a live
#   `Worktree` claim against in qontinui-coord. Phase 6 Item 1 enforcement.
#   See: D:/qontinui-root/tmp_coord_phase6_agent_coordination_hardening.md
#        ("Item 1 — Worktree claims" / "Enforcement").
#
# Why this isn't a real git pre-checkout hook
#   Git ships no `pre-checkout` hook. The available hooks adjacent to checkout
#   are `post-checkout` (fires AFTER HEAD already moved — too late to block)
#   and `pre-commit` (fires on commit, not on checkout). The Phase 6 plan
#   uses the name "pre-checkout-coord-guard" as a logical label.
#
#   Real-world invocation is therefore as a wrapper script the agent (or the
#   user) invokes BEFORE running the underlying git command. Examples:
#
#       # Inline gate before a switch:
#       /path/to/pre-checkout-coord-guard.sh && git switch <branch>
#
#       # As a function in your shell rc:
#       git-switch-safe() {
#         /path/to/pre-checkout-coord-guard.sh && git switch "$@"
#       }
#
#       # Or alias:
#       alias gswitch='/path/to/pre-checkout-coord-guard.sh && git switch'
#
#   The script does NOT shell out to `git` itself — the caller is responsible
#   for running the underlying command after the gate passes. This keeps the
#   gate semantically simple ("is the claim held?") and lets it be reused
#   before `reset --hard`, `checkout -B`, etc., without the guard guessing
#   intent.
#
# Identity model
#   Caller identity is `machine_id` (UUID) — same identity the existing claim
#   API uses. Resolution order:
#     1. $QONTINUI_AGENT_ID (env override; agents in non-default machines)
#     2. ~/.qontinui/machine.json::.machine_id (canonical, per topology plan §3)
#     3. ~/.qontinui/machine_id (legacy bare-string form, if present)
#     4. None — guard fails open with a warning ("identity unknown — skipped").
#
# Override flag
#   Set QONTINUI_COORD_GUARD=skip to bypass the check (one-off interactive
#   use, recovery, etc.). Logged to stderr as a notice so the bypass is
#   auditable.
#
# Endpoint usage
#   This script reads:
#       GET <COORD_URL>/coord/claims/by-resource?kind=worktree&key=<urlenc>
#   The corresponding acquire endpoint is currently `POST /claims/acquire`
#   (NOT /coord-prefixed — the legacy unprefixed root, see Phase 6 plan
#   "Endpoint prefix policy"). The by-resource lookup is the new Phase 6
#   /coord-prefixed endpoint.
#
# Robustness
#   - Missing `curl`        → warn + exit 0 (fail-open).
#   - Coord unreachable     → warn + exit 0 (fail-open; guard is advisory).
#   - HTTP 4xx/5xx other    → warn + exit 0 (fail-open; e.g. endpoint not
#                              yet wired in a parallel deploy).
#   - JSON shape unexpected → warn + exit 0 (fail-open).
#   - Missing identity      → warn + exit 0 (can't decide ownership).
#
# Exit codes
#   0  — guard passed (claim held by us, or fail-open path taken, or skip)
#   1  — guard blocked (no claim, or claim held by another machine)
#   2  — usage error (e.g., not in a git working tree)
# ----------------------------------------------------------------------------

set -euo pipefail

# ---- trap unexpected exits -------------------------------------------------
on_unexpected_exit() {
  local rc=$?
  if [[ $rc -ne 0 && $rc -ne 1 && $rc -ne 2 ]]; then
    printf '%s: unexpected exit (rc=%d) — failing open\n' \
      "qontinui-coord guard" "$rc" >&2
    exit 0
  fi
}
trap on_unexpected_exit EXIT

# ---- escape hatch ----------------------------------------------------------
if [[ "${QONTINUI_COORD_GUARD:-}" == "skip" ]]; then
  printf '%s: QONTINUI_COORD_GUARD=skip — bypassing claim check\n' \
    "qontinui-coord guard" >&2
  trap - EXIT
  exit 0
fi

# ---- pre-flight: tooling ---------------------------------------------------
if ! command -v curl >/dev/null 2>&1; then
  printf '%s: curl not on PATH — guard skipped (fail-open)\n' \
    "qontinui-coord guard" >&2
  trap - EXIT
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  printf '%s: git not on PATH — cannot determine working tree\n' \
    "qontinui-coord guard" >&2
  trap - EXIT
  exit 2
fi

# ---- resolve working-tree root --------------------------------------------
wt_root=""
if ! wt_root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  printf '%s: not inside a git working tree — guard not applicable\n' \
    "qontinui-coord guard" >&2
  trap - EXIT
  exit 2
fi

# Canonicalize: prefer realpath (Linux/macOS/Git-Bash); fall back to pwd -P.
canonical_path=""
if command -v realpath >/dev/null 2>&1; then
  canonical_path="$(realpath "$wt_root" 2>/dev/null || true)"
fi
if [[ -z "$canonical_path" ]]; then
  canonical_path="$(cd "$wt_root" && pwd -P)"
fi
# On Git-Bash for Windows, paths come back as /d/qontinui-root/...; the
# coord stores the absolute path opaquely (caller-side canonicalization is
# the contract per claims.rs), so we leave the value as-is. Agents that
# acquire claims must use the SAME canonical form here. If your acquire
# script normalizes to D:/... we recommend adding a normalization step
# below; for now we ship the raw realpath output.

# ---- resolve machine identity ---------------------------------------------
machine_id=""

if [[ -n "${QONTINUI_AGENT_ID:-}" ]]; then
  machine_id="$QONTINUI_AGENT_ID"
fi

if [[ -z "$machine_id" ]] && [[ -f "$HOME/.qontinui/machine.json" ]]; then
  if command -v jq >/dev/null 2>&1; then
    machine_id="$(jq -r '.machine_id // empty' "$HOME/.qontinui/machine.json" 2>/dev/null || true)"
  else
    # Plain-text extraction: "machine_id":"<uuid>"
    machine_id="$(grep -oE '"machine_id"[[:space:]]*:[[:space:]]*"[^"]+"' \
      "$HOME/.qontinui/machine.json" 2>/dev/null \
      | sed -E 's/.*"machine_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' \
      | head -n1 || true)"
  fi
fi

if [[ -z "$machine_id" ]] && [[ -f "$HOME/.qontinui/machine_id" ]]; then
  machine_id="$(tr -d '[:space:]' < "$HOME/.qontinui/machine_id" 2>/dev/null || true)"
fi

if [[ -z "$machine_id" ]]; then
  printf '%s: identity unknown (no $QONTINUI_AGENT_ID, no ~/.qontinui/machine.json, no ~/.qontinui/machine_id) — guard skipped\n' \
    "qontinui-coord guard" >&2
  trap - EXIT
  exit 0
fi

# ---- coord URL -------------------------------------------------------------
coord_url="${COORD_URL:-http://localhost:9870}"
# Strip a trailing slash so the joined URL is clean.
coord_url="${coord_url%/}"

# ---- urlencode the path ----------------------------------------------------
# Pure-bash urlencoder. Encodes everything outside [A-Za-z0-9_.~-].
urlencode() {
  local s="$1" out="" c
  local i=0 len=${#s}
  while (( i < len )); do
    c="${s:$i:1}"
    case "$c" in
      [a-zA-Z0-9._~-]) out+="$c" ;;
      *) out+="$(printf '%%%02X' "'$c")" ;;
    esac
    i=$((i + 1))
  done
  printf '%s' "$out"
}
encoded_path="$(urlencode "$canonical_path")"

lookup_url="$coord_url/coord/claims/by-resource?kind=worktree&key=$encoded_path"

# ---- query coord -----------------------------------------------------------
# Capture body and HTTP status separately. -m 10s timeout. -f would suppress
# the body on 4xx/5xx, which we want to see for diagnostics, so use a custom
# write-out instead.
http_code=""
response_body=""
tmp_body="$(mktemp 2>/dev/null || mktemp -t coordguard)"
# shellcheck disable=SC2064
trap "rm -f '$tmp_body'; on_unexpected_exit" EXIT

if ! http_code="$(curl -sS -m 10 -o "$tmp_body" -w '%{http_code}' \
    "$lookup_url" 2>/dev/null)"; then
  printf '%s: coord unreachable at %s — guard skipped (fail-open)\n' \
    "qontinui-coord guard" "$coord_url" >&2
  rm -f "$tmp_body"
  trap - EXIT
  exit 0
fi

response_body="$(cat "$tmp_body" 2>/dev/null || true)"
rm -f "$tmp_body"

# Re-install the simple trap (no temp file to clean now).
trap on_unexpected_exit EXIT

if [[ "$http_code" =~ ^[45] ]]; then
  printf '%s: coord returned HTTP %s for %s — guard skipped (fail-open).\n' \
    "qontinui-coord guard" "$http_code" "$lookup_url" >&2
  printf '%s: response body: %s\n' "qontinui-coord guard" "$response_body" >&2
  trap - EXIT
  exit 0
fi

# ---- interpret response ----------------------------------------------------
# Expected shapes (from Phase 6 plan):
#   null                     — no live claim for this resource
#   { "machine_id": "...", "kind": "worktree", "resource_key": "...", ... }
#                             — live claim held by <machine_id>
# Defensive: if the shape is anything else, fail-open.

# Strip whitespace for the null check.
trimmed="$(printf '%s' "$response_body" | tr -d '[:space:]')"

if [[ "$trimmed" == "null" || -z "$trimmed" ]]; then
  cat >&2 <<EOF
qontinui-coord guard: no Worktree claim held for $canonical_path.
Acquire one before changing branches in this tree:

    curl -X POST $coord_url/claims/acquire \\
         -H 'Content-Type: application/json' \\
         -d '{"kind":"worktree","resource_key":"$canonical_path","machine_id":"$machine_id"}'

Override (e.g. for one-off interactive use):
    QONTINUI_COORD_GUARD=skip <git command>

EOF
  trap - EXIT
  exit 1
fi

# Extract holder machine_id from the response.
holder=""
if command -v jq >/dev/null 2>&1; then
  holder="$(printf '%s' "$response_body" | jq -r '.machine_id // empty' 2>/dev/null || true)"
fi
if [[ -z "$holder" ]]; then
  holder="$(printf '%s' "$response_body" \
    | grep -oE '"machine_id"[[:space:]]*:[[:space:]]*"[^"]+"' \
    | sed -E 's/.*"machine_id"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/' \
    | head -n1 || true)"
fi

if [[ -z "$holder" ]]; then
  printf '%s: response had no machine_id field — fail-open. Body was: %s\n' \
    "qontinui-coord guard" "$response_body" >&2
  trap - EXIT
  exit 0
fi

if [[ "$holder" == "$machine_id" ]]; then
  # We hold the claim. All clear.
  trap - EXIT
  exit 0
fi

# Different holder — block.
cat >&2 <<EOF
qontinui-coord guard: Worktree at $canonical_path is currently held by
machine_id=$holder.
The git command would race that holder. Acquire the claim once they
release it, or coordinate via the human channel.

Local machine_id: $machine_id
Coord lookup:     $lookup_url

Override (e.g. for one-off interactive use):
    QONTINUI_COORD_GUARD=skip <git command>

EOF
trap - EXIT
exit 1
