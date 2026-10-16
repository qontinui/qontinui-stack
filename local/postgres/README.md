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
| `qontinui-pg follower` | D7, A2–A4, B2 | The schema follower (`lib/qpg/follower.py`, see its docstring). It uses a stall marker on `public.alembic_version`: a `BEFORE INSERT OR UPDATE`, `ENABLE REPLICA`, `SECURITY DEFINER` trigger owned by `qontinui_follower`. The follower finds the migration transaction by its xid. With the subscription disabled, it replays the migration locally as `qontinui_user` while a replay-log trigger records every row the replay inserts. It deletes those rows again by typed key, locally, and re-enables the subscription. Apply then carries the migration's own DML with production's values, in commit order, and the follower reads no table data from the publisher. This replaces the plan's SKIP-then-repair (D7 steps 3–4). It fails closed: it halts on the shapes it can detect, and it refuses to run at all until `[follower] acknowledge_known_gaps = true` (see **Known gaps** below). It refuses a move into an unpublished schema (A3). The coord lane (A2) sits behind `coord_lane = true`, and `phase8_complete = true` forbids it (A4). |
| `qontinui-pg seqsync` | D6 | Copies sequence positions with a margin per sequence: 3× the largest 5-minute advance in 24 h, never under 1,000. `--final` raises the floor to 10,000. A sequence that reads NULL without privilege is an error. The command never moves a value backwards. `--reverse` copies to RDS for D8, and `--from-data` sets sequences from local data after a restore. |
| `qontinui-pg watchdog` | D11, A2 item 6, A6 | Checks the slot's `wal_status` and retained WAL (alert at 2 GB), the apply lag, the follower, the host's free space (alert under 30 GB), and the archiver. It reads the publisher's catalog for published tables that have no replica identity. When configured, it also checks the reverse subscription. A signal it cannot read counts as an alert. Notices are logged. They are posted to coord's `/coord/agent-findings` only with `[notify] enabled = true`. |
| `qontinui-pg partitions` | D9 | Calls lane D's `coord.test_results_partition_maintenance(retention, lookahead)` as `qontinui_user`. |
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
| Gap 10: DML then `ALTER … TYPE … USING` halts | `test_follower.TypeRewriteTest` | `no-rewrite-check` |
| Gap 5 with a column dropped and re-added under the same name | `test_follower.DropAddColumnTest` | `missing-by-name` |
| `follower resolve --case rename` carries a rename through | `test_follower.RenameTest` | `resolve-not-recorded` |
| `follower resolve --case missing-value` after copying the column | `test_follower.MissingValueTest` | `resolve-not-recorded`, `resolution-ignored` |
| A halt after re-enable disables the subscription and says so | `test_follower.DmlThenDdlTest` | `halt-leaves-enabled` |
| A3 refusal of a `SET SCHEMA` into an unpublished schema | `test_follower.MoveRefusalTest` | `no-move-refusal` |
| B2 ownership: the table owner is not a superuser, the subscription owner is | `test_follower.OwnershipTest` | `no-security-definer` (the initial copy of `alembic_version` never reaches `r`) |
| B3 security catalog diff, red with one policy dropped | `test_ops.SecDiffTest` | `secdiff-ignores-policies` |
| D11 watchdog alert on a table with no replica identity, and posting to coord only when configured | `test_ops.WatchdogTest` | `watchdog-no-replica-identity` |
| D6 margins, the final floor, and a NULL read as an error | `test_ops.SeqSyncTest` | `seqsync-null-is-zero` |
| Installer `--root`/`--dry-run`/`--uninstall`, and `--check` for D4 (both pg_hba arms), pgBackRest and A4 | `test_installer.InstallerTest` | `hba-check-accepts-any-non-tls`, `no-a4-guard` |

D9 partitions, the D2 credential and the B1 roles have tests that run green but
no mutation.

## Known gaps of the schema follower

