-- A computed default, then a rewrite of the same table in the same migration:
-- the rewrite clears atthasmissing and stores each side's own now().
ALTER TABLE app.items ADD COLUMN stamped timestamptz NOT NULL DEFAULT now();
ALTER TABLE app.items ADD COLUMN n int;
ALTER TABLE app.items ALTER COLUMN n TYPE bigint;
