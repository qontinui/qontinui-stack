-- KNOWN GAP 1: DML before later DDL in the same transaction. The UPDATE's row
-- changes carry column tmp, which the migrated table no longer has.
ALTER TABLE app.items ADD COLUMN tmp int;
UPDATE app.items SET tmp = 1;
ALTER TABLE app.items DROP COLUMN tmp;
