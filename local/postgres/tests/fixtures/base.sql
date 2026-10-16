-- The fixture's "r1" schema, loaded on BOTH sides as qontinui_user (so the
-- application role owns every table, as on production — B2). Rows are
-- inserted on the publisher only; the subscriber gets them by initial copy.
CREATE SCHEMA app;
CREATE TABLE public.alembic_version (version_num varchar(32) NOT NULL PRIMARY KEY);
CREATE TABLE app.settings (
    id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name  text NOT NULL UNIQUE,
    value text
);
CREATE TABLE app.items (
    id    bigserial PRIMARY KEY,
    label text NOT NULL
);
