#!/usr/bin/env bash
# ----------------------------------------------------------------------------
# pre-checkout-coord-guard.sh
#
# Purpose
#   Guard branch-mutating git operations (`git switch`, `git checkout <branch>`,
#   `git reset --hard`) against a working tree the caller does NOT hold a live
#   `Worktree` claim for in qontinui-coord. Originally Phase 6 Item 1
#   enforcement; hardened by plan
#   `2026-06-03-shared-checkout-coordination-gap-fix.md` (Layer 1) to:
#     - resolve the TARGET tree from an explicit cwd (issue A - the hook
#       process cwd is the non-git umbrella root, so the old
#       `git rev-parse --show-toplevel` against the process cwd could never
#       identify a sub-repo);
#     - treat "not inside a git tree" as ALLOW/exit 0 (issue B - it used to
#       exit 2, which the shim mistranslated into a block);
#     - default to WARN-ONLY (log + visible stderr) and only hard-block when
#       `QONTINUI_COORD_GUARD_ENFORCE` is set (operator opt-in once
#       worktree-mode/claims are live);
#     - distinguish a SAME-MACHINE peer session (different
#       `agent_session_id` under the same `machine_id`) from a foreign
#       machine (issue E);
#     - leave a breadcrumb on every fire (`~/.qontinui/coord-guard.log`) so
#       "did the guard run, and what did it decide?" is always answerable
#       instead of a silent no-op.
#
# Why this isn't a real git pre-checkout hook
#   Git ships no `pre-checkout` hook. This is a wrapper script the agent (or
#   the Claude Code PreToolUse shim) invokes BEFORE the underlying git
#   command. It does NOT run git itself - the caller runs the real command
#   after the gate passes. Keeps the gate semantically simple and reusable
#   before `reset --hard`, `checkout -B`, etc.
#
# Target working tree resolution (issue A fix)
#   The tree to guard is resolved in this order:
#     1. `--cwd <dir>` argument.
#     2. `$GIT_GUARD_CWD` env var (the shim sets this from the Bash tool's
#        envelope `cwd` plus any leading `cd <dir>` / `git -C <dir>`).
#     3. The guard's own process cwd (legacy behavior - correct only when
#        the guard is invoked from inside the target repo).
#   The resolved dir is fed to `git -C <dir> rev-parse --show-toplevel`.
#   If that dir is not inside a git tree, the guard ALLOWS (exit 0) - there
#   is nothing to guard (e.g. the umbrella root).
#
# Identity model (issue E fix)
#   Caller identity is a (machine_id, session_id) pair:
#     machine_id (the holder key coord stores):
#       1. $QONTINUI_AGENT_ID (env override)
#       2. ~/.qontinui/machine.json::.machine_id
#       3. ~/.qontinui/machine_id (legacy bare-string)
#     session_id (distinguishes same-machine peers; may be empty):
#       1. $QONTINUI_AGENT_SESSION_ID
#       2. ~/.qontinui/agent_session_id  (written by session-id-stamp.sh)
#   A claim is "ours" only when BOTH the holder's machine_id matches AND
#   either the holder reports no session_id (legacy) or it matches ours.
#   A holder with our machine_id but a DIFFERENT session_id is a
#   same-machine peer - the precise case the machine-only model was blind to.
#
# Enforcement mode
#   QONTINUI_COORD_GUARD_ENFORCE in {1,true,yes,block,enforce} → hard-block
#   on a conflict (exit 1). Anything else (incl. unset) → WARN-ONLY: log the
#   would-be block, print a visible stderr warning, and exit 0. Warn-only is
#   the default so this guard can ship before worktree-mode actually issues
#   claims, without locking out every branch switch.
#
# Override flag
#   QONTINUI_COORD_GUARD=skip bypasses the check entirely (recovery /
#   one-off interactive use). Logged so the bypass is auditable.
#
# Endpoint usage
#   GET <COORD_URL>/coord/claims/by-resource?kind=worktree&key=<urlenc>
#
# Robustness - every error path FAILS OPEN (warn + exit 0):
#   missing curl/git target, coord unreachable, HTTP 4xx/5xx, bad JSON,
#   unknown identity. The guard is advisory; it must never wedge git.
#
# Exit codes
#   0  - allow (held by us / fail-open / skip / not-a-git-tree / warn-only)
#   1  - block (enforce mode AND a real conflict: no claim, peer, or foreign)
#   2  - usage error (git not on PATH)
# ----------------------------------------------------------------------------

