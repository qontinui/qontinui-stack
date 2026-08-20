#!/usr/bin/env python3
"""Classify a full ``terraform plan`` and post the CLASSIFICATION to coord.

Phase 3b of ``2026-05-30-digital-twin-migrations-and-infra-layers``, authored by
``2026-08-15-terraform-plan-infra-observer-phase-3b``. This is the second
observer of coord's ``Ξ_Infra`` sub-space: coord's in-process
``infra_observer`` reads three narrow AWS-SDK dimensions, and this reads
everything terraform manages.

Where this runs, and why not in CI
----------------------------------
``qontinui-stack`` is a **public** repo and ``aws/`` declares 12
``aws_secretsmanager_secret_version`` resources, so a plan refreshes — and the
S3 state object already contains — the production database DSN, the RDS master
password, the coord admin secret, the web service secret, the Redis auth token
and the metrics token. ``-refresh=false`` does not avoid that.

So this script runs as a scheduled job **on a box that already holds those
credentials** (the operator box / runner fleet). It is never a GitHub Actions
job on this repo, and ``pull_request_target`` is forbidden outright. It
introduces no new IAM principal: the only new secret is ``COORD_INGEST_TOKEN``,
the shared bearer every other coord ingest already uses.

What crosses the wire
---------------------
The classification only. The posted payload carries structural fields —
resource address, resource type, attribute NAME, classification, ownership —
and the coord endpoint has no field to put a value in. ``changed_attributes``
reads names and never captures a value. The plan file is written to a temp
path and deleted in a ``finally``.

One honest exception, so nobody reads the paragraph above as absolute:
``terraform``'s own **stderr** is printed verbatim when it exits non-zero, and
terraform redacts only attributes it knows are ``sensitive`` — the same caveat
that makes an unmarked attribute a disclosure risk in the first place. That
stderr goes to the operator box's console, which is inside the trust boundary,
but a scheduled job that captures its output to a log file creates a durable
copy. Run it with stderr unredirected, or redirect it somewhere you would put
the state file itself.

Exit codes
----------
``0`` posted (or ``--dry-run`` printed) successfully; ``1`` terraform failed;
``2`` the post failed. Drift itself is **never** a non-zero exit: this is an
observation, not a gate, and a red-on-drift check would block every unrelated
stack PR on a condition its author cannot fix.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

OK = "ok"
BENIGN_ADD = "benign_add"
IN_PLACE_CHANGE = "in_place_change"
ACTIVE_NEGATION = "active_negation"
UNKNOWN = "unknown"

#: Worst-wins ordering. Mirrors coord's ``InfraDriftClass::severity`` exactly —
#: coord re-derives the fold server-side on the same order, so a divergence here
#: would only ever be caught as a mismatch between what this script reports and
#: what coord stores.
SEVERITY = {OK: 0, UNKNOWN: 1, BENIGN_ADD: 2, IN_PLACE_CHANGE: 3, ACTIVE_NEGATION: 4}

#: Resource types whose destruction removes something live and serving. A
#: ``delete`` here is an active negation regardless of what replaces it, because
#: the plan cannot promise the replacement lands.
#:
#: Derived from what ``aws/`` ACTUALLY declares, not from a generic AWS list —
#: several entries below exist because this stack uses an idiom a generic list
#: would miss. Re-derive it when the estate changes; a type absent from both this
#: set and ``NEGATING_ATTRIBUTES`` silently downgrades to ``in_place_change``.
SERVING_RESOURCE_TYPES = frozenset(
    {
        "aws_db_instance",
        "aws_elasticache_replication_group",
        "aws_ecs_service",
        "aws_lb",
        "aws_lb_target_group",
        "aws_lb_listener",
        "aws_secretsmanager_secret",
        "aws_iam_role",
        "aws_iam_role_policy",
        "aws_lambda_function",
        "aws_cloudwatch_log_group",
        # This stack gets ALL client/data-plane ingress from standalone rule
        # resources, not inline `ingress` blocks: 5 in modules/network (incl.
        # `data_pg_from_client`, the rule that lets the app tier reach Postgres)
        # and 1 in modules/web. Destroying one is a total outage, and it was
        # classified `in_place_change` until 2026-08-20.
        "aws_security_group_rule",
        # The metrics auth gate is a PAIR of listener rules in modules/tunnel
        # (`coord_metrics_authed` matching the X-Metrics-Token header, and
        # `coord_metrics_deny`). Deleting the deny rule opens /metrics — this
        # stack's real fail-open, distinct from the Lambda SIGNUP_ALLOWLIST one.
        "aws_lb_listener_rule",
        # 4 exist, all attaching AmazonECSTaskExecutionRolePolicy /
        # AWSLambdaBasicExecutionRole. Detaching one strips exactly the exec-role
        # grant the 2026-05-30 reconciliation exists because of — the incident
        # the aws_iam_role_policy entry below already cites.
        "aws_iam_role_policy_attachment",
        # Destroying either of the two public-access blocks (modules/blob,
        # modules/session-output-cold) exposes the bucket.
        "aws_s3_bucket_public_access_block",
        "aws_s3_bucket_server_side_encryption_configuration",
    }
)

#: Resource types where a bare ``create`` is NOT benign. ``SERVING_RESOURCE_TYPES``
#: gates destroys; without this, ``["create"]`` returned ``benign_add``
#: unconditionally — so a NEW security-group rule opening ``0.0.0.0/0``, a NEW
#: inline IAM policy granting ``*``, or a NEW listener rule inserted ahead of the
#: metrics deny rule all reported as benign additions.
#:
#: These classify ``in_place_change`` rather than ``active_negation``: a new grant
#: usually IS intentional, and firing the apply-block signal on every legitimate
#: addition would train the operator to ignore it. The point is that it stops
#: being invisible.
SECURITY_SENSITIVE_CREATE_TYPES = frozenset(
    {
        "aws_security_group_rule",
        "aws_security_group",
        "aws_iam_role_policy",
        "aws_iam_role_policy_attachment",
        "aws_iam_role",
        "aws_lb_listener_rule",
    }
)

#: Attributes whose *in-place* change is an active negation rather than an
#: ordinary update. Each entry records the incident that put it here — this
#: table is the whole "classify, do not just count" requirement, and an entry
#: without a reason is an entry nobody can audit.
#:
#: ``(resource_type, attribute) -> reason``
NEGATING_ATTRIBUTES: dict[tuple[str, str], str] = {
    ("aws_db_instance", "instance_class"): (
        "RDS instance class change — PR #66 found a 4x production database "
        "downsize hiding inside an ordinary 'update in place'"
    ),
    ("aws_db_instance", "allocated_storage"): (
        "RDS storage change — a shrink is destructive and AWS rejects it, so "
        "either direction warrants a human glance"
    ),
    ("aws_db_instance", "multi_az"): "RDS availability topology change",
    ("aws_db_instance", "deletion_protection"): "RDS deletion protection toggled",
    ("aws_db_instance", "backup_retention_period"): "RDS backup retention changed",
    ("aws_elasticache_replication_group", "node_type"): "Redis node class change",
    ("aws_elasticache_replication_group", "transit_encryption_enabled"): (
        "Redis in-transit encryption toggled"
    ),
    ("aws_ecs_service", "desired_count"): (
        "ECS desired count change — a drop to 0 is an outage expressed as an update"
    ),
    ("aws_lambda_function", "environment"): (
        "Lambda environment change — PR #66 found a SIGNUP_ALLOWLIST fail-open "
        "hiding inside an ordinary 'update in place'"
    ),
    ("aws_iam_role", "assume_role_policy"): "IAM trust relationship change",
    ("aws_iam_role_policy", "policy"): (
        "IAM inline policy change — the 2026-05-30 reconciliation exists because "
        "a plan silently stripped exec-role secret grants"
    ),
    ("aws_security_group", "ingress"): "Security group ingress change",
    ("aws_security_group", "egress"): "Security group egress change",
    # Standalone rule resources — this stack's actual ingress idiom (see
    # SERVING_RESOURCE_TYPES). Most attributes here are ForceNew, so a widened
    # CIDR usually arrives as create+delete and is caught by the delete arm; an
    # in-place `description`-only edit is not, hence the explicit entries.
    ("aws_security_group_rule", "cidr_blocks"): "Security group rule CIDR change",
    ("aws_security_group_rule", "source_security_group_id"): (
        "Security group rule source change"
    ),
    ("aws_security_group_rule", "from_port"): "Security group rule port range change",
    ("aws_security_group_rule", "to_port"): "Security group rule port range change",
    # The metrics auth gate: changing either rule's condition or action can open
    # /metrics without deleting anything.
    ("aws_lb_listener_rule", "condition"): (
        "ALB listener rule condition change - this stack gates coord /metrics on "
        "an X-Metrics-Token header condition plus a sibling deny rule"
    ),
    ("aws_lb_listener_rule", "action"): "ALB listener rule action change",
    ("aws_lb_listener_rule", "priority"): (
        "ALB listener rule priority change - ordering decides whether the deny "
        "rule is reached at all"
    ),
    # RDS, beyond the sizing attributes above.
    ("aws_db_instance", "engine_version"): (
        "RDS major-version upgrade is one-way and cannot be rolled back"
    ),
    ("aws_db_instance", "publicly_accessible"): "RDS public accessibility toggled",
    ("aws_db_instance", "skip_final_snapshot"): (
        "RDS final-snapshot skip toggled - turning it on makes a later destroy "
        "unrecoverable"
    ),
    ("aws_db_instance", "storage_encrypted"): "RDS storage encryption toggled",
    # Redis, beyond node_type / transit encryption above.
    ("aws_elasticache_replication_group", "at_rest_encryption_enabled"): (
        "Redis at-rest encryption toggled"
    ),
    ("aws_elasticache_replication_group", "automatic_failover_enabled"): (
        "Redis automatic failover toggled"
    ),
    ("aws_elasticache_replication_group", "num_cache_clusters"): (
        "Redis cluster count change - a drop removes a replica"
    ),
    ("aws_elasticache_replication_group", "snapshot_retention_limit"): (
        "Redis snapshot retention changed"
    ),
    # S3 exposure.
    ("aws_s3_bucket_public_access_block", "block_public_acls"): (
        "S3 public-ACL block toggled"
    ),
    ("aws_s3_bucket_public_access_block", "block_public_policy"): (
        "S3 public-policy block toggled"
    ),
    ("aws_s3_bucket_public_access_block", "ignore_public_acls"): (
        "S3 ignore-public-ACLs toggled"
    ),
    ("aws_s3_bucket_public_access_block", "restrict_public_buckets"): (
        "S3 restrict-public-buckets toggled"
    ),
}

#: Tie-break order when a single update touches several negating attributes.
#: Without it, ``classify_change`` returned on the FIRST match in sorted-name
#: order, so an update touching both ``allocated_storage`` and ``instance_class``
#: reported the storage change and lost the "4x downsize" reason string — the
#: exact signal the table exists to surface. Severity is unaffected either way;
#: what was lost was the explanation. Anything not listed sorts last.
NEGATING_ATTRIBUTE_PRIORITY: tuple[tuple[str, str], ...] = (
    ("aws_db_instance", "instance_class"),
    ("aws_db_instance", "engine_version"),
    ("aws_db_instance", "publicly_accessible"),
    ("aws_lambda_function", "environment"),
    ("aws_iam_role_policy", "policy"),
    ("aws_iam_role", "assume_role_policy"),
    ("aws_lb_listener_rule", "condition"),
    ("aws_ecs_service", "desired_count"),
)

#: Task-definition attributes the CI deploy rewrites on every push. Per the
#: reconciliation plan's ownership split these are **CI-owned**, not
#: terraform-owned, and terraform disagreeing with them is expected rather than
#: drift. Everything else on the resource is terraform-owned.
#:
#: ``container_definitions`` is the ONLY entry, and that is deliberate: ``image``,
#: ``environment`` and ``secrets`` are keys INSIDE that JSON string, not top-level
#: attributes of ``aws_ecs_task_definition``, so ``changed_attributes`` — which
#: reads top-level names — can never produce them. They were listed here until
#: 2026-08-20, where they made three tests pass over a path the code cannot reach.
CI_OWNED_TASK_DEF_ATTRIBUTES = frozenset({"container_definitions"})
CI_OWNED_RESOURCE_TYPES = frozenset({"aws_ecs_task_definition"})


def classify_change(resource_type: str, actions: list[str], changed_attrs: Iterable[str]) -> tuple[str, str | None, str]:
    """Classify one ``resource_changes[]`` entry.

    Returns ``(classification, attribute, reason)``. ``attribute`` is the single
    attribute that drove the verdict, or ``None`` for a whole-resource action.

    The verb alone is never the answer — that is exactly the blindness PR #66
    exposed, where ``2 to destroy`` was benign and an ``update in place`` was a
    production database downsize.
    """
    acts = [a for a in actions if a != "no-op"]

    if not acts or actions == ["no-op"]:
        return OK, None, "no change"

    if actions == ["read"]:
        return OK, None, "data source refresh"

    if "delete" in acts:
        if resource_type in SERVING_RESOURCE_TYPES:
            verb = "replace" if "create" in acts else "destroy"
            return (
                ACTIVE_NEGATION,
                None,
                f"{verb} of a live serving resource ({resource_type})",
            )
        # A destroy of something not in the serving set is still a removal, not
        # an addition — record it as a change rather than a benign add.
        return IN_PLACE_CHANGE, None, f"destroy of {resource_type}"

    if acts == ["create"]:
        if resource_type in SECURITY_SENSITIVE_CREATE_TYPES:
            return (
                IN_PLACE_CHANGE,
                None,
                f"create of a security-sensitive resource ({resource_type}) - a NEW "
                f"grant or rule is still a change to who can reach what",
            )
        return BENIGN_ADD, None, "create"

    if "update" in acts:
        hits = [
            (attr, NEGATING_ATTRIBUTES[(resource_type, attr)])
            for attr in changed_attrs
            if (resource_type, attr) in NEGATING_ATTRIBUTES
        ]
        if hits:
            # Report the HIGHEST-CONSEQUENCE hit, not the alphabetically first.
            # `changed_attributes` returns sorted names, so an update touching
            # both `allocated_storage` and `instance_class` used to report the
            # storage change and lose the "4x downsize" reason string.
            def rank(hit: tuple[str, str]) -> int:
                key = (resource_type, hit[0])
                if key in NEGATING_ATTRIBUTE_PRIORITY:
                    return NEGATING_ATTRIBUTE_PRIORITY.index(key)
                return len(NEGATING_ATTRIBUTE_PRIORITY)

            attr, reason = min(hits, key=rank)
            if len(hits) > 1:
                others = ", ".join(a for a, _ in hits if a != attr)
                reason = f"{reason} [also negating: {others}]"
            return ACTIVE_NEGATION, attr, reason
        first = next(iter(changed_attrs), None)
        return IN_PLACE_CHANGE, first, "update in place"

    # An action terraform grew after this table was written. Report it as
    # UNKNOWN rather than guessing: unknown lowers confidence, a wrong `ok`
    # clears a safety gate.
    return UNKNOWN, None, f"unrecognised actions {actions!r}"


def is_terraform_owned(resource_type: str, changed_attrs: Iterable[str]) -> bool:
    """Apply the reconciliation plan's ownership split.

    Keyed on the SET of changed attributes, not on the single attribute that
    drove the classification. Two reasons, both observed:

    * ``aws_ecs_task_definition`` is near-fully ForceNew, so a real change
      arrives as ``["create", "delete"]`` and yields no driving attribute at all.
      Keying on that attribute routed every genuine terraform-owned task-def
      change (``cpu``, ``memory``, ``execution_role_arn``, ``task_role_arn``)
      into the CI-owned bucket the reconciliation carve-out tells everyone to
      ignore.
    * On the update path a single unknown attribute sorting before
      ``container_definitions`` flipped the verdict, so ownership depended on
      which keys terraform happened to mark unknown.

    A task-def change is CI-owned only when ``container_definitions`` is the
    ONLY thing that changed — the carve-out is "CI rewrites the container spec",
    not "anything on a task definition is CI's".
    """
    attrs = set(changed_attrs)
    if resource_type in CI_OWNED_RESOURCE_TYPES and attrs:
        if attrs <= CI_OWNED_TASK_DEF_ATTRIBUTES:
            return False
    return True


def changed_attributes(change: dict[str, Any]) -> list[str]:
    """Top-level attribute names that differ between ``before`` and ``after``.

    Only NAMES are read — no value ever leaves this function, which is what
    makes it safe to run over a plan containing 12 plaintext secrets.
    ``after_unknown`` entries count as changed: terraform not knowing the value
    yet is not evidence the value is unchanged.
    """
    before = change.get("before") or {}
    after = change.get("after") or {}
    unknown = change.get("after_unknown") or {}
    names: set[str] = set()
    for key in set(before) | set(after):
        if before.get(key) != after.get(key):
            names.add(key)
    for key, val in unknown.items():
        if val:
            names.add(key)
    return sorted(names)


def classify_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Classify a ``terraform show -json`` document into ingest records."""
    records: list[dict[str, Any]] = []
    for rc in plan.get("resource_changes") or []:
        change = rc.get("change") or {}
        actions = change.get("actions") or []
        resource_type = rc.get("type") or "unknown"
        attrs = changed_attributes(change)
        classification, attribute, reason = classify_change(resource_type, actions, attrs)
        if classification == OK:
            continue  # a clean resource is the absence of a record, not a record
        records.append(
            {
                "resource": rc.get("address") or resource_type,
                "kind": resource_type,
                "attribute": attribute,
                "classification": classification,
                "terraform_owned": is_terraform_owned(resource_type, attrs),
                # Local-only: printed by --dry-run for a human, never posted.
                # coord's ingest schema has no field for it.
                "_reason": reason,
            }
        )
    return records


