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

``qontinui-pg partition-parity`` is Phase 3's partition check. It compares the
NAMES of every child partition under each published partitioned table on both
sides (``pg_partition_tree``), never their counts: a publisher that dropped
yesterday's child and created tomorrow's has the same count as a subscriber
that still holds yesterday's and missed tomorrow's (measured: qontinui-dev-notes
investigation-results/2026-10-17-off-aws-phase0-measured.md §4e, 16 children on
each side as different sets).
"""

from __future__ import annotations

from .config import Config
from .notify import log
from .pg import lit, qualified


# D9's "same arguments" rule: the local call must use coord's values, or the
# first row dated past the local horizon fails apply ("no partition of relation
# ... found for row") and the slot is invalidated hours later (Phase 0 measured
# it with a 3-day local lookahead against a 14-day publisher).
#  * lookahead: lane D (plan 2026-10-14-coord-high-ingest-tables-are-unbounded,
#    D2 "Lookahead is 14 days" and Phase 4's
#    COORD_TEST_RESULTS_PARTITION_LOOKAHEAD_DAYS, default 14). Not on coord's
#    main yet; when it lands, its flag_registry.rs default is the authority.
#  * retention: qontinui-coord crates/coord/src/table_retention.rs,
#    parse_days("COORD_TEST_RESULTS_RETENTION_DAYS", 14).
# A host whose coord overrides either flag sets the same value in [partitions].
DEFAULT_LOOKAHEAD_DAYS = 14
DEFAULT_RETENTION_DAYS = 14


class PartitionError(RuntimeError):
    pass


def run(cfg: Config) -> str:
    if not cfg.getbool("partitions", "enabled", False):
        log("partitions", "disabled ([partitions] enabled = false); nothing done")
        return "disabled"
    fn = cfg.get("partitions", "function", fallback="coord.test_results_partition_maintenance") or ""
    retention = cfg.getint("partitions", "retention_days", DEFAULT_RETENTION_DAYS)
    lookahead = cfg.getint("partitions", "lookahead_days", DEFAULT_LOOKAHEAD_DAYS)
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


def published_partition_roots(pub, publication: str) -> list[str]:
    """Every partitioned table at the root of a tree the publication carries."""
    rows = pub.rows(
        "SELECT DISTINCT rn.nspname || '.' || r.relname "
        "FROM pg_publication_tables pt "
        "JOIN pg_namespace n ON n.nspname = pt.schemaname "
        "JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = pt.tablename "
        "JOIN pg_class r ON r.oid = pg_partition_root(c.oid) "
        "JOIN pg_namespace rn ON rn.oid = r.relnamespace "
        f"WHERE pt.pubname = {lit(publication)} AND r.relkind = 'p' ORDER BY 1")
    return [r[0] for r in rows]


def child_partitions(psql, parent: str) -> list[str] | None:
    """Schema-qualified names of every partition under ``parent`` (not itself); None when it is absent."""
    if psql.scalar(f"SELECT to_regclass({lit(parent)}) IS NOT NULL") != "t":
        return None
    rows = psql.rows(
        "SELECT n.nspname || '.' || c.relname FROM pg_partition_tree(" + f"{lit(parent)}::regclass) t "
        "JOIN pg_class c ON c.oid = t.relid JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE t.level > 0 ORDER BY 1")
    return [r[0] for r in rows]


def compare_children(parent: str, pub_children: list[str] | None, sub_children: list[str] | None) -> dict | None:
    """The difference for one parent, by NAME; None when both sides hold the same set."""
    if pub_children is None or sub_children is None:
        absent = ("both" if pub_children is None and sub_children is None
                  else "publisher" if pub_children is None else "subscriber")
        return {"parent": parent, "absent_on": absent}
    if set(pub_children) != set(sub_children):
        return {"parent": parent,
                "only_on_publisher": sorted(set(pub_children) - set(sub_children)),
                "only_on_subscriber": sorted(set(sub_children) - set(pub_children)),
                "count_publisher": len(pub_children), "count_subscriber": len(sub_children)}
    return None


def parity(cfg: Config, parents: list[str] | None = None) -> dict:
    """Phase 3's partition parity: child names per published partitioned table, publisher vs subscriber."""
    pub = cfg.publisher("qontinui-pg-partition-parity")
    sub = cfg.sub_admin("qontinui-pg-partition-parity")
    roots = parents or published_partition_roots(pub, cfg.publication)
    differences, checked = [], []
    for parent in roots:
        a, b = child_partitions(pub, parent), child_partitions(sub, parent)
        checked.append({"parent": parent, "publisher": len(a) if a is not None else None,
                        "subscriber": len(b) if b is not None else None})
        diff = compare_children(parent, a, b)
        if diff:
            differences.append(diff)
    return {"equal": not differences, "checked": checked, "differences": differences}