set -euo pipefail

# ---- trap unexpected exits (fail open) -------------------------------------
on_unexpected_exit() {
  local rc=$?
  if [[ $rc -ne 0 && $rc -ne 1 && $rc -ne 2 ]]; then
    printf '%s: unexpected exit (rc=%d) - failing open\n' \
      "qontinui-coord guard" "$rc" >&2
    exit 0
  fi
}
trap on_unexpected_exit EXIT

# ---- arg parse: --cwd <dir> ------------------------------------------------
arg_cwd=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cwd)
      arg_cwd="${2:-}"
      shift 2 || shift
      ;;
    --cwd=*)
      arg_cwd="${1#--cwd=}"
      shift
      ;;
    *)
      # Unknown args are ignored - the guard takes no positional args.
      shift
      ;;
  esac
done

# ---- breadcrumb log --------------------------------------------------------
# Append one line per fire so "did the guard run, and what did it decide?"
# is answerable after the fact (issue B / I1). Never fails the guard.
GUARD_LOG="${QONTINUI_COORD_GUARD_LOG:-$HOME/.qontinui/coord-guard.log}"
log_breadcrumb() {
  local msg="$1"
  local ts
  ts="$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo '?')"
  {
    mkdir -p "$(dirname "$GUARD_LOG")" 2>/dev/null || true
    # Cheap size-based rotation: if the log grew past ~1MB, keep the tail.
    if [[ -f "$GUARD_LOG" ]]; then
      local sz
      sz="$(wc -c < "$GUARD_LOG" 2>/dev/null || echo 0)"
      if [[ "$sz" =~ ^[0-9]+$ ]] && (( sz > 1048576 )); then
        tail -n 500 "$GUARD_LOG" > "$GUARD_LOG.tmp" 2>/dev/null \
          && mv -f "$GUARD_LOG.tmp" "$GUARD_LOG" 2>/dev/null || true
      fi
    fi
    printf '%s pid=%s %s\n' "$ts" "$$" "$msg" >> "$GUARD_LOG" 2>/dev/null || true
  } 2>/dev/null || true
}

# ---- enforcement mode ------------------------------------------------------
enforce=0
case "${QONTINUI_COORD_GUARD_ENFORCE:-}" in
  1|true|TRUE|yes|YES|block|BLOCK|enforce|ENFORCE) enforce=1 ;;
  *) enforce=0 ;;
esac

# ---- escape hatch ----------------------------------------------------------
if [[ "${QONTINUI_COORD_GUARD:-}" == "skip" ]]; then
  printf '%s: QONTINUI_COORD_GUARD=skip - bypassing claim check\n' \
    "qontinui-coord guard" >&2
  log_breadcrumb "ALLOW reason=skip (QONTINUI_COORD_GUARD=skip)"
  trap - EXIT
  exit 0
fi

# ---- pre-flight: tooling ---------------------------------------------------
if ! command -v curl >/dev/null 2>&1; then
  printf '%s: curl not on PATH - guard skipped (fail-open)\n' \
    "qontinui-coord guard" >&2
  log_breadcrumb "ALLOW reason=no-curl (fail-open)"
  trap - EXIT
  exit 0
fi

if ! command -v git >/dev/null 2>&1; then
  printf '%s: git not on PATH - cannot determine working tree\n' \
    "qontinui-coord guard" >&2
  log_breadcrumb "ALLOW reason=no-git (usage)"
  trap - EXIT
  exit 2
fi

