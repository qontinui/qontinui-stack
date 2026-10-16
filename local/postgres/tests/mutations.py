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
        "                detail[\"undone\"] = self._undo_replay_inserts(detail)\n",
        "                detail[\"undone\"] = self._undo_replay_inserts(detail)\n"
        "                self.pub.rows(\"SELECT count(*) FROM app.items\")\n",
        ["test_follower.DoubleDmlTest"],
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
