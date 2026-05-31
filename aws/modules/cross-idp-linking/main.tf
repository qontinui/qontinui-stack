# PreSignUp_ExternalProvider auto-link Lambda for cross-IdP account linking.
#
# WHAT THIS PROVISIONS (and what it deliberately does NOT)
# --------------------------------------------------------
# The Cognito user pool (us-east-1_rgTB9dbZ1) is MANUALLY managed and NOT in
# Terraform — we reference it by ARN, never import it. Terraform therefore
# CANNOT attach this Lambda to the pool's PreSignUp trigger (that lives on the
# aws_cognito_user_pool resource we don't own). This module provisions
# everything around the trigger:
#
#   * aws_lambda_function          — the Python 3.12 handler (zipped from source)
#   * aws_iam_role + inline policy — execution role: CloudWatch Logs + the three
#                                    cognito-idp actions the handler calls,
#                                    scoped to ONLY the pool ARN
#   * aws_lambda_permission        — lets cognito-idp.amazonaws.com (from this
#                                    specific pool ARN) invoke the function
#   * aws_cloudwatch_log_group     — explicit log group (so retention is managed
#                                    and the group isn't auto-created untagged)
#
# THE ONE MANUAL STEP (cannot be Terraformed — pool not in TF):
# After `terraform apply`, attach the trigger to the pool. update-user-pool
# REPLACES the pool's whole config, so describe-then-merge to avoid wiping
# existing settings:
#
#   POOL=us-east-1_rgTB9dbZ1
#   REGION=us-east-1
#   ARN=$(terraform output -raw cross_idp_presignup_lambda_arn)
#   # Read current lambda-config, merge in PreSignUp, re-apply with the merged set:
#   aws cognito-idp describe-user-pool --user-pool-id "$POOL" --region "$REGION" \
#     --query 'UserPool.LambdaConfig' > /tmp/lambda-config.json
#   # ... merge PreSignUp=$ARN into /tmp/lambda-config.json ...
#   aws cognito-idp update-user-pool --user-pool-id "$POOL" --region "$REGION" \
#     --lambda-config "$(jq --arg a "$ARN" '. + {PreSignUp:$a}' /tmp/lambda-config.json)"
#
# (Re-supply any other existing pool attributes update-user-pool requires;
# describe-user-pool is the source of truth for the current values.)

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
  }
}

variable "environment" { type = string }
variable "region" { type = string }

variable "user_pool_arn" {
  type        = string
  description = "ARN of the manually-managed Cognito user pool (NOT in Terraform). The Lambda's cognito-idp grant + the invoke permission's SourceArn are scoped to ONLY this ARN. e.g. arn:aws:cognito-idp:us-east-1:047719635665:userpool/us-east-1_rgTB9dbZ1"
}

variable "user_pool_id" {
  type        = string
  description = "Bare pool id (e.g. us-east-1_rgTB9dbZ1) — passed to the handler as the USER_POOL_ID env var so it can call list_users / admin_link_provider_for_user. Derived from user_pool_arn by the composition root."
}

# ─── Source package ──────────────────────────────────────────────────────
# Zip the handler at plan/apply time. boto3 ships in the Lambda Python runtime,
# so no vendored deps are bundled — just handler.py.

data "archive_file" "presignup" {
  type        = "zip"
  source_file = "${path.module}/../../lambdas/cognito_presignup_autolink/handler.py"
  output_path = "${path.module}/.build/cognito_presignup_autolink.zip"
}

# ─── IAM (execution role) ────────────────────────────────────────────────

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "presignup" {
  name               = "qontinui-${var.environment}-presignup-autolink"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

# CloudWatch Logs (the Lambda basic-execution permissions).
resource "aws_iam_role_policy_attachment" "presignup_logs" {
  role       = aws_iam_role.presignup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The cognito-idp actions the handler actually calls, scoped to ONLY the pool
# ARN. AdminLinkProviderForUser is the privileged one; AdminGetUser + ListUsers
# back the match-resolution. Deliberately narrower than the web task role's
# linking grant (no Disable/Delete here — the auto-link path never removes
# users). Separate, auditable, least-privilege.
data "aws_iam_policy_document" "presignup_cognito" {
  statement {
    sid = "PreSignUpAutoLinkCognito"
    actions = [
      "cognito-idp:AdminLinkProviderForUser",
      "cognito-idp:AdminGetUser",
      "cognito-idp:ListUsers",
    ]
    resources = [var.user_pool_arn]
  }
}

resource "aws_iam_role_policy" "presignup_cognito" {
  name   = "qontinui-${var.environment}-presignup-autolink-cognito"
  role   = aws_iam_role.presignup.id
  policy = data.aws_iam_policy_document.presignup_cognito.json
}

# ─── Logging ──────────────────────────────────────────────────────────────
# Explicit log group so retention is managed (vs Lambda auto-creating an
# untagged, never-expiring group). Name matches the Lambda's implicit group.

resource "aws_cloudwatch_log_group" "presignup" {
  name              = "/aws/lambda/qontinui-${var.environment}-presignup-autolink"
  retention_in_days = 14
}

# ─── Lambda ────────────────────────────────────────────────────────────────

resource "aws_lambda_function" "presignup" {
  function_name = "qontinui-${var.environment}-presignup-autolink"
  role          = aws_iam_role.presignup.arn
  runtime       = "python3.12"
  handler       = "handler.handler"
  timeout       = 10
  memory_size   = 128

  filename         = data.archive_file.presignup.output_path
  source_code_hash = data.archive_file.presignup.output_base64sha256

  environment {
    variables = {
      USER_POOL_ID = var.user_pool_id
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.presignup_logs,
    aws_cloudwatch_log_group.presignup,
  ]
}

# ─── Invoke permission ───────────────────────────────────────────────────
# Allow the Cognito service — and ONLY this pool — to invoke the function. The
# trigger attachment itself is the manual update-user-pool step (pool not in
# TF); this permission is the half Terraform CAN express so the manual wiring
# works the moment it's applied.

resource "aws_lambda_permission" "cognito_invoke" {
  statement_id  = "AllowCognitoPreSignUpInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.presignup.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = var.user_pool_arn
}

# ─── Outputs ──────────────────────────────────────────────────────────────

output "lambda_arn" { value = aws_lambda_function.presignup.arn }
output "lambda_function_name" { value = aws_lambda_function.presignup.function_name }
output "execution_role_arn" { value = aws_iam_role.presignup.arn }
