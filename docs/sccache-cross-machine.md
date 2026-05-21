# Cross-machine sccache (shared S3)

Phase 2.1 of `plans/2026-05-21-coordination-improvements.md`. Migrates the
rustc compile-unit cache from the in-stack MinIO bucket (`qontinui-sccache`)
to a shared real-AWS S3 bucket (`qontinui-sccache-shared`) so multiple
developer machines (spaceship + MSI today; future cloud-burst builders later)
hit the same artifact store.

The in-stack MinIO sccache lane (`docker-compose.yml` `sccache` service +
the `qontinui-sccache` MinIO bucket) is **not removed** — it still serves
the docker-compose-only CI path that runs entirely inside the stack. The
shared S3 bucket is the lane developer machines use directly.

Phase 2.2 wires the runner / coord `.cargo/config.toml` defaults to the
new bucket. This doc is the operator-side procedure for the AWS resources
Phase 2.2 consumes, plus the per-machine env-var setup any future operator
will follow when onboarding a new build host.

## What's provisioned

| Resource | Value |
|---|---|
| S3 bucket | `qontinui-sccache-shared` |
| Bucket ARN | `arn:aws:s3:::qontinui-sccache-shared` |
| Region | `us-east-1` |
| Lifecycle | objects expire 30 days after creation; incomplete multipart uploads abort after 1 day |
| Versioning | disabled (sccache is content-addressable; versions add cost without benefit) |
| Public access | fully blocked (BlockPublicAcls + IgnorePublicAcls + BlockPublicPolicy + RestrictPublicBuckets all true) |
| IAM policy | `qontinui-sccache-shared-access` (ARN: `arn:aws:iam::047719635665:policy/qontinui-sccache-shared-access`) |
| IAM user | `qontinui-sccache-builder` (ARN: `arn:aws:iam::047719635665:user/qontinui-sccache-builder`) |

The IAM policy grants the minimum sccache needs:

- bucket-level: `s3:ListBucket`, `s3:HeadBucket` on `arn:aws:s3:::qontinui-sccache-shared`
- object-level: `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:HeadObject` on `arn:aws:s3:::qontinui-sccache-shared/*`

It is attached to the dedicated `qontinui-sccache-builder` user so the keys
are scoped to one capability and can be rotated without disturbing other
AWS access paths.

The smoke-test that verifies the policy at provisioning time:

```bash
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>
export AWS_DEFAULT_REGION=us-east-1
echo "hello-sccache-smoke" | aws s3 cp - s3://qontinui-sccache-shared/smoke/hello.txt
aws s3 cp s3://qontinui-sccache-shared/smoke/hello.txt -
aws s3 rm s3://qontinui-sccache-shared/smoke/hello.txt
aws s3api head-bucket --bucket qontinui-sccache-shared
```

All four commands must exit 0. The provisioning session for this doc
(Phase 2.1) saw PUT=0, GET=0 (round-tripped the literal payload), RM=0,
HEAD=0.

## Per-machine setup

Run this on each developer machine (spaceship, MSI, future hosts). Phase 2.2
will land matching `SCCACHE_BUCKET` / `SCCACHE_REGION` defaults in the
runner and coord `.cargo/config.toml` files so the per-machine env only
needs to supply credentials and unset the in-stack-specific vars.

### 1. Retrieve the IAM access key pair

The provisioning agent captures the create-access-key response to a
gitignored scratch file. After the access key has been distributed to
the operator, **retrieve it from the AWS console** (IAM → Users →
`qontinui-sccache-builder` → Security credentials) for any further
machine onboarding. AWS only shows the secret once at creation time; if
no operator captured it, rotate (see *Rotation* below).

### 2. Export env

```bash
# Credentials — from `aws iam create-access-key --user-name qontinui-sccache-builder`
# or the operator's password manager. AWS only shows the secret at creation time.
export AWS_ACCESS_KEY_ID=<key>
export AWS_SECRET_ACCESS_KEY=<secret>

# sccache S3 backend pointing at the shared bucket
export SCCACHE_BUCKET=qontinui-sccache-shared
export SCCACHE_REGION=us-east-1

# Unset any in-stack-MinIO env that a previous setup might have left in
# place. With SCCACHE_ENDPOINT unset, sccache talks to real AWS S3; with
# SCCACHE_S3_USE_SSL unset, sccache defaults to TLS (required for AWS).
unset SCCACHE_ENDPOINT
unset SCCACHE_S3_USE_SSL
unset SCCACHE_MINIO_HOST
unset SCCACHE_MINIO_USER
unset SCCACHE_MINIO_PASS

# Opt the toolchain in (keep this in your shell profile / dev-start
# script — pin it per-shell, not in .cargo/config.toml, so machines
# without sccache installed don't break).
export RUSTC_WRAPPER=sccache
```

PowerShell equivalents:

