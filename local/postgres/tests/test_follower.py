"""Phase 1d: the schema follower against real logical replication.

Each class is one required case; run.sh re-runs each under a source mutation
that must turn it red (tests/mutations.sh).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest

from harness import APP, FIXTURES, REPL, Pair, cli, dsn, wait_caught_up


class DoubleDmlTest(unittest.TestCase):
    """D7: migration DML is applied once, and the subscriber equals the publisher.

    r2 seeds a gen_random_uuid() row into an existing table (only the stall
    marker stops it); r3 adds a column and backfills it with clock_timestamp()
    (a missing-column error stops it first); r4+r5 land in ONE migrator
    transaction with a new seeded table. Red when the replay's inserts are not
    undone (the publisher's seed collides with the local one), red when the
    replay runs with the subscription enabled (apply races the replay), and
    red if the follower reads table data from the publisher (review blocker 1:
    the r3 step asserts the publisher's scan counters on app.items do not move
    while the follower works).
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_double_dml")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def follow_and_compare(self, rev: str, assert_no_publisher_reads: str | None = None):
        p = self.pair
        p.migrate_pub(rev)
        # Ordinary application writes keep flowing around the migration.
        p.pub(APP).execute(f"INSERT INTO app.items (label) VALUES ('after {rev}')")
        before = scans(p, assert_no_publisher_reads) if assert_no_publisher_reads else None
        res = p.follow()
        if before is not None:
            self.assertEqual(scans(p, assert_no_publisher_reads), before,
                             f"the follower read {assert_no_publisher_reads} on the publisher")
        self.assertEqual(res.returncode, 0, f"follower did not settle after {rev}:\n{res.stdout}\n{res.stderr}")
        wait_caught_up(p)
        self.assertEqual(p.sub().scalar("SELECT version_num FROM public.alembic_version"), rev)
        p.assert_equal(self)

    def test_1_seed_into_existing_table(self):
        self.follow_and_compare("r2")
        self.assertEqual(self.pair.sub().scalar("SELECT count(*) FROM app.settings WHERE name = 'feature_x'"), "1")

    def test_2_column_and_nondeterministic_backfill(self):
        self.follow_and_compare("r3", assert_no_publisher_reads="app.items")
        self.assertEqual(self.pair.sub().scalar("SELECT count(*) FROM app.items WHERE migrated_at IS NULL "
                                                "AND label NOT LIKE 'after%'"), "0")

    def test_3_multi_revision_transaction_with_new_table(self):
        self.follow_and_compare("r5")
        self.assertEqual(self.pair.sub().scalar("SELECT count(*) FROM app.flags"), "2")
        applied = self.pair.sub().rows("SELECT version_num, via FROM follower.applied_revisions ORDER BY 1")
        self.assertIn(["r5", "follower"], applied)

    def test_4_follower_check_is_green(self):
        res = cli(self.pair.conf, "follower", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)


def scans(pair: Pair, table: str) -> str:
    """seq_scan + idx_scan of a publisher table, after its stats have flushed."""
    time.sleep(1.5)
    schema, name = table.split(".")
    return pair.pub().scalar(
        "SELECT (coalesce(seq_scan, 0) + coalesce(idx_scan, 0))::text FROM pg_stat_user_tables "
        f"WHERE schemaname = '{schema}' AND relname = '{name}'")


class ConcurrentWritesTest(unittest.TestCase):
    """Review blocker 2: the follower's writes can never collide with apply.

    A writer thread inserts and updates app.settings and app.items on the
    publisher continuously — before, during and after two migrations that
    touch exactly those tables (r2 seeds app.settings, r3 backfills app.items).
    Afterwards both sides are equal, and the subscriber's log holds no apply
    error other than the stall on each migration's own transaction — in
    particular no unique violation. Red under `no-undo`, and under
    `overwrite-from-publisher` (254ab54's race: copying the publisher's
    current rows ahead of apply).
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_concurrent")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_concurrent_publisher_writes_during_follow(self):
        p = self.pair
        stop = threading.Event()
        errors: list[str] = []
        writes = [0]

        def writer():
            db = p.pub(APP)
            n = 0
            while not stop.is_set():
                n += 1
                try:
                    db.execute(
                        f"INSERT INTO app.settings (name, value) VALUES ('w{n}', 'v');"
                        "UPDATE app.settings SET value = md5(random()::text) "
                        f"WHERE name IN ('base', 'w{max(1, n - 1)}');"
                        f"INSERT INTO app.items (label) VALUES ('w{n}');"
                        f"UPDATE app.items SET label = label || '.' WHERE id = {1 + n % 50};")
                    writes[0] += 1
                except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                    errors.append(str(exc))
                time.sleep(0.05)

        start = p.sub().scalar("SELECT now()::text")
        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            time.sleep(1)
            for rev in ("r2", "r3"):
                p.migrate_pub(rev)
                res = p.follow()
                self.assertEqual(res.returncode, 0, f"follower did not settle after {rev}:\n{res.stdout}\n{res.stderr}")
            time.sleep(1)
        finally:
            stop.set()
            t.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertGreater(writes[0], 20, "the writer must actually have run during the migrations")
        wait_caught_up(p)
        p.assert_equal(self)
        migration_xids = {json.loads(r[0])["xid"] for r in p.sub().rows(
            "SELECT detail::text FROM follower.events WHERE kind = 'stall'")}
        self.assertEqual(len(migration_xids), 2, "one stall per migration")
        bad = []
        for line in p.sub().scalar("SELECT pg_read_file(pg_current_logfile('jsonlog'))").splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("error_severity") != "ERROR" or rec.get("timestamp", "") < start[:23]:
                continue
            m = re.search(r"in transaction (\d+)", rec.get("context") or "")
            if rec.get("state_code") == "23505" or (m and m.group(1) not in migration_xids):
                bad.append((rec.get("state_code"), rec.get("message"), rec.get("context")))
        self.assertEqual(bad, [], "apply hit an error outside the migrations' own stalls")


class RestampTest(unittest.TestCase):
    """Review minor 7: a re-stamp to a version applied before is not let through."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_restamp")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_restamp_halts(self):
        p = self.pair
        p.migrate_pub("r2")
        self.assertEqual(p.follow().returncode, 0)
        p.pub(APP).execute("INSERT INTO app.items (label) VALUES ('downgrade dml'); "
                           "UPDATE public.alembic_version SET version_num = 'r1'")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        state = json.loads(cli(p.conf, "follower", "status", check=True).stdout)
        self.assertIn("expected ['r1']", state["reason"])
        self.assertEqual(p.sub().scalar("SELECT count(*) FROM app.items WHERE label = 'downgrade dml'"), "0")


