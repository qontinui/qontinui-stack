# coord's identity-side grants for the operator pager (plan Phase 2).
#
# Attached FROM THIS MODULE to coord's task role so modules/coord/main.tf (owned
# by lane B2's plan) is not edited. A separate named inline policy, like the
# coord module's own task_session_output_cold: auditable on its own.
#   * sns:Publish on the pager topic — coord's operator leg (Phase 3). The topic
#     policy's AllowCoordPublish statement is the resource-side half.
#   * logs:FilterLogEvents on the relay's log group — Phase 5's ingest of the
#     relay records (delivered / delivery_failed, and AWS-originated pages).

locals {
  # Role name from the ARN's last path segment (arn:...:role[/path]/<name>).
  coord_task_role_name = regex("[^/]+$", var.coord_task_role_arn)
}

data "aws_iam_policy_document" "coord_operator_pager" {
  statement {
    sid       = "PublishOperatorPage"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.pager.arn]
  }

  statement {
    sid       = "ReadRelayRecords"
    actions   = ["logs:FilterLogEvents"]
    resources = ["${aws_cloudwatch_log_group.relay.arn}:*"]
  }
}

resource "aws_iam_role_policy" "coord_operator_pager" {
  name   = "${local.prefix}-coord-operator-pager"
  role   = local.coord_task_role_name
  policy = data.aws_iam_policy_document.coord_operator_pager.json
}
