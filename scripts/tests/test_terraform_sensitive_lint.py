"""Tests for ``terraform-sensitive-lint.py``.

Runnable via either::

    python -m pytest qontinui-stack/scripts/tests/test_terraform_sensitive_lint.py
    python -m unittest qontinui-stack.scripts.tests.test_terraform_sensitive_lint

Written against ``unittest.TestCase`` so both runners work, matching the sibling
``test_terraform_plan_drift.py`` / ``test_resolve_plan_deps.py`` convention.

Two kinds of test live here and they are doing different jobs.

The synthetic cases build a throwaway two-module tree per test, so a rule can be
shown firing *and* shown not firing on the near-miss next to it. The
:class:`RealTreeTests` cases run the checker over this repo's actual ``aws/``
and assert it is clean — that is the regression guard itself, and it is the one
that fails when somebody adds an unmarked value. :class:`PullRequest70Tests`
reconstructs the pre-fix shape of the defect that motivated the whole check, so
"this would have caught it" is an assertion rather than a claim in a docstring.
"""

from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "terraform-sensitive-lint.py"
_REPO_ROOT = _MODULE_PATH.parent.parent
_spec = importlib.util.spec_from_file_location("terraform_sensitive_lint", _MODULE_PATH)
assert _spec and _spec.loader
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


class _Tree:
    """A throwaway terraform tree: ``root/`` calling ``modules/<name>/``."""

    def __init__(self, stack: unittest.TestCase) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        stack.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name)
        (self.path / "root").mkdir()

    def module(self, name: str, body: str) -> "_Tree":
        directory = self.path / "modules" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "main.tf").write_text(body, encoding="utf-8")
        return self

    def root(self, body: str) -> "_Tree":
        (self.path / "root" / "main.tf").write_text(body, encoding="utf-8")
        return self

    def rules(self) -> list[str]:
        return [f.rule for f in lint.check_tree(self.path)]

    def findings(self) -> list[lint.Finding]:
        return lint.check_tree(self.path)


class MaskSourceTests(unittest.TestCase):
    """The mask is load-bearing: a desync mis-slices every later block.

    ``aws/staging/main.tf`` really does contain quotes nested two deep inside a
    string literal, so this is not a hypothetical parser worry — a naive
    ``"``-toggling scanner loses synchronisation at that line and reads the rest
    of the file as string content.
    """

    def test_nested_quotes_inside_interpolation_do_not_desync(self):
        src = 'a = "^x${replace(var.domain_name, ".", "y")}$"\nvariable "kept" {\n}\n'
        mask = lint.mask_source(src)
        self.assertEqual(len(mask), len(src))
        # The block AFTER the tricky literal is still found, with its label.
        blocks = [b for b in lint.top_level_blocks(src, mask) if b.type == "variable"]
        self.assertEqual([b.labels for b in blocks], [("kept",)])

    def test_var_references_inside_interpolation_stay_visible(self):
        src = 'value = "https://${var.subdomain}.${var.domain}"\n'
        mask = lint.mask_source(src)
        self.assertEqual(sorted(lint._VAR_REF.findall(mask)), ["domain", "subdomain"])

    def test_literal_text_is_blanked(self):
        # A `sensitive = true` that is only ever TEXT must not be read as code.
        src = 'description = "set sensitive = true here"\n'
        self.assertNotIn("sensitive = true", lint.mask_source(src).replace("description", ""))

    def test_comment_with_an_unbalanced_quote_does_not_desync(self):
        src = '# terraform\'s "sensitive" marker\nvariable "kept" {\n  sensitive = true\n}\n'
        mask = lint.mask_source(src)
        block = next(lint.top_level_blocks(src, mask))
        self.assertEqual(block.labels, ("kept",))
        self.assertTrue(lint._declares_sensitive(src, mask, block))

    def test_escaped_interpolation_marker_does_not_open_an_interpolation(self):
        # `$${` is HCL's escape for a LITERAL `${`. Treating it as the start of
        # an interpolation opens a context that never closes, and the scanner
        # then reads the whole rest of the file as code.
        src = 'description = "write $${var.x} to mean a literal"\nvariable "kept" {\n}\n'
        mask = lint.mask_source(src)
        self.assertEqual([b.labels for b in lint.top_level_blocks(src, mask)], [("kept",)])
        self.assertEqual(lint._VAR_REF.findall(mask), [])

    def test_directive_marker_is_treated_like_an_interpolation(self):
        src = 'value = "%{ if var.email != "" }x%{ endif }"\n'
        self.assertIn("email", lint._VAR_REF.findall(lint.mask_source(src)))

    def test_braces_inside_an_interpolation_close_themselves(self):
        # An object literal inside `${...}`. Without brace depth in there, the
        # inner `}` ends the interpolation early and everything after it is read
        # as string content — including the next block's header.
        src = 'a = "${lookup({ k = var.email }, "k")}"\nvariable "kept" {\n}\n'
        mask = lint.mask_source(src)
        self.assertEqual([b.labels for b in lint.top_level_blocks(src, mask)], [("kept",)])
        self.assertIn("email", lint._VAR_REF.findall(mask))

    def test_heredoc_body_is_blanked(self):
        src = 'variable "x" {\n  description = <<-EOT\n    sensitive = true\n  EOT\n}\n'
        mask = lint.mask_source(src)
        block = next(lint.top_level_blocks(src, mask))
        # The heredoc says the words but does not declare the attribute.
        self.assertFalse(lint._declares_sensitive(src, mask, block))

    def test_heredoc_braces_do_not_unbalance_the_block(self):
        src = 'variable "x" {\n  description = <<-EOT\n    { unbalanced\n  EOT\n}\n\nvariable "y" {\n}\n'
        mask = lint.mask_source(src)
        self.assertEqual([b.labels for b in lint.top_level_blocks(src, mask)], [("x",), ("y",)])