class CoordLaneTest(unittest.TestCase):
    """A2: the coord lane under r16's fallback, with a fixture coord-migrate.

    Adoption on both sides, the history tables joining the publication, the
    REFRESH with copy_data = false, then a boot-lane version seeding a
    gen_random_uuid() row and a background-lane idempotent backfill. Red under
    `no-undo`.
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_coord_lane", coord_lane=True)
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_coord_lane(self):
        p = self.pair
        fake = os.path.join(FIXTURES, "fake_coord_migrate.py")
        coord = os.path.join(FIXTURES, "coord")
        # 6.3: adoption on the publisher (history tables not yet published).
        subprocess.run([sys.executable, fake, "adopt", "--dir", coord, "--dsn", dsn("PUB", p.name, APP), "--to", "c1"],
                       check=True, capture_output=True)
        p.pub(APP).execute(f"GRANT SELECT ON public.coord_schema_history_boot, public.coord_schema_history_background, "
                           f"public.coord_schema_history_contract TO {REPL}")
        # ii-iii: adoption on the subscriber, then the stall triggers.
        cli(p.conf, "follower", "coord-adopt", "--version", "c1", check=True)
        gate = cli(p.conf, "follower", "coord-gate-check")
        self.assertEqual(gate.returncode, 1, "the gate must stay closed before the publication carries the tables")
        # iv: the lead adds the tables to the publication; the follower refreshes without copying.
        p.pub().execute("ALTER PUBLICATION qontinui_pub ADD TABLE public.coord_schema_history_boot, "
                        "public.coord_schema_history_background, public.coord_schema_history_contract")
        cli(p.conf, "follower", "coord-refresh", check=True)
        p.wait_synced()
        gate = cli(p.conf, "follower", "coord-gate-check")
        self.assertEqual(gate.returncode, 0, gate.stdout)
        # Each later coord migration: boot lane (seed) then background lane (backfill).
        for version in ("c2", "c3"):
            subprocess.run([sys.executable, fake, "apply", "--dir", coord, "--dsn", dsn("PUB", p.name, APP),
                            "--to", version], check=True, capture_output=True)
            res = p.follow()
            self.assertEqual(res.returncode, 0, f"follower did not settle after {version}:\n{res.stdout}\n{res.stderr}")
            wait_caught_up(p)
            p.assert_equal(self, ["app.settings", "app.items"])
            for lane in ("boot", "background"):
                q = f"SELECT string_agg(version, ',' ORDER BY version) FROM public.coord_schema_history_{lane}"
                self.assertEqual(p.pub().scalar(q), p.sub().scalar(q), f"{lane} history differs")
        self.assertEqual(p.sub().scalar("SELECT count(*) FROM app.settings WHERE name = 'coord_seed'"), "1")
        coord_rows = p.sub().rows("SELECT lane, version, via FROM follower.applied_coord_versions ORDER BY 1, 2")
        self.assertIn(["boot", "c2", "follower"], coord_rows)
        self.assertIn(["background", "c3", "follower"], coord_rows)


class MoveRefusalTest(unittest.TestCase):
    """A3: a revision moving a published table into an unpublished schema is refused.

    The follower halts with the A3 reason and applies NOTHING locally: the
    subscriber's table stays where it was and its version stays r1.
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_move_refusal", web_dir="web-move")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_refuses_and_halts(self):
        p = self.pair
        p.migrate_pub("m2", fixture_dir="web-move")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, f"follower must halt:\n{res.stdout}\n{res.stderr}")
        state = json.loads(cli(p.conf, "follower", "status", check=True).stdout)
        self.assertEqual(state["status"], "halted")
        self.assertIn("A3", state["reason"])
        self.assertIn("unpub.items", state["reason"])
        self.assertEqual(p.sub().scalar("SELECT version_num FROM public.alembic_version"), "r1")
        self.assertEqual(p.sub().scalar("SELECT to_regclass('app.items') IS NOT NULL"), "t")
        self.assertEqual(p.sub().scalar("SELECT to_regnamespace('unpub') IS NULL"), "t")
        check = cli(p.conf, "follower", "check")
        self.assertEqual(check.returncode, 1, "check must report the halted follower")


