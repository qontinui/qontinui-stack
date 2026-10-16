"""D6: copy sequence positions from the source to the target, with a margin.

Logical replication carries no sequence values. Every 5 minutes this reads
``pg_sequences.last_value`` on the source over the replication role and moves
the target's sequence to ``source + margin`` — never backwards.

* **Per-sequence margin (D6 correction).** 3x the largest 5-minute advance seen
  in the last ``window_hours`` (24 h), never under ``floor`` (1,000). The final
  sync at cutover (``--final``) uses ``final_floor`` (10,000) as the floor.
  Advances are computed from samples stored on the subscriber in
  ``follower.sequence_samples``.
* **A NULL read is an error, never a zero.** ``pg_sequences.last_value`` is NULL
  both for a sequence nobody has called yet and for one the reading role may
  not SELECT. The first is reported as ``unused`` and left alone; the second
  fails the run (the replication role needs SELECT on the published schemas'
  sequences, OD3).
* **Reverse (D8, the 7 rollback days).** ``--reverse`` reads the local primary
  and writes ``[seqsync] reverse_dsn`` (RDS).
* **From data (Phase 4 restore runbook).** ``from-data`` sets every sequence
  OWNED BY a column to ``max(column) + floor`` from the local data itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, ConfigError
from .notify import log
from .pg import Psql, ident, lit, qualified, text_array

FIVE_MINUTES = 300.0


class SeqSyncError(RuntimeError):
    pass


@dataclass
class SeqResult:
    seq: str
    source_value: int | None
    target_before: int | None
    target_after: int | None
    margin: int
    action: str  # set | ahead | unused | error


def read_source(db: Psql, schemas: list[str]) -> dict[str, tuple[int | None, bool]]:
    rows = db.rows(
        "SELECT quote_ident(schemaname) || '.' || quote_ident(sequencename), last_value::text, "
        "has_sequence_privilege(quote_ident(schemaname) || '.' || quote_ident(sequencename), 'SELECT')::text "
        f"FROM pg_sequences WHERE schemaname = ANY({text_array(schemas)}) ORDER BY 1")
    return {r[0]: (int(r[1]) if r[1] is not None else None, r[2] == "true") for r in rows}


def max_advance(samples: list[tuple[float, int]]) -> float:
    """Largest advance per 5 minutes between consecutive (epoch, value) samples.

    The rate between two samples is scaled to 5 minutes; pairs closer than 60 s
    are scaled as if 60 s apart, so two near-simultaneous reads cannot
    extrapolate a burst into a huge margin.
    """
    best = 0.0
    ordered = sorted(samples)
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:], strict=False):
        dt = t1 - t0
        if dt <= 0 or v1 <= v0:
            continue
        best = max(best, (v1 - v0) * FIVE_MINUTES / max(dt, 60.0))
    return best


def margin_for(samples: list[tuple[float, int]], floor: int) -> int:
    return max(floor, int(3 * max_advance(samples) + 0.999))


class SeqSync:
    def __init__(self, cfg: Config, reverse: bool = False):
        self.cfg = cfg
        self.local = cfg.sub_admin("qontinui-pg-seqsync")
        if reverse:
            target = cfg.reverse("seqsync", "qontinui-pg-seqsync")
            if target is None:
                raise ConfigError("[seqsync] reverse_dsn is not configured")
            self.source, self.target = self.local, target
        else:
            self.source, self.target = cfg.publisher("qontinui-pg-seqsync"), self.local
        self.reverse = reverse
        self.schemas = cfg.published_schemas

    def _samples(self, seq: str, hours: int) -> list[tuple[float, int]]:
        rows = self.local.rows(
            "SELECT extract(epoch FROM sampled_at)::float8::text, last_value::text FROM follower.sequence_samples "
            f"WHERE seq = {lit(self._sample_key(seq))} AND sampled_at > now() - make_interval(hours => {int(hours)})")
        return [(float(r[0]), int(r[1])) for r in rows]

    def _sample_key(self, seq: str) -> str:
        return ("reverse:" if self.reverse else "forward:") + seq

    def run(self, final: bool = False, dry_run: bool = False) -> list[SeqResult]:
        floor = self.cfg.getint("seqsync", "final_floor" if final else "floor", 10_000 if final else 1_000)
        hours = self.cfg.getint("seqsync", "window_hours", 24)
        source = read_source(self.source, self.schemas)
        target = {
            r[0]: (int(r[1]) if r[1] is not None else None)
            for r in self.target.rows(
                "SELECT quote_ident(schemaname) || '.' || quote_ident(sequencename), last_value::text "
                f"FROM pg_sequences WHERE schemaname = ANY({text_array(self.schemas)})")
        }
        results: list[SeqResult] = []
        errors: list[str] = []
        statements: list[str] = []
        samples: list[str] = []
        for seq, (value, readable) in source.items():
            if value is None:
                if readable:
                    results.append(SeqResult(seq, None, target.get(seq), target.get(seq), 0, "unused"))
                else:
                    errors.append(f"{seq}: last_value read NULL and the role lacks SELECT on it")
                    results.append(SeqResult(seq, None, None, None, 0, "error"))
                continue
            if seq not in target:
                errors.append(f"{seq}: exists on the source but not on the target")
                results.append(SeqResult(seq, value, None, None, 0, "error"))
                continue
            margin = margin_for(self._samples(seq, hours) + [(_now_epoch(self.local), value)], floor)
            wanted = value + margin
            current = target[seq]
            samples.append(
                "INSERT INTO follower.sequence_samples (seq, last_value) VALUES "
                f"({lit(self._sample_key(seq))}, {value}) ON CONFLICT DO NOTHING;")
            if current is not None and current >= wanted:
                results.append(SeqResult(seq, value, current, current, margin, "ahead"))
                continue
            statements.append(f"SELECT setval({lit(seq)}::regclass, {wanted}, true);")
            results.append(SeqResult(seq, value, current, wanted, margin, "set"))
        if not dry_run:
            if statements:
                self.target.execute("\n".join(statements))
            samples.append("DELETE FROM follower.sequence_samples WHERE sampled_at < now() - interval '48 hours';")
            self.local.execute("\n".join(samples))
        for r in results:
            if r.action in ("set", "error"):
                log("seqsync", f"{r.seq} {r.action}", source=r.source_value, before=r.target_before,
                    after=r.target_after, margin=r.margin, dry_run=dry_run)
        if errors:
            raise SeqSyncError("; ".join(errors))
        return results

    def from_data(self, dry_run: bool = False) -> list[SeqResult]:
        """Set every column-owned sequence from the restored data itself (Phase 4)."""
        floor = self.cfg.getint("seqsync", "floor", 1_000)
        rows = self.local.rows(
            "SELECT quote_ident(sn.nspname) || '.' || quote_ident(s.relname), tn.nspname, t.relname, a.attname "
            "FROM pg_depend d JOIN pg_class s ON s.oid = d.objid AND s.relkind = 'S' "
            "JOIN pg_namespace sn ON sn.oid = s.relnamespace "
            "JOIN pg_class t ON t.oid = d.refobjid JOIN pg_namespace tn ON tn.oid = t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid "
            "WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass "
            f"AND d.deptype IN ('a', 'i') AND sn.nspname = ANY({text_array(self.schemas)})")
        results = []
        statements = []
        for seq, tschema, tname, col in rows:
            top = self.local.scalar(f"SELECT max({ident(col)})::text FROM {qualified(tschema, tname)}")
            if top is None:
                results.append(SeqResult(seq, None, None, None, floor, "unused"))
                continue
            wanted = int(top) + floor
            statements.append(f"SELECT setval({lit(seq)}::regclass, greatest({wanted}, "
                              f"(SELECT last_value FROM {seq})), true);")
            results.append(SeqResult(seq, int(top), None, wanted, floor, "set"))
        if statements and not dry_run:
            self.local.execute("\n".join(statements))
        return results


def _now_epoch(db: Psql) -> float:
    return float(db.scalar("SELECT extract(epoch FROM now())::float8::text"))
