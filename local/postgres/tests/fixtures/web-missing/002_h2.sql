-- A constant default: stored once per side, equal on both. Must pass.
ALTER TABLE app.items ADD COLUMN flag boolean NOT NULL DEFAULT false;
