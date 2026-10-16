-- A seed into an EXISTING table: nothing in the schema stops apply, so only
-- the stall marker on public.alembic_version keeps this row from arriving
-- twice. gen_random_uuid() makes the local replay's row differ (repair).
INSERT INTO app.settings (name, value) VALUES ('feature_x', 'on');
