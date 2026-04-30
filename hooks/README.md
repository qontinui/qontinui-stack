# qontinui-stack hooks

Local-machine hook scripts that integrate with `qontinui-coord`. These are
**advisory** by default — they fail open when coord is unreachable or
identity is unknown, on the principle that a coordination service outage
must not block real work.

## What lives here

| Script | Purpose | Phase 6 plan reference |
|---|---|---|
| `pre-checkout-coord-guard.sh` | Reject `git switch` / `git checkout <branch>` / `git reset --hard` when no live `Worktree` claim exists for the current working tree on this machine. | Item 1 — Worktree claims (Hard enforcement, 6.1) |

## Why these aren't real git hooks

`pre-checkout-coord-guard.sh` is named after a hook git does not actually
ship (`pre-checkout`). The closest native hooks are `post-checkout` (fires
**after** HEAD already moved — too late to block) and `pre-commit` (fires
on commit, not on checkout). The Phase 6 plan uses the name as a logical
label.

The script is therefore a **standalone wrapper** the caller invokes
**before** the underlying git command. It does not run git itself — that
keeps the gate semantically simple ("is the claim held?") and lets the
same script gate `switch`, `reset --hard`, `checkout -B`, etc., without
guessing intent.

## Installation

There is no system-wide install step. Pick one of:

### Inline gate (zero-config)

```bash
/d/qontinui-root/qontinui-stack/hooks/pre-checkout-coord-guard.sh \
  && git switch <branch>
```

### Shell function (recommended for humans)

Add to `~/.bashrc` / `~/.zshrc`:

```bash
git-switch-safe() {
  /d/qontinui-root/qontinui-stack/hooks/pre-checkout-coord-guard.sh \
    && git switch "$@"
}

git-reset-safe() {
  /d/qontinui-root/qontinui-stack/hooks/pre-checkout-coord-guard.sh \
    && git reset "$@"
}
```

### Alias

```bash
alias gswitch='/d/qontinui-root/qontinui-stack/hooks/pre-checkout-coord-guard.sh && git switch'
```

### Agent integration

Agents that perform branch-mutating git operations should invoke the
script directly **before** the underlying command. The script's exit code
is the contract:

- `0` — proceed (claim held, or fail-open path taken)
- `1` — block (no claim, or claim held by another machine)
- `2` — usage error (e.g., not in a git working tree)

## Bypassing the guard

Set `QONTINUI_COORD_GUARD=skip` in the environment of the git command:

```bash
QONTINUI_COORD_GUARD=skip git switch <branch>
```

The bypass is logged to stderr. Use sparingly — the whole point of the
guard is to make working-tree contamination visible.

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `COORD_URL` | `http://localhost:9870` | Coord service URL. Override for staging/CI. |
| `QONTINUI_AGENT_ID` | unset | Caller identity (UUID). Takes precedence over `~/.qontinui/machine.json`. Useful when the same machine runs multiple agents. |
| `QONTINUI_COORD_GUARD` | unset | Set to `skip` to bypass the check entirely. |

## Identity resolution

The script identifies the caller by `machine_id` (UUID), in this order:

1. `$QONTINUI_AGENT_ID`
2. `~/.qontinui/machine.json` field `machine_id`
3. `~/.qontinui/machine_id` (legacy bare-string form, if present)
4. None — fail open with a warning. The guard cannot decide ownership
   without an identity, so it lets the operation proceed and prints to
   stderr.

This matches the identity model documented in the Phase 6 plan
("Identity model" section) and the runner-side
`~/.qontinui/machine.json` convention from the topology plan §3.

## Endpoints used

The guard reads from:

```
GET <COORD_URL>/coord/claims/by-resource?kind=worktree&key=<urlencoded-abs-path>
```

The corresponding **acquire** endpoint is, at the time of writing,
unprefixed:

```
POST <COORD_URL>/claims/acquire
```

(The Phase 6 plan's "Endpoint prefix policy" anticipates a future cleanup
that will re-mount the legacy `/claims/*` routes under `/coord/claims/*`
with the bare paths kept as deprecated aliases. Until that lands, the
acquire path is bare; the by-resource lookup is `/coord`-prefixed.)

## References

- Phase 6 plan: `D:/qontinui-root/tmp_coord_phase6_agent_coordination_hardening.md`
  (Item 1 — Worktree claims, especially the "Naming note" and
  "Enforcement" subsections)
- Claim primitives: `D:/qontinui-root/qontinui-coord/src/claims.rs`
  (lines 32-60 for `ClaimKind`, lines ~219+ for the Redis key shape)
