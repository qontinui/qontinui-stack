# Adopt the hand-built NATS JetStream stack into module.nats.
#
# Plan 2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication,
# Phase 1c (decision D13). Every resource below already exists in production;
# none is created.
#
# OFF BY DEFAULT, SO MERGING IS A NO-OP. Root-module import blocks run on ANY
# apply, so an unconditional block would let the next untargeted
# `terraform apply` by anyone write these 16 resources into production tfstate
# before operator gate 3fd552ff-f353-4b34-b1b7-b5e61106f39a authorizes it.
# Both module.nats (count) and every import block below (for_each) are gated
# on var.nats_under_code, default false: with the default a plan contains no
# nats resource and no import at all.
#
# OPERATOR STEPS (each writes nothing until the apply; the apply writes
# production tfstate and only that):
#   1. In aws/staging, set `nats_under_code = true` in production.auto.tfvars
#      (or pass -var nats_under_code=true to both commands below).
#   2. terraform plan -var nats_under_code=true -target=module.nats -out=nats-import.tfplan
#      The gate: it must read "16 to import, 0 to add, 0 to change, 0 to
#      destroy". Anything else for module.nats is a real change to production
#      NATS — do not apply it.
#   3. terraform apply nats-import.tfplan
#   4. terraform state list module.nats  -> the 16 module.nats[0] addresses.
# From then on the variable must STAY true: setting it back to false would
# plan the destruction of the whole NATS stack (the EFS's prevent_destroy
# makes that plan error rather than run). Once the import has landed these
# blocks are inert and may be deleted, and the variable flipped to a
# committed `true`, in a follow-up.
#
# The ids are AWS resource ids, not secrets. Where a module output already
# carries part of an id (the VPC, coord's SG), it is used instead of a literal.

variable "nats_under_code" {
  description = "Bring the hand-built NATS JetStream stack under module.nats (Phase 1c import). Default false so that merging the module and its import blocks changes nothing; the operator sets it true for the gated import apply, and it must stay true afterwards."
  type        = bool
  default     = false
}

locals {
  # One-element set when enabled, empty otherwise: the for_each of every import.
  nats_import_enabled = var.nats_under_code ? toset(["nats"]) : toset([])

  nats_import_ids = {
    namespace         = "ns-e2witsivqy5zd2u5"
    discovery_service = "srv-fguwnb2txqq7i5rx"
    task_sg           = "sg-0bccde2ab9d86a134"
    efs_sg            = "sg-0d9aa1ccb55156971"
    file_system       = "fs-0fa99b01b8c7a3663"
    access_point      = "fsap-07a5cd0192ce1f957"
    # Ordered as module.network.private_subnet_ids: [0] is us-east-1a, [1] us-east-1b.
    mount_targets  = ["fsmt-0511de7df72840128", "fsmt-0a06ee4464cf2eeff"]
    task_role      = "qontinui-staging-nats-task"
    log_group      = "/ecs/qontinui-staging/nats"
    task_def       = "arn:aws:ecs:us-east-1:047719635665:task-definition/qontinui-staging-nats:1"
    ecs_service_id = "qontinui-staging/nats"
  }
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_service_discovery_private_dns_namespace.main
  # The provider cannot read a private namespace's VPC back, so the id carries it.
  id = "${local.nats_import_ids.namespace}:${module.network.vpc_id}"
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_service_discovery_service.nats
  id       = local.nats_import_ids.discovery_service
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_security_group.nats
  id       = local.nats_import_ids.task_sg
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_security_group_rule.nats_client_from_coord
  id       = "${local.nats_import_ids.task_sg}_ingress_tcp_4222_4222_${module.network.client_sg_id}"
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_security_group_rule.nats_monitor_from_coord
  id       = "${local.nats_import_ids.task_sg}_ingress_tcp_8222_8222_${module.network.client_sg_id}"
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_security_group.efs
  id       = local.nats_import_ids.efs_sg
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_security_group_rule.efs_nfs_from_nats
  id       = "${local.nats_import_ids.efs_sg}_ingress_tcp_2049_2049_${local.nats_import_ids.task_sg}"
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_efs_file_system.jetstream
  id       = local.nats_import_ids.file_system
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_efs_mount_target.jetstream[0]
  id       = local.nats_import_ids.mount_targets[0]
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_efs_mount_target.jetstream[1]
  id       = local.nats_import_ids.mount_targets[1]
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_efs_access_point.jetstream
  id       = local.nats_import_ids.access_point
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_iam_role.task
  id       = local.nats_import_ids.task_role
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_iam_role_policy.efs_access
  id       = "${local.nats_import_ids.task_role}:efs-jetstream-access"
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_cloudwatch_log_group.nats
  id       = local.nats_import_ids.log_group
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_ecs_task_definition.nats
  id       = local.nats_import_ids.task_def
}

import {
  for_each = local.nats_import_enabled
  to       = module.nats[0].aws_ecs_service.nats
  id       = local.nats_import_ids.ecs_service_id
}
