# HA Phase C — Coord Git Store: Fargate-Ephemeral vs EC2+EBS Decision Record

**Status: DECIDED — Option A (Fargate). Signed off 2026-05-24.**
**Date opened: 2026-05-23 · Decided: 2026-05-24**

---

## DECISION (2026-05-24): Fargate. The gate measurement is MOOT.

This record originally gated the choice on measuring `coord_git_replica_bootstrap_seconds`
on "the largest repo," premised on coord hosting large (>1 GB) canonical repos whose
clone-on-bootstrap could rival the lease TTL. **That premise dissolved under investigation
([[proj_coord_bootstrap_runner_35gib]], vet 2026-05-24):**

1. The 35 GiB figure that triggered this concern was **`qontinui-runner`'s LOCAL unreachable
   packed objects** (Rust build-artifact cruft never garbage-collected on one machine) — **not
   clone cost.** GitHub reports the repo at **42 MB**; a fresh clone is **23 MB**. `git gc
   --prune=now` fixes the local anomaly; nothing transfers 35 GiB.
2. **coord does not even host `qontinui-runner`** — `git_origin.rs` `ALLOWED_REPOS =
   ["qontinui-coord.git"]` (runner is explicitly rejected in tests at `git_origin.rs:554`).
   The canonical repos coord hosts are small (coord.git today; the agent working set at
   cutover, all in the tens-of-MB range after the runner correction).
3. Therefore standby bootstrap is a **seconds-scale** clone, far under any
   `COORD_HA_LEASE_TTL_SECS / 2` threshold — Option A's only constraint is satisfied with
   wide margin. No measurement is required to decide.

**⇒ Option A (Fargate). No EC2/EBS capacity-provider changes. No Terraform infra change beyond
the `desired_count` baseline.** This keeps zero-ops simplicity (ECS placement/health/drain) and
avoids the EBS AZ-reattachment + AMI/capacity-provider operational surface for no benefit.

**Re-open condition (the only thing that would flip this):** if a genuinely large canonical
repo (>~1 GB of *reachable* objects) is ever registered into `coord.canonical_repos`, re-measure
bootstrap and revisit Option B. (Note: per [[proj_coord_git_storage_decision]], the C2 vetted
answer is local-disk replicated primary/standby — which Fargate-ephemeral satisfies via the
Phase-C.1 bootstrap; EFS was eliminated on the 12.8x metadata-latency wall and is NOT an option.)

---

## Context (original, retained for reference)

Coord stores a git-backed canonical repository on each replica's local disk. On Fargate every
task has ephemeral storage, so the Phase C.1 standby bootstrap (clone from leader via coord
git-http) runs on every task start. Bootstrap time caps how fast a replaced task rejoins the
quorum, worst-case RPO during AZ failure, and the committable lease TTL. The decision below
turns on whether that bootstrap is fast — which, per the 2026-05-24 correction above, it is.

## Option A — Fargate (CHOSEN)

Keep `launch_type = "FARGATE"`. No infra changes. Zero ops overhead (no AMI/EBS/ASG/capacity-
provider lifecycle); ECS handles placement, health replacement, drain; pay-per-task-second.
Constraint = bootstrap fast enough; **satisfied** (seconds-scale, small canonical repos).

## Option B — EC2 + EBS-backed git store (NOT chosen; reference)

`launch_type = "EC2"` + ECS Capacity Provider (ASG) + per-task EBS. Bootstrap becomes a no-op on
normal restarts, but adds always-on EC2/EBS cost, EBS AZ-reattachment complexity, AMI/instance/
capacity-provider ops surface, and Terraform (`aws_launch_template`, `aws_autoscaling_group`,
`aws_ecs_capacity_provider`, EBS volumes). Justified ONLY by slow bootstrap of a large canonical
repo — which does not exist. (EFS is NOT an option — eliminated on the 12.8x metadata-latency wall.)

---

## Decision

- [x] Bootstrap-metric gate reviewed → **MOOT** (premise was a local-disk anomaly, not clone cost; coord hosts only small repos).
- [x] **Option A (Fargate) confirmed.** No EC2/EBS Terraform changes authorized.
- [ ] Re-open only if a >~1 GB reachable-object canonical repo is registered (then re-measure).

**Signed off by:** operator (via coord session, 2026-05-24)
**Date:** 2026-05-24
