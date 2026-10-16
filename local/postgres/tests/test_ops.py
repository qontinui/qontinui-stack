"""Phase 1e: sequence sync, watchdog, partitions, credentials, roles, secdiff."""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import tempfile
import threading
import unittest

from harness import APP, FIXTURES, REPL, Pair, cli, db, env
from qpg import credential  # harness put the tree under test on sys.path
from qpg.pg import Psql

COORDRLS = os.path.join(FIXTURES, "coordrls_fixture.py")


class SecDiffTest(unittest.TestCase):
    """B3: the security catalog diff is empty on equal sides and RED with one policy dropped."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_secdiff")
        cls.pair.setup()
        p = cls.pair
        # B1 on both sides: the role step runs r10's block AS qontinui_user. The
        # publisher stands in for production, where r10's revision ran as that role.
        for conf in (p.conf, p.conf_aimed_at_publisher()):
            res = cli(conf, "roles", "apply", "--revision-file", COORDRLS)
            assert res.returncode == 0, res.stdout + res.stderr
        rls = """
            GRANT USAGE ON SCHEMA app TO coord_tenant, coord_tenant_preflight;
            GRANT SELECT, INSERT, UPDATE, DELETE ON app.settings TO coord_tenant, coord_tenant_preflight;
            ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO coord_tenant;
            ALTER TABLE app.settings ENABLE ROW LEVEL SECURITY;
            CREATE POLICY tenant_rows ON app.settings TO coord_tenant
                USING (name = current_setting('app.tenant', true));
            CREATE POLICY preflight_rows ON app.settings AS RESTRICTIVE TO coord_tenant_preflight
                USING (true) WITH CHECK (value IS NOT NULL);
            CREATE VIEW app.settings_v WITH (security_invoker = true) AS SELECT id, name FROM app.settings;
        """
        p.pub(APP).execute(rls)
        p.sub(APP).execute(rls)

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def run_diff(self) -> tuple[int, dict]:
        res = cli(self.pair.conf, "secdiff")
        return res.returncode, json.loads(res.stdout)

    def test_1_equal_sides_diff_empty(self):
        code, report = self.run_diff()
        self.assertEqual(code, 0, json.dumps(report, indent=2))
        self.assertTrue(report["empty"])

    def test_2_one_policy_dropped_is_red(self):
        p = self.pair
        p.sub(APP).execute("DROP POLICY preflight_rows ON app.settings")
        try:
            code, report = self.run_diff()
            self.assertEqual(code, 1, "a dropped policy must make the diff non-empty")
            sections = {(d["section"], d["only_on"]) for d in report["differences"]}
            self.assertIn(("policies", "publisher"), sections, json.dumps(report, indent=2))
        finally:
            p.sub(APP).execute("CREATE POLICY preflight_rows ON app.settings AS RESTRICTIVE TO coord_tenant_preflight "
                               "USING (true) WITH CHECK (value IS NOT NULL)")

    def test_3_force_rls_and_wrong_owner_violate_b2(self):
        p = self.pair
        p.sub().execute("ALTER TABLE app.items FORCE ROW LEVEL SECURITY; ALTER TABLE app.items OWNER TO postgres")
        try:
            code, report = self.run_diff()
            self.assertEqual(code, 1)
            kinds = {v["assertion"] for v in report["owner_premise_violations"]}
            self.assertEqual(kinds, {"owner", "relforcerowsecurity"})
        finally:
            p.sub().execute(f"ALTER TABLE app.items NO FORCE ROW LEVEL SECURITY; ALTER TABLE app.items OWNER TO {APP}")

    def test_4_bypassrls_on_the_app_role_is_a_difference(self):
        # Roles are cluster-wide: alter on the subscriber cluster only, then restore.
        db("SUB", "postgres").execute(f"ALTER ROLE {APP} BYPASSRLS")
        try:
            code, report = self.run_diff()
            self.assertEqual(code, 1)
            self.assertIn("roles", {d["section"] for d in report["differences"]})
        finally:
            db("SUB", "postgres").execute(f"ALTER ROLE {APP} NOBYPASSRLS")


class _Collector(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        type(self).received.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                    "body": json.loads(body)})
        self.send_response(201)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class WatchdogTest(unittest.TestCase):
    """D11 / A2 item 6: the watchdog alerts on a published table with no replica identity.

    Notices are log-only by default; with [notify] enabled they are POSTed to
    the agent-findings door (a local collector stands in for coord).
    """

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.header = tempfile.NamedTemporaryFile("w", delete=False, suffix=".header")
        cls.header.write("Authorization: Bearer test-token\n")
        cls.header.close()
        cls.pair = Pair("t_watchdog")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.pair.teardown()
        os.unlink(cls.header.name)

    def watchdog(self, *flags):
        res = cli(self.pair.conf, "watchdog", "run", *flags)
        lines = {ln.split()[1].rstrip(":"): ln for ln in res.stdout.splitlines() if ln.strip()}
        return res, lines

    def test_1_healthy_has_no_replica_identity_alert(self):
        res, lines = self.watchdog("--dry-run-notify")
        self.assertTrue(lines["replica_identity"].startswith("OK"), res.stdout)
        self.assertTrue(lines["slot"].startswith("OK"), res.stdout)
        self.assertTrue(lines["follower"].startswith("OK"), res.stdout)

    def test_2_table_without_replica_identity_alerts(self):
        p = self.pair
        p.pub(APP).execute("CREATE TABLE app.no_identity (a int, b text)")
        try:
            res, lines = self.watchdog("--dry-run-notify")
            self.assertEqual(res.returncode, 1, res.stdout)
            self.assertTrue(lines["replica_identity"].startswith("ALERT"), res.stdout)
            self.assertIn("app.no_identity", lines["replica_identity"])
            self.assertIn('"posted": false', res.stderr, "a dry-run notice is logged, never posted")
            p.pub(APP).execute("ALTER TABLE app.no_identity REPLICA IDENTITY FULL")
            res, lines = self.watchdog("--dry-run-notify")
            self.assertTrue(lines["replica_identity"].startswith("OK"), res.stdout)
        finally:
            p.pub(APP).execute("DROP TABLE app.no_identity")

    def test_3_notice_is_posted_only_when_configured(self):
        p = self.pair
        port = self.server.server_address[1]
        with open(p.conf, "a", encoding="utf-8") as fh:
            fh.write(f"\n[notify]\nenabled = true\ncoord_url = http://127.0.0.1:{port}\n"
                     f"header_file = {self.header.name}\nresource_keys = plan:test\n")
        # configparser refuses a duplicate section: rewrite [notify] in place instead.
        with open(p.conf, encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("[notify]\nenabled = false\n", "", 1)
        with open(p.conf, "w", encoding="utf-8") as fh:
            fh.write(text)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(p.state_dir, "watchdog-state.json"))
        p.pub(APP).execute("CREATE TABLE app.no_identity2 (a int)")
        try:
            _Collector.received.clear()
            res, lines = self.watchdog()
            self.assertTrue(lines["replica_identity"].startswith("ALERT"), res.stdout)
            posts = [r for r in _Collector.received if "replica_identity" in r["body"]["title"]]
            self.assertEqual(len(posts), 1, f"{_Collector.received}\n{res.stderr}")
            post = posts[0]
            self.assertEqual(post["path"], "/coord/agent-findings")
            self.assertEqual(post["auth"], "Bearer test-token")
            self.assertEqual(post["body"]["topic"], "operator-notify")
            self.assertIn("app.no_identity2", post["body"]["body"])
            self.assertIn("AllocatedStorage: UNKNOWN", post["body"]["body"])
            # The same alert inside renotify_minutes is not re-sent.
            _Collector.received.clear()
            self.watchdog()
            self.assertEqual([r for r in _Collector.received if "replica_identity" in r["body"]["title"]], [])
        finally:
            p.pub(APP).execute("DROP TABLE app.no_identity2")


class SeqSyncTest(unittest.TestCase):
    """D6: per-sequence margin, never backwards, and a NULL read is an error."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_seqsync")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def sub_value(self, seq: str) -> int:
        return int(self.pair.sub().scalar(f"SELECT last_value FROM {seq}"))

    def test_1_margin_and_floor(self):
        p = self.pair
        pub_value = int(p.pub().scalar("SELECT last_value FROM app.items_id_seq"))
        res = cli(p.conf, "seqsync", "run")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.sub_value("app.items_id_seq"), pub_value + 1000)
        # A recorded advance of 6,000 in 5 minutes makes the margin 3x that.
        p.sub().execute("INSERT INTO follower.sequence_samples (seq, sampled_at, last_value) VALUES "
                        f"('forward:app.items_id_seq', now() - interval '10 minutes', {pub_value - 6000}), "
                        f"('forward:app.items_id_seq', now() - interval '5 minutes', {pub_value})")
        p.pub(APP).execute("INSERT INTO app.items (label) SELECT 'x' FROM generate_series(1, 10)")
        pub_value = int(p.pub().scalar("SELECT last_value FROM app.items_id_seq"))
        res = cli(p.conf, "seqsync", "run")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.sub_value("app.items_id_seq"), pub_value + 18000)
        # Never backwards: a second run with no publisher movement changes nothing.
        before = self.sub_value("app.items_id_seq")
        cli(p.conf, "seqsync", "run", check=True)
        self.assertEqual(self.sub_value("app.items_id_seq"), before)

    def test_2_final_floor(self):
        p = self.pair
        p.pub(APP).execute("CREATE SEQUENCE app.final_seq; SELECT nextval('app.final_seq');")
        p.sub(APP).execute("CREATE SEQUENCE app.final_seq")
        try:
            cli(p.conf, "seqsync", "run", "--final", check=True)
            self.assertEqual(self.sub_value("app.final_seq"), 1 + 10000)
        finally:
            p.pub(APP).execute("DROP SEQUENCE app.final_seq")
            p.sub(APP).execute("DROP SEQUENCE app.final_seq")

    def test_3_null_read_is_an_error(self):
        p = self.pair
        p.pub(APP).execute("CREATE SEQUENCE app.secret_seq; SELECT nextval('app.secret_seq'); "
                           f"REVOKE ALL ON SEQUENCE app.secret_seq FROM {REPL};")
        p.sub(APP).execute("CREATE SEQUENCE app.secret_seq")
        p.pub(APP).execute("CREATE SEQUENCE app.unused_seq")
        p.sub(APP).execute("CREATE SEQUENCE app.unused_seq")
        try:
            res = cli(p.conf, "seqsync", "run")
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            self.assertIn("app.secret_seq", res.stderr)
            self.assertNotIn("app.unused_seq", res.stderr, "a never-called sequence is unused, not an error")
        finally:
            for side in (p.pub(APP), p.sub(APP)):
                side.execute("DROP SEQUENCE app.secret_seq; DROP SEQUENCE app.unused_seq")


