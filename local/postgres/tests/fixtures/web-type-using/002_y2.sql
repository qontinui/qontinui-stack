-- DML then a value-transforming type change in one transaction.
ALTER TABLE app.items ADD COLUMN amount int NOT NULL DEFAULT 0;
UPDATE app.items SET amount = 5;
ALTER TABLE app.items ALTER COLUMN amount TYPE bigint USING amount * 100;