class BodyAttributeTests(unittest.TestCase):
    def test_nested_block_attributes_are_not_read_as_the_block_s_own(self):
        # `validation { condition = ... }` must not supply the variable's
        # `sensitive`. Reading a nested attribute as the outer block's is how a
        # checker reports a marker that is not there.
        src = 'variable "x" {\n  type = string\n  validation {\n    sensitive = true\n  }\n}\n'
        mask = lint.mask_source(src)
        block = next(lint.top_level_blocks(src, mask))
        self.assertFalse(lint._declares_sensitive(src, mask, block))
        self.assertEqual([n for n, _, _ in lint.body_attributes(src, mask, block)], ["type"])

    def test_single_line_block_attributes_are_found(self):
        # `output "budget_name" { value = ... }` is written on one line in
        # aws/modules/cost-control/main.tf, and `terraform fmt` keeps it that
        # way. An attribute STARTS A STATEMENT, which is not the same as
        # starting a line — anchoring on `^` read these blocks as empty, and an
        # empty block reports a missing `sensitive` marker as clean.
        src = 'output "x" { value = var.email }\n'
        mask = lint.mask_source(src)
        block = next(lint.top_level_blocks(src, mask))
        attrs = dict((n, e) for n, e, _ in lint.body_attributes(src, mask, block))
        self.assertIn("value", attrs)
        self.assertEqual(attrs["value"].strip(), "var.email")

    def test_single_line_sensitive_marker_is_read(self):
        src = 'variable "x" { sensitive = true }\n'
        mask = lint.mask_source(src)
        self.assertTrue(lint._declares_sensitive(src, mask, next(lint.top_level_blocks(src, mask))))

    def test_map_literal_entries_are_not_read_as_block_attributes(self):
        # `tags = { Name = "x" }` — `Name` follows a `{` and would match the
        # statement-start pattern, so the depth check is what excludes it.
        src = 'resource "aws_lb" "x" {\n  tags = { Name = "y" }\n}\n'
        mask = lint.mask_source(src)
        block = next(lint.top_level_blocks(src, mask))
        self.assertEqual([n for n, _, _ in lint.body_attributes(src, mask, block)], ["tags"])

    def test_multiline_expression_is_captured_whole(self):
        src = 'output "x" {\n  value = concat(\n    var.a,\n    var.b,\n  )\n}\n'
        mask = lint.mask_source(src)
        block = next(lint.top_level_blocks(src, mask))
        value = next(e for n, e, _ in lint.body_attributes(src, mask, block) if n == "value")
        self.assertIn("var.a", value)
        self.assertIn("var.b", value)


