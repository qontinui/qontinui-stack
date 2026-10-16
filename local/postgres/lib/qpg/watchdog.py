"""D11 watchdog: the slot, the lag, the follower, the host, the catalog.

Runs once a minute (systemd timer). Every check yields OK, ALERT or UNKNOWN; an
UNKNOWN is a check that could not read its signal and alerts like an ALERT —
an unread signal never passes as healthy.

Checks:

* ``slot``          — the subscription's slot on the publisher: missing,
                      ``wal_status`` ``unreserved``/``lost``, or retained WAL
                      at or over ``retained_wal_alert_bytes`` (2 GB).
* ``apply_lag``     — no apply worker running, or ``now() - latest_end_time``
                      over ``lag_alert_seconds`` (30 min).
* ``follower``      — halted, an in-progress step older than
                      ``follower_lag_alert_seconds``, or the publisher's head
                      differing from the subscriber's for longer than that.
* ``free_space``    — the data directory's filesystem under
                      ``free_space_floor_gb`` (30 GB): a full subscriber disk
                      is the likeliest way to stop apply long enough to lose
                      the slot (D4).
* ``archiver``      — with pgBackRest on: archiving failing (the
                      ``archive-push-queue-max`` drop shows here too).
* ``replica_identity`` — (A2 item 6) a published table on the publisher with
                      ``relreplident = 'd'`` and no primary key, or ``'n'``:
                      production UPDATE/DELETE on it would fail.
* ``reverse``       — (A6, only with ``[watchdog] reverse_dsn``) the reverse
                      subscription's ``apply_error_count`` rising on RDS, or its
                      lag over 30 min.

Notices go through ``notify.Notifier`` (log always; coord only when
enabled). A notice is sent when an alert first appears, when its text changes,
and again every ``renotify_minutes``. Every notice carries the RDS
``AllocatedStorage`` line: read through ``allocated_storage_cmd`` when one is
configured on the host, otherwise stated as UNKNOWN.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from .config import Config
from .notify import Notifier, log
from .pg import PgError, lit

GB = 1024 ** 3


@dataclass
class Check:
    name: str
    status: str  # OK | ALERT | UNKNOWN
    message: str


def replica_identity_offenders(pub, publication: str) -> list[str]:
    rows = pub.rows(
        "SELECT pt.schemaname || '.' || pt.tablename, c.relreplident::text "
        "FROM pg_publication_tables pt "
        "JOIN pg_namespace n ON n.nspname = pt.schemaname "
        "JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = pt.tablename "
        f"WHERE pt.pubname = {lit(publication)} AND ("
        "  c.relreplident = 'n' OR (c.relreplident = 'd' AND NOT EXISTS ("
        "    SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary))"
        ") ORDER BY 1")
    return [f"{r[0]} (relreplident={r[1]})" for r in rows]


class Watchdog:
    def __init__(self, cfg: Config, notifier: Notifier | None = None):
        self.cfg = cfg
        self.notifier = notifier or Notifier(cfg)
        self.pub = cfg.publisher("qontinui-pg-watchdog")
        self.admin = cfg.sub_admin("qontinui-pg-watchdog")

    def _guard(self, name: str, fn) -> Check:
        try:
            return fn()
        except (PgError, OSError, ValueError, subprocess.SubprocessError) as exc:
            return Check(name, "UNKNOWN", f"could not read: {exc}")

    # -- the checks ------------------------------------------------------------
    def check_slot(self) -> Check:
        slot = self.admin.scalar(
            f"SELECT subslotname FROM pg_subscription WHERE subname = {lit(self.cfg.subscription)}")
        if not slot:
            return Check("slot", "UNKNOWN", f"subscription {self.cfg.subscription} or its slot name is absent")
        rows = self.pub.rows(
            "SELECT coalesce(wal_status, ''), active::text, "
            "coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), -1)::bigint::text, "
            "coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), confirmed_flush_lsn), -1)::bigint::text "
            f"FROM pg_replication_slots WHERE slot_name = {lit(slot)}")
        if not rows:
            return Check("slot", "ALERT", f"slot {slot} is MISSING on the publisher: a full re-seed is needed (D11)")
        wal_status, active, retained, lag = rows[0]
        retained_i, threshold = int(retained), self.cfg.getint("watchdog", "retained_wal_alert_bytes", 2 * GB)
        text = (f"slot {slot}: wal_status={wal_status} active={active} retained={retained_i / GB:.2f} GB "
                f"unconfirmed={int(lag) / GB:.2f} GB")
        if wal_status in ("unreserved", "lost"):
            return Check("slot", "ALERT", text + (
                " — the slot is past max_slot_wal_keep_size; when it is lost, recovery is a full re-seed and the "
                "local backups stop advancing (r02's copy goes stale, not lost) until the re-seed reaches 'r'"))
        if retained_i < 0:
            return Check("slot", "UNKNOWN", text)
        if retained_i >= threshold:
            return Check("slot", "ALERT", text + f" — at or over {threshold / GB:.1f} GB")
        return Check("slot", "OK", text)

    def check_apply_lag(self) -> Check:
        rows = self.admin.rows(
            "SELECT s.subenabled::text, coalesce(extract(epoch FROM now() - st.latest_end_time)::bigint::text, ''), "
            "(st.pid IS NOT NULL)::text FROM pg_subscription s "
            "LEFT JOIN pg_stat_subscription st ON st.subid = s.oid AND st.relid IS NULL "
            f"WHERE s.subname = {lit(self.cfg.subscription)}")
        if not rows:
            return Check("apply_lag", "UNKNOWN", f"subscription {self.cfg.subscription} absent")
        enabled, age, running = rows[0]
        limit = self.cfg.getint("watchdog", "lag_alert_seconds", 1800)
        state = self._state()
        if enabled != "true":
            # The follower disables the subscription for every migration it
            # replays (D7's window is minutes); only a long disable alerts.
            since = state.setdefault("disabled_since", time.time())
            self._save_state(state)
            held = int(time.time() - since)
            if held > limit:
                return Check("apply_lag", "ALERT", f"subscription has been DISABLED for {held} s (limit {limit})")
            return Check("apply_lag", "OK", f"subscription disabled for {held} s (within {limit} s)")
        if state.pop("disabled_since", None) is not None:
            self._save_state(state)
        if running != "true":
            return Check("apply_lag", "ALERT", "no apply worker is running")
        if age == "":
            return Check("apply_lag", "UNKNOWN", "apply worker reports no latest_end_time")
        if int(age) > limit:
            return Check("apply_lag", "ALERT", f"apply has reported no progress for {age} s (limit {limit})")
        return Check("apply_lag", "OK", f"last progress {age} s ago")

    def check_follower(self) -> Check:
        rows = self.admin.rows(
            "SELECT status, coalesce(reason, ''), extract(epoch FROM now() - updated_at)::bigint::text, "
            "coalesce(extract(epoch FROM now() - head_mismatch_since)::bigint::text, '') FROM follower.state")
        if not rows:
            return Check("follower", "UNKNOWN", "follower.state is empty")
        status, reason, age, mismatch = rows[0]
        limit = self.cfg.getint("watchdog", "follower_lag_alert_seconds", 1800)
        if status == "halted":
            return Check("follower", "ALERT", f"follower HALTED: {reason}")
        if status == "in_progress" and int(age) > limit:
            return Check("follower", "ALERT", f"follower step in progress for {age} s")
        if mismatch and int(mismatch) > limit:
            return Check("follower", "ALERT", f"publisher and subscriber schema versions differ for {mismatch} s")
        return Check("follower", "OK", f"status={status}")

    def check_free_space(self) -> Check:
        path = self.cfg.require("cluster", "data_directory")
        floor = float(self.cfg.get("watchdog", "free_space_floor_gb", fallback="30") or 30)
        free = shutil.disk_usage(path).free / 1e9
        text = f"{path}: {free:.1f} GB free (floor {floor:.0f} GB)"
        return Check("free_space", "ALERT" if free < floor else "OK", text)

    def check_archiver(self) -> Check:
        if not self.cfg.getbool("pgbackrest", "enabled", False):
            return Check("archiver", "OK", "pgBackRest not enabled")
        rows = self.admin.rows(
            "SELECT failed_count::text, coalesce(last_failed_time::text, ''), coalesce(last_archived_time::text, ''), "
            "(coalesce(last_failed_time, '-infinity') > coalesce(last_archived_time, '-infinity'))::text "
            "FROM pg_stat_archiver")
        failed, last_failed, last_ok, failing = rows[0]
        if failing == "true":
            return Check("archiver", "ALERT", f"WAL archiving is failing (last failure {last_failed}, last success "
                         f"{last_ok or 'never'}); PITR is accumulating a gap")
        return Check("archiver", "OK", f"failed_count={failed}, last success {last_ok or 'never'}")

    def check_replica_identity(self) -> Check:
        offenders = replica_identity_offenders(self.pub, self.cfg.publication)
        if offenders:
            return Check("replica_identity", "ALERT",
                         "published table(s) with no usable replica identity; production UPDATE/DELETE on them "
                         "fails while they are published: " + ", ".join(offenders))
        return Check("replica_identity", "OK", "every published table has a primary key or a replica identity")

    def check_reverse(self) -> Check | None:
        rev = self.cfg.reverse("watchdog", "qontinui-pg-watchdog")
        if rev is None:
            return None
        name = self.cfg.require("watchdog", "reverse_subscription")
        rows = rev.rows(
            "SELECT ss.apply_error_count::text, "
            "coalesce(extract(epoch FROM now() - st.latest_end_time)::bigint::text, '') "
            "FROM pg_subscription s JOIN pg_stat_subscription_stats ss ON ss.subid = s.oid "
            "LEFT JOIN pg_stat_subscription st ON st.subid = s.oid AND st.relid IS NULL "
            f"WHERE s.subname = {lit(name)}")
        if not rows:
            return Check("reverse", "UNKNOWN", f"reverse subscription {name} not readable on RDS")
        errors, age = int(rows[0][0]), rows[0][1]
        state = self._state()
        before = state.get("reverse_apply_errors")
        state["reverse_apply_errors"] = errors
        self._save_state(state)
        limit = self.cfg.getint("watchdog", "lag_alert_seconds", 1800)
        if before is not None and errors > before:
            return Check("reverse", "ALERT", f"reverse apply_error_count rose {before} -> {errors}: a schema change "
                         "reached the reverse window (A6); end the window (Phase 8) or fix it")
        if age == "" or int(age) > limit:
            return Check("reverse", "ALERT", f"reverse lag {age or 'UNKNOWN'} s over {limit} s")
        return Check("reverse", "OK", f"reverse errors={errors}, lag {age} s")

    # -- state, notices ----------------------------------------------------------
    def _state_path(self) -> str:
        return self.cfg.get("watchdog", "state_file", fallback="/var/lib/qontinui-pg/watchdog-state.json") or ""

    def _state(self) -> dict:
        try:
            with open(self._state_path(), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        path = self._state_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)

    def allocated_storage(self) -> str:
        cmd = (self.cfg.get("watchdog", "allocated_storage_cmd") or "").strip()
        if not cmd:
            return "RDS AllocatedStorage: UNKNOWN (no allocated_storage_cmd configured on this host)"
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        except subprocess.SubprocessError as exc:
            return f"RDS AllocatedStorage: UNKNOWN ({exc})"
        if out.returncode != 0:
            return f"RDS AllocatedStorage: UNKNOWN (reader exited {out.returncode})"
        return f"RDS AllocatedStorage: {out.stdout.strip()}"

    def run(self) -> list[Check]:
        checks = [
            self._guard("slot", self.check_slot),
            self._guard("apply_lag", self.check_apply_lag),
            self._guard("follower", self.check_follower),
            self._guard("free_space", self.check_free_space),
            self._guard("archiver", self.check_archiver),
            self._guard("replica_identity", self.check_replica_identity),
        ]
        rev = self._guard("reverse", lambda: self.check_reverse() or Check("reverse", "OK", "not configured"))
        checks.append(rev)
        for c in checks:
            log("watchdog-check", f"{c.name} {c.status}", detail=c.message)
        bad = [c for c in checks if c.status != "OK"]
        state = self._state()
        sent = state.get("sent", {})
        renotify = self.cfg.getint("watchdog", "renotify_minutes", 60) * 60
        now = time.time()
        fresh = {}
        for c in bad:
            key = f"{c.name}:{c.status}"
            prior = sent.get(key)
            if prior and prior.get("message") == c.message and now - prior.get("at", 0) < renotify:
                fresh[key] = prior
                continue
            fresh[key] = {"message": c.message, "at": now}
            self.notifier.notice(
                f"local/postgres watchdog {c.status}: {c.name}",
                f"{c.message}\n{self.allocated_storage()}",
                {"check": c.name, "status": c.status})
        state["sent"] = fresh
        self._save_state(state)
        return checks
