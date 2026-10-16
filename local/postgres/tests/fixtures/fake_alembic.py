#!/usr/bin/env python3
"""A minimal stand-in for ``alembic upgrade <rev>`` over fixture SQL files.

Revisions are ``NNN_<rev>.sql`` in ``--dir``, chained in file-name order. Like
qontinui-web's ``env.py``, ONE upgrade run is ONE transaction: every pending
revision's SQL followed by its ``UPDATE public.alembic_version``. Used as the
"production migrator" on the publisher and as the follower's ``migrate_cmd`` on
the subscriber.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("QPG_SRC", os.path.join(os.path.dirname(__file__), "..", "..")), "lib"))
from qpg.pg import Psql, lit  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("command", choices=["upgrade"])
    ap.add_argument("rev")
    a = ap.parse_args()
    files = sorted(f for f in os.listdir(a.dir) if f.endswith(".sql"))
    # r1 is the base schema (fixtures/base.sql), loaded before any upgrade.
    chain = [("r1", None)] + [(f.split("_", 1)[1][:-4], os.path.join(a.dir, f)) for f in files]
    revs = [r for r, _ in chain]
    if a.rev not in revs:
        print(f"unknown revision {a.rev}", file=sys.stderr)
        return 2
    db = Psql(a.dsn, label="fake-alembic")
    current = db.scalar("SELECT version_num FROM public.alembic_version")
    start = revs.index(current) + 1 if current in revs else 0
    end = revs.index(a.rev) + 1
    if start >= end:
        return 0  # at or past the target: alembic's upgrade does nothing
    script = []
    for rev, path in chain[start:end]:
        with open(path, encoding="utf-8") as fh:
            script.append(fh.read())
        script.append(f"UPDATE public.alembic_version SET version_num = {lit(rev)};")
    db.execute("\n".join(script))
    return 0


if __name__ == "__main__":
    sys.exit(main())