# ---- resolve the TARGET working-tree root (issue A) ------------------------
# Precedence: --cwd arg > $GIT_GUARD_CWD > the guard's own process cwd.
target_cwd="$arg_cwd"
if [[ -z "$target_cwd" ]]; then
  target_cwd="${GIT_GUARD_CWD:-}"
fi
if [[ -z "$target_cwd" ]]; then
  target_cwd="$(pwd)"
fi

wt_root=""
if ! wt_root="$(git -C "$target_cwd" rev-parse --show-toplevel 2>/dev/null)"; then
  # Not inside a git tree (e.g. the non-git umbrella root). Nothing to
  # guard -ALLOW. This is the issue-B fix: this case used to exit 2,
  # which the shim turned into a spurious block.
  printf '%s: %s is not inside a git working tree - nothing to guard (allow)\n' \
    "qontinui-coord guard" "$target_cwd" >&2
  log_breadcrumb "ALLOW reason=not-a-git-tree target=$target_cwd"
  trap - EXIT
  exit 0
fi

# Canonicalize: prefer realpath; fall back to `cd && pwd -P`.
canonical_path=""
if command -v realpath >/dev/null 2>&1; then
  canonical_path="$(realpath "$wt_root" 2>/dev/null || true)"
fi
if [[ -z "$canonical_path" ]]; then
  canonical_path="$(cd "$wt_root" && pwd -P)"
fi

# ---- resolve caller identity (machine_id + session_id) --------------------
machine_id=""
if [[ -n "${QONTINUI_AGENT_ID:-}" ]]; then
  machine_id="$QONTINUI_AGENT_ID"
fi
if [[ -z "$machine_id" ]] && [[ -f "$HOME/.qontinui/machine.json" ]]; then
  if command -v jq >/dev/null 2>&1; then
    machine_id="$(jq -r '.machine_id // empty' "$HOME/.qontinui/machine.json" 2>/dev/null || true)"
  else
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
  printf '%s: identity unknown (no $QONTINUI_AGENT_ID, machine.json, or machine_id) - guard skipped\n' \
    "qontinui-coord guard" >&2
  log_breadcrumb "ALLOW reason=no-identity key=$canonical_path"
  trap - EXIT
  exit 0
fi

# session_id distinguishes same-machine peer sessions (issue E). May be
# empty on older sessions / non-Claude callers - then we fall back to the
# machine-only comparison.
session_id="${QONTINUI_AGENT_SESSION_ID:-}"
if [[ -z "$session_id" ]] && [[ -f "$HOME/.qontinui/agent_session_id" ]]; then
  session_id="$(tr -d '[:space:]' < "$HOME/.qontinui/agent_session_id" 2>/dev/null || true)"
fi

# ---- coord URL -------------------------------------------------------------
coord_url="${COORD_URL:-http://localhost:9870}"
coord_url="${coord_url%/}"

# ---- urlencode the path ----------------------------------------------------
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

# ---- shared decision helper ------------------------------------------------
# Emits the breadcrumb + stderr message and exits with the mode-appropriate
# code. In warn-only mode every "conflict" still ALLOWS (exit 0).
decide_conflict() {
  local reason="$1" detail="$2" log_holder="${3:-none}"
  local logline="reason=\"$reason\" key=$canonical_path holder=$log_holder"
  if (( enforce )); then
    log_breadcrumb "BLOCK $logline enforce=on"
    cat >&2 <<EOF
qontinui-coord guard: BLOCK - $reason for $canonical_path
$detail
Local identity: machine_id=$machine_id session_id=${session_id:-<none>}
Coord lookup:   $lookup_url

Override (one-off interactive use / recovery):
    QONTINUI_COORD_GUARD=skip <git command>
EOF
    trap - EXIT
    exit 1
  else
    log_breadcrumb "WARN $logline enforce=off"
    cat >&2 <<EOF
qontinui-coord guard: WARNING (warn-only) - $reason for $canonical_path
$detail
This would be blocked once QONTINUI_COORD_GUARD_ENFORCE is set. Proceeding.
EOF
    trap - EXIT
    exit 0
  fi
}

