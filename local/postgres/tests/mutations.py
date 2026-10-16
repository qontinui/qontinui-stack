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
    # D7 step 3 disabled: the publisher's copy of the migration is not skipped.
    "no-skip": (
        "lib/qpg/follower.py",
        "f\"ALTER SUBSCRIPTION {ident(self.cfg.subscription)} SKIP (lsn = {lit(detail['finish_lsn'])})\"",
        "\"SELECT 'mutation: SKIP disabled'\"",
        ["test_follower.DoubleDmlTest", "test_follower.CoordLaneTest"],
    ),
    # D7 step 4 disabled: differences are found but never overwritten.
    "no-repair": (
        "lib/qpg/repair.py",
        "        result.rewritten += overwrite(pub, sub, s, keys)\n",
        "        result.clean = True\n        return result\n",
        ["test_follower.DoubleDmlTest", "test_follower.CoordLaneTest"],
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
        "LANGUAGE plpgsql\nSECURITY DEFINER\n",
        "LANGUAGE plpgsql\n",
        ["test_follower.OwnershipTest"],
    ),
    # D4: --check stops asserting who may log in without TLS.
    "hba-check-accepts-any-non-tls": (
        "lib/qpg/hostcfg.py",
        "ok = r.database == database and r.user == app and _in_tailnet(r.address, cidrs)",
        "ok = True",
        ["test_installer.InstallerTest"],
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