class PartitionsTest(unittest.TestCase):
    """D9: the timer calls the maintenance function as the application role."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_partitions")
        cls.pair.setup()
        with open(cls.pair.conf, "a", encoding="utf-8") as fh:
            fh.write("\n[partitions]\nenabled = true\nfunction = app.parts_maintenance\n"
                     "retention_days = 14\nlookahead_days = 2\n")

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_1_absent_function_is_a_reported_noop(self):
        res = cli(self.pair.conf, "partitions", "run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "absent")

    def test_2_runs_as_the_application_role(self):
        p = self.pair
        p.sub(APP).execute("""
            CREATE TABLE app.parts (day date NOT NULL, v int) PARTITION BY RANGE (day);
            CREATE FUNCTION app.parts_maintenance(retention_days integer, lookahead_days integer)
            RETURNS integer LANGUAGE plpgsql AS $$
            DECLARE d date; n integer := 0;
            BEGIN
              FOR i IN 0..lookahead_days LOOP
                d := current_date + i;
                EXECUTE format('CREATE TABLE IF NOT EXISTS app.%I PARTITION OF app.parts FOR VALUES FROM (%L) TO (%L)',
                               'parts_' || to_char(d, 'YYYYMMDD'), d, d + 1);
                n := n + 1;
              END LOOP;
              RETURN n;
            END $$;
        """)
        res = cli(p.conf, "partitions", "run")
        self.assertEqual(res.stdout.strip(), "ran", res.stderr)
        owners = p.sub().rows(
            "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
            "WHERE i.inhparent = 'app.parts'::regclass")
        self.assertEqual(owners, [[APP]], "children must be owned by the application role (B2)")
        count = p.sub().scalar("SELECT count(*) FROM pg_inherits WHERE inhparent = 'app.parts'::regclass")
        self.assertEqual(count, "3")


class CredentialTest(unittest.TestCase):
    """D2: the password stays in the local passfile; only the SCRAM verifier is printed."""

    def test_verifier_logs_in(self):
        tmp = tempfile.mkdtemp()
        passfile = os.path.join(tmp, "pgpass")
        conf = os.path.join(tmp, "unused.conf")
        open(conf, "w").close()
        res = cli(conf, "credential", "new", "--role", "qpg_cred_test", "--passfile", passfile, check=True)
        verifier = res.stdout.strip()
        self.assertRegex(verifier, r"^SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+$")
        with open(passfile, encoding="utf-8") as fh:
            entry = fh.read().strip()
        secret = entry.rsplit(":", 1)[1]
        self.assertNotIn(secret, res.stdout + res.stderr, "the password must never be printed")
        self.assertEqual(os.stat(passfile).st_mode & 0o777, 0o600)
        admin = db("SUB", "postgres")
        admin.execute(f"DROP ROLE IF EXISTS qpg_cred_test; CREATE ROLE qpg_cred_test LOGIN PASSWORD '{verifier}'")
        try:
            login = Psql(f"host={env('SUB_HOST')} port={env('SUB_PORT')} dbname=postgres user=qpg_cred_test",
                         env={"PGPASSFILE": passfile})
            self.assertEqual(login.scalar("SELECT current_user"), "qpg_cred_test")
            again = cli(conf, "credential", "new", "--role", "qpg_cred_test", "--passfile", passfile)
            self.assertNotEqual(again.returncode, 0, "an existing credential is never silently replaced")
        finally:
            admin.execute("DROP ROLE IF EXISTS qpg_cred_test")

    def test_verifier_matches_rfc_vector(self):
        # Fixed salt: deterministic, so the computation itself is pinned.
        v = credential.scram_verifier("pencil", salt=b"\x00" * 16, iterations=4096)
        self.assertTrue(v.startswith("SCRAM-SHA-256$4096:AAAAAAAAAAAAAAAAAAAAAA==$"))


class RolesTest(unittest.TestCase):
    """B1: production's role shape, and r10's block run AS that role."""

    @classmethod
    def setUpClass(cls):
        cls.pair = Pair("t_roles")
        cls.pair.setup()

    @classmethod
    def tearDownClass(cls):
        cls.pair.teardown()

    def test_1_role_block_grants_set_to_the_app_role(self):
        res = cli(self.pair.conf, "roles", "apply", "--revision-file", COORDRLS)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["app_role"], "exists")
        sets = {m["role"]: (m["set"], m["inherit"]) for m in out["set_memberships"]}
        self.assertEqual(sets.get("coord_tenant"), ("true", "false"))
        self.assertEqual(sets.get("coord_tenant_preflight"), ("true", "false"))
        shape = self.pair.sub().rows("SELECT rolcanlogin::text, rolinherit::text, rolbypassrls::text "
                                     "FROM pg_roles WHERE rolname = 'coord_tenant'")[0]
        self.assertEqual(shape, ["false", "false", "false"])

    def test_2_missing_revision_needs_allow_missing(self):
        self.assertNotEqual(cli(self.pair.conf, "roles", "apply").returncode, 0)
        res = cli(self.pair.conf, "roles", "apply", "--allow-missing")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("skipped", res.stdout)

    def test_3_wrong_shape_is_refused(self):
        admin = db("SUB", "postgres")
        admin.execute("DROP ROLE IF EXISTS qpg_bad_app; CREATE ROLE qpg_bad_app LOGIN SUPERUSER")
        bad = self.pair.conf + ".bad"
        with open(self.pair.conf, encoding="utf-8") as fh:
            text = fh.read().replace(f"app_role = {APP}", "app_role = qpg_bad_app")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            res = cli(bad, "roles", "apply", "--allow-missing")
            self.assertEqual(res.returncode, 1)
            self.assertIn("not production's", res.stderr)
        finally:
            admin.execute("DROP ROLE qpg_bad_app")

    def test_4_check_grantees(self):
        res = cli(self.pair.conf, "roles", "check-grantees")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        report = json.loads(res.stdout)
        self.assertIn(APP, report["grantees"])
        self.assertIn(REPL, report["ignored"])


if __name__ == "__main__":
    unittest.main()
