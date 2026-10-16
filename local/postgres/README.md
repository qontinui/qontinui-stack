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
| `qontinui-pg follower` | D7, A2–A4, B2 | The schema follower (`lib/qpg/follower.py`, see its docstring). It uses a stall marker on `public.alembic_version`: a `BEFORE INSERT OR UPDATE`, `ENABLE REPLICA`, `SECURITY DEFINER` trigger owned by `qontinui_follower`. The follower finds the migration transaction by its xid and replays it locally as `qontinui_user`. It then runs `ALTER SUBSCRIPTION … SKIP` and repairs the tables the replay touched from the publisher. It refuses a move into an unpublished schema (A3). The coord lane (A2) sits behind `coord_lane = true`, and `phase8_complete = true` forbids it (A4). |
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
| D7 double-DML fixture: a `gen_random_uuid()` seed, a `clock_timestamp()` backfill behind a new column, and a two-revision transaction with a new seeded table | `test_follower.DoubleDmlTest` | `no-skip` (the follower halts on the duplicate), `no-repair` (the rows differ) |
| A2 coord lane: adoption, `REFRESH … copy_data = false`, a boot-lane seed and a background-lane backfill | `test_follower.CoordLaneTest` | `no-skip`, `no-repair` |
| A3 refusal of a `SET SCHEMA` into an unpublished schema | `test_follower.MoveRefusalTest` | `no-move-refusal` |
| B2 ownership: the table owner is not a superuser, the subscription owner is | `test_follower.OwnershipTest` | `no-security-definer` (the initial copy of `alembic_version` never reaches `r`) |
| B3 security catalog diff, red with one policy dropped | `test_ops.SecDiffTest` | `secdiff-ignores-policies` |
| D11 watchdog alert on a table with no replica identity, and posting to coord only when configured | `test_ops.WatchdogTest` | `watchdog-no-replica-identity` |
| D6 margins, the final floor, and a NULL read as an error | `test_ops.SeqSyncTest` | `seqsync-null-is-zero` |
| Installer `--root`/`--dry-run`/`--uninstall`, and `--check` for D4 (both pg_hba arms), pgBackRest and A4 | `test_installer.InstallerTest` | `hba-check-accepts-any-non-tls`, `no-a4-guard` |

D9 partitions, the D2 credential and the B1 roles have tests that run green but
no mutation.

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
  qontinui_replication --passfile /etc/qontinui-pg/pgpass` prints the verifier.
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
2. `qontinui-pg credential new --role qontinui_user --passfile /etc/qontinui-pg/pgpass > /tmp/app.verifier`,
   then `qontinui-pg roles apply --verifier-file /tmp/app.verifier --revision-file <qontinui-web checkout at production head>/backend/alembic/versions/coordrls_01_tenant_role_and_scope_census.py`.
   Use `--allow-missing` instead of the file if `coordrls_01` is not applied in
   production yet. Then run `qontinui-pg roles check-grantees`.
3. Load the schema-only dump as `qontinui_user` (B2). Then run
   `qontinui-pg follower install`, `qontinui-pg secdiff`, which must exit 0,
   and `qontinui-pg follower seed`.
4. Create the subscription as `postgres` (D3's guards; `copy_data`,
   `streaming = on`, `binary = false`). Then run
   `install.sh --enable-services`.

## Configuration

Copy `qontinui-pg.conf.example` to `/etc/qontinui-pg/qontinui-pg.conf` with
mode 0640 and owner `root:postgres`, and fill in every `REPLACE_ME`. Those
values are host-specific, and this repository is public, so they never come
back here. Passwords live only in `/etc/qontinui-pg/pgpass` (mode 0600, owner
`postgres`). The repo2 passphrase lives only in
`/etc/pgbackrest/conf.d/qontinui-cipher.conf`.
