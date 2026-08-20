"""Tests for ``terraform-plan-drift.py``.

Runnable via either::

    python -m pytest qontinui-stack/scripts/tests/test_terraform_plan_drift.py
    python -m unittest qontinui-stack.scripts.tests.test_terraform_plan_drift

Written against ``unittest.TestCase`` so both runners work, matching the
sibling ``test_resolve_plan_deps.py`` convention.

Every case here is a **regression** against something that actually happened in
qontinui-stack PR #66: the whole point of Phase 3b is that a three-number
``N to add, N to change, N to destroy`` summary concealed a 4x production
database downsize and a fail-open auth gate while ``2 to destroy`` was benign.
A classifier that keys on the terraform verb reproduces exactly that blindness.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "terraform-plan-drift.py"
_spec = importlib.util.spec_from_file_location("terraform_plan_drift", _MODULE_PATH)
assert _spec and _spec.loader
tpd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tpd)


def _rc(address: str, rtype: str, actions: list[str], before=None, after=None, after_unknown=None):
    return {
        "address": address,
        "type": rtype,
        "change": {
            "actions": actions,
            "before": before,
            "after": after,
            "after_unknown": after_unknown or {},
        },
    }


class ClassifyChangeTests(unittest.TestCase):
    def test_no_op_is_ok(self):
        cls, attr, _ = tpd.classify_change("aws_db_instance", ["no-op"], [])
        self.assertEqual(cls, tpd.OK)
        self.assertIsNone(attr)

    def test_plain_create_is_benign(self):
        cls, _, _ = tpd.classify_change("aws_cloudwatch_log_group", ["create"], [])
        self.assertEqual(cls, tpd.BENIGN_ADD)

    def test_destroy_of_a_serving_resource_is_an_active_negation(self):
        cls, _, reason = tpd.classify_change("aws_ecs_service", ["delete"], [])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertIn("serving", reason)

    def test_replace_of_a_serving_resource_is_an_active_negation(self):
        # create+delete is a REPLACE. The plan cannot promise the replacement
        # lands, so it is not a benign add.
        cls, _, reason = tpd.classify_change("aws_db_instance", ["delete", "create"], [])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertIn("replace", reason)

    def test_destroy_of_a_non_serving_resource_is_not_benign(self):
        # PR #66's "2 to destroy was benign" is true of the SUMMARY, not of the
        # verb: a removal is still a change, never an addition.
        cls, _, _ = tpd.classify_change("aws_route53_record", ["delete"], [])
        self.assertEqual(cls, tpd.IN_PLACE_CHANGE)

    def test_rds_instance_class_update_is_an_active_negation(self):
        # THE PR #66 regression: a 4x production database downsize expressed as
        # an ordinary "update in place".
        cls, attr, reason = tpd.classify_change(
            "aws_db_instance", ["update"], ["instance_class", "tags"]
        )
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "instance_class")
        self.assertIn("PR #66", reason)

    def test_lambda_environment_update_is_an_active_negation(self):
        # THE OTHER PR #66 regression: a SIGNUP_ALLOWLIST fail-open.
        cls, attr, reason = tpd.classify_change("aws_lambda_function", ["update"], ["environment"])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "environment")
        self.assertIn("fail-open", reason)

    def test_ordinary_update_is_an_in_place_change(self):
        cls, _, _ = tpd.classify_change("aws_ecs_service", ["update"], ["tags"])
        self.assertEqual(cls, tpd.IN_PLACE_CHANGE)

    def test_ecs_desired_count_update_is_an_active_negation(self):
        cls, attr, _ = tpd.classify_change("aws_ecs_service", ["update"], ["desired_count"])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "desired_count")

    def test_iam_inline_policy_change_is_an_active_negation(self):
        # The 2026-05-30 reconciliation exists because a plan silently stripped
        # exec-role secret grants.
        cls, _, _ = tpd.classify_change("aws_iam_role_policy", ["update"], ["policy"])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)

    def test_an_unrecognised_action_is_unknown_never_ok(self):
        # A wrong `ok` clears a safety gate; `unknown` only lowers confidence.
        cls, _, _ = tpd.classify_change("aws_db_instance", ["frobnicate"], [])
        self.assertEqual(cls, tpd.UNKNOWN)


class ChangedAttributesTests(unittest.TestCase):
    def test_reports_names_only_and_finds_the_difference(self):
        attrs = tpd.changed_attributes(
            {"before": {"instance_class": "a", "id": "x"}, "after": {"instance_class": "b", "id": "x"}}
        )
        self.assertEqual(attrs, ["instance_class"])

    def test_after_unknown_counts_as_changed(self):
        # Terraform not knowing a value yet is not evidence it is unchanged.
        attrs = tpd.changed_attributes(
            {"before": {"arn": "x"}, "after": {"arn": "x"}, "after_unknown": {"policy": True}}
        )
        self.assertEqual(attrs, ["policy"])

    def test_null_before_and_after_do_not_raise(self):
        self.assertEqual(tpd.changed_attributes({"before": None, "after": None}), [])


class OwnershipTests(unittest.TestCase):
    """``is_terraform_owned`` is keyed on the SET of changed attributes.

    Keying it on the single attribute that drove the classification routed every
    genuine terraform-owned task-def change into the CI-owned bucket, because
    ``aws_ecs_task_definition`` is near-fully ForceNew and a real change arrives
    as create+delete with no driving attribute at all.
    """

    def test_a_container_definitions_only_change_is_ci_owned(self):
        self.assertFalse(tpd.is_terraform_owned("aws_ecs_task_definition", ["container_definitions"]))

    def test_task_def_cpu_or_memory_stays_terraform_owned(self):
        self.assertTrue(tpd.is_terraform_owned("aws_ecs_task_definition", ["cpu"]))
        self.assertTrue(tpd.is_terraform_owned("aws_ecs_task_definition", ["memory"]))
        self.assertTrue(tpd.is_terraform_owned("aws_ecs_task_definition", ["execution_role_arn"]))

    def test_a_mixed_task_def_change_stays_terraform_owned(self):
        # The carve-out is "CI rewrites the container spec", not "anything on a
        # task definition is CI's". A change touching cpu AND the container spec
        # is not expected churn.
        self.assertTrue(
            tpd.is_terraform_owned("aws_ecs_task_definition", ["container_definitions", "cpu"])
        )

    def test_a_forcenew_task_def_replace_stays_terraform_owned(self):
        # create+delete yields NO changed attributes. This used to hit the
        # `attribute is None` arm and report CI-owned.
        self.assertTrue(tpd.is_terraform_owned("aws_ecs_task_definition", []))

    def test_an_unknown_attribute_does_not_flip_ownership(self):
        # `after_unknown` on a ForceNew resource commonly adds `arn`/`revision`.
        self.assertTrue(
            tpd.is_terraform_owned("aws_ecs_task_definition", ["arn", "container_definitions"])
        )

    def test_service_shell_and_iam_are_terraform_owned(self):
        self.assertTrue(tpd.is_terraform_owned("aws_ecs_service", ["desired_count"]))
        self.assertTrue(tpd.is_terraform_owned("aws_iam_role_policy", ["policy"]))

    def test_nested_container_keys_are_not_top_level_attributes(self):
        # `image` / `environment` / `secrets` live INSIDE the
        # container_definitions JSON string, so `changed_attributes` can never
        # emit them. They were listed in the carve-out set until 2026-08-20,
        # where they made three tests pass over an unreachable path.
        self.assertNotIn("image", tpd.CI_OWNED_TASK_DEF_ATTRIBUTES)
        self.assertNotIn("environment", tpd.CI_OWNED_TASK_DEF_ATTRIBUTES)
        self.assertNotIn("secrets", tpd.CI_OWNED_TASK_DEF_ATTRIBUTES)


class ThisStacksIdiomsTests(unittest.TestCase):
    """Coverage of the resource types ``aws/`` actually declares.

    A generic AWS severity list misses these, and each one was classified
    ``in_place_change`` or ``benign_add`` until 2026-08-20.
    """

    def test_destroying_a_standalone_security_group_rule_is_an_active_negation(self):
        # This stack gets ALL client/data-plane ingress from standalone rule
        # resources. `data_pg_from_client` is what lets the app tier reach
        # Postgres; destroying it is a total outage.
        cls, _, _ = tpd.classify_change("aws_security_group_rule", ["delete"], [])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)

    def test_destroying_the_metrics_deny_listener_rule_is_an_active_negation(self):
        # The real fail-open in this stack: coord /metrics is gated by an
        # X-Metrics-Token match rule plus a sibling deny rule.
        cls, _, _ = tpd.classify_change("aws_lb_listener_rule", ["delete"], [])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)

    def test_changing_a_listener_rule_condition_is_an_active_negation(self):
        cls, attr, _ = tpd.classify_change("aws_lb_listener_rule", ["update"], ["condition"])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "condition")

    def test_detaching_a_managed_iam_policy_is_an_active_negation(self):
        # Detaching AmazonECSTaskExecutionRolePolicy strips exactly the exec-role
        # grant the 2026-05-30 reconciliation exists because of.
        cls, _, _ = tpd.classify_change("aws_iam_role_policy_attachment", ["delete"], [])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)

    def test_removing_an_s3_public_access_block_is_an_active_negation(self):
        cls, _, _ = tpd.classify_change("aws_s3_bucket_public_access_block", ["delete"], [])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)

    def test_unblocking_public_buckets_in_place_is_an_active_negation(self):
        cls, attr, _ = tpd.classify_change(
            "aws_s3_bucket_public_access_block", ["update"], ["restrict_public_buckets"]
        )
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "restrict_public_buckets")

    def test_an_rds_major_version_upgrade_is_an_active_negation(self):
        cls, attr, reason = tpd.classify_change("aws_db_instance", ["update"], ["engine_version"])
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "engine_version")
        self.assertIn("one-way", reason)


class SecuritySensitiveCreateTests(unittest.TestCase):
    """A bare ``create`` is not benign for everything.

    ``SERVING_RESOURCE_TYPES`` gates destroys; nothing gated creates, so a NEW
    rule opening 0.0.0.0/0 or a NEW inline policy granting ``*`` reported as
    ``benign_add``.
    """

    def test_a_new_security_group_rule_is_not_benign(self):
        cls, _, reason = tpd.classify_change("aws_security_group_rule", ["create"], [])
        self.assertEqual(cls, tpd.IN_PLACE_CHANGE)
        self.assertIn("security-sensitive", reason)

    def test_a_new_inline_iam_policy_is_not_benign(self):
        cls, _, _ = tpd.classify_change("aws_iam_role_policy", ["create"], [])
        self.assertEqual(cls, tpd.IN_PLACE_CHANGE)

    def test_a_new_listener_rule_is_not_benign(self):
        cls, _, _ = tpd.classify_change("aws_lb_listener_rule", ["create"], [])
        self.assertEqual(cls, tpd.IN_PLACE_CHANGE)

    def test_an_ordinary_new_resource_is_still_benign(self):
        # The gate must not fire on every legitimate addition, or the operator
        # learns to ignore it.
        cls, _, _ = tpd.classify_change("aws_cloudwatch_log_group", ["create"], [])
        self.assertEqual(cls, tpd.BENIGN_ADD)


class NegatingAttributePriorityTests(unittest.TestCase):
    def test_the_highest_consequence_attribute_is_reported_not_the_first(self):
        # `changed_attributes` returns SORTED names, so `allocated_storage`
        # precedes `instance_class`. Reporting the first match lost the
        # "4x downsize" reason string - the exact signal the table exists for.
        cls, attr, reason = tpd.classify_change(
            "aws_db_instance", ["update"], ["allocated_storage", "instance_class"]
        )
        self.assertEqual(cls, tpd.ACTIVE_NEGATION)
        self.assertEqual(attr, "instance_class")
        self.assertIn("PR #66", reason)

    def test_the_other_negating_attributes_are_still_named(self):
        _, _, reason = tpd.classify_change(
            "aws_db_instance", ["update"], ["allocated_storage", "instance_class"]
        )
        self.assertIn("also negating: allocated_storage", reason)

    def test_a_single_hit_carries_no_also_negating_suffix(self):
        _, _, reason = tpd.classify_change("aws_db_instance", ["update"], ["instance_class"])
        self.assertNotIn("also negating", reason)


class ClassifyPlanTests(unittest.TestCase):
    def test_a_clean_plan_yields_no_records(self):
        plan = {"resource_changes": [_rc("a", "aws_db_instance", ["no-op"])]}
        self.assertEqual(tpd.classify_plan(plan), [])
        self.assertEqual(tpd.worst([]), tpd.OK)

    def test_a_missing_resource_changes_key_is_a_clean_plan(self):
        self.assertEqual(tpd.classify_plan({}), [])

    def test_the_pr66_shape_is_not_summarised_away(self):
        # 1 add, 2 change, 1 destroy — a three-number summary reads "mostly
        # benign". The classification must surface the downsize instead.
        plan = {
            "resource_changes": [
                _rc("aws_cloudwatch_log_group.new", "aws_cloudwatch_log_group", ["create"]),
                _rc(
                    "module.postgres.aws_db_instance.main",
                    "aws_db_instance",
                    ["update"],
                    before={"instance_class": "db.t4g.large"},
                    after={"instance_class": "db.t4g.small"},
                ),
                _rc(
                    "aws_ecs_service.coord",
                    "aws_ecs_service",
                    ["update"],
                    before={"tags": {"a": "1"}},
                    after={"tags": {"a": "2"}},
                ),
                _rc("aws_route53_record.old", "aws_route53_record", ["delete"]),
            ]
        }
        records = tpd.classify_plan(plan)
        self.assertEqual(len(records), 4)
        self.assertEqual(tpd.worst(records), tpd.ACTIVE_NEGATION)
        negations = [r for r in records if r["classification"] == tpd.ACTIVE_NEGATION]
        self.assertEqual(len(negations), 1)
        self.assertEqual(negations[0]["resource"], "module.postgres.aws_db_instance.main")
        self.assertEqual(negations[0]["attribute"], "instance_class")
        self.assertTrue(negations[0]["terraform_owned"])

    def test_records_carry_no_before_or_after_value(self):
        # The scrubbing invariant: this script runs over a plan containing 12
        # plaintext secrets, so a value must have nowhere to go. Asserted on
        # the record SHAPE, not on a filter that could be bypassed.
        plan = {
            "resource_changes": [
                _rc(
                    "module.postgres.aws_secretsmanager_secret_version.master",
                    "aws_secretsmanager_secret_version",
                    ["update"],
                    before={"secret_string": "super-secret-password"},
                    after={"secret_string": "another-secret-password"},
                )
            ]
        }
        records = tpd.classify_plan(plan)
        self.assertEqual(len(records), 1)
        serialised = repr(records)
        self.assertNotIn("super-secret-password", serialised)
        self.assertNotIn("another-secret-password", serialised)
        self.assertEqual(
            set(records[0]) - {"_reason"},
            {"resource", "kind", "attribute", "classification", "terraform_owned"},
        )

    def test_reason_is_local_only_and_stripped_from_the_payload(self):
        plan = {"resource_changes": [_rc("x", "aws_ecs_service", ["delete"])]}
        records = tpd.classify_plan(plan)
        payload = [{k: v for k, v in r.items() if not k.startswith("_")} for r in records]
        self.assertIn("_reason", records[0])
        self.assertNotIn("_reason", payload[0])


class PayloadTests(unittest.TestCase):
    """The scrub that keeps local-only fields off the wire, tested on the
    SHIPPED code path rather than a comprehension re-implemented in the test."""

    def test_payload_for_strips_underscore_prefixed_fields(self):
        records = tpd.classify_plan({"resource_changes": [_rc("x", "aws_ecs_service", ["delete"])]})
        self.assertIn("_reason", records[0])
        payload = tpd.payload_for(records, 1.0)
        self.assertNotIn("_reason", payload["resources"][0])
        self.assertEqual(payload["coverage"], 1.0)

    def test_payload_carries_exactly_the_fields_coord_accepts(self):
        # Peer contract with crates/coord/src/infra_plan_ingest.rs
        # `PlanResourceRecord`. `terraform_owned` is REQUIRED there (no serde
        # default), so it must always be present here.
        records = tpd.classify_plan(
            {"resource_changes": [_rc("x", "aws_db_instance", ["update"], {"instance_class": "a"}, {"instance_class": "b"})]}
        )
        payload = tpd.payload_for(records, 1.0)
        self.assertEqual(
            set(payload["resources"][0]),
            {"resource", "kind", "attribute", "classification", "terraform_owned"},
        )

    def test_posting_over_plain_http_is_refused(self):
        # The bearer is a shared production ingest token.
        with self.assertRaises(ValueError):
            tpd.post_to_coord("http://coord.example.com", "tok", [], 1.0)

    def test_localhost_stays_allowed_for_local_testing(self):
        # Reaches the network layer rather than the scheme guard, which is the
        # distinction being asserted.
        with self.assertRaises(OSError):
            tpd.post_to_coord("http://127.0.0.1:1", "tok", [], 1.0)


class SeverityParityTests(unittest.TestCase):
    def test_severity_order_matches_coords_infra_drift_class(self):
        # coord re-derives the fold server-side on `InfraDriftClass::severity`.
        # A divergence here would only ever surface as this script's summary
        # disagreeing with what coord stored.
        self.assertEqual(
            [k for k, _ in sorted(tpd.SEVERITY.items(), key=lambda kv: kv[1])],
            ["ok", "unknown", "benign_add", "in_place_change", "active_negation"],
        )

    def test_every_negating_attribute_entry_records_a_reason(self):
        # An entry nobody can audit is an entry nobody will trust enough to keep.
        for key, reason in tpd.NEGATING_ATTRIBUTES.items():
            self.assertTrue(reason.strip(), f"{key} has no reason")


if __name__ == "__main__":
    unittest.main()
