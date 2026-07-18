# ─── cognito module — outputs ───────────────────────────────────────────────

output "user_pool_id" {
  description = "Bare user pool id (e.g. eu-central-1_xxxxxxxxx)."
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "User pool ARN — feed to the cross-idp-linking module (Lambda grant + invoke SourceArn scope)."
  value       = aws_cognito_user_pool.this.arn
}

output "user_pool_endpoint" {
  description = "User pool endpoint host (cognito-idp.<region>.amazonaws.com/<pool_id>)."
  value       = aws_cognito_user_pool.this.endpoint
}

# Map of client NAME => client id (the actual Cognito ClientName, e.g.
# "qontinui-web"). The composition root looks clients up by name.
output "client_ids" {
  description = "Map of app-client name => client id."
  value       = { for k, c in aws_cognito_user_pool_client.this : c.name => c.id }
}

# The dashboard client is the only one created with generate_secret = true.
output "dashboard_client_secret" {
  description = "Client secret for the qontinui-coord-staging-dashboard client (generate_secret = true)."
  value       = aws_cognito_user_pool_client.this["dashboard"].client_secret
  sensitive   = true
}

# NOTE: the OIDC issuer URL is https://cognito-idp.<region>.amazonaws.com/<user_pool_id>.
# The region is not known inside this module, so it is not composed here — the
# environment composes it from its provider region + user_pool_id.
