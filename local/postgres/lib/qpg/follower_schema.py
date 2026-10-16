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
* it passes a replicated version row only when the follower has ARMED that
  version (``follower.armed_versions``), and consumes the arming as it passes,
  so each armed version passes exactly once. Otherwise it raises SQLSTATE
  ``QF001``. The migrator's transaction always writes the version row, so apply
  stops at exactly that transaction with none of its DML applied. A re-stamp or
  downgrade to a version applied long ago is not armed, so it stalls too;
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

-- A version the stall marker lets through ONCE: the head the initial table
-- sync copies (seed), and a coord history row the follower's undo removed so
-- the publisher's own insert can apply. Consumed by the marker as it passes.
CREATE TABLE IF NOT EXISTS follower.armed_versions (
    source   text NOT NULL,
    version  text NOT NULL,
    armed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, version)
);

-- Rows the follower's local replay INSERTED into a replicated table, and
-- replica-identity key changes it made, recorded by follower.replay_log()
-- while the subscription is disabled. The follower deletes those rows again
-- before re-enabling, so the publisher's copy of the migration applies onto
-- exactly the pre-migration data.
CREATE TABLE IF NOT EXISTS follower.replay_log (
    id      bigserial PRIMARY KEY,
    relid   oid NOT NULL,
    op      text NOT NULL CHECK (op IN ('I', 'K')),
    new_key jsonb NOT NULL,
    old_key jsonb
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
        DELETE FROM follower.armed_versions a WHERE a.source = 'web' AND a.version = v;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '{STALL_SQLSTATE}',
                MESSAGE = format('qontinui-follower stall: web revision %s is not armed', v);
        END IF;
    ELSIF TG_ARGV[0] = 'coord' THEN
        v := to_jsonb(NEW) ->> TG_ARGV[2];
        DELETE FROM follower.armed_versions a WHERE a.source = 'coord:' || TG_ARGV[1] AND a.version = v;
        IF NOT FOUND THEN
            RAISE EXCEPTION USING
                ERRCODE = '{STALL_SQLSTATE}',
                MESSAGE = format('qontinui-follower stall: coord lane %s version %s is not armed',
                                 TG_ARGV[1], v);
        END IF;
    ELSE
        RAISE EXCEPTION 'qontinui-follower: stall_marker called with bad arguments %', TG_ARGV;
    END IF;
    RETURN NEW;
END
$fn$;

-- The replay log trigger function. It runs inside the follower's local
-- migration, as the migrating role, so it is SECURITY DEFINER like the marker.
-- TG_ARGV[0] is 'data' or 'version'; the rest are the replica-identity key
-- columns (none: the whole row is the key).
CREATE OR REPLACE FUNCTION follower.replay_log() RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, follower
AS $fn$
DECLARE
    keys text[] := TG_ARGV[1:];
    nk jsonb;
    ok jsonb;
BEGIN
    IF cardinality(keys) = 0 THEN
        nk := to_jsonb(NEW);
    ELSE
        SELECT jsonb_object_agg(k, to_jsonb(NEW) -> k) INTO nk FROM unnest(keys) k;
    END IF;
    IF TG_OP = 'INSERT' THEN
        INSERT INTO follower.replay_log (relid, op, new_key) VALUES (TG_RELID, 'I', nk);
    ELSIF TG_ARGV[0] = 'data' THEN
        IF cardinality(keys) = 0 THEN
            ok := to_jsonb(OLD);
        ELSE
            SELECT jsonb_object_agg(k, to_jsonb(OLD) -> k) INTO ok FROM unnest(keys) k;
        END IF;
        IF ok IS DISTINCT FROM nk THEN
            INSERT INTO follower.replay_log (relid, op, new_key, old_key) VALUES (TG_RELID, 'K', nk, ok);
        END IF;
    END IF;
    RETURN NULL;
END
$fn$;

RESET ROLE;

REVOKE ALL ON FUNCTION follower.stall_marker() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION follower.stall_marker() TO {ar};
REVOKE ALL ON FUNCTION follower.replay_log() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION follower.replay_log() TO {ar};
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


LOG_TRIGGER = "qontinui_follower_replay_log"


def log_trigger_sql(table: str, kind: str, key: list[str]) -> str:
    """The replay-log trigger, installed only while the subscription is disabled."""
    schema, name = table.split(".", 1)
    target = qualified(schema, name)
    args = ", ".join(lit(a) for a in [kind, *key])
    return (f"DROP TRIGGER IF EXISTS {LOG_TRIGGER} ON {target};\n"
            f"CREATE TRIGGER {LOG_TRIGGER} AFTER INSERT OR UPDATE ON {target} "
            f"FOR EACH ROW EXECUTE FUNCTION follower.replay_log({args});\n")


def drop_log_trigger_sql(table: str) -> str:
    schema, name = table.split(".", 1)
    return f"DROP TRIGGER IF EXISTS {LOG_TRIGGER} ON {qualified(schema, name)};\n"
