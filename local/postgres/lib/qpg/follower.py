"""The schema follower (plan D7, Amendment A2–A4, B2).

Logical replication carries no DDL, and the publisher's migrations also carry
DML (seed INSERTs, backfill UPDATEs). The follower makes every migration take
effect on the subscriber exactly once, with production's own values:

1. **Stall marker** — the version table on the subscriber carries the
   ``follower.stall_marker`` trigger (see ``follower_schema``). The migrator's
   transaction always writes the version row, so apply stops at exactly that
   transaction. A schema error earlier in the same transaction (a missing
   column) stops it there too. Apply is sequential, so at that moment the
   subscriber holds exactly the publisher's data as of the transaction before.
2. **Identify** — the apply error in the subscriber's jsonlog names the remote
   transaction id. Every poll records, per publisher version row, the ``xmin``
   that wrote it; an apply error whose transaction id matches one is a
   migration, and the recorded row gives the versions it LEFT BEHIND (a whole
   ``alembic upgrade`` batch is one transaction in qontinui-web's ``env.py``).
   Any other apply error halts the follower.
3. **Refuse a move out of the publication (A3)** before applying anything.
4. **Replay the schema locally** — with the subscription DISABLED, run the
   migration AS THE APPLICATION ROLE (B2) up to that end state, with a
   replay-log trigger on every subscribed table recording each row the replay
   INSERTS (and any replica-identity key it changes, which halts).
5. **Undo the replay's inserts, then let apply carry the migration's DML** —
   still disabled, delete the rows the replay inserted (in replica mode, by the
   key apply itself uses, captured BEFORE the replay). The data is then the
   pre-migration data again under the post-migration schema, which is what the
   publisher's copy of the migration transaction expects: re-enabled, apply
   replays its INSERTs with production's generated values, and its UPDATEs carry
   whole new tuples. Its version-row UPDATE finds no old row (the local replay
   already moved it) and is skipped. Later publisher transactions follow in
   commit order. The replay path reads no table data from the publisher (only
   the operator-run ``copy-columns`` remedy does, one table at a time).
6. Tables the migration CREATED are emptied locally and copied by ``REFRESH
   PUBLICATION WITH (copy_data = true)``.

This replaces the plan's SKIP-then-repair (D7 steps 3-4), whose repair raced
live apply (review of 254ab54). It is NOT a complete design: it fails CLOSED
on the shapes it can detect and has known gaps it cannot, so ``follower run``
refuses unless ``[follower] acknowledge_known_gaps = true``:

* detected, HALT, and every halt DISABLES the subscription: a migration that
  renames or moves a table (``follower resolve --case rename`` carries it
  through); a column added with a stored default (``atthasmissing``, compared
  per attnum) whose value differs from the publisher's (``follower resolve
  --case missing-value`` after copying the column); a table rewrite after the
  replay's DML, or one that fills a volatile default (a ``table_rewrite`` event
  trigger during the replay); DML before later DDL (apply fails again on the
  migration xid with 55000 / 23502); a unique violation (23505) replaying the
  migration; a replica-identity key changed by the replay; a step that fails
  ``max_step_failures`` times running.
* NOT detected (the README lists the manual remedy for each): row SELECTION
  that differs between the replay and production — production ran the
  migration under READ COMMITTED alongside concurrent transactions, so a
  statement there may not have seen rows that commit before the migration
  does, while the replay sees all of them; non-deterministic predicates
  (``DELETE … WHERE ts < now()``); partition DETACH / ATTACH.

Every step is persisted in ``follower.state`` so a restart resumes where it
stopped, and nothing here guesses: an unidentifiable state halts the follower
and raises an operator notice. A halted follower stays halted until
``qontinui-pg follower resume``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

from .config import Config, ConfigError
from .follower_schema import (
    DROP_REWRITE_EVENT_SQL,
    LOG_TRIGGER,
    REWRITE_EVENT_SQL,
    REWRITE_EVENT_TRIGGER,
    TOUCH_TRIGGER,
    TRIGGER_NAME,
    log_trigger_sql,
    schema_sql,
    touch_trigger_sql,
    trigger_sql,
)
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
    """The follower must stop and alert; nothing it could do next is safe.

    ``case`` and ``targets`` name an operator-resolvable halt
    (``follower resolve --case <case> --target <target>``); they are stored in
    the halt detail, and ``resolve`` accepts only what the current halt names.
    """

    def __init__(self, message: str, case: str | None = None, targets: list[str] | None = None):
        super().__init__(message)
        self.case = case
        self.targets = targets or []


class Locked(RuntimeError):
    """Another follower process (the daemon, or copy-columns) holds the follower lock."""


@contextlib.contextmanager
def follower_lock(path: str):
    """An exclusive, non-blocking lock shared by the daemon's cycle and copy-columns.

    A file lock rather than a session advisory lock: every query here is its
    own psql process, so no database session lives as long as a cycle. Both
    holders run on this host as the postgres user.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Locked(f"{path} is held by another follower process") from exc
        yield
    finally:
        os.close(fd)


