-- A3: moves a published table into a schema the publication does not carry.
CREATE SCHEMA unpub;
ALTER TABLE app.items SET SCHEMA unpub;
