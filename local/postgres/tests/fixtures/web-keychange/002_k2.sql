-- The replay changes replica-identity keys (deterministically here).
UPDATE app.items SET id = id + 1000 WHERE id <= 5;
