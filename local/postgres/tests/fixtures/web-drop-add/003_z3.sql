-- The same-named column dropped and re-added with a default evaluated once
-- per side: a new attnum, so it must read as a NEW stored default and halt.
ALTER TABLE app.items DROP COLUMN stamped;
ALTER TABLE app.items ADD COLUMN stamped timestamptz NOT NULL DEFAULT now();
