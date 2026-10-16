# Adopt the hand-built NATS JetStream stack into module.nats.
#
# Plan 2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication,
# Phase 1c (decision D13). Every resource below already exists in production;
# none is created. The gate is a `terraform plan` that shows
#   16 to import, 0 to add, 0 to change, 0 to destroy
# for module.nats. A plan that shows anything else for module.nats is a real
# change to production NATS and must not be applied as part of this import.
#
# Applying this is an OPERATOR step (it writes production tfstate):
#   terraform plan  -target=module.nats -out=nats-import.tfplan
#   terraform apply nats-import.tfplan
# then `terraform state list module.nats` shows the 16 addresses. Once that
# apply has landed these blocks are inert (an import of an address already in
# state is a no-op) and may be deleted in a follow-up.
#
# The ids are AWS resource ids, not secrets. Where a module output already
# carries part of an id (the VPC, coord's SG), it is used instead of a literal.

locals {
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
  to = module.nats.aws_service_discovery_private_dns_namespace.main
  # The provider cannot read a private namespace's VPC back, so the id carries it.
  id = "${local.nats_import_ids.namespace}:${module.network.vpc_id}"
}

import {
  to = module.nats.aws_service_discovery_service.nats
  id = local.nats_import_ids.discovery_service
}

import {
  to = module.nats.aws_security_group.nats
  id = local.nats_import_ids.task_sg
}

import {
  to = module.nats.aws_security_group_rule.nats_client_from_coord
  id = "${local.nats_import_ids.task_sg}_ingress_tcp_4222_4222_${module.network.client_sg_id}"
}

import {
  to = module.nats.aws_security_group_rule.nats_monitor_from_coord
  id = "${local.nats_import_ids.task_sg}_ingress_tcp_8222_8222_${module.network.client_sg_id}"
}

import {
  to = module.nats.aws_security_group.efs
  id = local.nats_import_ids.efs_sg
}

import {
  to = module.nats.aws_security_group_rule.efs_nfs_from_nats
  id = "${local.nats_import_ids.efs_sg}_ingress_tcp_2049_2049_${local.nats_import_ids.task_sg}"
}

import {
  to = module.nats.aws_efs_file_system.jetstream
  id = local.nats_import_ids.file_system
}

import {
  to = module.nats.aws_efs_mount_target.jetstream[0]
  id = local.nats_import_ids.mount_targets[0]
}

import {
  to = module.nats.aws_efs_mount_target.jetstream[1]
  id = local.nats_import_ids.mount_targets[1]
}

import {
  to = module.nats.aws_efs_access_point.jetstream
  id = local.nats_import_ids.access_point
}

import {
  to = module.nats.aws_iam_role.task
  id = local.nats_import_ids.task_role
}

import {
  to = module.nats.aws_iam_role_policy.efs_access
  id = "${local.nats_import_ids.task_role}:efs-jetstream-access"
}

import {
  to = module.nats.aws_cloudwatch_log_group.nats
  id = local.nats_import_ids.log_group
}

import {
  to = module.nats.aws_ecs_task_definition.nats
  id = local.nats_import_ids.task_def
}

import {
  to = module.nats.aws_ecs_service.nats
  id = local.nats_import_ids.ecs_service_id
}
