# local/postgres — the local subscriber of production Postgres

Code for Phases 1d and 1e of plan
`2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication`
(qontinui-dev-notes): a PostgreSQL 16 host that subscribes to production by
logical replication, keeps its schema in step, and watches the replication.

**Nothing here touches production until Phase 3.** No file in this tree
connects to RDS, creates a publication or a subscription, creates a role on
RDS, or moves a secret by itself. The installer refuses to run without root,
and it installs the systemd units without enabling them. The tests run only
against throwaway containers. Phase 3 is gated on OD1 and OD3 and is run by
the lead.

## What each piece is

| Piece | Plan | What it does |
|---|---|---|
| `install.sh` | D4, D15, D16 | Installs PGDG `postgresql-16`, `postgresql-16-pgvector` (0.8.x) and `pgbackrest` as native packages. Creates the cluster on the LUKS volume (it refuses any other volume). Renders `postgresql.conf` and `pg_hba.conf`. The server listens on the tailnet address and the unix socket only. The application role may log in without TLS, but only from the tailnet, because coord connects `NoTls`. Every other client must use `hostssl`. The installer also renders pgBackRest (encrypted repo2, 35 days, `archive-push-queue-max=20GiB`, no repo1 on a one-disk host) and the systemd units. It has three modes: `--check`, `--uninstall`, and safe `--dry-run`/`--root` modes. |
| `qontinui-pg follower` | D7, A2–A4, B2 | The schema follower (`lib/qpg/follower.py`, see its docstring). It uses a stall marker on `public.alembic_version`: a `BEFORE INSERT OR UPDATE`, `ENABLE REPLICA`, `SECURITY DEFINER` trigger owned by `qontinui_follower`. The follower finds the migration transaction by its xid. With the subscription disabled, it replays the migration locally as `qontinui_user` while a replay-log trigger records every row the replay inserts. It deletes those rows again by typed key, locally, and re-enables the subscription. Apply then carries the migration's own DML with production's values, in commit order. The replay path reads no table data from the publisher; only the operator-run `copy-columns` remedy does, one table at a time. This replaces the plan's SKIP-then-repair (D7 steps 3–4). It fails closed: it halts on the shapes it can detect, and it refuses to run at all until `[follower] acknowledge_known_gaps = true` (see **Known gaps** below). It refuses a move into an unpublished schema (A3). The coord lane (A2) sits behind `coord_lane = true`, and `phase8_complete = true` forbids it (A4). |
| `qontinui-pg seqsync` | D6 | Copies sequence positions with a margin per sequence: 3× the largest 5-minute advance in 24 h, never under 1,000. `--final` raises the floor to 10,000. A sequence that reads NULL without privilege is an error. The command never moves a value backwards. `--reverse` copies to RDS for D8, and `--from-data` sets sequences from local data after a restore. |
| `qontinui-pg watchdog` | D11, A2 item 6, A6 | Checks the slot's `wal_status` and retained WAL (alert at 2 GB), inactive tablesync slots (`pg_<subid>_sync_<relid>_<sysid>`) left on the publisher by a subscriber that died mid-copy, with each one's retained WAL (reported, never dropped: `DROP SUBSCRIPTION` does not remove them), the apply lag, the follower, the host's free space (alert under 30 GB), and the archiver. It reads the publisher's catalog for published tables that have no replica identity. When configured, it also checks the reverse subscription. A signal it cannot read counts as an alert. Notices are logged. They are posted to coord's `/coord/agent-findings` only with `[notify] enabled = true`. |
| `qontinui-pg partitions` | D9 | Calls lane D's `coord.test_results_partition_maintenance(retention, lookahead)` as `qontinui_user`, with coord's own arguments: 14 and 14 by default (`lib/qpg/partitions.py` cites both sources). |
| `qontinui-pg partition-parity` | D9, Phase 3 | Compares the child partition NAMES (`pg_partition_tree`) of every partitioned table the publication carries, publisher against subscriber. Partition DDL does not replicate, so equal counts can hide different sets. Exits 1 on any difference. |
| `qontinui-pg credential new` | D2 | Generates a 32-byte password on this host and stores it in the passfile (0600). It prints only the SCRAM-SHA-256 verifier. |
| `qontinui-pg roles` | B1 | `apply` creates `qontinui_user` in production's shape. It then runs r10's role block, loaded from `coordrls_01` and never retyped, as that role. `check-grantees` lists the roles named in the publisher's ACLs and policies and reports any that are missing locally. |
| `qontinui-pg secdiff` | B3, B2 | The security catalog diff between the publisher and the subscriber. It covers roles, memberships, policies, RLS flags, owners, ACLs, default ACLs and `security_invoker`. It also checks the owner premise. The diff must be empty. |
| `qontinui-pg host` | D4 | Renders and checks the managed files. `check-hba-live` applies the D4 rule to the `pg_hba_file_rules` the server actually loaded. |