```powershell
$env:AWS_ACCESS_KEY_ID = '<key>'
$env:AWS_SECRET_ACCESS_KEY = '<secret>'
$env:SCCACHE_BUCKET = 'qontinui-sccache-shared'
$env:SCCACHE_REGION = 'us-east-1'
Remove-Item Env:SCCACHE_ENDPOINT     -ErrorAction SilentlyContinue
Remove-Item Env:SCCACHE_S3_USE_SSL   -ErrorAction SilentlyContinue
Remove-Item Env:SCCACHE_MINIO_HOST   -ErrorAction SilentlyContinue
Remove-Item Env:SCCACHE_MINIO_USER   -ErrorAction SilentlyContinue
Remove-Item Env:SCCACHE_MINIO_PASS   -ErrorAction SilentlyContinue
$env:RUSTC_WRAPPER = 'sccache'
```

### 3. Restart the daemon

The sccache daemon inherits its env at fork time; changing env vars in the
parent shell after the daemon is running has **no effect** until you bounce
the daemon (memory: `feedback_sccache_daemon_env_inheritance`):

```bash
sccache --stop-server
sccache --start-server
sccache --show-stats
```

Confirm `--show-stats` reports the new backend:

```
Cache location                  S3, bucket: Bucket(name=qontinui-sccache-shared), region: us-east-1
```

If you still see `Bucket(name=qontinui-sccache)` or a MinIO endpoint, the
daemon picked up stale env — re-export, stop-server, then start-server
again, in that order in the same shell.

### 4. Verify a write actually hits the bucket

A one-line round-trip from the build machine confirms creds + bucket name
+ network path are all live before you commit to a long cargo build:

```bash
echo "sccache-roundtrip-$(date -u +%s)" | aws s3 cp - s3://qontinui-sccache-shared/diag/roundtrip.txt
aws s3 ls s3://qontinui-sccache-shared/diag/
aws s3 rm s3://qontinui-sccache-shared/diag/roundtrip.txt
```

Then run a trivial cargo build (`cargo check` in any workspace member) and
inspect `sccache --show-stats`. The S3 `Requests sent` counter should
increment.

## Verifying the cross-machine win

Phase 2.3 ships a repeatable mechanism so the cycle-time payoff is *measured*,
not eyeballed once (memory: `feedback_build_verification_over_manual_observation`).
The script is `scripts/verify-sccache-cross-machine.sh`.

It has two roles run on two machines against the **same source state** with the
same `SCCACHE_*` / `AWS_*` env exported (see *Per-machine setup* above):

```bash
# Machine A (the one that already built — populates S3):
./scripts/verify-sccache-cross-machine.sh --role producer

# Machine B (a different machine — should pull A's artifacts from S3):
./scripts/verify-sccache-cross-machine.sh --role consumer
```

- **producer** builds cold and uploads compile units to S3, then prints the
  exact consumer command to run on the other machine.
- **consumer** builds into a fresh empty target dir (so every compile unit must
  be resolved from cache, not a stale local `target/`) and **asserts the
  cache-hit rate clears 80%** — the plan's Stream-2 termination predicate. Exit
  0 = pass, exit 1 = fail (with a diagnosis of the likely cause: differing
  source state, `--remap-path-prefix` mismatch, or the producer never ran).

By default the script generates a small deterministic synthetic probe crate
(serde + tokio + serde_json) under `$TMPDIR` so there are real rustc
invocations to cache without depending on a heavy workspace member. Pass
`--crate <path>` to build a real member (e.g. `--crate ../../qontinui-coord`)
for a heavier signal. `--threshold <pct>` overrides the 80% gate.

`--dry-run` validates env + tooling + args and prints what *would* run without
touching S3 — use it as a per-machine readiness check before committing to a
long build:

```bash
./scripts/verify-sccache-cross-machine.sh --dry-run   # exits 0 if configured, 1 if not
```

### Single-machine substrate proof (no second machine needed)

Running `--role producer` then `--role consumer` on the **same** machine still
proves the shared backend works end-to-end: the consumer builds into a fresh
empty target dir, so its hits can *only* come from S3 (the cold producer
uploaded them). Both upload and download paths are exercised. This is what
Phase 2.3 ran on spaceship while MSI was offline.

Measured on spaceship 2026-05-22 (sccache 0.15.0, synthetic probe):

| Build | Wall-clock | Cache hits | Hit rate |
|---|---|---|---|
| Cold (empty S3, populates bucket) | 15.28 s | 0 / 21 | 0 % |
| Warm (fresh local target, reads S3) | 9.01 s | 18 / 21 | **85.71 %** |

The 21 compile units uploaded on the cold build were confirmed present in
`s3://qontinui-sccache-shared/` (object count went 0 → 21 → 24). The 3 residual
misses on warm are the path-dependent units (the bin crate itself) — expected,
and the reason `--remap-path-prefix` matters for getting the cross-machine
number above 80% when worktree paths differ between machines.

**Cross-machine (spaceship → MSI) is the true number and is PENDING MSI being
online.** The substrate is verified single-machine; run the producer/consumer
pair above on both machines to capture the two-machine figure.

## Coexistence with the in-stack MinIO

