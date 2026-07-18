# Public ingress: ALB + ACM cert (DNS-validated) + Route53 alias.
#
# This is the AWS equivalent of "Cloudflare Tunnel" in dev. The same
# logical role — terminating TLS for the public coord URL and routing
# to the right backend — but native to AWS.

variable "environment" { type = string }
variable "vpc_id" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "alb_sg_id" { type = string }
variable "domain_name" { type = string }
variable "route53_zone_id" { type = string }
variable "coord_subdomain" { type = string }
variable "coord_target_group" { type = string }
variable "web_subdomain" { type = string }
variable "web_target_group" {
  type        = string
  description = "Target group ARN for the web Fargate service. Web is no longer deferred (slim image + cloud-control overlay live as of plan 2026-05-17-web-image-slim); the listener rule below is unconditional."
}
variable "metrics_token" {
  type        = string
  sensitive   = true
  description = "Shared token gating the coord /metrics route. Requests must send `X-Metrics-Token: <value>` to reach coord's Prometheus endpoint; all other /metrics requests get a fixed 403 instead of falling through to the open coord default action."
}

# Parallel-stand-up cutover toggle. When two envs share the SAME domain/zone
# (us-east-1 + eu-central-1 during a region migration), only ONE of them may
# own the coord/web traffic A-alias Route53 records at a time — otherwise the
# second `terraform apply` would repoint live traffic at the new region's ALB
# before it's ready. Phase 1 stands up the eu env with this false: it creates
# its own ALB + ACM cert (validation CNAMEs are shared/idempotent via
# allow_overwrite) but leaves the traffic records pointing at us-east-1. At
# cutover, flip the eu env to true (allow_overwrite lets it take over the
# records the us-east-1 env created) and the us-east-1 env to false.
variable "manage_traffic_dns" {
  type        = bool
  default     = true
  description = "When true (default, us-east-1 env), create the coord/web traffic A-alias Route53 records pointing at THIS env's ALB. Set false for a parallel stand-up (eu-central-1 env in Phase 1) so the ALB + ACM cert are created but traffic DNS still points at the other region's ALB until an explicit cutover flips this to true."
}

locals {
  fqdn     = "${var.coord_subdomain}.${var.domain_name}"
  web_fqdn = "${var.web_subdomain}.${var.domain_name}"
}

# ─── ACM cert (DNS-validated against the existing Route53 zone) ─────────

resource "aws_acm_certificate" "coord" {
  domain_name               = local.fqdn
  subject_alternative_names = [local.web_fqdn]
  validation_method         = "DNS"

  tags = { Name = "qontinui-${var.environment}-ingress" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = {
    for dvo in aws_acm_certificate.coord.domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  }

  zone_id         = var.route53_zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = 60
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "coord" {
  certificate_arn         = aws_acm_certificate.coord.arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation : r.fqdn]
}

# ─── ALB ────────────────────────────────────────────────────────────────

resource "aws_lb" "main" {
  name               = "qontinui-${var.environment}"
  load_balancer_type = "application"
  security_groups    = [var.alb_sg_id]
  subnets            = var.public_subnet_ids

  idle_timeout = 180 # Long audit calls: web backend waits up to 90s for coord's auditor task (STARTER_PROFILE_WAIT_SECS=60s); 180s gives 90s of slack so the ALB doesn't tear the connection down before the backend can respond and emit a CORS-headered error. WebSockets stay alive via app-level pings independently.

  tags = { Name = "qontinui-${var.environment}-alb" }
}

resource "aws_lb_listener" "redirect_http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.coord.certificate_arn

  # Default → coord. Web is host-routed via the listener rule below.
  default_action {
    type             = "forward"
    target_group_arn = var.coord_target_group
  }
}

# Host-based routing: web.<domain> → web target group. coord stays the
# listener default (no rule needed for it). Unconditional now that web
# is live; the prior count-guard (`web_target_group == ""`) had to go
# because the composition root passes module.web.target_group_arn which
# is "known after apply" — count can't depend on that. Plan B if web is
# ever re-deferred: a literal `enabled` bool input + count = enabled.
resource "aws_lb_listener_rule" "web" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = var.web_target_group
  }

  condition {
    host_header {
      values = [local.web_fqdn]
    }
  }
}

# ─── coord /metrics gate ────────────────────────────────────────────────
#
# The HTTPS listener's DEFAULT action forwards to coord, so without these
# rules coord's full Prometheus surface (/metrics) is open to the internet.
# Two path-pattern rules close that (plan
# 2026-06-09-mode-c-graduation-unblock-and-observability, WS5a):
#
#   110  /metrics + X-Metrics-Token == <token>  → forward to coord TG
#   111  /metrics (anything else)               → fixed 403
#
# Path-only (no host condition) on purpose: coord is reachable via every
# hostname that isn't claimed by a web host rule (coord.<domain>, the raw
# ALB DNS name, plus out-of-band production aliases like coord.qontinui.io
# added during the staging→production domain transition). Gating by path
# after the web host rules (priorities 90/100) covers all of them; web
# hosts are matched earlier and keep their normal routing.
resource "aws_lb_listener_rule" "coord_metrics_authed" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 110

  action {
    type             = "forward"
    target_group_arn = var.coord_target_group
  }

  condition {
    path_pattern {
      values = ["/metrics", "/metrics/*"]
    }
  }

  condition {
    http_header {
      http_header_name = "X-Metrics-Token"
      values           = [var.metrics_token]
    }
  }
}

resource "aws_lb_listener_rule" "coord_metrics_deny" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 111

  action {
    type = "fixed-response"

    fixed_response {
      content_type = "text/plain"
      message_body = "forbidden"
      status_code  = "403"
    }
  }

  condition {
    path_pattern {
      values = ["/metrics", "/metrics/*"]
    }
  }
}

# Operator/agent retrieval point for the metrics token (mirrors the
# coord-module Secrets Manager idiom for shared inline secrets).
resource "aws_secretsmanager_secret" "metrics_token" {
  name        = "qontinui/${var.environment}/coord/metrics_token"
  description = "X-Metrics-Token header value gating the ALB coord /metrics route (${var.environment}). Send it verbatim: curl -H \"X-Metrics-Token: $(aws secretsmanager get-secret-value ...)\" https://coord.<domain>/metrics"
}

resource "aws_secretsmanager_secret_version" "metrics_token" {
  secret_id     = aws_secretsmanager_secret.metrics_token.id
  secret_string = var.metrics_token
}

# ─── DNS ────────────────────────────────────────────────────────────────

resource "aws_route53_record" "coord" {
  count           = var.manage_traffic_dns ? 1 : 0
  zone_id         = var.route53_zone_id
  name            = local.fqdn
  type            = "A"
  allow_overwrite = true

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "web" {
  count           = var.manage_traffic_dns ? 1 : 0
  zone_id         = var.route53_zone_id
  name            = local.web_fqdn
  type            = "A"
  allow_overwrite = true

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "alb_dns_name" { value = aws_lb.main.dns_name }
output "alb_arn_suffix" { value = aws_lb.main.arn_suffix }
output "fqdn" { value = local.fqdn }
output "web_fqdn" { value = local.web_fqdn }
