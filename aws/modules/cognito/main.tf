# ─── cognito module ─────────────────────────────────────────────────────────
# Reproduces the manually-created us-east-1 Cognito user pool
# (us-east-1_rgTB9dbZ1, "qontinui-coord-staging") as Terraform-managed
# infrastructure so a fresh eu-central-1 pool can be stood up with
# `terraform apply`. Config captured from `aws cognito-idp describe-*`.
#
# Faithful reproduction notes:
#   * The us-east-1 pool is NOT in Terraform; this module is the greenfield
#     equivalent. The PreSignUp trigger — a manual update-user-pool step on the
#     legacy pool — is wired directly here via lambda_config.
#   * Standard OIDC attributes (email, name, sub, the `identities` attribute,
#     etc.) are implicit and are NOT declared; only the one CUSTOM attribute
#     (tenant_slug) is.
#   * The hosted-UI / custom domain (aws_cognito_user_pool_domain) is
#     deliberately NOT created — see the commented block below.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# ─── User pool ──────────────────────────────────────────────────────────────

resource "aws_cognito_user_pool" "this" {
  name = var.pool_name

  # ESSENTIALS tier (enables choice-based sign-in policy below).
  user_pool_tier = "ESSENTIALS"

  deletion_protection = "INACTIVE"
  mfa_configuration   = "OFF"

  # Sign-in is by generated username; email is auto-verified but NOT a sign-in
  # alias (username_attributes intentionally omitted).
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 8
    require_uppercase                = true
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # ESSENTIALS-tier default: password as the only first auth factor.
  sign_in_policy {
    allowed_first_auth_factors = ["PASSWORD"]
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
    recovery_mechanism {
      name     = "verified_phone_number"
      priority = 2
    }
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  email_configuration {
    email_sending_account = "DEVELOPER"
    source_arn            = var.ses_source_arn
    from_email_address    = var.from_email
  }

  lambda_config {
    pre_sign_up = var.presignup_lambda_arn
  }

  verification_message_template {
    default_email_option = "CONFIRM_WITH_CODE"
  }

  user_attribute_update_settings {
    attributes_require_verification_before_update = []
  }

  # The one custom attribute. Terraform/Cognito stores it as
  # `custom:tenant_slug`; declare it here WITHOUT the `custom:` prefix. No
  # length constraints (source has an empty StringAttributeConstraints).
  schema {
    name                = "tenant_slug"
    attribute_data_type = "String"
    mutable             = true
    required            = false
  }
}

# ─── Hosted-UI / custom domain (DEFERRED — do not create yet) ────────────────
# The auth-domain (auth.qontinui.io) is single-pool-bound: a custom Cognito
# domain can attach to exactly one user pool, and it currently points at the
# legacy us-east-1 pool. Standing up a domain on this pool needs an operator
# decision on the hostname (a distinct eu auth host vs. cutting the existing one
# over). Left commented and gated behind that decision; the `domain` variable is
# already present so this can be enabled later with no variable-surface change.
#
# resource "aws_cognito_user_pool_domain" "this" {
#   domain       = var.domain
#   user_pool_id = aws_cognito_user_pool.this.id
#   # For a custom domain, also set: certificate_arn = <ACM cert in this region>
# }

# ─── Identity providers ─────────────────────────────────────────────────────
# Google is a native Google-type IdP (Cognito auto-populates the endpoint URLs
# from the provider type). GitHub and MicrosoftEntra are generic OIDC providers
# fronted by API-GW shims; Cognito auto-discovers their endpoints from
# <oidc_issuer>/.well-known/openid-configuration, so only oidc_issuer + the
# attributes request-method flags are supplied (matches the source pool exactly).

resource "aws_cognito_identity_provider" "google" {
  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = "Google"
  provider_type = "Google"

  provider_details = {
    client_id        = var.google_client_id
    client_secret    = var.google_client_secret
    authorize_scopes = "openid email profile"
  }

  attribute_mapping = {
    email          = "email"
    email_verified = "email_verified"
    name           = "name"
    username       = "sub"
  }
}

resource "aws_cognito_identity_provider" "github" {
  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = "GitHub"
  provider_type = "OIDC"

  provider_details = {
    client_id                     = var.github_client_id
    client_secret                 = var.github_client_secret
    authorize_scopes              = "openid read:user user:email"
    oidc_issuer                   = var.github_oidc_issuer
    attributes_request_method     = "GET"
    attributes_url_add_attributes = "false"
  }

  attribute_mapping = {
    email    = "email"
    username = "sub"
  }
}

resource "aws_cognito_identity_provider" "microsoft_entra" {
  user_pool_id  = aws_cognito_user_pool.this.id
  provider_name = "MicrosoftEntra"
  provider_type = "OIDC"

  provider_details = {
    client_id                     = var.entra_client_id
    client_secret                 = var.entra_client_secret
    authorize_scopes              = "openid profile email"
    oidc_issuer                   = var.entra_oidc_issuer
    attributes_request_method     = "GET"
    attributes_url_add_attributes = "false"
  }

  attribute_mapping = {
    email          = "email"
    email_verified = "email_verified"
    name           = "name"
    username       = "sub"
  }
}

# ─── App clients ────────────────────────────────────────────────────────────
# Seven clients, authored as a for_each over a keyed map. Common to all:
# refresh_token_validity=30 (days), enable_token_revocation=true,
# auth_session_validity=3, access/id token validity left at defaults.
#
# Empty list fields are coalesced to null on the resource so an unset attribute
# stays unset (vs. an empty-list diff). The federated clients list the three
# external IdPs, so the whole for_each depends_on the IdP resources — Cognito
# rejects a client naming an IdP that does not yet exist.

locals {
  cognito_clients = {
    dashboard = {
      name                                 = "qontinui-coord-staging-dashboard"
      generate_secret                      = true
      explicit_auth_flows                  = []
      allowed_oauth_flows_user_pool_client = false
      allowed_oauth_flows                  = []
      allowed_oauth_scopes                 = []
      supported_identity_providers         = []
      callback_urls = [
        "http://localhost:3000/api/auth/callback/cognito",
        "https://qontinui.io/api/auth/callback/cognito",
      ]
      logout_urls = [
        "http://localhost:3000",
        "https://qontinui.io",
      ]
    }

    runner = {
      name                                 = "qontinui-runner"
      generate_secret                      = false
      explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH"]
      allowed_oauth_flows_user_pool_client = true
      allowed_oauth_flows                  = ["code"]
      allowed_oauth_scopes                 = ["email", "openid", "profile"]
      supported_identity_providers         = ["COGNITO", "GitHub", "Google", "MicrosoftEntra"]
      callback_urls = [
        "http://127.0.0.1:53682/auth/callback",
        "http://localhost:53682/auth/callback",
      ]
      logout_urls = []
    }

    headless_ops = {
      name                                 = "qontinui-coord-headless-ops"
      generate_secret                      = false
      explicit_auth_flows                  = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
      allowed_oauth_flows_user_pool_client = false
      allowed_oauth_flows                  = []
      allowed_oauth_scopes                 = []
      supported_identity_providers         = []
      callback_urls                        = []
      logout_urls                          = []
    }

    verify = {
      name                                 = "qontinui-coord-verify"
      generate_secret                      = false
      explicit_auth_flows                  = ["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
      allowed_oauth_flows_user_pool_client = false
      allowed_oauth_flows                  = []
      allowed_oauth_scopes                 = []
      supported_identity_providers         = []
      callback_urls                        = []
      logout_urls                          = []
    }

    mobile = {
      name                                 = "qontinui-mobile"
      generate_secret                      = false
      explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH"]
      allowed_oauth_flows_user_pool_client = true
      allowed_oauth_flows                  = ["code"]
      allowed_oauth_scopes                 = ["email", "openid", "profile"]
      supported_identity_providers         = ["COGNITO", "GitHub", "Google", "MicrosoftEntra"]
      callback_urls                        = ["qontinui://oauth-callback"]
      logout_urls                          = []
    }

    web = {
      name                                 = "qontinui-web"
      generate_secret                      = false
      explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
      allowed_oauth_flows_user_pool_client = true
      allowed_oauth_flows                  = ["code"]
      allowed_oauth_scopes                 = ["email", "openid", "profile"]
      supported_identity_providers         = ["COGNITO", "GitHub", "Google", "MicrosoftEntra"]
      callback_urls = [
        "http://localhost:3000/auth/callback",
        "https://demo.staging.qontinui.io/auth/callback",
        "https://qontinui.io/auth/callback",
      ]
      logout_urls = [
        "https://demo.staging.qontinui.io/login",
        "https://qontinui.io/login",
      ]
    }

    ci = {
      name                                 = "qontinui-ci"
      generate_secret                      = false
      explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_PASSWORD_AUTH"]
      allowed_oauth_flows_user_pool_client = false
      allowed_oauth_flows                  = []
      allowed_oauth_scopes                 = []
      supported_identity_providers         = []
      callback_urls                        = []
      logout_urls                          = []
    }
  }
}

resource "aws_cognito_user_pool_client" "this" {
  for_each = local.cognito_clients

  user_pool_id = aws_cognito_user_pool.this.id
  name         = each.value.name

  generate_secret         = each.value.generate_secret
  refresh_token_validity  = 30
  auth_session_validity   = 3
  enable_token_revocation = true

  explicit_auth_flows = length(each.value.explicit_auth_flows) > 0 ? each.value.explicit_auth_flows : null

  allowed_oauth_flows_user_pool_client = each.value.allowed_oauth_flows_user_pool_client
  allowed_oauth_flows                  = length(each.value.allowed_oauth_flows) > 0 ? each.value.allowed_oauth_flows : null
  allowed_oauth_scopes                 = length(each.value.allowed_oauth_scopes) > 0 ? each.value.allowed_oauth_scopes : null

  supported_identity_providers = length(each.value.supported_identity_providers) > 0 ? each.value.supported_identity_providers : null

  callback_urls = length(each.value.callback_urls) > 0 ? each.value.callback_urls : null
  logout_urls   = length(each.value.logout_urls) > 0 ? each.value.logout_urls : null

  # A client naming an external IdP must not be created before that IdP exists.
  depends_on = [
    aws_cognito_identity_provider.google,
    aws_cognito_identity_provider.github,
    aws_cognito_identity_provider.microsoft_entra,
  ]
}
