"""D9: the local partition-maintenance timer.

Partition DDL does not replicate. After lane D converts ``coord.test_results``
to daily partitions, rows arrive addressed to the parent
(``publish_via_partition_root``), so a day with no local child fails apply, and
the publisher's ``DROP TABLE`` of an expired child never arrives. This calls the
same alembic-authored function coord's retention sweep calls, with the same
arguments and cadence, on the subscriber.

It runs as the APPLICATION role, so the children it creates are owned by
``qontinui_user`` like every other replicated table (B2's owner premise).

Before lane D's revision exists locally, the function is absent and the run is
a reported no-op (``require_function = false``, the default) — the follower's
replay of that revision creates it. At cutover the timer stops
(``enabled = false``), because coord's own sweep then runs against this
database.
"""

from __future__ import annotations

from .config import Config
from .notify import log
from .pg import lit, qualified


class PartitionError(RuntimeError):
    pass


def run(cfg: Config) -> str:
    if not cfg.getbool("partitions", "enabled", False):
        log("partitions", "disabled ([partitions] enabled = false); nothing done")
        return "disabled"
    fn = cfg.get("partitions", "function", fallback="coord.test_results_partition_maintenance") or ""
    retention = cfg.getint("partitions", "retention_days", 14)
    lookahead = cfg.getint("partitions", "lookahead_days", 3)
    app = cfg.sub_app("qontinui-pg-partitions")
    signature = f"{fn}(integer,integer)"
    present = app.scalar(f"SELECT to_regprocedure({lit(signature)}) IS NOT NULL")
    if present != "t":
        if cfg.getbool("partitions", "require_function", False):
            raise PartitionError(f"{signature} does not exist on the subscriber")
        log("partitions", f"{signature} absent on the subscriber; nothing to maintain yet")
        return "absent"
    schema, name = fn.split(".", 1)
    result = app.rows(f"SELECT {qualified(schema, name)}({int(retention)}, {int(lookahead)})::text")
    log("partitions", f"{signature} ran", retention_days=retention, lookahead_days=lookahead,
        result=result[0][0] if result else None)
    return "ran"