The follower replays each migration locally, undoes the rows the replay
inserted, and lets apply carry production's own copy of the migration. That
is correct only when the replay touches the same rows as production did and
production's row changes still fit the migrated tables. It halts on every
shape below that it can detect. **Every halt disables the subscription**, and the halt notice says whether it did. `follower resume` re-enables the subscription when it was the halt that disabled it. `follower run` refuses to start until
`[follower] acknowledge_known_gaps = true`, which the operator sets after
reading this section.

| # | Shape | Detected? | What happens | Manual remedy |
|---|---|---|---|---|
| 1 | DML before later DDL in one migration transaction (`UPDATE t SET c …; ALTER TABLE t DROP COLUMN c`) | yes | apply fails again on the migration xid (55000 / 23502); the follower halts naming the gap | Re-seed that table. With the subscription disabled, empty it locally, take it out of the subscription and put it back so table sync copies it, then `follower resume`. |
| 2 | A migration that renames or moves a table | yes | halt before anything is emptied; subscription disabled | Confirm the publisher made the same rename, then `qontinui-pg follower resolve --case rename`. That records the resolution and resumes; the table keeps its OID and its subscription on both sides. |
| 3 | A unique violation (23505) replaying the migration | yes | halt | Compare the key on both sides, remove the local row, then `follower resume`. |
| 4 | The replay changes a replica-identity key | yes | halt | Re-seed the table (as in 1). |
| 5 | A column added with a stored default (`atthasmissing`) whose value differs from the publisher's, e.g. `DEFAULT now()`, including a same-named column dropped and re-added | yes, by comparing `attmissingval` with the publisher's, per attnum | halt naming table and column; subscription disabled | With the subscription disabled, copy that column for the existing rows from the publisher (key + column, replica mode). Then run `qontinui-pg follower resolve --case missing-value --target <schema>.<table>.<column>` for each column. |
| 6 | Row selection that differs: production ran the migration under READ COMMITTED alongside other transactions, so a statement there may not have seen rows that commit before the migration does, while the local replay sees all of them | **no** | silent divergence on the rows only one side touched | The Phase 3 parity checks and a re-seed of the table. Avoid long-running data migrations while replication is live. |
| 7 | Non-deterministic row selection (`DELETE … WHERE ts < now()`) | **no** | silent divergence | As 6. |
| 8 | A volatile default that rewrites the table without row triggers (`ADD COLUMN … DEFAULT clock_timestamp()`) | yes: a `table_rewrite` event trigger, installed only during the replay, records the rewrite reason | halt; subscription disabled | Re-seed the table (as in 1). |
| 10 | Row changes followed by a rewrite that transforms values (`UPDATE …; ALTER … TYPE … USING x * 100`) | yes: a statement-level trigger records the DML and the event trigger records the rewrite, ordered | halt; subscription disabled | Re-seed the table (as in 1). Any rewrite after DML halts, even one that would not transform the data (a plain `int` → `bigint`). That is deliberate: it fails closed. |
| 11 | `ALTER TABLE … DETACH PARTITION` / `ATTACH PARTITION` in a migration | **no** | the detached table keeps its local rows, but the publisher's later changes route by the parent; an attached table's existing rows are not copied | Treat as a re-seed of the parent. Avoid partition DETACH/ATTACH while replication is live (lane D's maintenance function only creates and drops children). |
| 9 | A step that fails `max_step_failures` times running (publisher outages do not count) | yes | halt; subscription disabled | Read the error, fix it, `follower resume`. |

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

## Configuration

Copy `qontinui-pg.conf.example` to `/etc/qontinui-pg/qontinui-pg.conf` with
mode 0640 and owner `root:postgres`, and fill in every `REPLACE_ME`. Those
values are host-specific, and this repository is public, so they never come
back here. Passwords live only in `/var/lib/qontinui-pg/pgpass` (mode 0600, owner
`postgres`). The repo2 passphrase lives only in
`/etc/pgbackrest/conf.d/qontinui-cipher.conf`.