class InsecureValueSinkTests(unittest.TestCase):
    """R1 — ``.insecure_value`` drops the marker, so the sink must re-apply it."""

    SINK_UNMARKED = 'variable "email" {\n  type = string\n}\n'
    SINK_MARKED = 'variable "email" {\n  type      = string\n  sensitive = true\n}\n'
    CALL = (
        'module "c" {\n'
        '  source = "../modules/c"\n'
        "  email  = data.aws_ssm_parameter.e.insecure_value\n"
        "}\n"
    )

    def test_unmarked_sink_is_a_finding(self):
        tree = _Tree(self).module("c", self.SINK_UNMARKED).root(self.CALL)
        self.assertEqual(tree.rules(), ["insecure-value-needs-sensitive-sink"])

    def test_marked_sink_is_clean(self):
        tree = _Tree(self).module("c", self.SINK_MARKED).root(self.CALL)
        self.assertEqual(tree.rules(), [])

    def test_plain_value_accessor_is_not_this_rule_s_business(self):
        # `.value` is unconditionally sensitive in terraform's own model, so it
        # needs no boundary marker and must not be reported.
        tree = _Tree(self).module("c", self.SINK_UNMARKED).root(
            'module "c" {\n  source = "../modules/c"\n  email  = data.aws_ssm_parameter.e.value\n}\n'
        )
        self.assertEqual(tree.rules(), [])

    def test_unresolvable_sink_is_reported_rather_than_passed(self):
        # A registry source cannot be read, so the boundary is unverifiable.
        # Silence here would render as a clean result, which is the one answer
        # the checker must never give for a case it did not check.
        tree = _Tree(self).root(
            'module "c" {\n'
            '  source = "terraform-aws-modules/x/aws"\n'
            "  email  = data.aws_ssm_parameter.e.insecure_value\n"
            "}\n"
        )
        self.assertEqual(tree.rules(), ["insecure-value-unresolved-sink"])

    def test_argument_the_child_never_declares_is_reported(self):
        tree = _Tree(self).module("c", 'variable "other" {\n}\n').root(self.CALL)
        self.assertEqual(tree.rules(), ["insecure-value-unresolved-sink"])


class SensitiveSourceTests(unittest.TestCase):
    """R2 — sensitivity flows forward from the origin, not back from the sink."""

    CHILD = 'variable "email" {\n  sensitive = true\n}\n'
    CALL = 'module "c" {\n  source = "../modules/c"\n  email  = var.email\n}\n'

    def test_unmarked_caller_variable_is_a_finding(self):
        tree = _Tree(self).module("c", self.CHILD).root(self.CALL + 'variable "email" {\n}\n')
        self.assertEqual(tree.rules(), ["sensitive-input-needs-sensitive-source"])

    def test_marked_caller_variable_is_clean(self):
        tree = _Tree(self).module("c", self.CHILD).root(
            self.CALL + 'variable "email" {\n  sensitive = true\n}\n'
        )
        self.assertEqual(tree.rules(), [])

    def test_non_variable_expression_is_not_this_rule_s_business(self):
        # A `random_password.x.result` is sensitive at its own origin; there is
        # no caller variable to mark, so the rule must stay silent.
        tree = _Tree(self).module("c", self.CHILD).root(
            'module "c" {\n  source = "../modules/c"\n  email  = random_password.p.result\n}\n'
        )
        self.assertEqual(tree.rules(), [])

    def test_unmarked_child_input_does_not_arm_the_rule(self):
        tree = _Tree(self).module("c", 'variable "email" {\n}\n').root(
            self.CALL + 'variable "email" {\n}\n'
        )
        self.assertEqual(tree.rules(), [])