The installer is bash, because it drives apt, `pg_createcluster` and systemd.
The tools are Python 3 standard library and drive `psql`. No Python Postgres
driver is a dependency anywhere in this repo outside the migrator container,
and a host that holds production data should not carry a pip install. `psql`
comes with `postgresql-client-16` anyway.

## Tests

```bash
bash local/postgres/tests/run.sh                    # everything, then every mutation
bash local/postgres/tests/run.sh --no-mutations     # green suite only
bash local/postgres/tests/run.sh --only test_follower
```

`run.sh` starts two `pgvector/pgvector:pg16` servers with `wal_level=logical`:
a publisher, and a subscriber that writes jsonlog. Their data lives on tmpfs
and they publish no host ports. A third, tools container runs `tests/inside.sh`.
That script runs the suite green on the real tree. It then runs each mutation
in `tests/mutations.py` against a copy of the tree, and each mutation must turn
its tests RED. Every container is removed on exit. CI runs the same script
(`.github/workflows/local-postgres.yml`) on changes under `local/postgres/`.

| Case | Test | Mutation that turns it red |
|---|---|---|
| D7 double-DML fixture: a `gen_random_uuid()` seed, a `clock_timestamp()` backfill behind a new column, and a two-revision transaction with a new seeded table; the publisher's scan counters on the backfilled table do not move while the follower works | `test_follower.DoubleDmlTest` | `no-undo`, `reads-publisher-data` |
| Continuous publisher inserts and updates on both migrated tables during two migrations: both sides converge, and the log has no apply error but the migrations' own stalls | `test_follower.ConcurrentWritesTest` | `no-undo`, `overwrite-from-publisher` |
| A2 coord lane: adoption, `REFRESH … copy_data = false`, a boot-lane seed and a background-lane backfill | `test_follower.CoordLaneTest` | `no-undo` |
| A re-stamp to a version applied before halts instead of passing the marker | `test_follower.RestampTest` | — |
| Gap 1: DML before later DDL halts and names the gap | `test_follower.DmlThenDdlTest` | `no-dml-ddl-classification` |
| Gap 2: a rename halts and nothing is emptied | `test_follower.RenameTest` | `no-rename-check` |
| Gap 5: an equal stored default passes, a differing one halts | `test_follower.MissingValueTest` | `no-missing-value-check` |
| A new table referenced by a foreign key from an existing table follows | `test_follower.ForeignKeyNewTableTest` | `truncate-new-tables` (fails on every attempt and halts after `max_step_failures`) |
| A partitioned table in scope: the log trigger goes on the parent only | `test_follower.DoubleDmlTest` | `log-trigger-on-partitions` |
| The follower refuses to run until the known gaps are acknowledged | `test_follower.AcknowledgeTest` | `no-ack-guard` |
| Gap 6: DML then `ALTER … TYPE … USING` halts | `test_follower.TypeRewriteTest` | `no-rewrite-check` |
| Gap 5 with a column dropped and re-added under the same name | `test_follower.DropAddColumnTest` | `missing-by-name` |
| `follower resolve --case rename` carries a rename through | `test_follower.RenameTest` | `resolve-not-recorded` |
| `follower resolve --case missing-value`, then `copy-columns` | `test_follower.MissingValueTest` | `resolve-not-recorded`, `resolution-ignored` |
| Gap 6 exit: a mismatched case is refused; `resolve --case rewrite` and `copy-columns` converge | `test_follower.TypeRewriteTest` | `resolve-keeps-log`, `resolve-not-recorded` |
| Gap 8: a computed default added, then the table rewritten | `test_follower.ComputedDefaultRewriteTest` | `no-gained-default-check` |
| Gap 4 exit: `resolve --case key-change`, then `copy-columns` | `test_follower.KeyChangeTest` | `resolve-keeps-log` |
| A halt inside the replay leaves no trigger installed, including the database-wide event trigger | `test_follower.MigrateFailureTest` | `halt-keeps-triggers` |
| copy-columns refuses across an unreplayed migration, takes the lock, refuses under row-level security, and clears the owed copy | `test_follower.CopyColumnsTest` | `no-version-guard`, `copy-columns-no-lock`, `copy-columns-rls-on` |
| Gap 8 with DROP DEFAULT after the rewrite | `test_follower.DropDefaultRewriteTest` | `gained-needs-default` |
| A resumed replay re-installs its triggers by OID, after a partial replay renamed a table | `test_follower.RelogTest` | `relog-by-stored-name` |
| A halt after re-enable disables the subscription and says so | `test_follower.DmlThenDdlTest` | `halt-leaves-enabled` |
| A3 refusal of a `SET SCHEMA` into an unpublished schema | `test_follower.MoveRefusalTest` | `no-move-refusal` |
| B2 ownership: the table owner is not a superuser, the subscription owner is | `test_follower.OwnershipTest` | `no-security-definer` (the initial copy of `alembic_version` never reaches `r`) |
| B3 security catalog diff, red with one policy dropped | `test_ops.SecDiffTest` | `secdiff-ignores-policies` |
| D11 watchdog alert on a table with no replica identity, and posting to coord only when configured | `test_ops.WatchdogTest` | `watchdog-no-replica-identity` |
| D6 margins, the final floor, and a NULL read as an error | `test_ops.SeqSyncTest` | `seqsync-null-is-zero` |
| D11 watchdog alert on an inactive orphaned tablesync slot, which it never drops | `test_ops.WatchdogTest` | `watchdog-no-orphan-sync-slots` |
| D9 timer's default lookahead is lane D's 14 days | `test_ops.PartitionsTest` | `partition-lookahead-3` |
| Phase 3 partition parity: equal counts over different child names are a difference | `test_ops.PartitionParityTest` | `partition-parity-by-count` |
| Installer `--root`/`--dry-run`/`--uninstall`, and `--check` for D4 (both pg_hba arms), pgBackRest and A4 | `test_installer.InstallerTest` | `hba-check-accepts-any-non-tls`, `no-a4-guard` |

