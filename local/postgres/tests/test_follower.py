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


def _halted_reason(pair: Pair) -> str:
    state = json.loads(cli(pair.conf, "follower", "status", check=True).stdout)
    return state["status"] + ": " + (state["reason"] or "")


def _subenabled(pair: Pair) -> str:
    return pair.sub().scalar(f"SELECT subenabled::text FROM pg_subscription WHERE subname = '{pair.sub_name}'")


def _resolve_follow_copy(test: unittest.TestCase, p: Pair, case: str, target: str, table: str, *columns: str):
    """The documented remedy: resolve the halt, let the follower carry the migration, copy the columns."""
    res = cli(p.conf, "follower", "resolve", "--case", case, "--target", target)
    test.assertEqual(res.returncode, 0, res.stdout + res.stderr)
    res = p.follow()
    test.assertEqual(res.returncode, 0, res.stdout + res.stderr)
    args = ["follower", "copy-columns", "--table", table]
    for c in columns:
        args += ["--column", c]
    res = cli(p.conf, *args)
    test.assertEqual(res.returncode, 0, res.stdout + res.stderr)
    test.assertEqual(_subenabled(p), "true")
    wait_caught_up(p)
    p.assert_equal(test)


class RenameTest(unittest.TestCase):
    """Re-review A2: a migration that renames a table halts; nothing is emptied."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_rename", web_dir="web-rename")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_1_rename_halts_without_emptying(self):
        p = self.pair
        rows = p.sub().scalar("SELECT count(*) FROM app.settings")
        p.migrate_pub("n2")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("renamed or moved", reason)
        self.assertIn("app.settings -> app.settings_v2", reason)
        self.assertEqual(p.sub().scalar("SELECT count(*) FROM app.settings_v2"), rows)
        self.assertEqual(_subenabled(p), "false")

    def test_2_resolve_rename_carries_it_through(self):
        """Re-review B3: `follower resolve --case rename` is a working exit, not a re-halt."""
        p = self.pair
        res = cli(p.conf, "follower", "resolve", "--case", "rename", "--target", "app.settings")
        self.assertEqual(res.returncode, 2, "a target the halt did not name is refused")
        res = cli(p.conf, "follower", "resolve", "--case", "rename", "--target", "app.settings_v2")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        p.pub(APP).execute("INSERT INTO app.settings_v2 (name, value) VALUES ('after rename', 'x')")
        res = p.follow()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(_subenabled(p), "true")
        wait_caught_up(p)
        p.assert_equal(self)


class ForeignKeyNewTableTest(unittest.TestCase):
    """Re-review A3: a new table referenced by a foreign key from an existing one.

    TRUNCATE of the new table fails on that key, every time; the follower
    empties it with DELETE in replica mode instead, and a step that fails
    `max_step_failures` times running halts rather than looping.
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_fk_new_table", web_dir="web-fk")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_follows(self):
        p = self.pair
        p.migrate_pub("f2")
        res = p.follow(timeout=90)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wait_caught_up(p)
        p.assert_equal(self)
        self.assertEqual(p.sub().scalar("SELECT count(*) FROM app.kinds"), "2")


class DmlThenDdlTest(unittest.TestCase):
    """Re-review B1: DML before later DDL in the migration halts and names the gap."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_dml_then_ddl", web_dir="web-dml-ddl")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_halts_naming_the_gap(self):
        p = self.pair
        p.migrate_pub("d2")
        res = p.follow(timeout=90)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("DML before later DDL", reason)
        self.assertIn("re-seed", reason)
        # Re-review B5: this halt happens after re-enable; it must disable the
        # subscription and say so.
        self.assertEqual(_subenabled(p), "false")
        left = p.sub().scalar("SELECT detail ->> 'left' FROM follower.events WHERE kind = 'halted' "
                              "ORDER BY id DESC LIMIT 1")
        self.assertIn("DISABLED the subscription", left)


class MissingValueTest(unittest.TestCase):
    """Re-review B5: a stored column default that differs from the publisher's halts."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_missing_value", web_dir="web-missing")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_1_equal_stored_default_passes(self):
        p = self.pair
        p.migrate_pub("h2")
        res = p.follow()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wait_caught_up(p)
        p.assert_equal(self)

    def test_2_differing_stored_default_halts(self):
        p = self.pair
        p.migrate_pub("h3")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("stored column default", reason)
        self.assertIn("app.items.stamped", reason)
        self.assertEqual(_subenabled(p), "false")

    def test_3_recorded_resolution_then_copy(self):
        """Re-review B4: a recorded resolution lets the migration through; copy-columns fixes the column."""
        _resolve_follow_copy(self, self.pair, "missing-value", "app.items.stamped", "app.items", "stamped")