class SensitiveOutputTests(unittest.TestCase):
    """R3 — terraform errors on this only at the ROOT, so a child needs a lint."""

    VAR = 'variable "email" {\n  sensitive = true\n}\n'

    def test_unmarked_output_re_exporting_a_sensitive_var_is_a_finding(self):
        tree = _Tree(self).module("c", self.VAR + 'output "email" {\n  value = var.email\n}\n')
        self.assertEqual(tree.rules(), ["sensitive-var-needs-sensitive-output"])

    def test_marked_output_is_clean(self):
        tree = _Tree(self).module(
            "c", self.VAR + 'output "email" {\n  value     = var.email\n  sensitive = true\n}\n'
        )
        self.assertEqual(tree.rules(), [])

    def test_output_of_a_non_sensitive_var_is_clean(self):
        tree = _Tree(self).module(
            "c", 'variable "region" {\n}\noutput "region" {\n  value = var.region\n}\n'
        )
        self.assertEqual(tree.rules(), [])

    def test_sensitive_var_reached_through_interpolation_is_caught(self):
        # The reference is inside a string. Masking keeps `${...}` as code
        # precisely so this case is not invisible.
        tree = _Tree(self).module(
            "c", self.VAR + 'output "u" {\n  value = "mailto:${var.email}"\n}\n'
        )
        self.assertEqual(tree.rules(), ["sensitive-var-needs-sensitive-output"])

    def test_output_of_a_resource_attribute_is_clean(self):
        tree = _Tree(self).module(
            "c", self.VAR + 'output "arn" {\n  value = aws_sns_topic.t.arn\n}\n'
        )
        self.assertEqual(tree.rules(), [])


class PullRequest70Tests(unittest.TestCase):
    """The defect this whole checker exists because of, reconstructed.

    ``aws/modules/cost-control`` declared ``variable "alert_email" { type =
    string }`` with no marker while ``aws/staging`` fed it
    ``data.aws_ssm_parameter.budget_alert_email.insecure_value``, so the
    operator's personal address rendered verbatim in every ``terraform plan``
    diff touching the SNS subscription or either budget notification block — in
    a PUBLIC repo. ``terraform fmt``, ``terraform validate`` and the drift
    classifier's tests all passed throughout.

    Both halves of the eventual fix are asserted, in the order they would have
    been made, because the intermediate state — variable marked, output not — is
    a real state a person can leave the tree in and is the one ``terraform
    validate`` cannot see (it errors on an unmarked re-export only for a ROOT
    output).
    """

    ROOT = (
        'module "cost_control" {\n'
        '  source = "../modules/cost-control"\n'
        "  alert_email = data.aws_ssm_parameter.budget_alert_email.insecure_value\n"
        "}\n"
    )

    def _cost_control(self, variable: str, output: str) -> _Tree:
        return _Tree(self).module("cost-control", variable + output).root(self.ROOT)

    def test_the_shape_as_merged_in_pr_69_is_reported(self):
        tree = self._cost_control(
            'variable "alert_email" { type = string }\n',
            'output "alert_email" { value = var.alert_email }\n',
        )
        self.assertEqual(tree.rules(), ["insecure-value-needs-sensitive-sink"])
        self.assertIn("renders verbatim", tree.findings()[0].message)

    def test_marking_only_the_variable_still_reports_the_output(self):
        tree = self._cost_control(
            'variable "alert_email" {\n  type      = string\n  sensitive = true\n}\n',
            'output "alert_email" { value = var.alert_email }\n',
        )
        self.assertEqual(tree.rules(), ["sensitive-var-needs-sensitive-output"])

    def test_the_shape_as_merged_in_pr_70_is_clean(self):
        tree = self._cost_control(
            'variable "alert_email" {\n  type      = string\n  sensitive = true\n}\n',
            'output "alert_email" {\n  value     = var.alert_email\n  sensitive = true\n}\n',
        )
        self.assertEqual(tree.rules(), [])