class OwnershipTest(unittest.TestCase):
    """B2: a non-superuser table owner, a superuser subscription owner.

    The apply and table-sync workers fire the stall marker as the TABLE OWNER.
    The initial copy of public.alembic_version (a revision the follower has
    applied, so the marker must PASS) reaches state r only because the trigger
    function is SECURITY DEFINER; without it the copy fails with 42501 forever
    (setUpClass times out) and the follower refuses a 42501 stall.
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_ownership")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_shape(self):
        sub = self.pair.sub()
        owner = sub.rows("SELECT r.rolname, r.rolsuper::text FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                         "WHERE c.oid = 'public.alembic_version'::regclass")[0]
        self.assertEqual(owner, [APP, "false"])
        subowner = sub.rows("SELECT r.rolsuper::text FROM pg_subscription s JOIN pg_roles r ON r.oid = s.subowner "
                            f"WHERE s.subname = '{self.pair.sub_name}'")[0][0]
        self.assertEqual(subowner, "true")
        self.assertEqual(sub.scalar("SELECT version_num FROM public.alembic_version"), "r1")
        app_usage = sub.scalar(f"SELECT has_schema_privilege('{APP}', 'follower', 'USAGE')::text")
        self.assertEqual(app_usage, "false", "the application role must not reach follower.* directly")

    def test_stall_and_follow_under_b2(self):
        p = self.pair
        p.migrate_pub("r2")
        res = p.follow()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wait_caught_up(p)
        p.assert_equal(self)
        check = cli(p.conf, "follower", "check")
        self.assertEqual(check.returncode, 0, check.stdout)

    def test_parallel_streaming_is_refused(self):
        p = self.pair
        p.sub().execute(f"ALTER SUBSCRIPTION {p.sub_name} SET (streaming = parallel)", single_transaction=False)
        try:
            check = cli(p.conf, "follower", "check")
            self.assertEqual(check.returncode, 1, check.stdout)
            self.assertIn("FAIL subscription streaming is not parallel", check.stdout)
        finally:
            p.sub().execute(f"ALTER SUBSCRIPTION {p.sub_name} SET (streaming = on)", single_transaction=False)


if __name__ == "__main__":
    unittest.main()
