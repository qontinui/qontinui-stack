"""The schema follower (plan D7, Amendment A2–A4, B2).

Logical replication carries no DDL, and the publisher's migrations also carry
DML (seed INSERTs, backfill UPDATEs). The follower replays each migration on
the subscriber exactly once and drops the publisher's copy of it:

1. **Stall marker** — the version table on the subscriber carries the
   ``follower.stall_marker`` trigger (see ``follower_schema``). The migrator's
   transaction always writes the version row, so apply stops at exactly that
   transaction. A schema error earlier in the same transaction (a missing
   column) stops it at the same transaction too.
2. **Identify** — the apply error in the subscriber's jsonlog names the remote
   transaction id and its finish LSN. Every poll records, per publisher version
   row, the ``xmin`` that wrote it; an apply error whose transaction id matches
   one of those is a migration, and the recorded row gives the versions that
   transaction LEFT BEHIND (its end state — a whole ``alembic upgrade`` batch is
   one transaction in qontinui-web's ``env.py``). Any other apply error is not a
   migration, and the follower halts rather than guess.
3. **Refuse a move out of the publication (A3)** — if a subscribed table now
   lives on the publisher in a schema the publication does not carry, halt and
   alert before applying anything.
4. **Apply locally** — with the subscription DISABLED (so apply cannot retry
   against a half-migrated subscriber), run the migration locally AS THE
   APPLICATION ROLE (B2), up to that end state. ``pg_stat_user_tables``
   counters taken before and after name the tables its DML touched.
5. **SKIP** — ``ALTER SUBSCRIPTION … SKIP (lsn = <finish LSN>)`` drops the
   publisher's copy of that transaction; record the versions; tables the
   migration created are truncated locally and copied by ``REFRESH PUBLICATION
   WITH (copy_data = true)`` after re-enabling.
6. **Repair** — once apply has caught up, the DML-target tables are compared
   with the publisher and differences overwritten from it (``repair``).

Every step is persisted in ``follower.state`` so a restart resumes where it
stopped, and nothing here guesses: an unidentifiable state halts the follower
and raises an operator notice. A halted follower stays halted until
``qontinui-pg follower resume``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

from . import repair as repair_mod
from .config import Config, ConfigError
from .follower_schema import TRIGGER_NAME, schema_sql, trigger_sql
from .notify import Notifier, log
from .pg import PgError, Psql, ident, lit, qualified, text_array

MIGRATE_APPNAME = "qontinui-pg-follower-migrate"

#: SQLSTATEs that are never schema lag: a permission failure (B2's missing
#: SECURITY DEFINER shows up as exactly this) or a plain RAISE from a function
#: that is not the stall marker.
REFUSED_SQLSTATES = {"42501", "P0001"}

_CONTEXT_RE = re.compile(
    r'replication origin "pg_(?P<oid>\d+)".*?in transaction (?P<xid>\d+), '
    r"finished at (?P<lsn>[0-9A-Fa-f]+/[0-9A-Fa-f]+)",
    re.S,
)


def lsn_int(lsn: str | None) -> int:
    if not lsn:
        return 0
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


class Halt(RuntimeError):
    """The follower must stop and alert; nothing it could do next is safe."""


@dataclass
class Stall:
    xid: str
    finish_lsn: str
    sqlstate: str
    message: str
    at: str


class Follower:
    def __init__(self, cfg: Config, notifier: Notifier | None = None):
        self.cfg = cfg
        self.notifier = notifier or Notifier(cfg)
        self.pub = cfg.publisher("qontinui-pg-follower")
        self.admin = cfg.sub_admin("qontinui-pg-follower")
        self.stall_fresh = cfg.getint("follower", "stall_fresh_seconds", 120)
        self.log_tail = cfg.getint("follower", "log_tail_bytes", 4 * 1024 * 1024)

    # ------------------------------------------------------------------ setup
    def _version_tables(self) -> list[tuple[str, list[str]]]:
        """(table, trigger args) for every version table the follower watches."""
        tables = [(self.cfg.version_table, ["web"])]
        if self.cfg.coord_lane:
            for h in self.cfg.coord_history_tables:
                tables.append((h.table, ["coord", h.lane, self.cfg.coord_version_column]))
        return tables

    def _table_exists(self, db: Psql, table: str) -> bool:
        schema, name = table.split(".", 1)
        return db.scalar(f"SELECT to_regclass({lit(qualified(schema, name))}) IS NOT NULL") == "t"

    def install(self) -> None:
        """Create the follower schema and the stall triggers (idempotent)."""
        self._guard_a4()
        self.admin.execute(schema_sql(self.cfg.follower_role, self.cfg.app_role))
        for table, args in self._version_tables():
            if not self._table_exists(self.admin, table):
                if args[0] == "web":
                    raise ConfigError(f"version table {table} does not exist on the subscriber")
                log("follower-install", f"{table} absent; its trigger is installed by coord-adopt")
                continue
            self.admin.execute(trigger_sql(table, args))
        self.event("installed", {"tables": [t for t, _ in self._version_tables()]})

    def seed(self) -> dict:
        """Record the publisher's current versions as applied (Phase 3, before subscribing).

        The subscriber's schema was loaded from production at this head, so the
        initial table sync of the version table must pass the stall marker.
        """
        snap = self.pub_snapshot()
        web = sorted({row["v"] for row in snap["web"]})
        for v in web:
            self.admin.execute(
                "INSERT INTO follower.applied_revisions (version_num, via) "
                f"VALUES ({lit(v)}, 'seed') ON CONFLICT DO NOTHING")
        coord = []
        for lane, rows in snap["coord"].items():
            for row in rows:
                coord.append((lane, row["v"]))
                self.admin.execute(
                    "INSERT INTO follower.applied_coord_versions (lane, version, via) "
                    f"VALUES ({lit(lane)}, {lit(row['v'])}, 'seed') ON CONFLICT DO NOTHING")
        self.event("seeded", {"web": web, "coord": coord})
        return {"web": web, "coord": coord}

    # ------------------------------------------------------------------ state
    def state(self) -> dict:
        rows = self.admin.rows(
            "SELECT status, coalesce(reason, ''), detail::text, "
            "coalesce(extract(epoch FROM now() - head_mismatch_since)::bigint::text, ''), "
            "extract(epoch FROM now() - updated_at)::bigint::text FROM follower.state WHERE id = 1")
        status, reason, detail, mismatch, age = rows[0]
        return {"status": status, "reason": reason, "detail": json.loads(detail),
                "head_mismatch_seconds": int(mismatch) if mismatch else None, "age_seconds": int(age)}

    def set_state(self, status: str, reason: str | None = None, detail: dict | None = None) -> None:
        self.admin.execute(
            f"UPDATE follower.state SET status = {lit(status)}, reason = {lit(reason)}, "
            f"detail = {lit(json.dumps(detail or {}))}::jsonb, updated_at = now() WHERE id = 1")

    def event(self, kind: str, detail: dict) -> None:
        self.admin.execute(
            "INSERT INTO follower.events (kind, detail) VALUES "
            f"({lit(kind)}, {lit(json.dumps(detail, default=str))}::jsonb)")
        log(f"follower-{kind}", kind, **{"detail": detail})

    def halt(self, reason: str, detail: dict | None = None) -> None:
        self.set_state("halted", reason, detail)
        self.event("halted", {"reason": reason, **(detail or {})})
        self.notifier.notice(
            "local/postgres schema follower HALTED",
            f"{reason}\n\nNothing was applied past this point. Apply on the subscriber stays stopped at "
            "the stalled transaction and the publisher's slot holds its WAL (bounded by "
            "max_slot_wal_keep_size). Resolve, then run `qontinui-pg follower resume`.",
            {"reason": reason, **(detail or {})},
        )

    def resume(self) -> None:
        st = self.state()
        if st["status"] != "halted":
            log("follower-resume", f"not halted (status {st['status']}); nothing to do")
            return
        prior = st["detail"].get("in_progress")
        if prior:
            self.set_state("in_progress", None, prior)
        else:
            self.set_state("idle")
        self.event("resumed", {"previous_reason": st["reason"]})

    def _guard_a4(self) -> None:
        violations = self.cfg.a4_violations()
        if violations:
            raise ConfigError(
                "A4: [follower] phase8_complete = true, but the follower configuration still names a "
                "coord source: " + "; ".join(violations))

    # ------------------------------------------------------------------ reads
    def sub_oid(self) -> str:
        oid = self.admin.scalar(
            f"SELECT oid::text FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}")
        if oid is None:
            raise Halt(f"subscription {self.cfg.subscription} does not exist on the subscriber")
        return oid

    def slot_name(self) -> str:
        return self.admin.scalar(
            f"SELECT subslotname FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}") or ""

    def pub_snapshot(self) -> dict:
        """The publisher's LSN plus every version row with the xmin that wrote it."""
        vt = self.cfg.version_table
        schema, name = vt.split(".", 1)
        web = self.pub.rows(
            f"SELECT pg_current_wal_lsn()::text, xmin::text, version_num::text FROM {qualified(schema, name)}")
        lsn = web[0][0] if web else self.pub.scalar("SELECT pg_current_wal_lsn()::text")
        snap = {"lsn": lsn, "web": [{"xmin": r[1], "v": r[2]} for r in web], "coord": {}}
        if self.cfg.coord_lane:
            col = ident(self.cfg.coord_version_column)
            for h in self.cfg.coord_history_tables:
                if not self._table_exists(self.pub, h.table):
                    continue
                rows = self.pub.rows(f"SELECT xmin::text, {col}::text FROM {qualified(h.schema, h.name)}")
                snap["coord"][h.lane] = [{"xmin": r[0], "v": r[1]} for r in rows]
        return snap

    def record_heads(self, snap: dict) -> None:
        stmts = []
        web_versions = sorted({r["v"] for r in snap["web"]})
        for xmin in sorted({r["xmin"] for r in snap["web"]}):
            stmts.append(
                "INSERT INTO follower.publisher_heads (source, writer_xid, versions, pub_lsn) VALUES "
                f"('web', {lit(xmin)}, {text_array(web_versions)}, {lit(snap['lsn'])}) ON CONFLICT DO NOTHING;")
        for lane, rows in snap["coord"].items():
            by_xmin: dict[str, list[str]] = {}
            for r in rows:
                by_xmin.setdefault(r["xmin"], []).append(r["v"])
            for xmin, versions in by_xmin.items():
                stmts.append(
                    "INSERT INTO follower.publisher_heads (source, writer_xid, versions, pub_lsn) VALUES "
                    f"({lit('coord:' + lane)}, {lit(xmin)}, {text_array(sorted(versions))}, {lit(snap['lsn'])}) "
                    "ON CONFLICT DO NOTHING;")
        # Old observations are pruned so a wrapped-around xid can never match one.
        stmts.append("DELETE FROM follower.publisher_heads WHERE observed_at < now() - interval '30 days';")
        self.admin.execute("\n".join(stmts))

    def local_web_versions(self) -> list[str]:
        schema, name = self.cfg.version_table.split(".", 1)
        return sorted(r[0] for r in self.admin.rows(f"SELECT version_num::text FROM {qualified(schema, name)}"))

    def local_coord_versions(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        col = ident(self.cfg.coord_version_column)
        for h in self.cfg.coord_history_tables:
            if self._table_exists(self.admin, h.table):
                out[h.lane] = sorted(r[0] for r in self.admin.rows(
                    f"SELECT {col}::text FROM {qualified(h.schema, h.name)}"))
        return out

    def latest_apply_error(self, oid: str) -> Stall | None:
        """Newest apply error for this subscription in the subscriber's jsonlog."""
        raw = self.admin.scalar(
            "SELECT pg_read_file(f, greatest(0, s.size - " + str(self.log_tail) + "), "
            "least(s.size, " + str(self.log_tail) + ")) "
            "FROM (SELECT pg_current_logfile('jsonlog') AS f) x, LATERAL pg_stat_file(x.f) s "
            "WHERE x.f IS NOT NULL")
        if raw is None:
            raise Halt("the subscriber writes no jsonlog (log_destination must include jsonlog)")
        newest = None
        for line in raw.splitlines():
            if '"ERROR"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # the first line of a tail read is usually partial
            if rec.get("error_severity") != "ERROR":
                continue
            m = _CONTEXT_RE.search(rec.get("context") or "")
            if not m or m.group("oid") != oid:
                continue
            newest = Stall(xid=m.group("xid"), finish_lsn=m.group("lsn").upper(),
                           sqlstate=rec.get("state_code") or "", message=rec.get("message") or "",
                           at=rec.get("timestamp") or "")
        return newest

    def current_stall(self, oid: str) -> Stall | None:
        """The apply error apply is stuck on right now, or None."""
        err = self.latest_apply_error(oid)
        if err is None:
            return None
        row = self.admin.rows(
            f"SELECT pg_replication_origin_progress('pg_{oid}', true)::text, "
            f"extract(epoch FROM now() - {lit(err.at)}::timestamptz)::bigint::text")[0]
        progress, age = row[0], int(row[1])
        if lsn_int(err.finish_lsn) <= lsn_int(progress):
            return None  # that transaction has since been applied or skipped
        if age > self.stall_fresh:
            return None  # apply has not retried it recently: not what it is stuck on
        return err

    def caught_up(self, lsn: str) -> bool:
        slot = self.slot_name()
        flushed = self.pub.scalar(
            f"SELECT confirmed_flush_lsn::text FROM pg_replication_slots WHERE slot_name = {lit(slot)}")
        if flushed is None or lsn_int(flushed) < lsn_int(lsn):
            return False
        pending = self.admin.scalar(
            "SELECT count(*) FROM pg_subscription_rel r JOIN pg_subscription s ON s.oid = r.srsubid "
            f"WHERE s.subname = {lit(self.cfg.subscription)} AND r.srsubstate <> 'r'")
        return pending == "0"

    def publication_tables(self) -> set[str]:
        rows = self.pub.rows(
            "SELECT schemaname, tablename FROM pg_publication_tables "
            f"WHERE pubname = {lit(self.cfg.publication)}")
        return {f"{r[0]}.{r[1]}" for r in rows}

    def subscribed_tables(self) -> set[str]:
        rows = self.admin.rows(
            "SELECT n.nspname, c.relname FROM pg_subscription_rel r "
            "JOIN pg_subscription s ON s.oid = r.srsubid JOIN pg_class c ON c.oid = r.srrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE s.subname = {lit(self.cfg.subscription)}")
        return {f"{r[0]}.{r[1]}" for r in rows}

    def moved_out_of_publication(self) -> list[dict]:
        """A3: subscribed tables that the publisher now holds in an unpublished schema."""
        published = self.publication_tables()
        published_schemas = {t.split(".", 1)[0] for t in published} | set(self.cfg.published_schemas)
        moved = []
        for table in sorted(self.subscribed_tables() - published):
            schema, name = table.split(".", 1)
            rows = self.pub.rows(
                "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE c.relname = {lit(name)} AND c.relkind IN ('r', 'p') "
                f"AND n.nspname <> ALL({text_array(sorted(published_schemas))})")
            for r in rows:
                if f"{r[0]}.{name}" not in published:
                    moved.append({"from": table, "to": f"{r[0]}.{name}"})
        return moved

    def counters(self) -> dict[str, int]:
        rows = self.admin.rows(
            "SELECT schemaname, relname, (n_tup_ins + n_tup_upd + n_tup_del)::text FROM pg_stat_user_tables")
        return {f"{r[0]}.{r[1]}": int(r[2]) for r in rows}

    # ----------------------------------------------------------------- cycle
    def cycle(self) -> str:
        """One poll. Returns the follower's status afterwards."""
        try:
            self._guard_a4()
        except ConfigError as exc:
            self.halt(str(exc))
            return "halted"
        st = self.state()
        if st["status"] == "halted":
            log("follower-halted", st["reason"])
            return "halted"
        try:
            oid = self.sub_oid()
            snap = self.pub_snapshot()
            self.record_heads(snap)
            if st["status"] == "in_progress":
                self._advance(st["detail"])
                return self.state()["status"]
            stall = self.current_stall(oid)
            if stall is not None:
                self._handle_stall(stall)
                return self.state()["status"]
            if self.caught_up(snap["lsn"]):
                self._run_repairs()
            return self._compare_heads(snap)
        except Halt as exc:
            st = self.state()
            detail = {"in_progress": st["detail"]} if st["status"] == "in_progress" else {}
            self.halt(str(exc), detail)
            return "halted"

    def _compare_heads(self, snap: dict) -> str:
        pub_web = sorted({r["v"] for r in snap["web"]})
        same = pub_web == self.local_web_versions()
        if self.cfg.coord_lane:
            pub_coord = {lane: sorted(r["v"] for r in rows) for lane, rows in snap["coord"].items()}
            local = self.local_coord_versions()
            same = same and all(local.get(lane, []) == v for lane, v in pub_coord.items())
        if same:
            self.admin.execute("UPDATE follower.state SET head_mismatch_since = NULL WHERE id = 1")
            return "idle"
        self.admin.execute(
            "UPDATE follower.state SET head_mismatch_since = coalesce(head_mismatch_since, now()) WHERE id = 1")
        if self.caught_up(snap["lsn"]):
            # Apply has passed the transaction that moved the publisher's head
            # without stalling on it: the stall marker did not fire, so its DML
            # may have been applied as replicated rows. Never guess past that.
            raise Halt(
                f"the publisher's versions {pub_web} were reached by apply without a stall, but the "
                f"subscriber's are {self.local_web_versions()}; the stall marker did not fire")
        return "idle"

    def _identify(self, xid: str) -> tuple[str, list[str]]:
        rows = self.admin.rows(
            f"SELECT source, array_to_json(versions)::text FROM follower.publisher_heads WHERE writer_xid = {lit(xid)}")
        if not rows:
            return "", []
        source, versions = rows[0]
        return source, json.loads(versions)

    def _already_processed(self, xid: str) -> bool:
        return self.admin.scalar(
            "SELECT (EXISTS (SELECT 1 FROM follower.applied_revisions WHERE remote_xid = "
            f"{lit(xid)}) OR EXISTS (SELECT 1 FROM follower.applied_coord_versions "
            f"WHERE remote_xid = {lit(xid)}))::text"
        ) == "true"

    def _handle_stall(self, stall: Stall) -> None:
        base = {"xid": stall.xid, "finish_lsn": stall.finish_lsn, "sqlstate": stall.sqlstate,
                "error": stall.message}
        if self._already_processed(stall.xid):
            raise Halt(
                f"apply stalled again on remote transaction {stall.xid} (finish {stall.finish_lsn}), which the "
                f"follower already applied locally and skipped: {stall.sqlstate} {stall.message}")
        source, versions = self._identify(stall.xid)
        if not source:
            raise Halt(
                f"apply error in remote transaction {stall.xid} (finish {stall.finish_lsn}), which wrote no "
                f"version row the follower has seen, so it is not a migration: {stall.sqlstate} {stall.message}")
        if stall.sqlstate in REFUSED_SQLSTATES:
            raise Halt(
                f"the migration transaction {stall.xid} failed on the subscriber with {stall.sqlstate} "
                f"({stall.message}); that is not schema lag. A 42501 here usually means the stall marker "
                "cannot read follower.* as the table owner (B2: SECURITY DEFINER)")
        if source.startswith("coord:") and not self.cfg.coord_lane:
            raise Halt(f"transaction {stall.xid} wrote a coord history row but [follower] coord_lane is off")
        moved = self.moved_out_of_publication()
        if moved:
            raise Halt(
                "A3: refusing to apply a migration that moves published table(s) into a schema the "
                f"publication does not carry: {moved}. Remedy: add the target schema to the publication and "
                "REFRESH PUBLICATION, let the follower apply the revision, then repair every moved table.",
                )
        detail = dict(base, source=source, versions=versions, step="disable")
        self.set_state("in_progress", None, detail)
        self.event("stall", detail)
        self._advance(detail)

    # The per-step machine. Each step is idempotent and persisted, so a crash
    # resumes from the recorded step.
    def _advance(self, detail: dict) -> None:
        while True:
            step = detail.get("step")
            if step == "disable":
                self._disable()
                detail["step"] = "migrate"
            elif step == "migrate":
                if "counters_before" not in detail:
                    detail["counters_before"] = self.counters()
                    self.set_state("in_progress", None, detail)
                self._migrate(detail["source"], detail["versions"])
                after = self._settled_counters()
                excluded = {t for t, _ in self._version_tables()}
                before = detail["counters_before"]
                detail["dml_targets"] = sorted(
                    t for t, n in after.items() if t not in excluded and n != before.get(t, 0))
                detail["step"] = "skip"
            elif step == "skip":
                self._verify_local(detail["source"], detail["versions"])
                self.admin.execute(
                    f"ALTER SUBSCRIPTION {ident(self.cfg.subscription)} SKIP (lsn = {lit(detail['finish_lsn'])})",
                    single_transaction=False)
                self._record_applied(detail)
                detail["step"] = "enable"
            elif step == "enable":
                new_tables = self._new_published_tables()
                if new_tables:
                    self.admin.execute(
                        "TRUNCATE " + ", ".join(qualified(*t.split(".", 1)) for t in new_tables))
                sub = ident(self.cfg.subscription)
                self.admin.execute(f"ALTER SUBSCRIPTION {sub} ENABLE", single_transaction=False)
                self.admin.execute(
                    f"ALTER SUBSCRIPTION {sub} REFRESH PUBLICATION WITH (copy_data = true)",
                    single_transaction=False)
                targets = [t for t in detail.get("dml_targets", []) if t not in new_tables]
                for t in targets:
                    self.admin.execute(
                        "INSERT INTO follower.pending_repairs (relation, reason) VALUES "
                        f"({lit(t)}, {lit('migration DML: ' + ','.join(detail['versions']))}) "
                        "ON CONFLICT (relation) DO NOTHING")
                detail["new_tables"] = new_tables
                self.event("applied", {k: v for k, v in detail.items() if k != "counters_before"})
                self.set_state("idle")
                return
            else:
                raise Halt(f"unknown in-progress step {step!r}")
            self.set_state("in_progress", None, detail)

    def _disable(self) -> None:
        sub = ident(self.cfg.subscription)
        self.admin.execute(f"ALTER SUBSCRIPTION {sub} DISABLE", single_transaction=False)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            live = self.admin.scalar(
                "SELECT count(*) FROM pg_stat_subscription WHERE pid IS NOT NULL AND subname = "
                f"{lit(self.cfg.subscription)}")
            if live == "0":
                return
            time.sleep(0.5)
        raise Halt("the subscription's workers did not exit within 60 s of DISABLE")

    def _migrate(self, source: str, versions: list[str]) -> None:
        env = self.cfg.command_env()
        env["PGAPPNAME"] = MIGRATE_APPNAME
        app_dsn = self.cfg.require("subscriber", "app_dsn")
        commands = []
        if source == "web":
            for v in versions:
                prep = self.cfg.optional_command("prepare_cmd", rev=v, version=v, app_dsn=app_dsn)
                if prep:
                    commands.append(prep)
                commands.append(self.cfg.command("migrate_cmd", rev=v, version=v, app_dsn=app_dsn))
        else:
            for v in versions:
                commands.append(self.cfg.command("coord_migrate_cmd", rev=v, version=v, app_dsn=app_dsn))
        full_env = dict(os.environ)
        full_env.update(env)
        for cmd in commands:
            log("follower-migrate", " ".join(cmd))
            proc = subprocess.run(cmd, env=full_env, capture_output=True, text=True,
                                  timeout=self.cfg.getint("follower", "migrate_timeout_seconds", 3600))
            if proc.returncode != 0:
                raise Halt(f"local migration command failed ({proc.returncode}): {' '.join(cmd)}\n"
                           f"{proc.stderr.strip()[-2000:]}")

    def _settled_counters(self) -> dict[str, int]:
        """Counters after the migration's backends have exited and flushed their stats."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            live = self.admin.scalar(
                f"SELECT count(*) FROM pg_stat_activity WHERE application_name = {lit(MIGRATE_APPNAME)}")
            if live == "0":
                break
            time.sleep(0.2)
        previous = self.counters()
        for _ in range(20):
            time.sleep(0.5)
            current = self.counters()
            if current == previous:
                return current
            previous = current
        return previous

    def _verify_local(self, source: str, versions: list[str]) -> None:
        if source == "web":
            local = self.local_web_versions()
            if local != sorted(versions):
                raise Halt(f"after the local migration the subscriber's versions are {local}, "
                           f"expected {sorted(versions)}")
        else:
            lane = source.split(":", 1)[1]
            local = self.local_coord_versions().get(lane, [])
            missing = [v for v in versions if v not in local]
            if missing:
                raise Halt(f"after the local coord-migrate run lane {lane} lacks {missing}")

    def _record_applied(self, detail: dict) -> None:
        xid, lsn = lit(detail["xid"]), lit(detail["finish_lsn"])
        if detail["source"] == "web":
            for v in detail["versions"]:
                self.admin.execute(
                    "INSERT INTO follower.applied_revisions (version_num, remote_xid, finish_lsn, via) VALUES "
                    f"({lit(v)}, {xid}, {lsn}, 'follower') ON CONFLICT (version_num) DO UPDATE SET "
                    "remote_xid = EXCLUDED.remote_xid, finish_lsn = EXCLUDED.finish_lsn")
        else:
            lane = detail["source"].split(":", 1)[1]
            for v in detail["versions"]:
                self.admin.execute(
                    "INSERT INTO follower.applied_coord_versions (lane, version, remote_xid, finish_lsn, via) VALUES "
                    f"({lit(lane)}, {lit(v)}, {xid}, {lsn}, 'follower') ON CONFLICT (lane, version) DO UPDATE SET "
                    "remote_xid = EXCLUDED.remote_xid, finish_lsn = EXCLUDED.finish_lsn")

    def _new_published_tables(self) -> list[str]:
        """Published on the publisher, present locally, not yet in the subscription."""
        candidates = sorted(self.publication_tables() - self.subscribed_tables())
        return [t for t in candidates if self._table_exists(self.admin, t)]

    def _run_repairs(self) -> None:
        rows = self.admin.rows("SELECT relation FROM follower.pending_repairs ORDER BY queued_at, relation")
        for (table,) in rows:
            if not (self._table_exists(self.admin, table) and self._table_exists(self.pub, table)):
                self.admin.execute(f"DELETE FROM follower.pending_repairs WHERE relation = {lit(table)}")
                self.event("repair-dropped", {"table": table, "why": "no longer exists on both sides"})
                continue
            try:
                result = repair_mod.repair_table(
                    self.pub, self.admin, table,
                    full_max_rows=self.cfg.getint("follower", "repair_full_max_rows", 1_000_000),
                    bucket_rows=self.cfg.getint("follower", "repair_bucket_rows", 10_000))
            except (repair_mod.RepairError, PgError) as exc:
                raise Halt(f"repair of {table} failed: {exc}") from exc
            self.event("repaired", result.__dict__)
            if not result.clean:
                raise Halt(f"repair of {table} did not converge: {result.notes}")
            self.admin.execute(f"DELETE FROM follower.pending_repairs WHERE relation = {lit(table)}")

    # ------------------------------------------------------------ run loops
    def settled(self) -> bool:
        """Idle, heads equal, no pending repair, and apply caught up."""
        st = self.state()
        if st["status"] != "idle" or st["head_mismatch_seconds"] is not None:
            return False
        if self.admin.scalar("SELECT count(*) FROM follower.pending_repairs") != "0":
            return False
        return self.caught_up(self.pub.scalar("SELECT pg_current_wal_lsn()::text"))

    def run(self, once: bool = False, until_idle: bool = False, timeout: float = 0.0) -> int:
        poll = self.cfg.getint("follower", "poll_seconds", 60)
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            try:
                status = self.cycle()
            except PgError as exc:
                log("follower-cycle-error", str(exc))
                status = "error"
            if status == "halted" and (once or until_idle):
                return 3
            if once:
                return 0 if status != "error" else 1
            if until_idle and status == "idle":
                try:
                    if self.settled():
                        return 0
                except PgError as exc:
                    log("follower-cycle-error", str(exc))
            if deadline and time.monotonic() > deadline:
                log("follower-timeout", f"not settled within {timeout} s")
                return 4
            time.sleep(poll)

    # ---------------------------------------------------------- coord lane (A2)
    def coord_adopt(self, version: str) -> None:
        """A2 step 3.ii–iii: adopt on the subscriber, then install the history triggers."""
        if not self.cfg.coord_lane:
            raise ConfigError("[follower] coord_lane is off")
        self._guard_a4()
        app_dsn = self.cfg.require("subscriber", "app_dsn")
        cmd = self.cfg.command("coord_adopt_cmd", version=version, rev=version, app_dsn=app_dsn)
        env = dict(os.environ)
        env.update(self.cfg.command_env())
        env["PGAPPNAME"] = MIGRATE_APPNAME
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
        if proc.returncode != 0:
            self.halt("A2: coord adoption failed on the subscriber; steps iii-v do not run and the gate stays "
                      f"closed: {proc.stderr.strip()[-2000:]}")
            raise Halt("coord adoption failed")
        col = ident(self.cfg.coord_version_column)
        for h in self.cfg.coord_history_tables:
            if not self._table_exists(self.admin, h.table):
                raise Halt(f"adoption did not create {h.table} on the subscriber")
            self.admin.execute(trigger_sql(h.table, ["coord", h.lane, self.cfg.coord_version_column]))
            self.admin.execute(
                "INSERT INTO follower.applied_coord_versions (lane, version, via) "
                f"SELECT {lit(h.lane)}, {col}::text, 'adopt' FROM {qualified(h.schema, h.name)} "
                "ON CONFLICT DO NOTHING")
        tables = ", ".join(h.table for h in self.cfg.coord_history_tables)
        self.event("coord-adopted", {"version": version, "tables": tables})
        print(f"Adopted at {version}. Next, on the PUBLISHER (lead-run): "
              f"ALTER PUBLICATION {self.cfg.publication} ADD TABLE {tables};\n"
              "then: qontinui-pg follower coord-refresh")

    def coord_gate(self) -> dict:
        """A2 step v gate: the history tables published, and their triggers on the subscriber."""
        published = self.publication_tables()
        tables = [h.table for h in self.cfg.coord_history_tables]
        triggers = {}
        for t in tables:
            schema, name = t.split(".", 1)
            triggers[t] = self.admin.scalar(
                "SELECT tgenabled::text FROM pg_trigger WHERE tgname = "
                f"{lit(TRIGGER_NAME)} AND tgrelid = to_regclass({lit(qualified(schema, name))})")
        return {"published": {t: t in published for t in tables}, "trigger_enabled": triggers,
                "ok": bool(tables) and all(t in published for t in tables)
                and all(v == "R" for v in triggers.values())}

    def coord_refresh(self) -> None:
        """A2 step 3.iv: REFRESH with copy_data = false, only while the follower is idle."""
        if not self.cfg.coord_lane:
            raise ConfigError("[follower] coord_lane is off")
        st = self.state()
        if st["status"] != "idle":
            raise Halt(f"coord-refresh needs an idle follower (status {st['status']})")
        if self.current_stall(self.sub_oid()) is not None:
            raise Halt("coord-refresh refused: apply is stalled")
        history = {h.table for h in self.cfg.coord_history_tables}
        missing = sorted(self.publication_tables() - self.subscribed_tables() - history)
        if missing:
            raise Halt("coord-refresh refused: published tables not yet in pg_subscription_rel would join "
                       f"with no initial copy: {missing}")
        gate = self.coord_gate()
        if not gate["ok"]:
            raise Halt(f"coord-refresh refused: history tables or triggers not in place: {gate}")
        self.admin.execute(
            f"ALTER SUBSCRIPTION {ident(self.cfg.subscription)} REFRESH PUBLICATION WITH (copy_data = false)",
            single_transaction=False)
        self.event("coord-refreshed", gate)

    # ------------------------------------------------------------------ check
    def check(self) -> list[tuple[bool, str]]:
        results: list[tuple[bool, str]] = []
        a4 = self.cfg.a4_violations()
        results.append((not a4, "A4: no coord source after Phase 8" + (f" — VIOLATED: {a4}" if a4 else "")))
        fn = self.admin.rows(
            "SELECT p.prosecdef::text, r.rolname, coalesce(array_to_string(p.proconfig, ' '), '') "
            "FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'follower' AND p.proname = 'stall_marker'")
        if not fn:
            results.append((False, "follower.stall_marker() exists"))
        else:
            secdef, owner, config = fn[0]
            results.append((secdef == "true", f"stall_marker is SECURITY DEFINER (B2) [{secdef}]"))
            results.append((owner == self.cfg.follower_role,
                            f"stall_marker owned by {self.cfg.follower_role} [{owner}]"))
            results.append(("search_path=pg_catalog, follower" in config, f"stall_marker pins search_path [{config}]"))
            can_exec = self.admin.scalar(
                f"SELECT has_function_privilege({lit(self.cfg.app_role)}, 'follower.stall_marker()', 'EXECUTE')::text")
            results.append((can_exec == "true", f"{self.cfg.app_role} may EXECUTE stall_marker [{can_exec}]"))
        for table, args in self._version_tables():
            schema, name = table.split(".", 1)
            if args[0] == "coord" and not self._table_exists(self.admin, table):
                results.append((True, f"{table}: not adopted yet (no trigger expected)"))
                continue
            row = self.admin.rows(
                "SELECT tgenabled::text, tgtype::text FROM pg_trigger WHERE tgname = "
                f"{lit(TRIGGER_NAME)} AND tgrelid = to_regclass({lit(qualified(schema, name))})")
            ok = bool(row) and row[0][0] == "R" and (int(row[0][1]) & 23) == 23
            results.append((ok, f"{table}: stall trigger BEFORE INSERT OR UPDATE ROW, ENABLE REPLICA {row}"))
        sub = self.admin.rows(
            "SELECT r.rolsuper::text FROM pg_subscription s JOIN pg_roles r ON r.oid = s.subowner "
            f"WHERE s.subname = {lit(self.cfg.subscription)}")
        results.append((bool(sub), f"subscription {self.cfg.subscription} exists"))
        if sub:
            results.append((sub[0][0] == "true", "subscription owned by a superuser (B2)"))
        st = self.state()
        results.append((st["status"] != "halted", f"follower not halted [{st['status']}: {st['reason']}]"))
        return results
