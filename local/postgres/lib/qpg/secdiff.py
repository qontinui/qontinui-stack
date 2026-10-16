"""B3: the security catalog diff, publisher against subscriber, must be empty.

Logical replication and ``pg_dump --no-owner`` carry none of r10's roles,
memberships or ownership, so these are compared explicitly:

* ``pg_roles`` attributes of the r10 roles and of the application role itself
  (``rolsuper``, ``rolbypassrls``, ``rolcreaterole``, ``rolinherit``,
  ``rolcanlogin``, ``rolreplication``). A local ``qontinui_user`` with BYPASSRLS
  or superuser would pass every other section while voiding the design.
* ``pg_auth_members`` rows among those roles, with ``admin_option``,
  ``inherit_option`` and ``set_option`` (grantor excluded: RDS grants as its
  own bootstrap role). ``rds_*`` memberships are excluded by name.
* ``pg_policies`` per published table: name, permissive, command, roles, qual,
  with_check.
* ``relrowsecurity`` / ``relforcerowsecurity`` and the owner of every table,
  view and sequence in the published schemas.
* Table, sequence, view and schema ACLs after ``aclexplode`` — a NULL ACL is
  expanded to its ``acldefault`` so an explicit owner-only ACL and the default
  compare equal, as they are.
* ``pg_default_acl``.
* the ``security_invoker`` option of views.

Grantees in ``[secdiff] ignore_roles`` (the replication role, which exists only
on RDS) and ``rds_*`` are excluded everywhere.

Two subscriber-only assertions (B2) are reported with the diff: every table in
a published schema is owned by the application role, and no table anywhere has
``relforcerowsecurity``.
"""

from __future__ import annotations

import json

from .config import Config
from .pg import Psql, lit, text_array

R10_ROLES = ("coord_tenant", "coord_tenant_preflight")


def _sections(db: Psql, schemas: list[str], roles: list[str], ignore: list[str]) -> dict[str, set[str]]:
    sch = text_array(schemas)
    rl = text_array(roles)
    ign = text_array(ignore or [""])
    name = "CASE WHEN {0} = 0 THEN 'PUBLIC' ELSE pg_get_userbyid({0}) END"
    keep = f"NOT ({{0}} = ANY({ign}) OR {{0}} LIKE 'rds%')"
    queries = {
        "roles": f"""
            SELECT json_build_array(rolname, rolsuper, rolbypassrls, rolcreaterole, rolinherit,
                                    rolcanlogin, rolreplication)::text
              FROM pg_roles WHERE rolname = ANY({rl})""",
        "memberships": f"""
            SELECT json_build_array(r.rolname, u.rolname, m.admin_option, m.inherit_option, m.set_option)::text
              FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member
             WHERE (r.rolname = ANY({rl}) OR u.rolname = ANY({rl}))
               AND r.rolname NOT LIKE 'rds%' AND u.rolname NOT LIKE 'rds%'""",
        "policies": f"""
            SELECT json_build_array(schemaname, tablename, policyname, permissive,
                                    (SELECT array_agg(x ORDER BY x) FROM unnest(roles) x), cmd, qual, with_check)::text
              FROM pg_policies WHERE schemaname = ANY({sch})""",
        "relations": f"""
            SELECT json_build_array(n.nspname, c.relname, c.relkind, pg_get_userbyid(c.relowner),
                                    c.relrowsecurity, c.relforcerowsecurity)::text
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY({sch}) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')""",
        "relation_acls": f"""
            SELECT json_build_array(n.nspname, c.relname, {name.format('a.grantor')}, {name.format('a.grantee')},
                                    a.privilege_type, a.is_grantable)::text
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                   LATERAL aclexplode(coalesce(c.relacl, acldefault(
                       CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::"char", c.relowner))) a
             WHERE n.nspname = ANY({sch}) AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
               AND {keep.format(name.format('a.grantee'))}""",
        "schema_acls": f"""
            SELECT json_build_array(n.nspname, pg_get_userbyid(n.nspowner), {name.format('a.grantor')},
                                    {name.format('a.grantee')}, a.privilege_type, a.is_grantable)::text
              FROM pg_namespace n,
                   LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
             WHERE n.nspname = ANY({sch}) AND {keep.format(name.format('a.grantee'))}""",
        "default_acls": f"""
            SELECT json_build_array(pg_get_userbyid(d.defaclrole), n.nspname, d.defaclobjtype,
                                    {name.format('a.grantee')}, a.privilege_type, a.is_grantable)::text
              FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid = d.defaclnamespace,
                   LATERAL aclexplode(d.defaclacl) a
             WHERE (n.nspname = ANY({sch}) OR d.defaclnamespace = 0)
               AND {keep.format(name.format('a.grantee'))}
               AND pg_get_userbyid(d.defaclrole) NOT LIKE 'rds%'""",
        "views": f"""
            SELECT json_build_array(n.nspname, c.relname,
                   coalesce((SELECT option_value FROM pg_options_to_table(c.reloptions)
                              WHERE option_name = 'security_invoker'), 'false'))::text
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY({sch}) AND c.relkind = 'v'""",
    }
    return {k: {r[0] for r in db.rows(q)} for k, q in queries.items()}


def diff(left: Psql, right: Psql, schemas: list[str], app_role: str, ignore: list[str]) -> list[dict]:
    roles = list(R10_ROLES) + [app_role]
    a = _sections(left, schemas, roles, ignore)
    b = _sections(right, schemas, roles, ignore)
    out = []
    for section in a:
        for row in sorted(a[section] - b[section]):
            out.append({"section": section, "only_on": left.label, "row": json.loads(row)})
        for row in sorted(b[section] - a[section]):
            out.append({"section": section, "only_on": right.label, "row": json.loads(row)})
    return out


def owner_premise(sub: Psql, schemas: list[str], app_role: str) -> list[dict]:
    """B2 on the subscriber: owner of every published table; no FORCE anywhere."""
    rows = sub.rows(
        "SELECT n.nspname || '.' || c.relname, pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = ANY({text_array(schemas)}) AND c.relkind IN ('r', 'p') "
        f"AND pg_get_userbyid(c.relowner) <> {lit(app_role)} ORDER BY 1")
    out = [{"assertion": "owner", "table": r[0], "owner": r[1], "expected": app_role} for r in rows]
    forced = sub.rows(
        "SELECT n.nspname || '.' || c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relforcerowsecurity ORDER BY 1")
    out += [{"assertion": "relforcerowsecurity", "table": r[0]} for r in forced]
    return out


def run(cfg: Config) -> dict:
    ignore = [r.strip() for r in (cfg.get("secdiff", "ignore_roles") or "").split(",") if r.strip()]
    pub = cfg.publisher("qontinui-pg-secdiff")
    sub = cfg.sub_admin("qontinui-pg-secdiff")
    differences = diff(pub, sub, cfg.published_schemas, cfg.app_role, ignore)
    premise = owner_premise(sub, cfg.published_schemas, cfg.app_role)
    return {"differences": differences, "owner_premise_violations": premise,
            "empty": not differences and not premise}
