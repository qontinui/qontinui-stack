"""Test fixture with the SHAPE of qontinui-web's coordrls_01 role block.

The production role step loads the real revision file from a qontinui-web
checkout at production's head (``roles.load_role_block``); this fixture gives
the tests the same three names (``ROLES``, ``create_role_sql``,
``self_grant_sql``) with the same semantics: idempotent NOLOGIN NOINHERIT
NOBYPASSRLS roles, and the migrating role granting ITSELF the SET option.
"""
from alembic import op  # noqa: F401  (the real revision imports it; the loader stubs it)

ROLES = ("coord_tenant", "coord_tenant_preflight")


def create_role_sql(role: str) -> str:
    return f"""
        DO $$
        BEGIN
            BEGIN
                CREATE ROLE {role} NOLOGIN NOINHERIT NOBYPASSRLS;
            EXCEPTION WHEN duplicate_object OR unique_violation THEN
                RAISE NOTICE 'role {role} already exists';
            END;
        END
        $$
    """


def self_grant_sql(role: str) -> str:
    return f"""
        DO $$
        BEGIN
            IF NOT pg_has_role(current_user, '{role}', 'SET') THEN
                EXECUTE format('GRANT {role} TO %I WITH INHERIT FALSE, SET TRUE', current_user);
            END IF;
        END
        $$
    """
