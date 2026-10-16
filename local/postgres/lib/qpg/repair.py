"""D7 step 4: compare a table with the publisher and overwrite differences.

The publisher is authoritative. A table under ``full_max_rows`` is compared row
by row (key text + md5 of the row); above it, rows are hashed into buckets of
about ``bucket_rows`` and only differing buckets are compared row by row.

Rows are rendered in a canonical session (UTC, ISO dates, shortest-exact floats,
hex bytea) and columns are taken by name in sorted order, so the same row
renders to the same text on both sides. Ordering inside a bucket digest uses
``COLLATE "C"`` so a collation difference between the two servers cannot make
equal buckets differ.

Overwrite runs on the subscriber as the local superuser with
``session_replication_role = replica`` — the same mode the apply worker uses — so
foreign keys and user triggers behave as they do for replicated rows. Each batch
is one transaction: delete the differing keys, then ``COPY`` the publisher's
rows for those keys back in. Replication keeps running throughout, so the
compare is repeated after the write until it reads clean (bounded attempts).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .pg import CANONICAL_SESSION, Psql, ident, lit, qualified, text_array


class RepairError(RuntimeError):
    pass


@dataclass
class TableShape:
    schema: str
    name: str
    columns: list[str]
    key: list[str] | None  # None: whole-row (multiset) comparison

    @property
    def qname(self) -> str:
        return qualified(self.schema, self.name)

    def row_md5(self) -> str:
        cols = ", ".join(ident(c) for c in self.columns)
        return f"md5(ROW({cols})::text)"

    def key_text(self) -> str:
        if self.key is None:
            return self.row_md5()
        cols = ", ".join(ident(c) for c in self.key)
        return f"ROW({cols})::text"


@dataclass
class RepairResult:
    table: str
    mode: str
    rows_estimate: int
    differing_first: int = 0
    rewritten: int = 0
    attempts: int = 0
    clean: bool = False
    notes: list[str] = field(default_factory=list)


def _columns(db: Psql, schema: str, name: str) -> list[str]:
    rows = db.rows(
        "SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {lit(schema)} AND c.relname = {lit(name)} "
        "AND a.attnum > 0 AND NOT a.attisdropped AND a.attgenerated = '' ORDER BY a.attname"
    )
    return [r[0] for r in rows]


def _key(db: Psql, schema: str, name: str) -> list[str] | None:
    rows = db.rows(
        "SELECT a.attname FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
        f"WHERE n.nspname = {lit(schema)} AND c.relname = {lit(name)} "
        "AND (i.indisprimary OR (i.indisreplident AND NOT EXISTS ("
        "  SELECT 1 FROM pg_index p WHERE p.indrelid = c.oid AND p.indisprimary))) "
        "ORDER BY i.indisprimary DESC, k.ord"
    )
    return [r[0] for r in rows] or None


def shape(pub: Psql, sub: Psql, table: str) -> TableShape:
    schema, name = table.split(".", 1)
    pub_cols = _columns(pub, schema, name)
    sub_cols = _columns(sub, schema, name)
    if not pub_cols or not sub_cols:
        raise RepairError(f"{table}: missing on {'publisher' if not pub_cols else 'subscriber'}")
    if pub_cols != sub_cols:
        raise RepairError(
            f"{table}: column sets differ (publisher {pub_cols}, subscriber {sub_cols}); "
            "the schema has diverged and a row repair cannot fix that"
        )
    return TableShape(schema, name, sub_cols, _key(sub, schema, name))


def _estimate(pub: Psql, s: TableShape) -> int:
    value = pub.scalar(
        "SELECT c.reltuples::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        f"WHERE n.nspname = {lit(s.schema)} AND c.relname = {lit(s.name)}"
    )
    est = int(value) if value is not None else -1
    if est < 0:  # never analyzed
        est = int(pub.scalar(f"SELECT count(*) FROM {s.qname}") or 0)
    return est


def _row_map(db: Psql, s: TableShape, where: str = "true") -> dict[str, str]:
    if s.key is None:
        sql = (f"SELECT {s.row_md5()}, count(*)::text FROM {s.qname} WHERE {where} GROUP BY 1")
    else:
        sql = f"SELECT {s.key_text()}, {s.row_md5()} FROM {s.qname} WHERE {where}"
    return {r[0]: r[1] for r in db.rows(sql, canonical=True)}


def _bucket_expr(s: TableShape, buckets: int) -> str:
    return f"((('x' || substr(md5({s.key_text()}), 1, 8))::bit(32)::bigint) % {buckets})"


def _bucket_map(db: Psql, s: TableShape, buckets: int) -> dict[str, str]:
    expr = _bucket_expr(s, buckets)
    sql = (
        f"SELECT {expr}::text, md5(string_agg({s.key_text()} || ':' || {s.row_md5()}, ',' "
        f"ORDER BY {s.key_text()} COLLATE \"C\")) FROM {s.qname} GROUP BY 1"
    )
    return {r[0]: r[1] for r in db.rows(sql, canonical=True)}


def differing_keys(pub: Psql, sub: Psql, s: TableShape, full_max_rows: int, bucket_rows: int,
                   estimate: int) -> tuple[str, set[str]]:
    if s.key is None or estimate <= full_max_rows:
        mode = "full" if s.key is not None else "whole-row"
        a, b = _row_map(pub, s), _row_map(sub, s)
        return mode, {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    buckets = max(1, math.ceil(estimate / max(1, bucket_rows)))
    a, b = _bucket_map(pub, s, buckets), _bucket_map(sub, s, buckets)
    bad = [k for k in set(a) | set(b) if a.get(k) != b.get(k)]
    keys: set[str] = set()
    for bucket in bad:
        where = f"{_bucket_expr(s, buckets)} = {int(bucket)}"
        ra, rb = _row_map(pub, s, where), _row_map(sub, s, where)
        keys |= {k for k in set(ra) | set(rb) if ra.get(k) != rb.get(k)}
    return f"bucketed({buckets})", keys


def overwrite(pub: Psql, sub: Psql, s: TableShape, keys: set[str], batch: int = 500) -> int:
    cols = ", ".join(ident(c) for c in s.columns)
    ordered = sorted(keys)
    written = 0
    for i in range(0, len(ordered), batch):
        chunk = ordered[i:i + batch]
        match = f"{s.key_text()} = ANY({text_array(chunk)})"
        data = pub.copy_out(f"SELECT {cols} FROM {s.qname} WHERE {match}")
        before = (
            CANONICAL_SESSION
            + "\nSET LOCAL session_replication_role = replica;\n"
            + f"DELETE FROM {s.qname} WHERE {match};"
        )
        sub.execute_with_copy(before, f"COPY {s.qname} ({cols}) FROM STDIN", data)
        written += len(chunk)
    return written


def repair_table(pub: Psql, sub: Psql, table: str, full_max_rows: int = 1_000_000,
                 bucket_rows: int = 10_000, attempts: int = 3) -> RepairResult:
    s = shape(pub, sub, table)
    estimate = _estimate(pub, s)
    result = RepairResult(table=table, mode="", rows_estimate=estimate)
    for attempt in range(1, attempts + 1):
        result.attempts = attempt
        mode, keys = differing_keys(pub, sub, s, full_max_rows, bucket_rows, estimate)
        result.mode = mode
        if attempt == 1:
            result.differing_first = len(keys)
        if not keys:
            result.clean = True
            return result
        result.rewritten += overwrite(pub, sub, s, keys)
    mode, keys = differing_keys(pub, sub, s, full_max_rows, bucket_rows, estimate)
    result.clean = not keys
    if keys:
        result.notes.append(f"{len(keys)} key(s) still differ after {attempts} attempts")
    return result
