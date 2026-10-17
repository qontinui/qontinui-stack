# Replication tunnel, option (e): plain WireGuard on one t4g.nano EC2 instance in
# a public subnet. The network path between the private RDS instance and the
# local Postgres subscriber that replicates it (plan
# 2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication, design D2(e),
# chosen over D2(a) by "OD1-OD4 resolved 2026-10-17", OD3(ii)). The D2(a)
# Tailscale module (aws/modules/replication-tunnel) stays in the tree, disabled,
# as the upgrade path; no data moves when switching between them.
#
# DISABLED BY DEFAULT. Every resource and data source below is counted on
# var.enabled, so the module plans zero resources and makes zero AWS reads until
# a composition turns it on. aws/staging calls it with enabled = false.
#
# What it builds when enabled:
#
#   local subscriber ──WireGuard UDP (the subscriber dials and keeps alive)──>
#     instance (public subnet, public IPv4 / optional EIP)
#     peer -> <server_tunnel_address>:5432 ─DNAT─> RDS's current address (/32)
#     :5432, masqueraded from the instance's private address. The one rule this
#     module adds to the RDS group admits :5432 from the instance's group.
#
# Security surface, and how each piece is closed:
#   - Inbound: the security group admits ONLY UDP <wg_port> (51820), from
#     anywhere, because the subscriber's home address changes. WireGuard answers
#     nothing to a packet that does not authenticate against the one configured
#     peer key, so the port does not even reveal that a listener is there.
#   - Outbound: only :5432 to the RDS security group, plus 80/443 for package
#     updates. DNS (Route 53 Resolver), NTP and IMDS are link-local services that
#     security groups do not filter.
#   - No key pair, no SSH ingress, sshd disabled at first boot, no instance
#     profile at all (so no SSM agent registration and no AWS credentials on the
#     box), IMDSv2 required with a hop limit of 1.
#   - Fail-closed at boot: a static base ruleset (nftables.service, ordered
#     before wg-quick@wg0) drops all forwarding, and ip_forward stays 0 until
#     the firewall script has loaded the peer-to-RDS ruleset.
#   - Forwarding is filtered on the instance with nftables: the peer's tunnel
#     address may reach the RDS /32 on tcp/5432 and nothing else; the host itself
#     offers the peer nothing. The RDS address is re-resolved every 5 minutes,
#     because a single-AZ instance's private IP moves on host replacement.
#   - unattended-upgrades applies Debian security updates daily and reboots at
#     04:30 UTC when a kernel update needs it.
#
# Keys — no secret in Terraform state, in this repo, or in any agent's hands:
#   - The instance generates its WireGuard private key at first boot
#     (user-data.sh.tftpl). It never leaves the instance.
#   - The instance PUBLISHES its public key to the EC2 console output, re-printed
#     hourly so it stays in the latest buffer. Read it with
#       aws ec2 get-console-output --instance-id <instance_id> --latest \
#         --output text | grep QONTINUI_WG_PUBLIC_KEY
#     That needs only ec2:GetConsoleOutput; the console was chosen over an SSM
#     parameter because writing one would need an instance profile, and this
#     instance deliberately has none.
#   - The subscriber's PUBLIC key comes in through var.peer_public_key.
#   - Rebuilding the instance (replacement) generates a NEW key pair: re-read the
#     console and update the subscriber's [Peer] PublicKey afterwards.
#
# Not here yet: the reverse path (RDS -> local subscriber) that D8's reverse
# subscription needs. Unlike a Fargate task, this instance can host it with a
# DNAT on the VPC side; it is added with the Phase 7 rehearsal, not before.
#
# Enabling (Phase 3):
#   1. Generate the subscriber's key pair on the subscriber; set
#      peer_public_key to its public half, and enabled = true. Targeted apply.
#      NOTE: peer_public_key, db_endpoint_address, wg_port and tunnel_cidr are
#      rendered into user_data, and user_data_replace_on_change is on, so
#      changing ANY of them later REPLACES the instance, which generates a NEW
#      server key pair (and, without the Elastic IP, a new public IP). Redo
#      step 2 after every such apply.
#   2. Read the instance's public key from the console (above) and its address
#      from the public_ip output; write the subscriber's wg0.conf with
#      Endpoint = <public_ip>:<wg_port>, AllowedIPs = <server_tunnel_address>/32
#      and PersistentKeepalive = 25.
#   3. The subscription connects with hostaddr=<server_tunnel_address>
#      host=<RDS endpoint name> sslmode=verify-full, so TLS still verifies the
#      RDS certificate end to end through the tunnel.
# Turning it off is enabled = false, which deletes everything here.
#
# Cost (D17), us-east-1 on-demand: t4g.nano about $3.07/month plus an 8 GB gp3
# root volume about $0.64/month (about $3-4/month for the instance), plus $3.65
# for the public IPv4 address (an auto-assigned public IP and an attached Elastic
# IP are billed the same). The optional Elastic IP (use_elastic_ip, on by
# default) adds no cost while attached; it keeps the subscriber's Endpoint stable
# across an instance replacement. About $7.4/month in total while enabled. No NAT
# gateway: the instance is in a public subnet.