The D2 credential and the B1 roles have tests that run green but no mutation.

## Known gaps of the schema follower

The follower replays each migration locally, undoes the rows the replay
inserted, and lets apply carry production's own copy of the migration. That
is correct only when the replay touches the same rows as production did and
production's row changes still fit the migrated tables. It halts on every
shape below that it can detect. **Every halt disables the subscription**, and the halt notice says whether it did. `follower resume` re-enables the subscription when it was the halt that disabled it. `follower run` refuses to start until
`[follower] acknowledge_known_gaps = true`, which the operator sets after
reading this section.

Two operator actions recur in the remedies below.
- `qontinui-pg follower resolve --case <case> --target <target>` records a
  resolution for the halt that is CURRENT. It refuses any other case or a
  target the halt did not name, and resumes once every named target is
  resolved. Each halt message prints the exact command.
- `qontinui-pg follower copy-columns --table <schema>.<table> [--column <c>]… [--allow-unmatched N]`
  runs once the follower is idle. It overwrites existing rows' columns from
  the publisher, collision-free:
  - It holds the follower lock, so the daemon skips its cycles meanwhile.
  - It waits until apply has caught up to the publisher, then disables the
    subscription itself.
  - It reads key + columns in ONE repeatable-read snapshot on the publisher.
    That snapshot first checks the publisher's version tables equal the
    subscriber's, so the copy is refused if production has applied a
    migration the subscriber has not.
  - It reads with `row_security = off`, so a row-level policy makes the copy
    fail instead of silently filtering it.
  - It only UPDATEs existing rows (by key, replica mode), so it cannot
    collide with apply. It fails if more than N rows read from the publisher
    do not exist locally.
  - It reports rows read and rows updated, and re-enables the subscription.

  A `resolve` of a rewrite, key-change or missing-value halt records that a
  copy is owed. `follower check` fails and `follower status` lists the owed
  copy until copy-columns has covered it. A rewrite or key-change owes EVERY non-key column, so only a copy-columns run without `--column` (or one naming all of them) clears it; a missing-value halt owes just its column.

