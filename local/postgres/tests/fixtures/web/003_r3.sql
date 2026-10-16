-- A column plus a non-deterministic backfill: apply stops on the missing
-- column before it reaches the version row; the local replay's timestamps
-- differ from production's; production's must win.
ALTER TABLE app.items ADD COLUMN migrated_at timestamptz;
UPDATE app.items SET migrated_at = clock_timestamp();