class TypeRewriteTest(unittest.TestCase):
    """Re-review B1: DML followed by ALTER … TYPE … USING in one migration halts.

    Apply would carry the UPDATE's value (5) decoded before the rewrite, while
    the publisher's rows were transformed (500): a silent divergence.
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_type_rewrite", web_dir="web-type-using")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_halts(self):
        p = self.pair
        p.migrate_pub("y2")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("rewritten after the migration changed its rows", reason)
        self.assertIn("follower resolve --case rewrite --target app.items", reason)
        self.assertEqual(_subenabled(p), "false")

    def test_resolve(self):
        """Fourth review B1 + minor 5: a mismatched case is refused; `resolve --case rewrite` gets out."""
        p = self.pair
        res = cli(p.conf, "follower", "resolve", "--case", "rename", "--target", "app.items")
        self.assertEqual(res.returncode, 2, "the current halt is a rewrite, not a rename")
        _resolve_follow_copy(self, p, "rewrite", "app.items", "app.items", "amount")


class ComputedDefaultRewriteTest(unittest.TestCase):
    """Fourth review B2: ADD COLUMN … DEFAULT now() and a later rewrite of the same table halt."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_computed_rewrite", web_dir="web-missrw")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_halts_and_resolves(self):
        p = self.pair
        p.migrate_pub("m2")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("computed default", reason)
        self.assertIn("stamped", reason)
        _resolve_follow_copy(self, p, "rewrite", "app.items", "app.items", "stamped")


class KeyChangeTest(unittest.TestCase):
    """Fourth review B1: the key-change halt has a working exit."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_key_change", web_dir="web-keychange")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_halts_and_resolves(self):
        p = self.pair
        p.migrate_pub("k2")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("follower resolve --case key-change --target app.items", _halted_reason(p))
        _resolve_follow_copy(self, p, "key-change", "app.items", "app.items")


class MigrateFailureTest(unittest.TestCase):
    """Fourth review B3: a halt inside the replay leaves no trigger installed, database-wide."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_migrate_failure", web_dir="web-type-using")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_halt_removes_replay_triggers(self):
        p = self.pair
        p.sub().execute("ALTER TABLE app.items ADD COLUMN amount int")  # makes the local replay fail
        p.migrate_pub("y2")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("local migration command failed", _halted_reason(p))
        self.assertEqual(p.sub().scalar(
            "SELECT count(*) FROM pg_event_trigger WHERE evtname LIKE 'qontinui_follower%'"), "0")
        self.assertEqual(p.sub().scalar(
            "SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'qontinui_follower_replay%'"), "0")
        check = cli(p.conf, "follower", "check")
        self.assertIn("OK   no replay trigger or rewrite event trigger left installed", check.stdout)


