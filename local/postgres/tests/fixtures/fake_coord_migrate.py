#!/usr/bin/env python3
"""A minimal stand-in for r16's ``coord-migrate`` (adopt / apply --to).

``adopt --to V`` creates the three history tables and records V as the boot
lane's baseline. ``apply --to V`` applies each fixture file ``<version>.<lane>.sql``
up to V that its lane has not recorded:

* boot / contract: the file and its history-row INSERT in ONE transaction;
* background (``coord:no-transaction``): the file's statement commits on its
  own, then the history row in a transaction of its own.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.environ.get("QPG_SRC", os.path.join(os.path.dirname(__file__), "..", "..")), "lib"))
from qpg.pg import Psql, lit  # noqa: E402

LANES = ("boot", "background", "contract")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["adopt", "apply"])
    ap.add_argument("--dir", required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--to", required=True)
    a = ap.parse_args()
    db = Psql(a.dsn, label="fake-coord-migrate")
    if a.command == "adopt":
        stmts = [
            f"CREATE TABLE IF NOT EXISTS public.coord_schema_history_{lane} ("
            "version text PRIMARY KEY, applied_on timestamptz NOT NULL DEFAULT now(), checksum text)"
            ";" for lane in LANES
        ]
        stmts.append("INSERT INTO public.coord_schema_history_boot (version, checksum) "
                     f"VALUES ({lit(a.to)}, 'baseline') "
                     "ON CONFLICT DO NOTHING;")
        db.execute("\n".join(stmts))
        return 0
    files = sorted(f for f in os.listdir(a.dir) if f.endswith(".sql"))
    for f in files:
        version, lane, _ = f.split(".")
        if version > a.to:
            continue
        done = db.scalar(f"SELECT count(*) FROM public.coord_schema_history_{lane} WHERE version = {lit(version)}")
        if done != "0":
            continue
        with open(os.path.join(a.dir, f), encoding="utf-8") as fh:
            body = fh.read()
        row = f"INSERT INTO public.coord_schema_history_{lane} (version, checksum) VALUES ({lit(version)}, {lit(f)});"
        if lane == "background":
            db.execute(body, single_transaction=False)
            db.execute(row)
        else:
            db.execute(body + "\n" + row)
    return 0


if __name__ == "__main__":
    sys.exit(main())
