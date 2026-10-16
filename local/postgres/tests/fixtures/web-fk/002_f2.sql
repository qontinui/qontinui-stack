-- A NEW table referenced by a foreign key from an EXISTING table. TRUNCATE of
-- the new table would fail on that key; the follower empties it with DELETE.
CREATE TABLE app.kinds (id int PRIMARY KEY, name text NOT NULL);
INSERT INTO app.kinds VALUES (1, 'plain'), (2, 'special');
ALTER TABLE app.items ADD COLUMN kind int REFERENCES app.kinds (id);
UPDATE app.items SET kind = 1;