A **re-seed** of one table is not automated. Table sync cannot re-copy a
table that a schema publication already carries. So it is either the full
re-seed of D11, or a lead-run procedure with a temporary second subscription.
That is a follow-up, and until then it is the remedy only where no
copy-columns remedy exists.

| # | Shape | Detected? | What happens | Remedy |
|---|---|---|---|---|
| 1 | DML before later DDL in one migration transaction (`UPDATE t SET c …; ALTER TABLE t DROP COLUMN c`) | yes | apply fails again on the migration xid (55000 / 23502); halt naming the gap | Re-seed the table (not automated, see above). |
| 2 | A migration that renames or moves a table | yes | halt before anything is emptied | Confirm the publisher made the same rename, then `resolve --case rename --target <new name>`. The table keeps its OID and its subscription on both sides. |
| 3 | A unique violation (23505) replaying the migration | yes | halt | Compare the key on both sides, remove the local row, then `follower resume`. |
| 4 | The replay changes a replica-identity key | yes | halt | When the replay's new keys equal the publisher's (a deterministic key update), run `resolve --case key-change --target <table>` and then `copy-columns --table <table>`. Otherwise re-seed the table. |
| 5 | A column added with a stored default (`atthasmissing`) whose value differs from the publisher's, e.g. `DEFAULT now()`. This includes a same-named column dropped and re-added; values are compared per attnum. | yes | halt naming table and column | `resolve --case missing-value --target <schema>.<table>.<column>`, then `copy-columns --table <schema>.<table> --column <column>`. |
| 6 | A rewrite after the replay changed the table's rows (`UPDATE …; ALTER … TYPE … USING x * 100`) | yes. While the replay runs, a statement trigger on each table and a database-wide `table_rewrite` event trigger log, in order, which tables were changed and which were rewritten. Every halt removes them, and `follower check` flags any left over. | halt; any rewrite after DML halts, even a harmless `int` → `bigint` (fail closed) | `resolve --case rewrite --target <table>`, then `copy-columns` for each transformed column. |
| 7 | A rewrite that fills a volatile default (`ADD COLUMN … DEFAULT clock_timestamp()`) | yes, from the rewrite event's reason | halt | As 6. |
| 8 | A column added and the same table then rewritten in the same migration, e.g. alembic's add-with-default, rewrite, DROP DEFAULT idiom. The rewrite stores each side's own default value, and after DROP DEFAULT the catalog shows nothing. | yes: any rewritten pre-existing table that gained a column halts, whatever its default (fail closed) | halt | As 6, copying the new column. |
| 9 | A step that fails `max_step_failures` times running (publisher outages do not count) | yes | halt | Read the error, fix it, `follower resume`. |
| 10 | Row selection that differs: production ran the migration under READ COMMITTED alongside other transactions, so a statement there may not have seen rows that commit before the migration does, while the local replay sees all of them | **no** | silent divergence on the rows only one side touched | The Phase 3 parity checks, then copy-columns or a re-seed. Avoid long-running data migrations while replication is live. |
| 11 | Non-deterministic row selection (`DELETE … WHERE ts < now()`) | **no** | silent divergence | As 10. |
| 12 | `ALTER TABLE … DETACH PARTITION` / `ATTACH PARTITION` in a migration | **no** | the detached table keeps its local rows while the publisher's later changes route by the parent; an attached table's existing rows are not copied | Re-seed the parent. Avoid DETACH/ATTACH while replication is live; lane D's maintenance function only creates and drops children. |

