"""B1: roles first, before the schema load.

1. Create the login role coord and web connect as, with production's NAME and
   SHAPE: ``qontinui_user``, ``LOGIN CREATEROLE NOBYPASSRLS``, not superuser.
   Its password is generated on this host and only its SCRAM verifier is used
   (``credential``). An existing role in any other shape is refused.
2. Run r10's role block AS THAT ROLE. The block is not retyped here: it is
   taken from the ``coordrls_01`` revision file of the qontinui-web checkout at
   production's alembic head (``create_role_sql`` and ``self_grant_sql`` for
   each role in ``ROLES``). Run as a superuser, its
   ``GRANT … TO CURRENT_USER`` would grant the roles to the wrong role.
   If the revision is not applied in production yet, the step is a no-op
   (``--allow-missing``) and the follower creates the roles when it replays the
   revision as ``qontinui_user``.
3. ``check-grantees``: every role named in the published schemas' ACLs,
   policies and default ACLs on the publisher must exist locally. ``rds_*``
   grantees are reported and excluded, never recreated.
"""

from __future__ import annotations

import importlib.util
import sys
import types

from .config import Config
from .pg import Psql, ident, lit, text_array


class RoleError(RuntimeError):
    pass


def load_role_block(revision_file: str) -> list[str]:
    """The role block of ``coordrls_01``'s ``upgrade()``, as statements."""
    stub_alembic = types.ModuleType("alembic")
    stub_alembic.op = types.SimpleNamespace()  # never called: only the SQL builders are used
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = stub_alembic
    try:
        spec = importlib.util.spec_from_file_location("qpg_coordrls_revision", revision_file)
        if spec is None or spec.loader is None:
            raise RoleError(f"cannot load {revision_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("alembic", None)
        else:
            sys.modules["alembic"] = saved
    for name in ("ROLES", "create_role_sql", "self_grant_sql"):
        if not hasattr(module, name):
            raise RoleError(f"{revision_file} has no {name}; it is not the coordrls_01 role block this step expects")
    statements = []
    for role in module.ROLES:
        statements.append(module.create_role_sql(role))
        statements.append(module.self_grant_sql(role))
    return statements


def ensure_app_role(admin: Psql, role: str, verifier: str | None) -> str:
    rows = admin.rows(
        "SELECT rolsuper::text, rolbypassrls::text, rolcreaterole::text, rolcanlogin::text "
        f"FROM pg_roles WHERE rolname = {lit(role)}")
    if not rows:
        if not verifier or not verifier.startswith("SCRAM-SHA-256$"):
            raise RoleError(f"{role} does not exist; pass the SCRAM verifier from `qontinui-pg credential new`")
        admin.execute(f"CREATE ROLE {ident(role)} LOGIN CREATEROLE NOBYPASSRLS NOSUPERUSER "
                      f"PASSWORD {lit(verifier)}")
        return "created"
    sup, bypass, createrole, login = rows[0]
    if sup == "true" or bypass == "true" or createrole != "true" or login != "true":
        raise RoleError(
            f"{role} exists in a shape that is not production's (superuser={sup}, bypassrls={bypass}, "
            f"createrole={createrole}, login={login}); refusing")
    return "exists"


def apply(cfg: Config, revision_file: str | None, verifier: str | None, allow_missing: bool) -> dict:
    admin = cfg.sub_admin("qontinui-pg-roles")
    role = cfg.app_role
    outcome = {"app_role": ensure_app_role(admin, role, verifier)}
    if not revision_file:
        if not allow_missing:
            raise RoleError("no --revision-file given "
                            "(pass --allow-missing when coordrls_01 is not applied in production)")
        outcome["role_block"] = ("skipped: coordrls_01 not applied in production; "
                                 "the follower's replay creates the roles")
        return outcome
    statements = load_role_block(revision_file)
    app = cfg.sub_app("qontinui-pg-roles")
    who = app.scalar("SELECT current_user")
    if who != role:
        raise RoleError(f"the app connection authenticates as {who}, not {role}")
    for stmt in statements:
        app.execute(stmt, single_transaction=False)
    members = admin.rows(
        "SELECT r.rolname, m.set_option::text, m.inherit_option::text FROM pg_auth_members m "
        "JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member "
        f"WHERE u.rolname = {lit(role)} AND m.set_option ORDER BY 1")
    outcome["role_block"] = "applied"
    outcome["set_memberships"] = [{"role": r, "set": s, "inherit": i} for r, s, i in members]
    return outcome


def check_grantees(cfg: Config) -> dict:
    pub = cfg.publisher("qontinui-pg-roles")
    schemas = text_array(cfg.published_schemas)
    rows = pub.rows(f"""
        WITH names AS (
            SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS name
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace,
                   LATERAL aclexplode(c.relacl) a
             WHERE n.nspname = ANY({schemas}) AND c.relacl IS NOT NULL
            UNION
            SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END
              FROM pg_namespace n, LATERAL aclexplode(n.nspacl) a
             WHERE n.nspname = ANY({schemas}) AND n.nspacl IS NOT NULL
            UNION
            SELECT unnest(p.roles)::text FROM pg_policies p WHERE p.schemaname = ANY({schemas})
            UNION
            SELECT pg_get_userbyid(d.defaclrole) FROM pg_default_acl d
              JOIN pg_namespace n ON n.oid = d.defaclnamespace WHERE n.nspname = ANY({schemas})
            UNION
            SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END
              FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace,
                   LATERAL aclexplode(d.defaclacl) a
             WHERE n.nspname = ANY({schemas})
        )
        SELECT DISTINCT name FROM names WHERE name IS NOT NULL ORDER BY 1""")
    names = [r[0] for r in rows]
    rds = [n for n in names if n.startswith("rds")]
    configured = (cfg.get("secdiff", "ignore_roles") or "").split(",")
    ignore = {"PUBLIC", "public"} | {r.strip() for r in configured if r.strip()}
    wanted = [n for n in names if n not in rds and n not in ignore]
    admin = cfg.sub_admin("qontinui-pg-roles")
    rows = admin.rows(f"SELECT rolname FROM pg_roles WHERE rolname = ANY({text_array(wanted or [''])})")
    present = {r[0] for r in rows}
    missing = [n for n in wanted if n not in present]
    return {"grantees": wanted, "missing_locally": missing, "rds_only_stripped": rds,
            "ignored": sorted(ignore & set(names))}
