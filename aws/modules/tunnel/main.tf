# Public ingress: ALB + ACM cert (DNS-validated) + Route53 alias.
#
# This is the AWS equivalent of "Cloudflare Tunnel" in dev. The same
# logical role — terminating TLS for the public coord URL and routing
# to the right backend — but native to AWS.

variable "environment"         { type = string }
variable "vpc_id"              { type = string }
variable "public_subnet_ids"   { type = list(string) }
variable "alb_sg_id"           { type = string }
variable "domain_name"         { type = string }
variable "route53_zone_id"     { type = string }
variable "coord_subdomain"     { type = string }
variable "coord_target_group"  { type = string }

locals {
  fqdn = "${var.coord_subdomain}.${var.domain_name}"
}

# ─── ACM cert (DNS-validated against the existing Route53 zone) ─────────

resource "aws_acm_certificate" "coord" {
  domain_name       = local.fqdn
  validation_method = "DNS"

  tags = { Name = "qontinui-${var.environment}-coord" }

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

  zone_id = var.route53_zone_id
  name    = each.value.name
  type    = each.value.type
  records = [each.value.record]
  ttl     = 60
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

  idle_timeout = 60   # WebSockets stay alive via app-level pings; 60s is fine

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

  default_action {
    type             = "forward"
    target_group_arn = var.coord_target_group
  }
}

# ─── DNS ────────────────────────────────────────────────────────────────

resource "aws_route53_record" "coord" {
  zone_id = var.route53_zone_id
  name    = local.fqdn
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "alb_dns_name" { value = aws_lb.main.dns_name }
output "fqdn"         { value = local.fqdn }