# ---- query coord -----------------------------------------------------------
http_code=""
response_body=""
tmp_body="$(mktemp 2>/dev/null || mktemp -t coordguard)"
# shellcheck disable=SC2064
trap "rm -f '$tmp_body'; on_unexpected_exit" EXIT

if ! http_code="$(curl -sS -m 10 -o "$tmp_body" -w '%{http_code}' \
    "$lookup_url" 2>/dev/null)"; then
  printf '%s: coord unreachable at %s - guard skipped (fail-open)\n' \
    "qontinui-coord guard" "$coord_url" >&2
  rm -f "$tmp_body"
  log_breadcrumb "ALLOW reason=coord-unreachable key=$canonical_path"
  trap - EXIT
  exit 0
fi

response_body="$(cat "$tmp_body" 2>/dev/null || true)"
rm -f "$tmp_body"
trap on_unexpected_exit EXIT

if [[ "$http_code" =~ ^[45] ]]; then
  printf '%s: coord returned HTTP %s for %s - guard skipped (fail-open).\n' \
    "qontinui-coord guard" "$http_code" "$lookup_url" >&2
  log_breadcrumb "ALLOW reason=coord-http-$http_code key=$canonical_path"
  trap - EXIT
  exit 0
fi

# ---- interpret response ----------------------------------------------------
# Shapes:
#   null                       - no live claim
#   { "machine_id": "...", "session_id": "...", ... } - live claim
trimmed="$(printf '%s' "$response_body" | tr -d '[:space:]')"

if [[ "$trimmed" == "null" || -z "$trimmed" ]]; then
  decide_conflict "no Worktree claim held" \
"Acquire one before changing branches in this tree:
    curl -X POST $coord_url/claims/acquire \\
         -H 'Content-Type: application/json' \\
         -d '{\"kind\":\"worktree\",\"resource_key\":\"$canonical_path\",\"machine_id\":\"$machine_id\"}'" \
    "none"
fi

# Extract holder machine_id + session_id.
extract_field() {
  local field="$1"
  local val=""
  if command -v jq >/dev/null 2>&1; then
    val="$(printf '%s' "$response_body" | jq -r ".$field // empty" 2>/dev/null || true)"
  fi
  if [[ -z "$val" ]]; then
    val="$(printf '%s' "$response_body" \
      | grep -oE "\"$field\"[[:space:]]*:[[:space:]]*\"[^\"]+\"" \
      | sed -E "s/.*\"$field\"[[:space:]]*:[[:space:]]*\"([^\"]+)\".*/\1/" \
      | head -n1 || true)"
  fi
  printf '%s' "$val"
}
holder="$(extract_field machine_id)"
holder_session="$(extract_field session_id)"

if [[ -z "$holder" ]]; then
  printf '%s: response had no machine_id field - fail-open. Body: %s\n' \
    "qontinui-coord guard" "$response_body" >&2
  log_breadcrumb "ALLOW reason=no-holder-field key=$canonical_path"
  trap - EXIT
  exit 0
fi

# Decision matrix:
#   different machine                                  → foreign conflict
#   our machine, holder has no session OR session==ours → we hold it (allow)
#   our machine, holder session differs               → same-machine peer
if [[ "$holder" != "$machine_id" ]]; then
  decide_conflict "Worktree claim held by another machine" \
"Held by machine_id=$holder${holder_session:+ session_id=$holder_session}. The git command would race that holder." \
    "machine:$holder"
fi

if [[ -z "$holder_session" || "$holder_session" == "$session_id" ]]; then
  # We hold the claim (or a legacy machine-only claim by us).
  log_breadcrumb "ALLOW reason=held-by-us key=$canonical_path session=${session_id:-<none>}"
  trap - EXIT
  exit 0
fi

# Same machine, different session - the same-machine peer case.
decide_conflict "Worktree claim held by another session on THIS machine" \
"Held by session_id=$holder_session (machine_id=$holder, same box). A concurrent session is editing this tree." \
    "session:$holder_session"
