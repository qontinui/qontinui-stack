# Auto-loaded inputs for THIS composition root (aws/staging — which IS
# production; see main.tf's header). Loaded by terraform automatically because
# of the `.auto.tfvars` suffix, so `terraform plan` and `terraform apply` run
# here with no tfvars argument and no -var. That is the property plan
# 2026-08-04-terraform-secretless-input-surface shipped, and this file is what
# keeps it true for a variable that is deliberately required-and-defaultless.
#
# SECRET-FREE BY CONSTRUCTION. This file is checked in (aws/.gitignore carries an
# explicit negation for it), so nothing that is a credential may appear here.
# Secrets reach terraform as Secrets Manager / SSM data sources instead.
#
# Copying this root to a NEW environment? Change the value below — it grants a
# superuser. Leaving it seeds this operator into your environment.

# Bootstrap superuser for qontinui-web's init_db seed. Required, no default, and
# validated lowercase at both module and root level (a case variant crashes web
# startup — see aws/staging/variables.tf). An address, not a credential; it is
# already committed verbatim in terraform.tfvars.example in this public repo.
first_superuser_email = "josh@qontinui.io"
