# ─── coord's grants for the operator pager (plan Phases 3 and 5) ───────
#
# Attached to coord's task role from THIS module, so aws/modules/coord/main.tf is not edited: the grant
# travels with the capability that needs it. Two statements, both scoped to one resource:
#   - coord's operator leg (Phase 3) publishes coord-side pages to the pager topic;
#   - coord's ingest (Phase 5) reads the relay's outcome records from its log group.

locals {
  coord_task_role_name = regex("[^/]+$", var.coord_task_role_arn)
}

data "aws_iam_policy_document" "coord_operator_pager" {
  statement {
    sid       = "PublishOperatorPages"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.pager.arn]
  }
  statement {
    sid       = "ReadRelayOutcomeRecords"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.relay.arn}:*"]
  }
}

resource "aws_iam_role_policy" "coord_operator_pager" {
  name   = "qontinui-${var.environment}-coord-operator-pager"
  role   = local.coord_task_role_name
  policy = data.aws_iam_policy_document.coord_operator_pager.json
}