variable "environment" { type = string }

variable "enabled" {
  description = "Create the WireGuard replication tunnel. false (the default) plans zero resources and makes no AWS reads."
  type        = bool
  default     = false
}

variable "vpc_id" { type = string }

variable "public_subnet_ids" {
  description = "Public subnets; the instance runs in the first one, with a public IPv4 address."
  type        = list(string)
}

variable "db_security_group_id" {
  description = "Security group attached to RDS. Gets an ingress rule admitting 5432 from the tunnel group, and is the only egress destination on 5432. In aws/staging this is the data-plane group, which ElastiCache shares."
  type        = string
}

variable "db_endpoint_address" {
  description = "RDS endpoint hostname. The instance resolves it inside the VPC to the current private /32 it NATs to. Required when enabled."
  type        = string
  default     = null
}

# A WireGuard PUBLIC key (32 bytes, base64): not a secret, by construction.
variable "peer_public_key" {
  description = "WireGuard public key of the local subscriber. Required when enabled."
  type        = string
  default     = null

  validation {
    condition     = var.peer_public_key == null || can(regex("^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$", var.peer_public_key))
    error_message = "peer_public_key must be a base64 WireGuard public key (44 characters ending in '=')."
  }
}

variable "wg_port" {
  description = "UDP port WireGuard listens on."
  type        = number
  default     = 51820
}

variable "tunnel_cidr" {
  description = "Tunnel-internal network. Host 1 is the instance, host 2 the subscriber. Must not overlap the VPC or the subscriber's LAN."
  type        = string
  default     = "10.254.254.0/30"

  validation {
    condition     = can(cidrhost(var.tunnel_cidr, 2)) && tonumber(split("/", var.tunnel_cidr)[1]) <= 30
    error_message = "tunnel_cidr must be an IPv4 CIDR of /30 or larger."
  }
}

variable "instance_type" {
  description = "EC2 instance type (arm64)."
  type        = string
  default     = "t4g.nano"
}

variable "use_elastic_ip" {
  description = "Attach an Elastic IP so the subscriber's Endpoint survives an instance replacement. Billed the same $3.65/month as the auto-assigned public IPv4 while attached."
  type        = bool
  default     = true
}

locals {
  name                  = "qontinui-${var.environment}-replication-wg"
  enabled_count         = var.enabled ? 1 : 0
  server_tunnel_address = cidrhost(var.tunnel_cidr, 1)
  peer_tunnel_address   = cidrhost(var.tunnel_cidr, 2)
}

