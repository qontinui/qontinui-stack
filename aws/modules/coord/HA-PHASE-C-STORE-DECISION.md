# HA Phase C — Coord Git Store: Fargate-Ephemeral vs EC2+EBS Decision Record

**Status: PENDING OPERATOR SIGN-OFF**
**Date opened: 2026-05-23**
**Gate: measure `coord_git_replica_bootstrap_seconds` on the largest repo**

---

## Context

Coord stores a git-backed canonical repository on each replica's local disk.
In the current Fargate deployment every task has ephemeral storage only —
local disk is wiped on each task replacement.  This means the Phase C.1
standby bootstrap (clone from leader via coord git-http) runs on **every**
task start, not just first boot.  Bootstrap time directly caps:

- How fast a replaced task rejoins the quorum and can accept leader election.
- Worst-case RPO during an AZ failure if the replacement task is slow.
- The effective lease TTL we can commit to (currently configured in
  `COORD_HA_LEASE_TTL_SECS`).

---

## The Two Options

### Option A — Stay on Fargate (ephemeral storage, fast bootstrap)

Keep `launch_type = "FARGATE"`.  No infra changes.

**Wins:**
- Zero ops overhead: no AMI management, no EBS volume lifecycle, no AZ
  re-attachment complexity.
- ECS handles placement, health replacement, and drain automatically.
- Cost is pay-per-task-second; no idle EBS charges.

**Constraint:**
- Bootstrap time must be acceptable.  If `coord_git_replica_bootstrap_seconds`
  p99 < lease TTL / 2 for the largest repo, this option is viable.
- Repos that are large (>1 GB objects) will have long bootstrap times on
  cold-start under Fargate network throughput limits (~1.25 Gbps burst).

### Option B — Switch to EC2 launch type + EBS-backed git store

Change `launch_type = "EC2"`, provision an ECS Capacity Provider backed by
an EC2 Auto Scaling Group, and mount an EBS volume per task (or use a shared
EFS volume — note: [[proj_coord_git_storage_decision]] spike ELIMINATED EFS
due to 12.8x metadata-churn latency wall; C2 = local disk on replicated
primary/standby is the vetted answer, which maps to EC2+EBS here).

**Wins:**
- Bootstrap is a no-op on normal restarts (git store persists across task
  stops; only volume detach/reattach on instance replacement).
- Allows the coord VCS-substrate engine to be un-flag-gated without bootstrap
  latency risk.

**Costs:**
- EC2 instance + EBS volume always-on charges even at zero traffic.
- AZ re-attachment: EBS is AZ-local; if the EC2 instance is replaced in a
  different AZ, volume must be detached and re-attached (or snapshotted).
- Additional operational surface: AMI pins, instance type selection, capacity
  provider tuning, drain lifecycle hooks.
- Terraform changes required: add `aws_autoscaling_group`,
  `aws_ecs_capacity_provider`, `aws_launch_template`, EBS volume resources,
  and update `aws_ecs_service.coord` to use the capacity provider instead of
  `launch_type = "FARGATE"`.

---

## The Gate Measurement

**Before deciding, run this measurement against the staging cluster:**

```bash
# On a freshly started coord standby task (task that was just placed and
# ran its C.1 bootstrap), query the coord metrics endpoint:
curl -s https://coord.staging.qontinui.io/metrics \
  | grep coord_git_replica_bootstrap_seconds
```

Record **p50, p95, p99** for the largest repo currently tracked by coord.
The decision threshold:

| Result | Decision |
|--------|----------|
| p99 bootstrap < `COORD_HA_LEASE_TTL_SECS / 2` | **Option A (Fargate)** — proceed, no infra change |
| p99 bootstrap >= `COORD_HA_LEASE_TTL_SECS / 2` | **Option B (EC2+EBS)** — operator must approve Terraform changes |

If the metric does not yet exist (bootstrap telemetry not yet instrumented),
the gate is blocked until it is added to the coord binary.  The chaos
validation script (`aws/scripts/coord-ha-chaos-validate.sh`) measures
effective promotion time end-to-end, which is a proxy but not a substitute
for the raw bootstrap metric.

---

## Terraform Changes Needed for Option B (reference, not pre-applied)

The following resources would need to be added/changed in
`aws/modules/coord/main.tf`:

1. `aws_launch_template.coord_ec2` — AL2023 AMI, instance type (e.g.
   `t3.medium`), IAM instance profile, ECS-optimized user data.
2. `aws_autoscaling_group.coord` — min=2, max=4, multi-AZ subnets.
3. `aws_ecs_capacity_provider.coord_ec2` — managed scaling enabled.
4. `aws_ecs_cluster_capacity_providers` — associate provider with cluster.
5. `aws_ecs_service.coord` — remove `launch_type = "FARGATE"`, add
   `capacity_provider_strategy` block.
6. EBS volume resources or instance-store layout for git data path.

**DO NOT apply these changes without operator sign-off.**

---

## Decision

- [ ] Operator has reviewed bootstrap metric measurement.
- [ ] Option A (Fargate) confirmed acceptable, OR
- [ ] Option B (EC2+EBS) approved; Terraform implementation authorized.

**Signed off by:** _pending_
**Date:** _pending_
