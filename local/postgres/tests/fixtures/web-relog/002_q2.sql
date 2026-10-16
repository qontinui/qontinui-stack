-- First revision of a batch: a rename (idempotent, so a re-run is safe).
ALTER TABLE IF EXISTS app.settings RENAME TO settings_q;
