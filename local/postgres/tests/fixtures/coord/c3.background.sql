-- A background-lane (coord:no-transaction) idempotent backfill: it commits on
-- its own before the history row, so it replicates normally; the local --to
-- run repeats it (a no-op on equal data) and only the history row is skipped.
UPDATE app.items SET label = upper(label) WHERE label <> upper(label);
