-- Bootstrap the five canonical schemas + pgvector extension.
-- Runs once on first container start (when /var/lib/postgresql/data is empty).
--
-- Per topology plan §4 the canonical DB owns five schemas:
--   project — durable project state (workflows, task runs, ML state)
--   coord   — cross-machine/cross-session coordination (claims, leases)
--   agent   — per-agent ephemeral state with TTL
--   auth    — identity (when self-hosting auth without an external IdP)
--   cloud   — cloud-only tables (subscriptions, admin notif settings, beta signup);
--             empty on self-host installs. Migrations live in OSS; the ORM
--             classes live in the private qontinui-cloud-control repo.
--             See tmp_cloud_control_carve_out.md §5.
--
-- Idempotent (IF NOT EXISTS) so wiping and re-creating the volume is safe,
-- and so a future alembic migration can also run these statements without
-- conflict.
--
-- Once alembic owns schema lifecycle end-to-end (post-consolidation), this
-- script can be deleted — the migrator container's first revision will
-- create the schemas. Until then, it guarantees the schemas exist before
-- alembic runs, so its `op.create_table(..., schema="project")` calls
-- don't fail on a fresh DB.

CREATE SCHEMA IF NOT EXISTS project;
CREATE SCHEMA IF NOT EXISTS coord;
CREATE SCHEMA IF NOT EXISTS agent;
CREATE SCHEMA IF NOT EXISTS auth;
CREATE SCHEMA IF NOT EXISTS cloud;

GRANT ALL ON SCHEMA project TO qontinui_user;
GRANT ALL ON SCHEMA coord   TO qontinui_user;
GRANT ALL ON SCHEMA agent   TO qontinui_user;
GRANT ALL ON SCHEMA auth    TO qontinui_user;
GRANT ALL ON SCHEMA cloud   TO qontinui_user;

-- pgvector for embedding columns (used by productivity_knowledge,
-- entailment_cache, etc.). The base image is pgvector/pgvector so the
-- extension is available; we just need to enable it for this database.
CREATE EXTENSION IF NOT EXISTS vector;
