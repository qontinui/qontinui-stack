#!/usr/bin/env python3
"""Source mutations that must turn named tests RED (testing a-test-must-be-able-to-fail).

Each mutation replaces exactly one occurrence of ``old`` with ``new`` in a COPY
of the local/postgres tree (``apply`` refuses when ``old`` does not occur
exactly once, so a refactor cannot silently make a mutation vacuous). inside.sh
runs every listed test against the mutated copy and fails the suite if any of
them passes.

Usage: mutations.py list | apply <name> <tree>
"""
import os
import sys

MUTATIONS = {
    # Review blocker 2: the replay's inserted rows are not undone, so the
    # publisher's copy of the migration collides with them.
    "no-undo": (
        "lib/qpg/follower.py",
        'match = " AND ".join(f"t.{ident(k)} = r.{ident(k)}" for k in key)',
        'match = "false AND " + " AND ".join(f"t.{ident(k)} = r.{ident(k)}" for k in key)',
        ["test_follower.DoubleDmlTest", "test_follower.CoordLaneTest", "test_follower.ConcurrentWritesTest"],
    ),
    # Review blocker 2, the race itself: put back 254ab54's overwrite of a
    # replicated table from the publisher's CURRENT rows. Rows committed there
    # but not yet applied get copied in, and apply's own INSERT then collides.
    "overwrite-from-publisher": (
        "lib/qpg/follower.py",
        "        if detail.get(\"was_enabled\", True):\n",
        "        self.admin.execute_with_copy(\n"
        "            \"SET LOCAL session_replication_role = replica; DELETE FROM app.settings;\",\n"
        "            \"COPY app.settings (id, name, value) FROM STDIN\",\n"
        "            self.pub.copy_out(\"SELECT id, name, value FROM app.settings\"))\n"
        "        if detail.get(\"was_enabled\", True):\n",
        ["test_follower.ConcurrentWritesTest"],
    ),
    # Review blocker 1: the follower reads table data from the publisher.
    "reads-publisher-data": (
        "lib/qpg/follower.py",
        "                self._check_replay_shape(detail)\n",
        "                self._check_replay_shape(detail)\n"
        "                self.pub.rows(\"SELECT count(*) FROM app.items\")\n",
        ["test_follower.DoubleDmlTest"],
    ),
    # Re-review A4: the replay-log trigger on partitions as well as their parent.
    "log-trigger-on-partitions": (
        "lib/qpg/follower.py",
        '                  for t in scope.values() if not t["partition"]]',
        '                  for t in scope.values()]',
        ["test_follower.DoubleDmlTest"],
    ),
    # Re-review A2: a renamed table is not detected.
    "no-rename-check": (
        "lib/qpg/follower.py",
        "        if renamed and unresolved:\n",
        "        if False:\n",
        ["test_follower.RenameTest"],
    ),
    # Re-review A3: new tables emptied with TRUNCATE, which a foreign key from
    # an existing table refuses on every attempt.
    "truncate-new-tables": (
        "lib/qpg/follower.py",
        '            script.append(f"DELETE FROM {qualified(*t.split(\'.\', 1))};")',
        '            script.append(f"TRUNCATE {qualified(*t.split(\'.\', 1))};")',
        ["test_follower.ForeignKeyNewTableTest"],
    ),
    # Re-review B1: DML-before-later-DDL is not named.
    "no-dml-ddl-classification": (
        "lib/qpg/follower.py",
        '        if stall.sqlstate in ("55000", "23502"):',
        "        if False:",
        ["test_follower.DmlThenDdlTest"],
    ),
    # Re-review B5: stored column defaults are not compared.
    "no-missing-value-check": (
        "lib/qpg/follower.py",
        "        if differing:\n",
        "        if False:\n",
        ["test_follower.MissingValueTest"],
    ),
    # Re-review C: the follower runs without the known gaps acknowledged.
    "no-ack-guard": (
        "lib/qpg/follower.py",
        '        if not self.cfg.getbool("follower", "acknowledge_known_gaps", False):',
        "        if False:",
        ["test_follower.AcknowledgeTest"],
    ),
    # Re-review B1: rewrites after the replay's DML are not checked.
    "no-rewrite-check": (
        "lib/qpg/follower.py",
        "        if unsafe:\n",
        "        if False:\n",
        ["test_follower.TypeRewriteTest"],
    ),
    # Re-review B2: stored defaults keyed by column NAME, so a drop + re-add is missed.
    "missing-by-name": (
        "lib/qpg/follower.py",
        "\"SELECT a.attrelid::text || ':' || a.attnum::text, a.attname,",
        "\"SELECT a.attrelid::text || ':' || a.attname, a.attname,",
        ["test_follower.DropAddColumnTest"],
    ),
    # Re-review B3: `follower resolve` does not record the resolution.
    "resolve-not-recorded": (
        "lib/qpg/follower.py",
        '        script = [f"INSERT INTO follower.resolutions (sub_oid, xid, kase, target) VALUES '
        '({lit(sub)}, {lit(xid)}, "\n'
        '                  f"{lit(kase)}, {lit(target)}) ON CONFLICT DO NOTHING;"]',
        '        script = ["SELECT 1;"]',
        ["test_follower.RenameTest", "test_follower.MissingValueTest", "test_follower.TypeRewriteTest"],
    ),
    # Re-review B4: the stored-default check ignores recorded resolutions.
    "resolution-ignored": (
        "lib/qpg/follower.py",
        '            if self._resolved(xid, "missing-value", f"{table}.{column}"):\n',
        "            if False:\n",
        ["test_follower.MissingValueTest"],
    ),
    # Re-review B5: a halt leaves the subscription enabled.
    "halt-leaves-enabled": (
        "lib/qpg/follower.py",
        '            if enabled == "true":\n                self.admin.execute(f"ALTER SUBSCRIPTION',
        '            if False:\n                self.admin.execute(f"ALTER SUBSCRIPTION',
        ["test_follower.DmlThenDdlTest"],
    ),
    # Fourth review B1: resolving a rewrite / key change keeps its log rows, so it re-halts forever.
    "resolve-keeps-log": (
        "lib/qpg/follower.py",
        '        if kase in ("rewrite", "key-change"):\n',
        "        if False:\n",
        ["test_follower.TypeRewriteTest", "test_follower.KeyChangeTest"],
    ),
    # Fourth review B2: a computed default added before a rewrite is not checked.
    "no-gained-default-check": (
        "lib/qpg/follower.py",
        "                if gained:\n",
        "                if False:\n",
        ["test_follower.ComputedDefaultRewriteTest"],
    ),
    # Fourth review B3: a halt leaves the replay triggers (one database-wide) installed.
    "halt-keeps-triggers": (
        "lib/qpg/follower.py",
        "        cleaned = self._cleanup_replay_triggers()\n",
        "        cleaned = ''\n",
        ["test_follower.MigrateFailureTest"],
    ),
    # Fifth review 1: copy-columns reads the publisher without the version guard.
    "no-version-guard": (
        "lib/qpg/follower.py",
        '                prelude="SET LOCAL row_security = off;\\n" + self._version_guard(),',
        '                prelude="SET LOCAL row_security = off;\\n",',
        ["test_follower.CopyColumnsTest"],
    ),
    # Fifth review 2: copy-columns runs without the follower lock.
    "copy-columns-no-lock": (
        "lib/qpg/follower.py",
        "        with follower_lock(self.lock_path):\n            return self._copy_columns(",
        "        if True:\n            return self._copy_columns(",
        ["test_follower.CopyColumnsTest"],
    ),
    # Fifth review 5: the publisher read honours row security (silently filtering).
    "copy-columns-rls-on": (
        "lib/qpg/follower.py",
        '                prelude="SET LOCAL row_security = off;\\n" + self._version_guard(),',
        '                prelude=self._version_guard(),',
        ["test_follower.CopyColumnsTest"],
    ),
    # Fifth review 3: only gained columns that still have a default count.
    "gained-needs-default": (
        "lib/qpg/follower.py",
        '                    f"SELECT a.attname FROM pg_attribute a WHERE a.attrelid = {oid} AND NOT a.attisdropped "',
        '                    f"SELECT a.attname FROM pg_attribute a JOIN pg_attrdef d ON d.adrelid = a.attrelid "\n'
        '                    f"AND d.adnum = a.attnum WHERE a.attrelid = {oid} AND NOT a.attisdropped "',
        ["test_follower.DropDefaultRewriteTest"],
    ),
    # Fifth review 4: relog re-installs the triggers by the stored names.
    "relog-by-stored-name": (
        "lib/qpg/follower.py",
        '                self._install_log_triggers({oid: current[oid] for oid in detail["scope"] if oid in current})',
        '                self._install_log_triggers(detail["scope"])',
        ["test_follower.RelogTest"],
    ),
    # Sixth review: an owed '*' is cleared by a copy of any single column.
    "owed-cleared-by-any-copy": (
        "lib/qpg/follower.py",
        "            covered_all = set(all_nonkey) <= set(cols)\n",
        "            covered_all = True\n",
        ["test_follower.CopyColumnsTest"],
    ),
    # A3's refusal removed.
    "no-move-refusal": (
        "lib/qpg/follower.py",
        "moved = self.moved_out_of_publication()",
        "moved = []",
        ["test_follower.MoveRefusalTest"],
    ),
    # B2: the stall marker runs as the table owner, not as the follower role.
    "no-security-definer": (
        "lib/qpg/follower_schema.py",
        "LANGUAGE plpgsql\nSECURITY DEFINER\nSET search_path = pg_catalog, follower\nAS $fn$\nDECLARE\n    v text;",
        "LANGUAGE plpgsql\nSET search_path = pg_catalog, follower\nAS $fn$\nDECLARE\n    v text;",
        ["test_follower.OwnershipTest"],
    ),
    # B3: the diff stops comparing policies.
    "secdiff-ignores-policies": (
        "lib/qpg/secdiff.py",
        "    for section in a:\n",
        "    for section in [k for k in a if k != 'policies']:\n",
        ["test_ops.SecDiffTest"],
    ),
    # A2 item 6: the watchdog stops reading replica identities.
    "watchdog-no-replica-identity": (
        "lib/qpg/watchdog.py",
        "offenders = replica_identity_offenders(self.pub, self.cfg.publication)",
        "offenders = []",
        ["test_ops.WatchdogTest"],
    ),
    # Orphaned tablesync slots: the watchdog stops reading them.
    "watchdog-no-orphan-sync-slots": (
        "lib/qpg/watchdog.py",
        "found = orphan_sync_slots(self.pub)",
        "found = []",
        ["test_ops.WatchdogTest"],
    ),
    # The own-subscription exclusion removed: a table still syncing reads as an orphan.
    "orphan-no-syncing-exclusion": (
        "lib/qpg/watchdog.py",
        "own = [o for o in found if any(o[0].startswith(p) for p in syncing)]",
        "own = []",
        ["test_ops.WatchdogTest"],
    ),
    # The LIKE pattern unescaped: '_' becomes a wildcard and look-alike names match.
    "orphan-pattern-unescaped": (
        "lib/qpg/watchdog.py",
        "LIKE 'pg\\\\_%\\\\_sync\\\\_%'",
        "LIKE 'pg_%_sync_%'",
        ["test_ops.WatchdogTest"],
    ),
    # partition-parity: an unreadable side reported as a difference (exit 1), not UNKNOWN.
    "parity-unknown-is-difference": (
        "lib/qpg/cli.py",
        "could not be read: {exc}\", file=sys.stderr)\n        return 2\n",
        "could not be read: {exc}\", file=sys.stderr)\n        return 1\n",
        ["test_ops.PartitionParityTest"],
    ),
    # D9: the local lookahead drifts back from lane D's 14 days to 3.
    "partition-lookahead-3": (
        "lib/qpg/partitions.py",
        "DEFAULT_LOOKAHEAD_DAYS = 14",
        "DEFAULT_LOOKAHEAD_DAYS = 3",
        ["test_ops.PartitionsTest"],
    ),
    # Phase 3 parity: children compared by count instead of by name.
    "partition-parity-by-count": (
        "lib/qpg/partitions.py",
        "    if set(pub_children) != set(sub_children):",
        "    if len(pub_children) != len(sub_children):",
        ["test_ops.PartitionParityTest"],
    ),
    # D4: --check stops asserting who may log in without TLS.
    "hba-check-accepts-any-non-tls": (
        "lib/qpg/hostcfg.py",
        "ok = r.database == database and r.user == app and _in_tailnet(r.address, cidrs)",
        "ok = True",
        ["test_installer.InstallerTest"],
    ),
    # A4: --check stops guarding the coord source after Phase 8.
    "no-a4-guard": (
        "lib/qpg/config.py",
        "        if not self.phase8_complete:\n            return []\n",
        "        return []\n",
        ["test_installer.InstallerTest"],
    ),
    # D6: a NULL read becomes a zero instead of an error.
    "seqsync-null-is-zero": (
        "lib/qpg/seqsync.py",
        "                    errors.append(f\"{seq}: last_value read NULL and the role lacks SELECT on it\")\n",
        "                    pass\n",
        ["test_ops.SeqSyncTest"],
    ),
}


def apply(name: str, tree: str) -> None:
    rel, old, new, _ = MUTATIONS[name]
    path = os.path.join(tree, rel)
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    count = text.count(old)
    if count != 1:
        sys.exit(f"mutation {name}: expected exactly one occurrence in {rel}, found {count}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.replace(old, new))


if __name__ == "__main__":
    if sys.argv[1:2] == ["list"]:
        for name, (_, _, _, tests) in MUTATIONS.items():
            print(name, " ".join(tests))
    elif sys.argv[1:2] == ["apply"]:
        apply(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
