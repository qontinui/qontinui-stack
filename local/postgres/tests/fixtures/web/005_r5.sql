-- Upgraded together with r4 in ONE migrator transaction (qontinui-web's
-- env.py runs a whole upgrade batch in one transaction): the follower must
-- replay to r5, the batch's end state, not to r4.
INSERT INTO app.settings (name, value) VALUES ('feature_y', md5(random()::text));
