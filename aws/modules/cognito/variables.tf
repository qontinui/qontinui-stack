# ─── cognito module — variables ─────────────────────────────────────────────
# Inputs for a faithful reproduction of the manually-created us-east-1 pool
# (us-east-1_rgTB9dbZ1, "qontinui-coord-staging") in a fresh region.

variable "environment" {
  type        = string
  description = "Deployment environment slug (e.g. \"staging\"). Used only for tag/name derivation by the composition root; the pool name is set explicitly via pool_name."
  default     = "staging"
}

variable "pool_name" {
  type        = string
  description = "Cognito user pool Name. Defaults to the existing us-east-1 pool name so the reproduction is byte-identical; override for the eu env if a distinct name is wanted to avoid confusion between the two pools."
  default     = "qontinui-coord-staging"
}

# ─── Email (SES) ────────────────────────────────────────────────────────────

variable "ses_source_arn" {
  type        = string
  description = "ARN of the verified SES identity Cognito sends from (email_configuration.source_arn). Region-specific — for eu supply the eu-central-1 SES identity, e.g. arn:aws:ses:eu-central-1:047719635665:identity/staging.qontinui.io"
}

variable "from_email" {
  type        = string
  description = "email_configuration.from_email_address — the RFC5322 From header used on Cognito emails."
  default     = "Qontinui <no-reply@staging.qontinui.io>"
}

# ─── Lambda triggers ────────────────────────────────────────────────────────

variable "presignup_lambda_arn" {
  type        = string
  description = "ARN of the PreSignUp trigger Lambda (wired to the cross-idp-linking module's aws_lambda_function output). Set on lambda_config.pre_sign_up. Unlike the us-east-1 pool — which is not in Terraform, so its trigger is attached by a manual update-user-pool step — this pool IS Terraform-managed and wires the trigger directly."
}

# ─── Hosted-UI / custom domain (deferred) ───────────────────────────────────

variable "domain" {
  type        = string
  description = "Hosted-UI / custom auth domain. Left empty and unused for now: the aws_cognito_user_pool_domain resource is intentionally NOT created in this module because auth.qontinui.io is single-pool-bound and needs an operator hostname decision. Kept as a variable so the domain can be wired later without a variable-surface change."
  default     = ""
}

# ─── Identity provider: Google ──────────────────────────────────────────────

variable "google_client_id" {
  type        = string
  description = "Google OAuth client_id for the Google IdP."
}

variable "google_client_secret" {
  type        = string
  description = "Google OAuth client_secret for the Google IdP."
  sensitive   = true
}

# ─── Identity provider: GitHub (OIDC shim) ──────────────────────────────────

variable "github_client_id" {
  type        = string
  description = "GitHub OIDC client_id (the API-GW OIDC shim's client id)."
}

variable "github_client_secret" {
  type        = string
  description = "GitHub OIDC client_secret."
  sensitive   = true
}

variable "github_oidc_issuer" {
  type        = string
  description = "GitHub OIDC issuer base URL. Cognito auto-discovers authorize/token/userinfo/jwks endpoints from <issuer>/.well-known/openid-configuration, so no explicit URL vars are needed (matches the us-east-1 pool, which sets only oidc_issuer). Region-specific — the us-east-1 shim is https://wedach6yaj.execute-api.us-east-1.amazonaws.com; supply the eu-central-1 API-GW shim URL here."
}

# ─── Identity provider: Microsoft Entra (OIDC) ──────────────────────────────

variable "entra_client_id" {
  type        = string
  description = "Microsoft Entra OIDC client_id (application id)."
}

variable "entra_client_secret" {
  type        = string
  description = "Microsoft Entra OIDC client_secret."
  sensitive   = true
}

variable "entra_oidc_issuer" {
  type        = string
  description = "Microsoft Entra OIDC issuer base URL. Cognito auto-discovers the endpoints from <issuer>/.well-known/openid-configuration (matches the us-east-1 pool, which sets only oidc_issuer). Region-specific — the us-east-1 value is https://kl7s75gy2c.execute-api.us-east-1.amazonaws.com; supply the eu-central-1 issuer here."
}
