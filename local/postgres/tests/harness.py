"""Test harness: a publisher/subscriber pair of real PostgreSQL 16 + pgvector.

``run.sh`` starts two server containers (``wal_level=logical``; the subscriber
writes jsonlog) and runs these tests in a third, tools container. Each test
class gets its own database pair named after it, with B2's ownership: every
table owned by ``qontinui_user`` (not a superuser), the publication and the
subscription owned by the superuser ``postgres``, and the subscription
connecting as ``qontinui_replication`` (REPLICATION, SELECT only) — RDS's shape.

Environment (set by run.sh):
  PUB_HOST PUB_PORT SUB_HOST SUB_PORT   how the tools container reaches each
  PUB_FROM_SUB_HOST PUB_FROM_SUB_PORT   how the subscriber server reaches the publisher
  PGPASSFILE                            throwaway passwords generated per run
  QPG_SRC                               the local/postgres tree under test
                                        (run.sh points it at a mutated copy)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("QPG_SRC", os.path.dirname(HERE))
sys.path.insert(0, os.path.join(SRC, "lib"))

from qpg.pg import Psql, ident, lit  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
SUPER = "postgres"
APP = "qontinui_user"
REPL = "qontinui_replication"


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise unittest.SkipTest(f"{name} is not set: run these tests through tests/run.sh")
    return value


def password(user: str) -> str:
    with open(env("PGPASSFILE"), encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split(":")
            if parts[3] == user:
                return parts[4]
    raise RuntimeError(f"no password for {user}")


def dsn(side: str, db: str, user: str) -> str:
    host, port = env(f"{side}_HOST"), env(f"{side}_PORT")
    return f"host={host} port={port} dbname={db} user={user}"


def db(side: str, name: str, user: str = SUPER) -> Psql:
    return Psql(dsn(side, name, user), label=f"{side.lower()}:{name}:{user}")


def cli(conf: str, *args: str, timeout: float = 300, check: bool = False) -> subprocess.CompletedProcess:
    cmd = [sys.executable, os.path.join(SRC, "qontinui-pg"), "-c", conf, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise AssertionError(f"{' '.join(args)} exited {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


def wait_for(predicate, timeout: float = 60, what: str = "condition", interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")


class Pair:
    """A publisher database and a subscriber database with the fixture schema."""

    def __init__(self, name: str, *, coord_lane: bool = False, extra_conf: str = "",
                 published: str = "TABLES IN SCHEMA app, TABLE public.alembic_version", web_dir: str = "web"):
        self.name = name
        self.web_dir = web_dir
        self.sub_name = f"qsub_{name}"
        self.coord_lane = coord_lane
        self.published = published
        self.extra_conf = extra_conf
        self.conf = os.path.join(tempfile.gettempdir(), f"{name}.conf")
        self.state_dir = tempfile.mkdtemp(prefix=f"{name}-state-")

    # connections
    def pub(self, user: str = SUPER) -> Psql:
        return db("PUB", self.name, user)

    def sub(self, user: str = SUPER) -> Psql:
        return db("SUB", self.name, user)

    def write_conf(self) -> None:
        web = os.path.join(FIXTURES, self.web_dir)
        coord = os.path.join(FIXTURES, "coord")
        fixtures = FIXTURES
        coord_keys = ""
        if self.coord_lane:
            coord_keys = (
                f"coord_migrate_cmd = python3 {fixtures}/fake_coord_migrate.py apply --dir {coord} "
                f"--dsn \"{{app_dsn}}\" --to {{version}}\n"
                f"coord_adopt_cmd = python3 {fixtures}/fake_coord_migrate.py adopt --dir {coord} "
                f"--dsn \"{{app_dsn}}\" --to {{version}}\n"
                "coord_history_tables = public.coord_schema_history_boot:boot, "
                "public.coord_schema_history_background:background, "
                "public.coord_schema_history_contract:contract\n"
            )
        text = f"""
