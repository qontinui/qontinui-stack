-- A seed into an EXISTING table: nothing in the schema stops apply, so only
-- the stall marker on public.alembic_version keeps this row from arriving
-- twice. gen_random_uuid() makes the local replay's row differ:
-- the follower must end with PRODUCTION's row, not its own.
INSERT INTO app.settings (name, value) VALUES ('feature_x', 'on');
-- ...and into a partitioned table (rows land in its partition).
INSERT INTO app.events (day, note) VALUES (current_date, 'seeded by r2');
