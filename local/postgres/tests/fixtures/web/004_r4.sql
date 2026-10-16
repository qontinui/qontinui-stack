-- A new table with seed rows. It is published the moment it exists (schema
-- publication); the follower truncates its local replay and copies it.
CREATE TABLE app.flags (
    id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL UNIQUE
);
INSERT INTO app.flags (name) VALUES ('alpha'), ('beta');