[cluster]
data_directory = {self.state_dir}
app_role = {APP}
follower_role = qontinui_follower
tailnet_address = 100.64.0.10
ssl_cert_file = /dev/null
ssl_key_file = /dev/null

[subscriber]
admin_dsn = {dsn('SUB', self.name, SUPER)}
app_dsn = {dsn('SUB', self.name, APP)}
passfile = {env('PGPASSFILE')}

[publisher]
dsn = {dsn('PUB', self.name, REPL)}
passfile = {env('PGPASSFILE')}

[replication]
subscription = {self.sub_name}
publication = qontinui_pub
published_schemas = app

[follower]
poll_seconds = 1
migrate_cmd = python3 {fixtures}/fake_alembic.py --dir {web} --dsn "{{app_dsn}}" upgrade {{rev}}
version_table = public.alembic_version
coord_lane = {'true' if self.coord_lane else 'false'}
{coord_keys}phase8_complete = false
stall_fresh_seconds = 60

[seqsync]
floor = 1000
final_floor = 10000

[watchdog]
free_space_floor_gb = 0
state_file = {self.state_dir}/watchdog-state.json

[secdiff]
ignore_roles = {REPL}

[notify]
enabled = false
{self.extra_conf}
"""
        with open(self.conf, "w", encoding="utf-8") as fh:
            fh.write(text)

    def conf_aimed_at_publisher(self) -> str:
        """A copy of the config whose [subscriber] DSNs point at the PUBLISHER database."""
        path = self.conf + ".pub"
        with open(self.conf, encoding="utf-8") as fh:
            text = fh.read()
        for user, key in ((SUPER, "admin_dsn"), (APP, "app_dsn")):
            text = text.replace(f"{key} = {dsn('SUB', self.name, user)}", f"{key} = {dsn('PUB', self.name, user)}")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def setup(self) -> None:
        self.teardown(quiet=True)
        for side in ("PUB", "SUB"):
            d = db(side, "postgres")
            d.execute(f"CREATE DATABASE {ident(self.name)} OWNER {APP}", single_transaction=False)
        with open(os.path.join(FIXTURES, "base.sql"), encoding="utf-8") as fh:
            base = fh.read()
        self.pub(APP).execute(base)
        self.sub(APP).execute(base)
        self.pub(APP).execute(
            "INSERT INTO public.alembic_version VALUES ('r1');\n"
            "INSERT INTO app.items (label) SELECT 'item ' || g FROM generate_series(1, 50) g;\n"
            "INSERT INTO app.settings (name, value) VALUES ('base', 'v');\n"
            # OD3's shape: the replication role reads the published schemas,
            # including tables and sequences created later.
            f"GRANT USAGE ON SCHEMA app TO {REPL};\n"
            f"GRANT SELECT ON ALL TABLES IN SCHEMA app TO {REPL};\n"
            f"GRANT SELECT ON ALL SEQUENCES IN SCHEMA app TO {REPL};\n"
            f"GRANT SELECT ON public.alembic_version TO {REPL};\n"
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON TABLES TO {REPL};\n"
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA app GRANT SELECT ON SEQUENCES TO {REPL};\n"
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {REPL};\n")
        self.pub().execute(
            f"CREATE PUBLICATION qontinui_pub FOR {self.published} WITH (publish_via_partition_root = true)")
        self.write_conf()
        cli(self.conf, "follower", "install", check=True)
        cli(self.conf, "follower", "seed", "--expect", "r1", check=True)
        self.subscribe()

    def subscribe(self, wait: bool = True) -> None:
        conninfo = (f"host={env('PUB_FROM_SUB_HOST')} port={env('PUB_FROM_SUB_PORT')} dbname={self.name} "
                    f"user={REPL} password={password(REPL)}")
        self.sub().execute(
            f"CREATE SUBSCRIPTION {self.sub_name} CONNECTION {lit(conninfo)} PUBLICATION qontinui_pub "
            f"WITH (copy_data = true, streaming = on, binary = false, disable_on_error = false, "
            f"slot_name = {lit(self.sub_name)})", single_transaction=False)
        if wait:
            self.wait_synced()

    def wait_synced(self, timeout: float = 90) -> None:
        sub = self.sub()

        def ready():
            rows = sub.rows(
                "SELECT count(*) FILTER (WHERE srsubstate <> 'r'), count(*) FROM pg_subscription_rel r "
                f"JOIN pg_subscription s ON s.oid = r.srsubid WHERE s.subname = {lit(self.sub_name)}")
            return rows[0][0] == "0" and rows[0][1] != "0"
        wait_for(ready, timeout, f"{self.sub_name} tables to reach state r")

    def teardown(self, quiet: bool = False) -> None:
        try:
            sub = db("SUB", "postgres")
            exists = sub.scalar(f"SELECT count(*) FROM pg_database WHERE datname = {lit(self.name)}")
            if exists != "0":
                s = self.sub()
                if s.scalar(f"SELECT count(*) FROM pg_subscription WHERE subname = {lit(self.sub_name)}") != "0":
                    s.execute(f"ALTER SUBSCRIPTION {self.sub_name} DISABLE", single_transaction=False)
                    s.execute(f"ALTER SUBSCRIPTION {self.sub_name} SET (slot_name = NONE)", single_transaction=False)
                    s.execute(f"DROP SUBSCRIPTION {self.sub_name}", single_transaction=False)
                sub.execute(f"DROP DATABASE {ident(self.name)} WITH (FORCE)", single_transaction=False)
            pub = db("PUB", "postgres")
            if pub.scalar(f"SELECT count(*) FROM pg_replication_slots WHERE slot_name = {lit(self.sub_name)}") != "0":
                wait_for(lambda: pub.scalar(
                    f"SELECT count(*) FROM pg_replication_slots WHERE slot_name = {lit(self.sub_name)} AND active"
                ) == "0", 30, "slot inactive")
                pub.execute(f"SELECT pg_drop_replication_slot({lit(self.sub_name)})", single_transaction=False)
            pub.execute(f"DROP DATABASE IF EXISTS {ident(self.name)} WITH (FORCE)", single_transaction=False)
        except Exception:
            if not quiet:
                raise

    # operations
    def migrate_pub(self, rev: str, fixture_dir: str = "web") -> None:
        subprocess.run(
            [sys.executable, os.path.join(FIXTURES, "fake_alembic.py"), "--dir", os.path.join(FIXTURES, fixture_dir),
             "--dsn", dsn("PUB", self.name, APP), "upgrade", rev], check=True, capture_output=True, text=True)

    def follow(self, timeout: float = 120) -> subprocess.CompletedProcess:
        return cli(self.conf, "follower", "run", "--until-idle", "--timeout", str(timeout),
                   "--dry-run-notify", timeout=timeout + 60)

    def tables(self) -> list[str]:
        rows = self.pub().rows(
            "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
            "WHERE pubname = 'qontinui_pub' ORDER BY 1")
        return [r[0] for r in rows]

    def assert_equal(self, test: unittest.TestCase, tables: list[str] | None = None) -> None:
        for table in tables or self.tables():
            q = (f"SELECT coalesce(md5(string_agg(t::text, '|' ORDER BY t::text COLLATE \"C\")), 'empty') "
                 f"FROM {table} t")
            a = self.pub().rows(q, canonical=True)[0][0]
            b = self.sub().rows(q, canonical=True)[0][0]
            test.assertEqual(a, b, f"{table}: publisher and subscriber rows differ")


def wait_caught_up(pair: Pair, timeout: float = 60) -> None:
    pub = pair.pub()
    target = pub.scalar("SELECT pg_current_wal_lsn()::text")

    def done():
        return pub.scalar(
            f"SELECT (confirmed_flush_lsn >= {lit(target)}::pg_lsn)::text FROM pg_replication_slots "
            f"WHERE slot_name = {lit(pair.sub_name)}") == "true"
    wait_for(done, timeout, "apply to catch up")
