-- KNOWN GAP 5: a default evaluated once per side (now() is stable, so no
-- rewrite): existing rows read different stored values. Must halt.
ALTER TABLE app.items ADD COLUMN stamped timestamptz NOT NULL DEFAULT now();
