"""DDL for the follower's own schema on the subscriber (plan D7, A2, B2).

``follower`` lives in the subscriber database but in no published schema, so it
never replicates and never appears in the B3 catalog diff. It is owned by a
NOLOGIN role (``[cluster] follower_role``, default ``qontinui_follower``) that
nothing else holds: the application role has no USAGE on it.

The stall marker (D7 step 1, A2 item 2) is one trigger function used by every
version table:

* ``BEFORE INSERT OR UPDATE … FOR EACH ROW``, set ``ENABLE REPLICA``, so it fires
  only under ``session_replication_role = replica`` — in the apply and
  table-sync workers, never in the follower's own migration session;
* it raises SQLSTATE ``QF001`` when the version a replicated row carries is not
  in ``follower.applied_revisions`` (web) or ``follower.applied_coord_versions``
  (coord lane). The migrator's transaction always writes that row, so apply
  stops at exactly that transaction with none of its DML applied;
* **B2:** under PostgreSQL 16's default ``run_as_owner = false`` the apply
  worker switches to the table OWNER (``qontinui_user``, not a superuser)
  before firing it. That role cannot read ``follower.*``, so the function is
  ``SECURITY DEFINER``, owned by the follower role, with a pinned
  ``search_path``. Without that, every replicated version row — including the
  initial table sync of ``public.alembic_version`` for a revision the follower
  HAS applied — fails with 42501 and stalls apply forever.
"""

from __future__ import annotations

from .pg import ident, lit, qualified

STALL_SQLSTATE = "QF001"
TRIGGER_NAME = "qontinui_follower_stall"


def schema_sql(follower_role: str, app_role: str) -> str:
    fr = ident(follower_role)
    ar = ident(app_role)
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {lit(follower_role)}) THEN
        CREATE ROLE {fr} NOLOGIN NOINHERIT;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS follower AUTHORIZATION {fr};
REVOKE ALL ON SCHEMA follower FROM PUBLIC;

SET ROLE {fr};

CREATE TABLE IF NOT EXISTS follower.applied_revisions (
    version_num text PRIMARY KEY,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    remote_xid  text,
    finish_lsn  pg_lsn,
    via         text NOT NULL
);

CREATE TABLE IF NOT EXISTS follower.applied_coord_versions (
    lane        text NOT NULL,
    version     text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    remote_xid  text,
    finish_lsn  pg_lsn,
    via         text NOT NULL,
    PRIMARY KEY (lane, version)
);

-- Every publisher version row the follower has seen, keyed by the xmin of the
-- transaction that wrote it. An apply error names the remote transaction id;
-- this table maps it back to the versions that transaction left behind.
CREATE TABLE IF NOT EXISTS follower.publisher_heads (
    source      text NOT NULL,
    writer_xid  text NOT NULL,
    versions    text[] NOT NULL,
    pub_lsn     pg_lsn NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, writer_xid)
);

CREATE TABLE IF NOT EXISTS follower.state (
    id                  int PRIMARY KEY CHECK (id = 1),
    status              text NOT NULL CHECK (status IN ('idle', 'in_progress', 'halted')),
    reason              text,
    detail              jsonb NOT NULL DEFAULT '{{}}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    head_mismatch_since timestamptz
);
INSERT INTO follower.state (id, status) VALUES (1, 'idle') ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS follower.pending_repairs (
    relation  text PRIMARY KEY,
    reason    text NOT NULL,
    queued_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS follower.events (
    id     bigserial PRIMARY KEY,
    at     timestamptz NOT NULL DEFAULT now(),
    kind   text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{{}}'::jsonb
);

CREATE TABLE IF NOT EXISTS follower.sequence_samples (
    seq        text NOT NULL,
    sampled_at timestamptz NOT NULL DEFAULT now(),
    last_value numeric NOT NULL,
    PRIMARY KEY (seq, sampled_at)
);

CREATE OR REPLACE FUNCTION follower.stall_marker() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, follower
AS $fn$
DECLARE
    v text;
BEGIN
    IF TG_ARGV[0] = 'web' THEN
        v := to_jsonb(NEW) ->> 'version_num';
        IF NOT EXISTS (SELECT 1 FROM follower.applied_revisions a WHERE a.version_num = v) THEN
            RAISE EXCEPTION USING
                ERRCODE = '{STALL_SQLSTATE}',
                MESSAGE = format('qontinui-follower stall: web revision %s not applied locally', v);
        END IF;
    ELSIF TG_ARGV[0] = 'coord' THEN
        v := to_jsonb(NEW) ->> TG_ARGV[2];
        IF NOT EXISTS (SELECT 1 FROM follower.applied_coord_versions a
                        WHERE a.lane = TG_ARGV[1] AND a.version = v) THEN
            RAISE EXCEPTION USING
                ERRCODE = '{STALL_SQLSTATE}',
                MESSAGE = format('qontinui-follower stall: coord lane %s version %s not applied locally',
                                 TG_ARGV[1], v);
        END IF;
    ELSE
        RAISE EXCEPTION 'qontinui-follower: stall_marker called with bad arguments %', TG_ARGV;
    END IF;
    RETURN NEW;
END
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION follower.stall_marker() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION follower.stall_marker() TO {ar};
"""


def trigger_sql(table: str, args: list[str]) -> str:
    schema, name = table.split(".", 1)
    target = qualified(schema, name)
    arglist = ", ".join(lit(a) for a in args)
    return f"""
DROP TRIGGER IF EXISTS {TRIGGER_NAME} ON {target};
CREATE TRIGGER {TRIGGER_NAME} BEFORE INSERT OR UPDATE ON {target}
    FOR EACH ROW EXECUTE FUNCTION follower.stall_marker({arglist});
ALTER TABLE {target} ENABLE REPLICA TRIGGER {TRIGGER_NAME};
"""
