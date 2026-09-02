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
| `GIT_GUARD_COMMAND` | unset | The git command line being guarded (the `--command` argument is the same input). **Observation-only** — nothing about the claim verdict or the exit code reads it. It feeds the branch-provenance POST below, which is simply not made when it is absent. |

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

### Branch-provenance observation (write)

Plan `2026-08-28-shared-checkout-branch-provenance-and-reclaim-signal`, Phase 3.

```
POST <COORD_URL>/coord/trees/branch-events
{ "device_id": "<uuid>",
  "repo": "<basename of the target checkout>",
  "branch": "<the branch being created / switched to>",
  "agent_session_id": "<omitted when unknown>",
  "created_via": "checkout_guard_observed" }
```

This records the fact that a branch was created or switched to in a
primary/shared checkout, so coord can join `(repo, branch)` to
`repo_branches`/`pr_events` and answer *whose branch is this, and did its PR
ever conclude* — the `git reflog` + `gh pr view` archaeology that plan was
written after doing by hand.

Four things about it are load-bearing:

- **It is an observation, never a gate.** It adds no refuse path and no exit
  code. A 404, an unreachable coord, an unparseable command — all of them land
  in the same fail-open shape as the claim GET's arms: one breadcrumb, and
  nothing surfaced to the session.
- **It is bearer-less and device-scoped on purpose**, mirroring
  `POST /coord/trees/upsert` (the runner's tree publisher). This hook must work
  with no runner up, so there is no JWT to mint. Do not add an
  `Authorization` header — it would make the write fail closed on exactly the
  boxes the event exists to cover.
- **It skips coord-allocated worktrees** (any path under `agent-worktrees/`).
  Those already report their branch through `POST /agents/allocate` into
  `coord.agent_worktrees.branch`; a second event for the same checkout would
  double-count a row Phase 4 joins on.
- **It fires only for a branch create/switch.** `git checkout .`,
  `git checkout -- <path>`, `pull`, `rebase`, `stash pop|apply` and
  `reset --hard` produce nothing: none of them creates or switches a branch.

It runs in the background with both file descriptors detached, so it costs the
caller no wall-clock time and cannot hold a caller's stdout pipe open. Its
outcome is recorded in the breadcrumb log as
`EVENT reason=branch-event-posted … http=<code|unreachable>`.

**A missing event is UNKNOWN, never "nobody checked anything out."** A caller
that passes no command (the shell functions above), a harness with no hook
installed, or raw git outside any tool all produce no event — the same
absence-is-not-zero discipline the fleet's `silent-empty-is-unknown` policy
states elsewhere.

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
