# local/postgres — the local subscriber of production Postgres

Code for Phase 1d of plan
`2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication`
(qontinui-dev-notes): a PostgreSQL 16 host that subscribes to production by
logical replication and keeps its schema in step. This commit carries Phase 1d
(the installer and the schema follower); Phase 1e adds the operations tools.

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
| Installer `--root`/`--dry-run`/`--uninstall`, and `--check` for D4 (both pg_hba arms) and pgBackRest | `test_installer.InstallerTest` | `hba-check-accepts-any-non-tls` |


## Operator steps that remain

OD1-OD4 (plan "Operator decisions") gate Phase 3; the Phase 1e change to this
README lists what each needs on this host.
