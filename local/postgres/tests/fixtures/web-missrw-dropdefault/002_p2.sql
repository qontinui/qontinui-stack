-- alembic's add-with-default idiom: add the column with a computed default,
-- (a rewrite of the same table), then drop the default. The rewrite stores each
-- side's own now(), and after DROP DEFAULT the catalog no longer shows it.
ALTER TABLE app.items ADD COLUMN stamped timestamptz NOT NULL DEFAULT now();
ALTER TABLE app.items ADD COLUMN n int;
ALTER TABLE app.items ALTER COLUMN n TYPE bigint;
ALTER TABLE app.items ALTER COLUMN stamped DROP DEFAULT;