def resolve_cli(case: str, targets: list[str]) -> str:
    return "; ".join(f"`qontinui-pg follower resolve --case {case} --target {t}`" for t in targets)


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
        self.lock_path = cfg.get("follower", "lock_file", fallback="/var/lib/qontinui-pg/follower.lock") or ""
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

    def seed(self, expect: str) -> dict:
        """Record and ARM the publisher's versions (Phase 3, before subscribing).

        The subscriber's schema was loaded from a schema-only dump taken at web
        revision ``expect``; the publisher must still be at exactly that head,
        or the copy would land on a schema the dump does not have. Arming lets
        the initial table sync of the version table (and of any history table)
        pass the stall marker once.
        """
        snap = self.pub_snapshot()
        web = sorted({row["v"] for row in snap["web"]})
        if web != [expect]:
            raise Halt(f"seed refused: the publisher's web head is {web}, the dumped schema's is {expect!r}")
        stmts = []
        for v in web:
            stmts.append("INSERT INTO follower.applied_revisions (version_num, via) "
                         f"VALUES ({lit(v)}, 'seed') ON CONFLICT DO NOTHING;")
            stmts.append(f"INSERT INTO follower.armed_versions (source, version) VALUES ('web', {lit(v)}) "
                         "ON CONFLICT DO NOTHING;")
        coord = []
        for lane, rows in snap["coord"].items():
            for row in rows:
                coord.append((lane, row["v"]))
                stmts.append("INSERT INTO follower.applied_coord_versions (lane, version, via) "
                             f"VALUES ({lit(lane)}, {lit(row['v'])}, 'seed') ON CONFLICT DO NOTHING;")
                stmts.append("INSERT INTO follower.armed_versions (source, version) VALUES "
                             f"({lit('coord:' + lane)}, {lit(row['v'])}) ON CONFLICT DO NOTHING;")
        self.admin.execute("\n".join(stmts))
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

    def _cleanup_replay_triggers(self) -> str:
        """Drop the database-wide rewrite event trigger and every replay trigger, wherever they are."""
        try:
            rows = self.admin.rows(
                "SELECT g.tgrelid::regclass::text, g.tgname FROM pg_trigger g "
                f"WHERE g.tgname IN ({lit(LOG_TRIGGER)}, {lit(TOUCH_TRIGGER)}) AND g.tgparentid = 0")
            self.admin.execute(DROP_REWRITE_EVENT_SQL + "".join(
                f"DROP TRIGGER IF EXISTS {ident(r[1])} ON {r[0]};\n" for r in rows))
            return f"Replay triggers removed ({len(rows)} table trigger(s) and the rewrite event trigger)."
        except PgError as exc:
            return f"Replay triggers could NOT be removed ({exc}); `follower check` lists them."

    def halt(self, reason: str, detail: dict | None = None) -> None:
        """Halt, and DISABLE the subscription on every halt path, saying truthfully what was left."""
        detail = dict(detail or {})
        cleaned = self._cleanup_replay_triggers()
        try:
            enabled = self.admin.scalar(
                f"SELECT subenabled::text FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}")
            if enabled == "true":
                self.admin.execute(f"ALTER SUBSCRIPTION {ident(self.cfg.subscription)} DISABLE",
                                   single_transaction=False)
                detail["subscription_disabled_by_halt"] = True
                left = "The follower DISABLED the subscription."
            elif enabled == "false":
                left = "The subscription was already disabled and stays disabled."
            else:
                left = "The subscription does not exist."
        except PgError as exc:
            left = f"The follower could NOT disable the subscription ({exc}); apply may keep retrying."
        left = f"{left} {cleaned}"
        self.set_state("halted", reason, detail)
        self.event("halted", {"reason": reason, "left": left, **detail})
        self.notifier.notice(
            "local/postgres schema follower HALTED",
            f"{reason}\n\n{left} Nothing past the stalled transaction is applied while it is disabled; the "
            "publisher's slot holds the WAL meanwhile (bounded by max_slot_wal_keep_size). "
            + (f"Resolve with {resolve_cli(detail['halt_case'], detail['halt_targets'])}."
               if detail.get("halt_case") else "Resolve, then run `qontinui-pg follower resume`."),
            {"reason": reason, "left": left, **detail},
        )

    def resume(self) -> None:
        st = self.state()
        if st["status"] != "halted":
            log("follower-resume", f"not halted (status {st['status']}); nothing to do")
            return
        prior = st["detail"].get("in_progress")
        if prior:
            # The halt removed the replay triggers. A migration that halted
            # before the undo re-installs them (keeping the log: part of the
            # replay may have committed) before it re-runs. The step machine
            # re-enables the subscription at its enable step.
            if prior.get("step") in ("log", "migrate"):
                prior["step"] = "relog" if "scope" in prior else "log"
            self.set_state("in_progress", None, prior)
        else:
            if st["detail"].get("subscription_disabled_by_halt"):
                self.admin.execute(f"ALTER SUBSCRIPTION {ident(self.cfg.subscription)} ENABLE",
                                   single_transaction=False)
            self.set_state("idle")
        self.event("resumed", {"previous_reason": st["reason"]})

    #: The halt cases an operator can resolve, and what each resolution means.
    RESOLVABLE = {
        "rename": "the publisher made the same rename/move; carry it through (the table keeps its OID on both "
                  "sides and stays subscribed)",
        "missing-value": "carry the migration through; afterwards copy <schema>.<table>.<column> for existing rows "
                         "from the publisher with `follower copy-columns`",
        "rewrite": "carry the migration through; afterwards copy the rewritten table's affected columns from the "
                   "publisher with `follower copy-columns`",
        "key-change": "carry the migration through; afterwards copy the table's non-key columns from the publisher "
                      "with `follower copy-columns`",
    }

    def resolve(self, kase: str, target: str | None) -> None:
        """Record an operator resolution for the CURRENT halt; resume once every target it named is resolved."""
        if kase not in self.RESOLVABLE:
            raise ConfigError(f"unknown case {kase!r}; resolvable: {sorted(self.RESOLVABLE)}")
        st = self.state()
        prior = st["detail"].get("in_progress")
        if st["status"] != "halted" or not prior:
            raise Halt(f"`follower resolve` needs a follower halted inside a migration step (status {st['status']})")
        halt_case = st["detail"].get("halt_case")
        targets = st["detail"].get("halt_targets", [])
        if halt_case != kase:
            raise ConfigError(f"the current halt is {halt_case or 'not resolvable'!r}, not {kase!r}")
        if target not in targets:
            raise ConfigError(f"--target must be one of the halt's targets {targets}, got {target!r}")
        sub = self.sub_oid()
        xid = prior["xid"]
        script = [f"INSERT INTO follower.resolutions (sub_oid, xid, kase, target) VALUES ({lit(sub)}, {lit(xid)}, "
                  f"{lit(kase)}, {lit(target)}) ON CONFLICT DO NOTHING;"]
        if kase in ("rewrite", "key-change"):
            # The target is the regclass text the halt printed; resolve it to an
            # OID through the live catalog rather than by splitting the text.
            oid = self.admin.scalar(
                "SELECT c.oid::text FROM pg_class c WHERE c.oid::regclass::text = "
                f"{lit(target)} OR (SELECT n.nspname || '.' || c.relname FROM pg_namespace n "
                f"WHERE n.oid = c.relnamespace) = {lit(target)} LIMIT 1")
            if oid is None:
                raise ConfigError(f"{target} does not name a table on the subscriber")
            ops = "'T', 'R'" if kase == "rewrite" else "'K'"
            script.append(f"DELETE FROM follower.replay_log WHERE op IN ({ops}) AND (relid = {oid} OR relid IN "
                          f"(SELECT relid FROM pg_partition_tree({oid})));")
        if kase in ("rewrite", "key-change", "missing-value"):
            table, columns = (target.rsplit(".", 1) if kase == "missing-value" else (target, "*"))
            script.append("INSERT INTO follower.owed (relation, columns, xid, kase) VALUES "
                          f"({lit(table)}, {lit(columns)}, {lit(xid)}, {lit(kase)}) ON CONFLICT DO NOTHING;")
        self.admin.execute("\n".join(script))
        self.event("resolved", {"xid": xid, "case": kase, "target": target, "meaning": self.RESOLVABLE[kase]})
        remaining = [t for t in targets if not self._resolved(xid, kase, t)]
        if remaining:
            log("follower-resolve", f"recorded; still unresolved: {remaining}")
            return
        self.resume()

    def _resolved(self, xid: str, kase: str, target: str) -> bool:
        return self.admin.scalar(
            f"SELECT count(*) FROM follower.resolutions WHERE sub_oid = {lit(self.sub_oid())} AND xid = {lit(xid)} "
            f"AND kase = {lit(kase)} AND target = {lit(target)}") != "0"

    def owed(self) -> list[dict]:
        rows = self.admin.rows("SELECT relation, columns, kase, xid FROM follower.owed ORDER BY 1, 2")
        return [{"table": r[0], "columns": r[1], "case": r[2], "xid": r[3]} for r in rows]

    def _version_guard(self) -> str:
        """A DO block for the publisher: raise unless its versions equal the subscriber's applied ones."""
        checks = [(self.cfg.version_table, "version_num")]
        if self.cfg.coord_lane:
            checks += [(h.table, self.cfg.coord_version_column) for h in self.cfg.coord_history_tables
                       if self._table_exists(self.admin, h.table)]
        conds = []
        for table, col in checks:
            target = qualified(*table.split(".", 1))
            v = f"{ident(col)}::text"
            agg = f"(SELECT coalesce(string_agg({v}, ',' ORDER BY {v} COLLATE \"C\"), '') FROM {target})"
            local = self.admin.scalar(f"SELECT {agg}") or ""
            conds.append(f"{agg} IS DISTINCT FROM {lit(local)}")
        return ("DO $$ BEGIN IF " + " OR ".join(conds) + " THEN RAISE EXCEPTION "
                "'qontinui-follower copy-columns: the publisher has applied a migration the subscriber has not; "
                "refusing to copy rows across it'; END IF; END $$;")

    def copy_columns(self, table: str, columns: list[str] | None, allow_unmatched: int = 0) -> dict:
        """Overwrite existing rows' column values from the publisher, collision-free.

        The remedy the rewrite / key-change / missing-value halts name, run once
        the migration has been carried through:

        * it holds the follower lock, so the daemon's cycle cannot run meanwhile;
        * it requires apply caught up (to the publisher's LSN read just before)
          and then DISABLES the subscription itself;
        * it reads key + columns from the publisher in ONE repeatable-read
          snapshot that first checks the publisher's version tables equal the
          subscriber's, with ``row_security = off``, so an unreplayed migration
          or a row-level policy refuses the copy instead of shaping it;
        * it UPDATEs only rows that already exist locally (by key, replica
          mode) and never inserts, so it cannot collide with a row apply has yet
          to insert; a row the publisher changed again later is replaced again,
          in order, by apply;
        * rows read but not matched locally (rows newer than the subscriber)
          fail the run unless ``allow_unmatched`` covers them.
        """
        with follower_lock(self.lock_path):
            return self._copy_columns(table, columns, allow_unmatched)

    def _copy_columns(self, table: str, columns: list[str] | None, allow_unmatched: int) -> dict:
        st = self.state()
        if st["status"] != "idle":
            raise Halt(f"copy-columns needs an idle follower (status {st['status']}); resolve the halt first")
        entry = next((t for t in self.scope_snapshot().values() if t["name"] == table), None)
        if entry is None or not entry["key"]:
            raise ConfigError(f"{table} is not a subscribed table with a key")
        key = entry["key"]
        schema, name = table.split(".", 1)
        both = []
        for db in (self.admin, self.pub):
            both.append([r[0] for r in db.rows(
                "SELECT a.attname FROM pg_attribute a WHERE a.attrelid = "
                f"to_regclass({lit(qualified(schema, name))}) AND a.attnum > 0 AND NOT a.attisdropped "
                "AND a.attgenerated = '' ORDER BY a.attnum")])
        cols = columns or [c for c in both[0] if c in both[1] and c not in key]
        missing = [c for c in cols if c not in both[0] or c not in both[1]]
        if missing or not cols:
            raise ConfigError(f"columns {missing or cols} are not on both sides of {table}")
        target = qualified(schema, name)
        sel = ", ".join(ident(c) for c in key + cols)
        enabled = self.admin.scalar(
            f"SELECT subenabled::text FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}") == "true"
        if not enabled:
            raise Halt("copy-columns needs the subscription enabled and caught up; it disables it itself")
        lsn = self.pub.scalar("SELECT pg_current_wal_lsn()::text")
        deadline = time.monotonic() + self.cfg.getint("follower", "copy_catchup_seconds", 120)
        while not self.caught_up(lsn):
            if time.monotonic() > deadline:
                raise Halt(f"copy-columns refused: apply did not catch up to the publisher's {lsn}")
            time.sleep(0.5)
        self._disable()
        try:
            data = self.pub.copy_out(
                f"SELECT {sel} FROM {target}",
                prelude="SET LOCAL row_security = off;\n" + self._version_guard(),
                in_transaction="ISOLATION LEVEL REPEATABLE READ READ ONLY")
            read = data.count(b"\n")
            tmp = "qpg_copy_columns"
            sets = ", ".join(f"{ident(c)} = s.{ident(c)}" for c in cols)
            match = " AND ".join(f"t.{ident(k)} = s.{ident(k)}" for k in key)
            out = self.admin.execute_with_copy(
                "SET LOCAL session_replication_role = replica;\n"
                f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {sel} FROM {target} WITH NO DATA;",
                f"COPY {tmp} ({sel}) FROM STDIN", data,
                f"WITH u AS (UPDATE {target} t SET {sets} FROM {tmp} s WHERE {match} RETURNING 1) "
                "SELECT count(*) FROM u;\n"
                + (f"DO $$ BEGIN IF (SELECT count(*) FROM {tmp} s WHERE NOT EXISTS (SELECT 1 FROM {target} t "
                   f"WHERE {match})) > {int(allow_unmatched)} THEN RAISE EXCEPTION 'qontinui-follower copy-columns: "
                   "rows read from the publisher do not exist locally (more than --allow-unmatched); nothing "
                   "was updated'; END IF; END $$;"))
            updated = int((out.strip().splitlines() or ["0"])[-1])
            self.admin.execute(
                f"DELETE FROM follower.owed WHERE relation = {lit(table)} AND (columns = '*' OR columns = ANY("
                f"{text_array(cols)}))")
        finally:
            self.admin.execute(f"ALTER SUBSCRIPTION {ident(self.cfg.subscription)} ENABLE", single_transaction=False)
        result = {"table": table, "columns": cols, "rows_read": read, "rows_updated": updated,
                  "subscription_reenabled": True}
        self.event("copied-columns", result)
        return result

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
        if len(web) > 1:
            raise Halt(f"{vt} holds {len(web)} rows on the publisher (several alembic heads); the follower "
                       "replays a single head only")
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
            f"extract(epoch FROM now() - {lit(err.at)}::timestamptz)::bigint::text, "
            f"({lit(err.at)}::timestamptz < coalesce((detail ->> 'enabled_at')::timestamptz, '-infinity'))::text "
            "FROM follower.state WHERE id = 1")[0]
        progress, age, before_enable = row[0], int(row[1]), row[2] == "true"
        if lsn_int(err.finish_lsn) <= lsn_int(progress):
            return None  # that transaction has since been applied or skipped
        if before_enable:
            return None  # logged before the follower last re-enabled apply: history, not the current state
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

    def substream(self) -> str:
        return self.admin.scalar(
            f"SELECT substream::text FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}") or ""

    def syncs_in_flight(self) -> list[str]:
        rows = self.admin.rows(
            "SELECT r.srrelid::regclass::text || ':' || r.srsubstate::text FROM pg_subscription_rel r "
            "JOIN pg_subscription s ON s.oid = r.srsubid "
            f"WHERE s.subname = {lit(self.cfg.subscription)} AND r.srsubstate IN ('i', 'd') ORDER BY 1")
        return [r[0] for r in rows]

    def scope_snapshot(self) -> dict[str, dict]:
        """Every table in scope by OID: name, partition flag, and the key APPLY uses.

        Apply finds a row by the replica identity: the REPLICA IDENTITY USING
        INDEX columns, else (DEFAULT) the primary key; FULL / NOTHING have no
        key, and the whole row is matched.
        """
        rows = self.admin.rows(
            "SELECT c.oid::text, n.nspname || '.' || c.relname, c.relispartition::text, "
            "coalesce((SELECT array_to_json(array_agg(a.attname ORDER BY k.ord)) FROM pg_index i "
            "  JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true "
            "  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
            "  WHERE i.indrelid = c.oid AND ((c.relreplident = 'i' AND i.indisreplident) "
            "                              OR (c.relreplident = 'd' AND i.indisprimary)))::text, '[]') "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            f"WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY({text_array(self._schemas_in_scope())})")
        return {r[0]: {"name": r[1], "partition": r[2] == "true", "key": json.loads(r[3])} for r in rows}

    def max_attnums(self, oids: list[str]) -> dict[str, int]:
        if not oids:
            return {}
        rows = self.admin.rows(
            "SELECT attrelid::text, max(attnum)::text FROM pg_attribute "
            f"WHERE attrelid = ANY(ARRAY[{','.join(oids)}]::oid[]) GROUP BY 1")
        return {r[0]: int(r[1]) for r in rows}

    def local_tables(self) -> set[str]:
        return {t["name"] for t in self.scope_snapshot().values()}

    def missing_value_columns(self, oids: list[str]) -> dict[str, tuple[str, str]]:
        """``oid:attnum`` -> (attname, stored missing value), for columns with atthasmissing.

        Keyed by attnum, not name: a column dropped and re-added under the same
        name gets a new attnum, so it reads as new.
        """
        if not oids:
            return {}
        rows = self.admin.rows(
            "SELECT a.attrelid::text || ':' || a.attnum::text, a.attname, coalesce(a.attmissingval::text, '') "
            f"FROM pg_attribute a WHERE a.atthasmissing AND a.attrelid = ANY(ARRAY[{','.join(oids)}]::oid[])",
            canonical=True)
        return {r[0]: (r[1], r[2]) for r in rows}

    def _schemas_in_scope(self) -> list[str]:
        extra = {t.split(".", 1)[0] for t, _ in self._version_tables()}
        return sorted(set(self.cfg.published_schemas) | extra)

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
            if self.substream() == "p":
                raise Halt("the subscription streams in parallel mode (streaming = parallel): apply is then not "
                           "strictly sequential, which the follower's stall handling requires; use streaming = on")
            snap = self.pub_snapshot()
            self.record_heads(snap)
            if st["status"] == "in_progress":
                self._advance(st["detail"])
                return self.state()["status"]
            stall = self.current_stall(oid)
            if stall is not None:
                self._handle_stall(stall)
                return self.state()["status"]
            return self._compare_heads(snap)
        except Halt as exc:
            st = self.state()
            detail = {"in_progress": st["detail"]} if st["status"] == "in_progress" else {}
            if exc.case:
                detail.update(halt_case=exc.case, halt_targets=exc.targets)
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

    @staticmethod
    def _restall_reason(stall: Stall) -> str:
        head = (f"apply failed again on migration transaction {stall.xid} (finish {stall.finish_lsn}) after the "
                f"follower replayed it: {stall.sqlstate} {stall.message}. ")
        if stall.sqlstate in ("55000", "23502"):
            return head + (
                "KNOWN GAP (DML before later DDL): the migration changed rows and then changed the table's shape in "
                "the same transaction, so its row changes no longer fit the migrated table. Remedy (manual): "
                "re-seed the table named above. A one-table re-seed is not automated yet; see "
                "local/postgres/README.md, Known gaps, row 1. The subscription stays disabled until then.")
        if stall.sqlstate == "23505":
            return head + (
                "A unique violation replaying the migration: a row the migration writes already exists locally "
                "(the replay's insert was not undone, or local data diverged). Remedy (manual): find the key in "
                "the message, compare the row on both sides, remove the local one, then `follower resume`.")
        return head + "Remedy (manual): read the error; nothing past this transaction has been applied."

    def _handle_stall(self, stall: Stall) -> None:
        base = {"xid": stall.xid, "finish_lsn": stall.finish_lsn, "sqlstate": stall.sqlstate,
                "error": stall.message}
        if self._already_processed(stall.xid):
            raise Halt(self._restall_reason(stall))
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
                f"publication does not carry: {moved}. Remedy (lead): add the target schema to the publication "
                "and REFRESH PUBLICATION, `follower resume`, then re-copy every moved table, whose changes since "
                "the move were never sent.")
        syncs = self.syncs_in_flight()
        if syncs:
            # DISABLE would abandon these copies (D3); a stalled apply worker
            # does not stop a table sync's COPY, so wait for it to finish.
            log("follower-waiting", f"stall at {stall.xid} waits for table syncs in flight: {syncs}")
            return
        enabled = self.admin.scalar(
            f"SELECT subenabled::text FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}")
        detail = dict(base, source=source, versions=versions, step="disable", was_enabled=enabled == "true")
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
                detail["step"] = "log"
            elif step == "log":
                snap = self.scope_snapshot()
                detail["scope"] = snap
                detail["missing_before"] = sorted(self.missing_value_columns(list(snap)))
                detail["attnums_before"] = self.max_attnums(list(snap))
                self.admin.execute("TRUNCATE follower.replay_log")
                self._install_log_triggers(snap)
                detail["step"] = "migrate"
            elif step == "relog":
                # Resuming a migration that halted before its undo: the log so
                # far is kept, the triggers come back on the tables the stored
                # scope names BY OID, under their current names (a committed
                # part of the replay may have renamed one; a dropped one is
                # skipped), and the migration re-runs.
                current = self.scope_snapshot()
                self._install_log_triggers({oid: current[oid] for oid in detail["scope"] if oid in current})
                detail["step"] = "migrate"
            elif step == "migrate":
                self._migrate(detail["source"], detail["versions"])
                detail["step"] = "undo"
            elif step == "undo":
                self._drop_log_triggers(detail["scope"])
                self._verify_local(detail["source"], detail["versions"])
                self._check_replay_shape(detail)
                # The undo, the applied-version record and the step change
                # commit together: a crash leaves either all or none of them.
                self._undo_replay_inserts(detail)
            elif step == "enable":
                self._enable(detail)
                self.event("applied", {k: v for k, v in detail.items() if k not in ("scope", "missing_before")})
                now = self.admin.scalar("SELECT now()::text")
                self.set_state("idle", None, {"enabled_at": now, "xid": detail["xid"]})
                return
            else:
                raise Halt(f"unknown in-progress step {step!r}")
            if step != "undo":
                self.set_state("in_progress", None, detail)

    def _check_replay_shape(self, detail: dict) -> None:
        """Halt on the replay shapes the undo-then-apply design cannot carry."""
        scope = detail["scope"]
        xid = detail["xid"]
        now = self.scope_snapshot()
        renamed = [f"{t['name']} -> {now[oid]['name']} (oid {oid})" for oid, t in scope.items()
                   if oid in now and now[oid]["name"] != t["name"]]
        renamed_targets = [now[oid]["name"] for oid, t in scope.items() if oid in now and now[oid]["name"] != t["name"]]
        unresolved = [t for t in renamed_targets if not self._resolved(xid, "rename", t)]
        if renamed and unresolved:
            raise Halt(
                f"the migration renamed or moved table(s) {renamed}. Remedy: confirm on the publisher that it made "
                f"the same rename, then run {resolve_cli('rename', unresolved)}, which carries it through (the "
                "table keeps its OID and its subscription on both sides) and resumes.",
                case="rename", targets=unresolved)
        changed = self.admin.rows(
            "SELECT DISTINCT l.relid::regclass::text FROM follower.replay_log l WHERE l.op = 'K' ORDER BY 1")
        if changed:
            tables = [r[0] for r in changed]
            raise Halt(
                f"KNOWN GAP (key change): the local replay changed a replica-identity key in {tables}, which the "
                "publisher's copy of the migration then cannot find. Remedy: "
                f"{resolve_cli('key-change', tables)}, then, once the follower is idle, "
                "`qontinui-pg follower copy-columns --table <that table>` to take every non-key column from the "
                "publisher.", case="key-change", targets=tables)
        self._check_rewrites(scope, now, detail.get("attnums_before", {}))
        before = set(detail.get("missing_before", []))
        added = {k: v for k, v in self.missing_value_columns(list(scope)).items() if k not in before}
        differing = []
        for key, (column, local_value) in sorted(added.items()):
            oid = key.split(":", 1)[0]
            table = now.get(oid, {}).get("name") or scope[oid]["name"]
            if self._resolved(xid, "missing-value", f"{table}.{column}"):
                continue
            target = f"{table}.{column}"
            remote = self.pub.rows(
                "SELECT coalesce(attmissingval::text, '') FROM pg_attribute "
                f"WHERE attrelid = to_regclass({lit(qualified(*table.split('.', 1)))}) AND attname = {lit(column)} "
                "AND atthasmissing", canonical=True)
            if not remote or remote[0][0] != local_value:
                differing.append((target, f"local {local_value!r}, publisher "
                                          f"{remote[0][0] if remote else 'UNREADABLE'!r}"))
        if differing:
            targets = [t for t, _ in differing]
            raise Halt(
                "KNOWN GAP (stored column default): the migration added column(s) whose default was stored once "
                f"per side and differs: {differing}. Existing rows read that stored value and no row change "
                f"carries production's. Remedy: {resolve_cli('missing-value', targets)}, then, once the follower "
                "is idle, `qontinui-pg follower copy-columns --table <schema>.<table> --column <column>` for each.",
                case="missing-value", targets=targets)

    def _check_rewrites(self, scope: dict, now: dict, attnums_before: dict) -> None:
        """A table rewritten AFTER the replay changed its rows, or rewritten for a volatile default, halts.

        Apply carries the migration's row changes as they were decoded, BEFORE
        the rewrite transformed them (``ALTER … TYPE … USING``), and a volatile
        default is evaluated once per side. Tables the migration created are
        copied, so they are exempt.
        """
        rows = self.admin.rows(
            "SELECT r.relid::text, (r.new_key ->> 'reason')::int::text, EXISTS ("
            "  SELECT 1 FROM follower.replay_log t WHERE t.op = 'T' AND t.id < r.id AND (t.relid = r.relid "
            "  OR t.relid IN (SELECT relid FROM pg_partition_ancestors(r.relid))))::text "
            "FROM follower.replay_log r WHERE r.op = 'R' ORDER BY r.id")
        unsafe: dict[str, str] = {}
        for oid, reason, dml_before in rows:
            if oid not in scope:
                continue
            name = now.get(oid, scope[oid])["name"]
            # A partition's rewrite is resolved through its partitioned parent.
            table = self.admin.scalar(
                f"SELECT coalesce((SELECT a.relid::regclass::text FROM pg_partition_ancestors({oid}) a "
                f"WHERE a.relid <> {oid} ORDER BY a.relid LIMIT 1), {oid}::regclass::text)")
            if dml_before == "true":
                unsafe[table] = f"{name}: rewritten after the migration changed its rows"
            elif int(reason) & 2:
                unsafe[table] = f"{name}: rewritten to fill a column with a volatile default"
            else:
                # Any column the migration added to a table it then rewrote:
                # the rewrite stores each side's own default value, and a later
                # DROP DEFAULT leaves no trace of it in the catalog.
                gained = self.admin.rows(
                    f"SELECT a.attname FROM pg_attribute a WHERE a.attrelid = {oid} AND NOT a.attisdropped "
                    f"AND a.attnum > {int(attnums_before.get(oid, 0))}")
                if gained:
                    unsafe[table] = (f"{name}: rewritten after the migration added column(s) "
                                     f"{[g[0] for g in gained]}; the rewrite stores each side's own default value "
                                     "(computed default)")
        if unsafe:
            targets = sorted(unsafe)
            raise Halt(
                f"KNOWN GAP (rewrite after DML / computed default): {sorted(unsafe.values())}. The publisher's row "
                "changes were decoded before the rewrite, or a default was computed once per side, so the rows "
                f"would diverge silently. Remedy: {resolve_cli('rewrite', targets)}, then, once the follower is "
                "idle, `qontinui-pg follower copy-columns --table <table> --column <each affected column>`.",
                case="rewrite", targets=targets)

    def _install_log_triggers(self, scope: dict) -> None:
        # Partitions inherit a partitioned parent's row trigger, so it goes on
        # the parent (and plain tables) only: never on both.
        versions = {t for t, _ in self._version_tables()}
        script = [log_trigger_sql(t["name"], "version" if t["name"] in versions else "data", t["key"])
                  for t in scope.values() if not t["partition"]]
        script += [touch_trigger_sql(t["name"]) for t in scope.values()]
        script.append(REWRITE_EVENT_SQL)
        self.admin.execute("".join(script))

    def _drop_log_triggers(self, scope: dict) -> None:
        script = [DROP_REWRITE_EVENT_SQL]
        if scope:
            rows = self.admin.rows(
                "SELECT c.oid::regclass::text, g.tgname FROM pg_class c JOIN pg_trigger g ON g.tgrelid = c.oid "
                f"WHERE c.oid = ANY(ARRAY[{','.join(scope)}]::oid[]) "
                f"AND g.tgname IN ({lit(LOG_TRIGGER)}, {lit(TOUCH_TRIGGER)}) "
                "AND NOT g.tgisinternal AND g.tgparentid = 0")
            script += [f"DROP TRIGGER IF EXISTS {ident(r[1])} ON {r[0]};\n" for r in rows]
        self.admin.execute("".join(script))

    def _undo_replay_inserts(self, detail: dict) -> None:
        """Delete, locally and by apply's key, every row the replay inserted; commit with the next step."""
        scope = detail["scope"]
        now = self.scope_snapshot()
        groups = self.admin.rows(
            "SELECT relid::text, relid::regclass::text, count(*)::text FROM follower.replay_log "
            "WHERE op = 'I' GROUP BY 1, 2 ORDER BY 2")
        history = {h.table: h.lane for h in self.cfg.coord_history_tables} if self.cfg.coord_lane else {}
        undone: dict[str, int] = {}
        script = ["SET LOCAL session_replication_role = replica;"]
        for oid, target, count in groups:
            if oid not in now:
                continue  # dropped later in the same migration: nothing to delete
            entry = scope.get(oid) or now[oid]
            key = entry["key"]
            source = f"(SELECT new_key FROM follower.replay_log WHERE op = 'I' AND relid = {oid})"
            if key:
                match = " AND ".join(f"t.{ident(k)} = r.{ident(k)}" for k in key)
                script.append(
                    f"DELETE FROM {target} t USING (SELECT (jsonb_populate_record(NULL::{target}, k.new_key)).* "
                    f"FROM {source} k(new_key)) r WHERE {match};")
            else:
                # No key: delete exactly one matching row per logged insert.
                script.append(
                    f"DELETE FROM {target} t WHERE t.ctid IN (SELECT x.ctid FROM (SELECT t2.ctid, "
                    "row_number() OVER (PARTITION BY to_jsonb(t2)) AS n, to_jsonb(t2) AS j "
                    f"FROM {target} t2) x JOIN (SELECT new_key AS j, count(*) AS c FROM {source} s(new_key) "
                    "GROUP BY 1) w ON w.j = x.j AND x.n <= w.c);")
            undone[entry["name"]] = int(count)
            if entry["name"] == self.cfg.version_table:
                script.append(f"INSERT INTO follower.armed_versions (source, version) SELECT 'web', "
                              f"k.new_key ->> 'version_num' FROM {source} k(new_key) ON CONFLICT DO NOTHING;")
            elif entry["name"] in history:
                col = self.cfg.coord_version_column
                script.append(f"INSERT INTO follower.armed_versions (source, version) SELECT "
                              f"{lit('coord:' + history[entry['name']])}, k.new_key ->> {lit(col)} "
                              f"FROM {source} k(new_key) ON CONFLICT DO NOTHING;")
        # Tables the migration CREATED (new OIDs; partitions ride with their
        # parent): emptied here with DELETE, which in replica mode does not
        # trip a foreign key from an existing table, then copied by the refresh.
        new_tables = sorted(t["name"] for oid, t in now.items() if oid not in scope and not t["partition"])
        for t in new_tables:
            script.append(f"DELETE FROM {qualified(*t.split('.', 1))};")
        script.append("TRUNCATE follower.replay_log;")
        script.append(f"DELETE FROM follower.resolutions WHERE xid = {lit(detail['xid'])};")
        script.append(self._record_applied_sql(detail))
        detail["new_tables"] = new_tables
        detail["undone"] = undone
        detail["step"] = "enable"
        script.append(f"UPDATE follower.state SET status = 'in_progress', reason = NULL, "
                      f"detail = {lit(json.dumps(detail))}::jsonb, updated_at = now() WHERE id = 1;")
        self.admin.execute("\n".join(script))

    def _enable(self, detail: dict) -> None:
        sub = ident(self.cfg.subscription)
        new_tables = set(detail.get("new_tables", []))
        pending = self.publication_tables() - self.subscribed_tables()
        others = sorted(pending - new_tables)
        if new_tables and others:
            raise Halt(
                f"the migration created {sorted(new_tables)}, which need REFRESH PUBLICATION (copy_data = true), "
                f"but {others} are also published and not yet subscribed (D3 batches or A2 history tables); a "
                "refresh would copy them too. Resolve, then `follower resume`.")
        if detail.get("was_enabled", True):
            self.admin.execute(f"ALTER SUBSCRIPTION {sub} ENABLE", single_transaction=False)
        if new_tables & pending:
            self.admin.execute(f"ALTER SUBSCRIPTION {sub} REFRESH PUBLICATION WITH (copy_data = true)",
                               single_transaction=False)

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

    def _record_applied_sql(self, detail: dict) -> str:
        xid, lsn = lit(detail["xid"]), lit(detail["finish_lsn"])
        out = []
        if detail["source"] == "web":
            for v in detail["versions"]:
                out.append(
                    "INSERT INTO follower.applied_revisions (version_num, remote_xid, finish_lsn, via) VALUES "
                    f"({lit(v)}, {xid}, {lsn}, 'follower') ON CONFLICT (version_num) DO UPDATE SET "
                    "remote_xid = EXCLUDED.remote_xid, finish_lsn = EXCLUDED.finish_lsn;")
        else:
            lane = detail["source"].split(":", 1)[1]
            for v in detail["versions"]:
                out.append(
                    "INSERT INTO follower.applied_coord_versions (lane, version, remote_xid, finish_lsn, via) VALUES "
                    f"({lit(lane)}, {lit(v)}, {xid}, {lsn}, 'follower') ON CONFLICT (lane, version) DO UPDATE SET "
                    "remote_xid = EXCLUDED.remote_xid, finish_lsn = EXCLUDED.finish_lsn;")
        return "\n".join(out)

    # ------------------------------------------------------------ run loops
    def settled(self) -> bool:
        """Idle, heads equal, and apply caught up."""
        st = self.state()
        if st["status"] != "idle" or st["head_mismatch_seconds"] is not None:
            return False
        return self.caught_up(self.pub.scalar("SELECT pg_current_wal_lsn()::text"))

    def _halt_failing_step(self, exc: Exception, failures: int) -> str:
        """A step that keeps failing halts, leaving the subscription as the step left it (disabled)."""
        try:
            st = self.state()
            self.halt(f"step {st['detail'].get('step')!r} failed {failures} times running; the subscription stays "
                      f"disabled until resolved: {exc}", {"in_progress": st["detail"]})
            return "halted"
        except PgError as again:
            log("follower-halt-failed", str(again))
            return "error"

    def _notify_stuck_step(self, exc: Exception, already: str | None) -> str | None:
        """A database error inside an in-progress step notifies once per step, never loops silently."""
        try:
            st = self.state()
        except PgError:
            st = {"status": "UNKNOWN", "detail": {}}
        if st["status"] not in ("in_progress", "UNKNOWN"):
            return already
        step = f"{st['status']}:{st['detail'].get('step')}:{st['detail'].get('xid')}"
        if step != already:
            self.notifier.notice("local/postgres schema follower step failing",
                                 f"step {step} failed with a database error and will be retried:\n{exc}",
                                 {"step": step})
        return step

    def run(self, once: bool = False, until_idle: bool = False, timeout: float = 0.0) -> int:
        if not self.cfg.getbool("follower", "acknowledge_known_gaps", False):
            raise ConfigError(
                "the follower refuses to run until [follower] acknowledge_known_gaps = true: its undo-then-apply "
                "design has known gaps it cannot detect (local/postgres/README.md, 'Known gaps'); set the key "
                "only after reading them")
        poll = self.cfg.getint("follower", "poll_seconds", 60)
        max_failures = self.cfg.getint("follower", "max_step_failures", 5)
        deadline = time.monotonic() + timeout if timeout else None
        notified_step = None
        failures = 0
        while True:
            try:
                with follower_lock(self.lock_path):
                    status = self.cycle()
                notified_step = None
                failures = 0
            except Locked as exc:
                log("follower-skipped", f"cycle skipped: {exc} (copy-columns is running)")
                status = "locked"
            except PgError as exc:
                log("follower-cycle-error", str(exc))
                status = "error"
                notified_step = self._notify_stuck_step(exc, notified_step)
                # A publisher outage is not a failing step: the watchdog reports
                # it, and it never counts toward the repeated-failure halt.
                if exc.label != "publisher":
                    failures = failures + 1 if notified_step and notified_step.startswith("in_progress") else 0
                if failures >= max_failures:
                    status = self._halt_failing_step(exc, failures)
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
            stream = self.substream()
            results.append((stream != "p", f"subscription streaming is not parallel (sequential apply) [{stream}]"))
        st = self.state()
        results.append((st["status"] != "halted", f"follower not halted [{st['status']}: {st['reason']}]"))
        for o in self.owed():
            results.append((False, f"copy-columns owed for {o['table']} columns {o['columns']} ({o['case']} halt "
                                   f"resolved on transaction {o['xid']}): `qontinui-pg follower copy-columns --table "
                                   f"{o['table']}{'' if o['columns'] == '*' else ' --column ' + o['columns']}`"))
        in_replay = st["status"] == "in_progress" and st["detail"].get("step") == "migrate"
        if not in_replay:
            leftover = self.admin.scalar(
                f"SELECT (SELECT count(*) FROM pg_trigger WHERE tgname IN ({lit(LOG_TRIGGER)}, {lit(TOUCH_TRIGGER)})) "
                f"+ (SELECT count(*) FROM pg_event_trigger WHERE evtname = {lit(REWRITE_EVENT_TRIGGER)})")
            results.append((leftover == "0", f"no replay trigger or rewrite event trigger left installed "
                                             f"outside a replay [{leftover}]"))
        return results
