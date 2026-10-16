-- A constant stored default: equal on both sides, passes.
ALTER TABLE app.items ADD COLUMN stamped timestamptz NOT NULL DEFAULT '2020-01-01 00:00:00+00';