class VendoredSourceTests(unittest.TestCase):
    """`terraform init` runs BEFORE the lint step in the same CI job."""

    def test_dot_directories_are_not_linted(self):
        tree = _Tree(self).module("c", 'variable "email" {\n}\n')
        vendored = tree.path / "root" / ".terraform" / "modules" / "third_party"
        vendored.mkdir(parents=True)
        # A finding-shaped file we neither own nor can fix.
        (vendored / "main.tf").write_text(
            'variable "email" {\n  sensitive = true\n}\noutput "email" {\n  value = var.email\n}\n',
            encoding="utf-8",
        )
        self.assertEqual(tree.rules(), [])
        self.assertNotIn(
            ".terraform", " ".join(p.as_posix() for p in lint.source_files(tree.path))
        )


class RealTreeTests(unittest.TestCase):
    """Over this repo's actual ``aws/`` — the guard itself, not a demonstration."""

    def test_aws_tree_is_clean(self):
        findings = lint.check_tree(_REPO_ROOT / "aws")
        self.assertEqual(
            [f.render(_REPO_ROOT) for f in findings],
            [],
            "a terraform value would render unredacted in plan output of a PUBLIC repo",
        )

    def test_the_tree_actually_parsed(self):
        # A checker that silently parsed nothing also reports "clean". Pin the
        # two module inputs the rules are anchored on, so an empty result cannot
        # pass for a clean one.
        modules = _REPO_ROOT / "aws" / "modules"
        cost_control = lint.TerraformModule(modules / "cost-control")
        cross_idp = lint.TerraformModule(modules / "cross-idp-linking")
        self.assertIs(cost_control.variables.get("alert_email"), True)
        self.assertIs(cross_idp.variables.get("signup_allowlist"), True)
        # And a genuinely non-sensitive neighbour, so `True` is not universal.
        self.assertIs(cost_control.variables.get("monthly_limit"), False)

    def test_every_insecure_value_call_site_is_covered_by_a_rule(self):
        # `.insecure_value` is the accessor that drops the marker, so each use
        # is a boundary R1 must have looked at. If a new one appears somewhere
        # the rule cannot see — a `locals` hop, say — this count moves and the
        # gap is visible instead of silent.
        sites = 0
        for path in lint.source_files(_REPO_ROOT / "aws"):
            src = path.read_text(encoding="utf-8")
            mask = lint.mask_source(src)
            for block in lint.top_level_blocks(src, mask):
                if block.type != "module":
                    continue
                sites += sum(
                    1
                    for _, expr, _ in lint.body_attributes(src, mask, block)
                    if ".insecure_value" in expr
                )
        self.assertEqual(sites, 2, "aws/staging passes .insecure_value to exactly two modules")

    def test_insecure_value_appears_nowhere_the_module_rule_cannot_see(self):
        # Pairs with the count above. `.insecure_value` outside a `module` block
        # — in a `locals`, or straight into a resource attribute — is a boundary
        # R1 structurally cannot reach, so it must not appear without somebody
        # noticing. Comments about the accessor are excluded by the mask.
        stray = []
        for path in lint.source_files(_REPO_ROOT / "aws"):
            src = path.read_text(encoding="utf-8")
            mask = lint.mask_source(src)
            in_module = [
                (b.start, b.end) for b in lint.top_level_blocks(src, mask) if b.type == "module"
            ]
            for hit in re.finditer(r"\.insecure_value", mask):
                if not any(start <= hit.start() < end for start, end in in_module):
                    stray.append(f"{path.name}:{lint.line_of(src, hit.start())}")
        self.assertEqual(stray, [], "an `.insecure_value` no rule here can check")


if __name__ == "__main__":
    unittest.main()