Every halt disables the subscription and removes the replay triggers, and
the halt notice says what state it left. Resuming a migration that halted
before its undo re-installs the triggers. It keeps the replay log, because
part of the replay may have committed. Then it re-runs the migration.

**Operator writes during a halt** to a table the halted migration touches can
cause a later, false halt, for example a unique violation when apply
replays the migration. The false halt is fail-closed: nothing diverges, but
the follower stops again. Keep manual writes to the remedies above.

## Operator steps that remain

These are decisions only the operator holds. The code here assumes the
recommended answer and does nothing until each one is made.

- **OD1: the interim host and where the data lives.** Approve, or refuse,
  production data on the interim VM. Before `install.sh` runs, prepare the LUKS
  volume and mount it where `[cluster] data_directory` points. LUKS protects a
  lost disk. It does not protect the data from whoever runs the hypervisor.
- **OD2: recurring cost.** Approve the egress and the router task (stack Phase
  1b).
- **OD3: credentials.** Approve the RDS replication role and the
  reverse-subscription owner. On this host, `qontinui-pg credential new --role
  qontinui_replication --passfile /var/lib/qontinui-pg/pgpass` prints the verifier.
  The lead creates the role on RDS through ECS Exec with
  `CREATE ROLE … PASSWORD '<verifier>'`. Also mint the tag-scoped Tailscale auth
  key in the tailnet console and paste it into the empty secret.
- **OD4: the off-site repository.** Choose the repo2 destination and set
  `[pgbackrest] repo2_*`. Choose the drill host. Escrow the repo2 cipher
  passphrase: `install.sh` shows it once on the console, and you record it
  offline.

## Phase 3 order on the host, for the lead

This is not run by anything in this tree. Each command is idempotent.

1. `install.sh --config qontinui-pg.conf`, which ends with `install.sh --check`.
2. `qontinui-pg credential new --role qontinui_user --passfile /var/lib/qontinui-pg/pgpass > /tmp/app.verifier`,
   then `qontinui-pg roles apply --verifier-file /tmp/app.verifier --revision-file <qontinui-web checkout at production head>/backend/alembic/versions/coordrls_01_tenant_role_and_scope_census.py`.
   Use `--allow-missing` instead of the file if `coordrls_01` is not applied in
   production yet. Then run `qontinui-pg roles check-grantees`.
3. Load the schema-only dump as `qontinui_user` (B2). Then run
   `qontinui-pg follower install`, `qontinui-pg secdiff`, which must exit 0,
   and `qontinui-pg follower seed --expect <the web revision the dump was taken at>`; seed refuses if production has moved since. Keep the subscription `streaming = on`, never `parallel`.
4. Create the subscription as `postgres` (D3's guards; `copy_data`,
   `streaming = on`, `binary = false`). Then run
   `install.sh --enable-services`.
5. Parity, once the copy has reached `r`: `qontinui-pg secdiff` and
   `qontinui-pg partition-parity` must both exit 0. Partition parity compares
   child NAMES, never counts (D9). Run it again after the first day boundary,
   when both sides' maintenance has created and dropped a child.

## Configuration

Copy `qontinui-pg.conf.example` to `/etc/qontinui-pg/qontinui-pg.conf` with
mode 0640 and owner `root:postgres`, and fill in every `REPLACE_ME`. Those
values are host-specific, and this repository is public, so they never come
back here. Passwords live only in `/var/lib/qontinui-pg/pgpass` (mode 0600, owner
`postgres`). The repo2 passphrase lives only in
`/etc/pgbackrest/conf.d/qontinui-cipher.conf`.