| Lane | Backend | Bucket | Who uses it |
|---|---|---|---|
| In-stack | `qontinui-canonical-minio` container | `qontinui-sccache` | Docker-compose-only builders that live inside the stack network (Phase 6 cloud-burst CI containers, the `sccache` sidecar's own daemon). |
| Cross-machine | AWS S3 (us-east-1) | `qontinui-sccache-shared` | Developer machines (spaceship, MSI, future hosts) running cargo directly on the host. |

The in-stack `sccache` service in `docker-compose.yml` and its
`SCCACHE_BUCKET=qontinui-sccache` default stay in place. The cross-machine
lane is purely additive; nothing in this doc requires the in-stack lane
to change.

Two key reasons to keep both:

1. The docker-compose stack must remain runnable offline / without AWS
   credentials. Pointing the in-stack sccache at AWS would couple stack
   bring-up to having IAM keys present, which violates the "stack is
   self-contained" property.
2. The Phase 6 cloud-burst CI workers will run inside the stack and want
   the lowest-latency cache they can get — the MinIO container on
   loopback (or stack-internal LAN) beats a trans-region S3 call.

## Cost / observability notes

- **Storage**: lifecycle deletes after 30 days. Steady-state expectation is
  ~3 GB stored across the fleet (heuristic: ~100 MB ingest per dev per
  active build day, 30-day rolling window, 2 active devs). At
  `$0.023/GB/mo` (us-east-1 S3 Standard), that's well under $0.10/mo
  storage.
- **Requests**: each cargo build does on the order of thousands of GET +
  HEAD calls. Even at 10k req per dev per day across 30 days, request
  charges stay under a dollar a month (`$0.0004 per 1000 GET`,
  `$0.005 per 1000 PUT`).
- **Egress**: each developer machine pulls cache hits from S3 over the
  internet. Compressed compile-unit objects are small (mostly <1 MB) but
  cumulative bandwidth from large rebuilds can become the dominant cost.
  Watch for egress >5 GB/day per dev — that's a signal the local sccache
  disk cache should be made larger so warm-build hits don't re-fetch.
- **Stats**: there is no per-machine telemetry endpoint for the shared S3
  lane yet (the in-stack `stats_server.py` only knows about the MinIO
  bucket). Use AWS CloudWatch S3 metrics (`BucketSizeBytes`,
  `NumberOfObjects`, request counts) for fleet-level visibility. A
  follow-up could teach the coord scraper to also hit
  `aws s3api head-bucket` + `s3:ListBucket` for headline counts.

## Rotation

Quarterly cadence (or immediately on any suspected leak). The cycle:

```bash
# List current keys for the user
aws iam list-access-keys --user-name qontinui-sccache-builder

# Create the new key BEFORE deleting the old one so machines have a
# window to roll forward.
aws iam create-access-key --user-name qontinui-sccache-builder

# Distribute the new key to each developer machine. Re-export the env
# and bounce the sccache daemon (see "Per-machine setup" step 3) on
# each machine.

# Once every machine is confirmed on the new key (sccache --show-stats
# still passes; a fresh round-trip works), delete the old one:
aws iam delete-access-key \
  --user-name qontinui-sccache-builder \
  --access-key-id <old-AKID>
```

If a key is known to be compromised, skip the overlap window and
`delete-access-key` immediately; any in-flight builds that lose access
will simply stop hitting the cache (sccache falls back to local on
failed S3 calls and the build still completes).

## Re-provisioning from scratch

If the bucket or IAM resources need to be rebuilt from zero (e.g. test
account, disaster recovery), the procedure that produced this state was:

```bash
# 1. Bucket
aws s3api create-bucket --bucket qontinui-sccache-shared --region us-east-1
aws s3api put-public-access-block \
  --bucket qontinui-sccache-shared \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-lifecycle-configuration \
  --bucket qontinui-sccache-shared \
  --lifecycle-configuration file://lifecycle.json
# (lifecycle.json contents shown above — 30d expiration, 1d abort-multipart)

# 2. IAM policy
aws iam create-policy \
  --policy-name qontinui-sccache-shared-access \
  --policy-document file://policy.json
# (policy.json: Allow GetObject/PutObject/DeleteObject/HeadObject on /* and
#  ListBucket/HeadBucket on the bucket arn — see "What's provisioned")

# 3. IAM user + binding + keys
aws iam create-user --user-name qontinui-sccache-builder
aws iam attach-user-policy \
  --user-name qontinui-sccache-builder \
  --policy-arn arn:aws:iam::047719635665:policy/qontinui-sccache-shared-access
aws iam create-access-key --user-name qontinui-sccache-builder
# (capture the AccessKey.AccessKeyId + AccessKey.SecretAccessKey from the
#  response to a gitignored scratch file — AWS only shows the secret once)
```

Note `us-east-1` deliberately omits `--create-bucket-configuration` /
`LocationConstraint` (AWS rejects the constraint for the default region).
Any non-`us-east-1` re-provision must pass it.
