# terraform test for the WireGuard replication tunnel, with a MOCKED AWS
# provider: no credentials, no AWS calls. CI does NOT run this file (CI runs
# fmt/validate on aws/staging and the pytest source guards in
# scripts/tests/test_replication_tunnel_wireguard.py). Run by hand:
#   cd aws/modules/replication-tunnel-wireguard
#   terraform init -backend=false && terraform test
# then delete the .terraform/ and .terraform.lock.hcl that init leaves here.

mock_provider "aws" {
  mock_data "aws_ami" {
    defaults = { id = "ami-123" }
  }
}

variables {
  environment          = "test"
  vpc_id               = "vpc-123"
  public_subnet_ids    = ["subnet-1"]
  db_security_group_id = "sg-db"
  db_endpoint_address  = "db.example.internal"
}

run "disabled_creates_nothing" {
  command = plan
  assert {
    condition     = length(aws_instance.tunnel) + length(aws_security_group.tunnel) + length(aws_security_group_rule.db_pg_from_tunnel) + length(aws_eip.tunnel) + length(data.aws_ami.debian) == 0
    error_message = "disabled module plans resources or reads"
  }
}

run "enabled_shape" {
  command = apply
  variables {
    enabled         = true
    peer_public_key = "xTIBA5rboUvnH4htodjb6e697QjLERt1NAB4mZqp8Dg="
  }
  assert {
    condition     = aws_instance.tunnel[0].metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 / no profile / no key"
  }
  assert {
    condition     = length(aws_security_group.tunnel[0].ingress) == 1 && one(aws_security_group.tunnel[0].ingress).protocol == "udp" && one(aws_security_group.tunnel[0].ingress).from_port == 51820
    error_message = "ingress must be UDP 51820 only"
  }
  assert {
    condition     = toset([for e in aws_security_group.tunnel[0].egress : e.to_port]) == toset([5432, 443, 80])
    error_message = "egress must be 5432/443/80 only"
  }
  assert {
    condition     = strcontains(aws_instance.tunnel[0].user_data, "wg genkey") && !strcontains(aws_instance.tunnel[0].user_data, "PrivateKey =")
    error_message = "key must be generated on the instance"
  }
  assert {
    condition     = strcontains(aws_instance.tunnel[0].user_data, "chain forward {\n    type filter hook forward priority filter; policy drop;\n  }\n}\nEOF")
    error_message = "base ruleset in /etc/nftables.conf must drop all forwarding"
  }
  assert {
    condition     = strcontains(aws_instance.tunnel[0].user_data, "[Unit]\nBefore=wg-quick@wg0.service") && strcontains(aws_instance.tunnel[0].user_data, "Requires=nftables.service\nAfter=nftables.service") && strcontains(aws_instance.tunnel[0].user_data, "systemctl enable --now nftables.service")
    error_message = "nftables.service must be enabled and ordered before (and required by) wg-quick@wg0"
  }
  assert {
    condition     = !strcontains(aws_instance.tunnel[0].user_data, "net.ipv4.ip_forward = 1") && strcontains(aws_instance.tunnel[0].user_data, "NFT\n# Only now, with the peer-to-RDS ruleset in place, does the host forward.\nsysctl -q -w net.ipv4.ip_forward=1")
    error_message = "ip_forward must not be persisted on; only the firewall script enables it, after nft -f"
  }
  assert {
    condition     = length(aws_eip.tunnel) == 1 && aws_security_group_rule.db_pg_from_tunnel[0].from_port == 5432
    error_message = "eip / rds rule"
  }
}

run "enabled_requires_peer_key" {
  command = plan
  variables { enabled = true }
  expect_failures = [data.aws_ami.debian]
}

run "rejects_bad_key" {
  command = plan
  variables {
    enabled         = true
    peer_public_key = "not-a-key"
  }
  expect_failures = [var.peer_public_key]
}