# Current Debian 13 (trixie) arm64 image from Debian's own AWS account. The
# required-when-enabled inputs are checked here, on the first object the module
# reads, so a missing one fails the plan with its own message. (A cross-variable
# `validation` would need Terraform >= 1.9; the stack requires >= 1.7.)
data "aws_ami" "debian" {
  count       = local.enabled_count
  most_recent = true
  owners      = ["136693071363"] # Debian

  filter {
    name   = "name"
    values = ["debian-13-arm64-*"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }

  lifecycle {
    precondition {
      condition     = var.peer_public_key != null
      error_message = "peer_public_key (the local subscriber's WireGuard public key) is required when the WireGuard replication tunnel is enabled."
    }
    precondition {
      condition     = var.db_endpoint_address != null && var.db_endpoint_address != ""
      error_message = "db_endpoint_address is required when the WireGuard replication tunnel is enabled."
    }
  }
}

# ─── Security group ─────────────────────────────────────────────────────

resource "aws_security_group" "tunnel" {
  count       = local.enabled_count
  name        = local.name
  description = "Replication WireGuard tunnel; inbound UDP ${var.wg_port} only"
  vpc_id      = var.vpc_id

  ingress {
    description = "WireGuard from the subscriber (its address changes, so any source; unauthenticated packets get no answer)"
    from_port   = var.wg_port
    to_port     = var.wg_port
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description     = "Forwarded peer traffic to RDS Postgres"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.db_security_group_id]
  }

  egress {
    description = "Package updates (HTTPS)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Package updates (HTTP; apt verifies signatures)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = local.name }
}

resource "aws_security_group_rule" "db_pg_from_tunnel" {
  count                    = local.enabled_count
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.tunnel[0].id
  security_group_id        = var.db_security_group_id
  description              = "Replication WireGuard tunnel to RDS Postgres"
}

# ─── Instance ───────────────────────────────────────────────────────────

resource "aws_instance" "tunnel" {
  count                       = local.enabled_count
  ami                         = data.aws_ami.debian[0].id
  instance_type               = var.instance_type
  subnet_id                   = var.public_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.tunnel[0].id]
  associate_public_ip_address = true # package installs at first boot need it before an EIP attaches

  # Forwards (and DNATs) traffic that is neither from nor to its own address.
  source_dest_check = false

  # No key_name and no iam_instance_profile, on purpose (module header).

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 8
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user-data.sh.tftpl", {
    wg_port               = var.wg_port
    server_tunnel_address = local.server_tunnel_address
    peer_tunnel_address   = local.peer_tunnel_address
    tunnel_prefix_length  = split("/", var.tunnel_cidr)[1]
    peer_public_key       = coalesce(var.peer_public_key, "unset")
    db_endpoint_address   = coalesce(var.db_endpoint_address, "unset")
  })
  # The box is rebuilt from code: a changed user_data replaces it rather than
  # editing a running host (cloud-init would not re-run it anyway).
  user_data_replace_on_change = true

  lifecycle {
    # A newly published Debian image must not replace a working tunnel (and
    # rotate its key) on an unrelated apply. Rebuild deliberately with
    # -replace=module.<name>.aws_instance.tunnel[0].
    ignore_changes = [ami]
  }

  tags = { Name = local.name }
}

resource "aws_eip" "tunnel" {
  count    = var.enabled && var.use_elastic_ip ? 1 : 0
  domain   = "vpc"
  instance = aws_instance.tunnel[0].id
  tags     = { Name = local.name }
}

# ─── Outputs ────────────────────────────────────────────────────────────

output "public_ip" {
  description = "Address the subscriber dials (Endpoint = <public_ip>:<wg_port>): the Elastic IP when attached, else the instance's public IPv4."
  value       = var.enabled ? (var.use_elastic_ip ? aws_eip.tunnel[0].public_ip : aws_instance.tunnel[0].public_ip) : null
}

output "instance_id" {
  value = var.enabled ? aws_instance.tunnel[0].id : null
}

output "public_key_source" {
  description = "Where the instance's WireGuard public key is read from (generated on the instance; never in state)."
  value       = var.enabled ? "aws ec2 get-console-output --instance-id ${aws_instance.tunnel[0].id} --latest --output text | grep QONTINUI_WG_PUBLIC_KEY" : null
}

output "server_tunnel_address" {
  description = "Tunnel address the subscriber connects to on :5432 (DNATed to RDS)."
  value       = local.server_tunnel_address
}

output "security_group_id" {
  value = var.enabled ? aws_security_group.tunnel[0].id : null
}

output "wg_port" {
  value = var.wg_port
}
