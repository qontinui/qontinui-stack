-- A coord boot-lane file: one transaction with its history-row insert.
INSERT INTO app.settings (name, value) VALUES ('coord_seed', gen_random_uuid()::text);