def worst(records: list[dict[str, Any]]) -> str:
    """Worst-wins fold, for the human summary. coord re-derives its own."""
    if not records:
        return OK
    return max((r["classification"] for r in records), key=lambda c: SEVERITY[c])


# ---------------------------------------------------------------------------
# terraform driver
# ---------------------------------------------------------------------------


class TerraformOutputUnparseable(RuntimeError):
    """``terraform show -json`` returned something that is not a JSON document."""


class PlanFileNotRemoved(RuntimeError):
    """The secret-bearing plan file could not be deleted.

    Raised rather than warned because the alternative is exiting 0 with
    production credentials left on disk.
    """


def run_plan(chdir: Path, timeout_seconds: int) -> tuple[dict[str, Any], float]:
    """Run ``terraform plan`` + ``terraform show -json``; return (doc, seconds).

    ``-lock=false`` is mandatory, not an optimisation: a read-only observation
    must never contend for the ``qontinui-tfstate-lock`` DynamoDB lock with an
    operator mid-apply.

    The plan file is written to a temp path and removed in ``finally`` — it
    contains the secret values, so it must not survive the run even on failure.
    """
    tmpdir = tempfile.mkdtemp(prefix="tfplan-drift-")
    planfile = Path(tmpdir) / "plan.bin"
    started = time.monotonic()
    try:
        subprocess.run(
            [
                "terraform",
                f"-chdir={chdir}",
                "plan",
                "-lock=false",
                "-input=false",
                "-no-color",
                f"-out={planfile}",
            ],
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        shown = subprocess.run(
            ["terraform", f"-chdir={chdir}", "show", "-json", str(planfile)],
            check=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        elapsed = time.monotonic() - started
        try:
            return json.loads(shown.stdout), elapsed
        except json.JSONDecodeError as e:
            # Not a leak — the message carries a position, not content — but an
            # uncaught traceback in a scheduled job is a worse signal than a
            # named failure.
            raise TerraformOutputUnparseable(
                f"terraform show -json produced output that is not JSON: {e}"
            ) from e
    finally:
        # These two failures are NOT the same severity, so they no longer share
        # an `except`. The plan file contains the secret values; the directory
        # does not. A PermissionError from a transient AV handle is realistic on
        # the Windows operator box, and the old code downgraded it to a warning
        # and then went on to post and exit 0 — leaving production secrets on
        # disk while reporting success.
        try:
            planfile.unlink(missing_ok=True)
        except OSError as e:
            raise PlanFileNotRemoved(
                f"could not remove the terraform plan file at {planfile} ({e}) - it "
                f"contains plaintext production secrets and must not survive this run"
            ) from e
        try:
            Path(tmpdir).rmdir()
        except OSError:
            print(f"WARNING: could not remove the empty temp dir {tmpdir}", file=sys.stderr)


def payload_for(records: list[dict[str, Any]], coverage: float) -> dict[str, Any]:
    """Build the wire payload. Underscore-prefixed keys are local-only and stripped.

    Factored out of :func:`post_to_coord` so the scrub that keeps ``_reason``
    (and anything else local) off the wire is the SHIPPED code path under test,
    rather than a comprehension a test re-implements inline and therefore can
    never catch a change to.
    """
    return {
        "resources": [{k: v for k, v in r.items() if not k.startswith("_")} for r in records],
        "coverage": coverage,
    }


def post_to_coord(base_url: str, token: str, records: list[dict[str, Any]], coverage: float) -> dict[str, Any]:
    """POST the classification. Local-only fields are stripped by :func:`payload_for`."""
    if not base_url.startswith("https://") and not base_url.startswith("http://127.0.0.1"):
        # The bearer is a shared production ingest token; over plain http it
        # crosses the wire in cleartext. 127.0.0.1 stays allowed for local
        # testing against a dev coord.
        raise ValueError(f"refusing to send COORD_INGEST_TOKEN over a non-https base: {base_url}")
    payload = payload_for(records, coverage)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/coord/infra/plan-drift",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--chdir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "aws" / "staging",
        help="terraform working directory (default: aws/staging)",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=900,
        help=(
            "seconds before terraform is killed, PER INVOCATION - `plan` and "
            "`show` are timed separately, so worst-case wall time is 2x this. "
            "Default 900. Sized against the TAIL, not the median: every untargeted "
            "plan during PR #66 finished inside 500s over ~159 resources, so this "
            "is that bound with headroom for a cold provider download. Re-measure "
            "before lowering it."
        ),
    )
    ap.add_argument("--coord-url", default=os.environ.get("COORD_HTTP_URL", "https://coord.qontinui.io"))
    ap.add_argument(
        "--coverage",
        type=float,
        default=1.0,
        help=(
            "D6 coverage in [0,1] — this observer's OWN completion fraction, not "
            "a fraction of the estate. 1.0 when the plan ran to completion over "
            "every workspace intended. The deliberately-unmanaged Cognito user "
            "pool is a SCOPE property and does not reduce this."
        ),
    )
    ap.add_argument("--dry-run", action="store_true", help="classify and print; post nothing")
    args = ap.parse_args(argv)

    try:
        plan, elapsed = run_plan(args.chdir, args.timeout)
    except subprocess.TimeoutExpired:
        print(f"terraform plan exceeded --timeout={args.timeout}s", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        # See the module docstring: terraform diagnostics CAN echo a value for an
        # attribute it does not know is sensitive. Kept on stderr, never in a
        # payload, and never in the --dry-run stdout the operator might paste.
        print(f"terraform failed (exit {e.returncode}):\n{e.stderr.decode(errors='replace')}", file=sys.stderr)
        return 1
    except TerraformOutputUnparseable as e:
        print(str(e), file=sys.stderr)
        return 1
    except PlanFileNotRemoved as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 1

    records = classify_plan(plan)
    summary = worst(records)
    # coord's ingest caps a single POST at 2000 records (MAX_RESOURCE_RECORDS in
    # crates/coord/src/infra_plan_ingest.rs) and answers 413 above it - never a
    # silent truncation. At ~159 resources there is an order of magnitude of
    # headroom, so this is a note rather than a check.
    print(f"plan completed in {elapsed:.1f}s; {len(records)} classified change(s); worst = {summary}")
    for r in records:
        print(f"  [{r['classification']}] {r['resource']}" + (f".{r['attribute']}" if r["attribute"] else "") + f" — {r['_reason']}")

    if args.dry_run:
        return 0

    token = os.environ.get("COORD_INGEST_TOKEN", "").strip()
    if not token:
        print("COORD_INGEST_TOKEN is unset — refusing to post unauthenticated", file=sys.stderr)
        return 2

    try:
        resp = post_to_coord(args.coord_url, token, records, args.coverage)
    except urllib.error.HTTPError as e:
        print(f"coord ingest rejected the post: {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"coord ingest unreachable: {e}", file=sys.stderr)
        return 2

    print(f"posted: {json.dumps(resp)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