class DropAddColumnTest(unittest.TestCase):
    """Re-review B2: a same-named column dropped and re-added with a stored default is caught."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_drop_add", web_dir="web-drop-add")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_1_constant_default_passes(self):
        p = self.pair
        p.migrate_pub("z2")
        res = p.follow()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wait_caught_up(p)
        p.assert_equal(self)

    def test_2_readded_column_halts(self):
        p = self.pair
        p.migrate_pub("z3")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("stored column default", reason)
        self.assertIn("app.items.stamped", reason)


class AcknowledgeTest(unittest.TestCase):
    """Re-review C: the follower refuses to run until the known gaps are acknowledged."""

    def test_refuses_without_acknowledgement(self):
        p = Pair("t_no_ack", no_ack=True)
        p.write_conf()  # no database is touched: the refusal comes first
        res = cli(p.conf, "follower", "run", "--once")
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("acknowledge_known_gaps", res.stderr)



class CopyColumnsTest(unittest.TestCase):
    """Fifth review 1, 2, 5 and the owed-copy minor: copy-columns is guarded, locked and RLS-honest."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_copy_columns", web_dir="web-type-using")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def _follower(self):
        from qpg import config as config_mod
        from qpg.follower import Follower
        from qpg.notify import Notifier
        cfg = config_mod.load(self.pair.conf)
        return Follower(cfg, Notifier(cfg, force_dry_run=True))

    def test_1_refuses_across_an_unreplayed_migration(self):
        """Even when the caught-up check is defeated, the snapshot's version guard refuses the copy."""
        from qpg.pg import PgError
        p = self.pair
        p.migrate_pub("y2")  # the follower is not running: the subscriber has not replayed it
        res = cli(p.conf, "follower", "copy-columns", "--table", "app.items", "--column", "label")
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("did not catch up", res.stderr)
        f = self._follower()
        f.caught_up = lambda lsn: True
        with self.assertRaises(PgError) as ctx:
            f.copy_columns("app.items", ["label"])
        self.assertIn("has applied a migration the subscriber has not", str(ctx.exception))
        self.assertEqual(_subenabled(p), "true", "copy-columns restores the subscription")

    def test_2_owed_until_copied(self):
        p = self.pair
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        res = cli(p.conf, "follower", "resolve", "--case", "rewrite", "--target", "app.items")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(p.follow().returncode, 0)
        check = cli(p.conf, "follower", "check")
        self.assertEqual(check.returncode, 1, check.stdout)
        self.assertIn("FAIL copy-columns owed for app.items", check.stdout)
        status = json.loads(cli(p.conf, "follower", "status", check=True).stdout)
        self.assertEqual([o["table"] for o in status["copy_columns_owed"]], ["app.items"])
        res = cli(p.conf, "follower", "copy-columns", "--table", "app.items", "--column", "amount")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["rows_read"], out["rows_updated"])
        self.assertEqual(cli(p.conf, "follower", "check").returncode, 0)
        wait_caught_up(p)
        p.assert_equal(self)

    def test_3_lock_excludes_the_daemon_and_copy_columns(self):
        from qpg.follower import follower_lock
        p = self.pair
        with follower_lock(os.path.join(p.state_dir, "follower.lock")):
            res = cli(p.conf, "follower", "copy-columns", "--table", "app.items", "--column", "label")
            self.assertEqual(res.returncode, 5, res.stdout + res.stderr)
            self.assertIn("locked", res.stderr)
            res = cli(p.conf, "follower", "run", "--once")
            self.assertIn("cycle skipped", res.stderr)

    def test_4_row_security_refuses_instead_of_filtering(self):
        p = self.pair
        p.pub(APP).execute("ALTER TABLE app.items ENABLE ROW LEVEL SECURITY")
        try:
            res = cli(p.conf, "follower", "copy-columns", "--table", "app.items", "--column", "label")
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("row-level security", res.stderr)
            self.assertEqual(_subenabled(p), "true")
        finally:
            p.pub(APP).execute("ALTER TABLE app.items DISABLE ROW LEVEL SECURITY")


class DropDefaultRewriteTest(unittest.TestCase):
    """Fifth review 3: add-with-computed-default, a rewrite, then DROP DEFAULT still halts."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_drop_default", web_dir="web-missrw-dropdefault")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_halts(self):
        p = self.pair
        p.migrate_pub("p2")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        reason = _halted_reason(p)
        self.assertIn("computed default", reason)
        self.assertIn("stamped", reason)


class RelogTest(unittest.TestCase):
    """Fifth review 4: a resumed replay re-installs its triggers by OID under current names.

    The batch's first revision renames a table; the replay fails at the second.
    The test then leaves the rename committed locally (as an autocommit block
    in a real revision would) and removes the cause, so the resume must find
    the renamed table by OID and skip the table that no longer exists.
    """

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_relog", web_dir="web-relog")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_resume_after_partial_replay(self):
        p = self.pair
        p.sub(APP).execute("CREATE TABLE app.q3 (id int PRIMARY KEY, note text)")  # the replay will fail here
        p.migrate_pub("q3")
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("local migration command failed", _halted_reason(p))
        p.sub(APP).execute("ALTER TABLE app.settings RENAME TO settings_q; DROP TABLE app.q3")
        self.assertEqual(cli(p.conf, "follower", "resume").returncode, 0)
        res = p.follow(timeout=60)
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("renamed or moved", _halted_reason(p))
        res = cli(p.conf, "follower", "resolve", "--case", "rename", "--target", "app.settings_q")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        res = p.follow()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wait_caught_up(p)
        p.assert_equal(self)

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
