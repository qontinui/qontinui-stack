# qontinui-stack

The canonical-database stack for qontinui. One Postgres + one Redis + one MinIO,
shared by every machine that's developing against this project. Per topology
plan §9 Phase 1 (`tmp_canonical_db_topology_plan.md`).

This stack is the dev/self-host equivalent of what `qontinui.cloud` runs
managed. Architecturally identical; same schemas, same wire protocols, same
alembic chain.

## What's in here

| Service | Purpose | Default port |
|---|---|---|
| `postgres` | Canonical durable state (`project`, `coord`, `agent`, `auth`, `cloud` schemas + `pgvector`) | 5433 |
| `redis` | Claims, presence, pub/sub | 6380 |
| `minio` | S3-compatible blob storage (snapshots, screenshots, recordings) | 9100 (API), 9101 (console) |
| `migrator` | One-shot — `alembic upgrade head` on `up`, exits 0 on no-op or success. Built from `./migrator/Dockerfile`. | — |

Default ports are deliberately offset from the legacy `qontinui-web/docker-compose.yml`
stack (5432/6379/9000-9001) so both can coexist during the cutover. Once the
legacy stack is decommissioned, you can move this one to standard ports.

## Bring it up

```bash
cd D:/qontinui-root/qontinui-stack
cp .env.example .env       # customize if needed
docker compose up -d
```

The migrator container exits when the schema is at head. The other three
services keep running in the background.

## Verify

```bash
# Postgres reachable + schemas present
docker exec qontinui-canonical-postgres psql -U qontinui_user -d qontinui_db -c "\dn"
# Expected: project, coord, agent, auth, cloud, public

# Redis reachable
docker exec qontinui-canonical-redis redis-cli ping
# Expected: PONG

# MinIO bucket created
curl -s http://localhost:9100/minio/health/live
# Expected: 200 OK
```

## Connection strings

For `~/.qontinui/profiles.json` (`dev` profile):

```json
{
  "active": "dev",
  "profiles": {
    "dev": {
      "database_url": "postgres://qontinui_user:qontinui_dev_password@<host>:5433/qontinui_db",
      "redis_url":    "redis://<host>:6380/0",
      "blob": {
        "kind":       "s3-compatible",
        "endpoint":   "http://<host>:9100",
        "access_key": "minioadmin",
        "secret_key": "minioadmin",
        "bucket":     "qontinui-dev"
      }
    }
  }
}
```

`<host>` is `localhost` when this stack runs on the same machine as the runner,
or the PC's LAN IP (typically `192.168.x.x`) when the runner is on the laptop or
the third machine. Topology plan §9 Phase 2 covers LAN exposure (firewall +
`pg_hba.conf` + Redis bind).

## Operations

```bash
# Stop without destroying data
docker compose stop

# Start again (data persists)
docker compose start

# Stop and remove containers (data persists in named volumes)
docker compose down

# Stop and DESTROY all data (Postgres + Redis + MinIO)
docker compose down -v

# Re-run migrator only (after authoring new alembic revisions)
docker compose run --rm migrator

# Rebuild migrator after touching qontinui-web/backend/{alembic,app} or
# migrator/{Dockerfile,pyproject.toml}.
docker compose build migrator

# Tail logs
docker compose logs -f postgres
docker compose logs -f redis
docker compose logs -f minio
```

## Backup

The PC's canonical DB is the single source of truth across all machines. Back
it up. From any machine that can reach the canonical PG:

```bash
PGPASSWORD=qontinui_dev_password pg_dump -h <host> -p 5433 -U qontinui_user qontinui_db \
  | gzip > qontinui_db_$(date -u +%Y-%m-%dT%H%M%S).sql.gz
```

A scheduled cron that uploads to S3 (or to MinIO + offsite mirror) is on the
roadmap (topology plan §10).

## Coord redeploy procedure

The `coord` service builds from `../qontinui-coord/Dockerfile` and is
referenced in compose as `qontinui-canonical-coord:latest`. **Every
`docker compose build coord` retags `:latest` to the freshly-built image,
and Docker prunes the previous `:latest`'s underlying layers** (no tag
references them any more). The live container keeps running on its
already-extracted rootfs, but the image it was created from is gone from
the content store. That is the ":latest tag landmine":

- Any `docker compose up -d coord` rolls the new (possibly
  DB-incompatible) binary forward with no way back.
- A Docker daemon / host restart cannot recreate the container — there is
  no image to recreate it from.
- `docker commit` on the live container then fails with
  `content digest ... not found`; recovery needs the heavier
  `docker export | docker import` flatten (see memory
  `feedback_docker_commit_export_import_recovery`).

### Preserve before every rebuild (required step)

Before **any** coord image rebuild, pin the current `:latest` as a
distinct rollback target:

```bash
cd D:/qontinui-root/qontinui-stack
./scripts/coord-preserve-before-rebuild.sh <deploy-name>
# e.g. ./scripts/coord-preserve-before-rebuild.sh wave-8
```

This tags the current `:latest` as `qontinui-canonical-coord:pre-<deploy-name>-pinned`.
The script is non-destructive (it only adds a tag), refuses to overwrite
an existing `:pre-<deploy-name>-pinned` tag, and exits 0 with a warning
if there is no local `:latest` to preserve. Choose a `<deploy-name>` that
identifies the deploy (`wave-8`, `2026-06-01-coord`, the PR number — any
`[A-Za-z0-9._-]` string).

Then rebuild and roll forward:

```bash
docker compose build coord
docker compose up -d coord
docker compose logs -f coord   # watch the healthcheck settle
```

### Rollback

If the new binary misbehaves (DB incompatibility, regression, failed
healthcheck), repin compose to the preserved tag and bounce only coord:

1. Edit the `coord:` service in `docker-compose.yml`:
   `image: qontinui-canonical-coord:latest`
   → `image: qontinui-canonical-coord:pre-<deploy-name>-pinned`
2. `docker compose up -d coord`

Rollback is container-only and non-disruptive to the rest of the stack.
Revert the compose edit once a corrected image is rebuilt and verified.

### Cleaning up old preserved tags

Preserved tags accumulate (`docker images qontinui-canonical-coord`).
Keep the **two most recent** `:pre-*-pinned` tags (current rollback target
+ one prior) and delete older ones during routine maintenance (weekly, or
after two consecutive deploys have proven stable):

```bash
docker rmi qontinui-canonical-coord:pre-<old-deploy-name>-pinned
```

Before deleting any tag, confirm it is not the image the live container
is running on:

```bash
docker inspect qontinui-canonical-coord --format '{{.Image}}'
```

Removing a tag is non-destructive when another tag still references the
same image; it only frees the image store once the *last* reference is
gone. Never delete the tag the running container resolves to.

> The preservation discipline above codifies Gap 4 of the 2026-05 rollout
> (memory `proj_deployment_config_gaps_2026-05-rollout`). The pattern was
> empirically validated three times by hand (`:pre-phase5-pinned`,
> `:pre-config-session-pinned`) before being scripted.

## Migrator

The `migrator` service is a one-shot container that runs `alembic upgrade head`
against the canonical PG and exits. On every `docker compose up`, it:

1. Waits for `postgres` to be healthy.
2. Compares `alembic current` to `alembic heads`.
3. Either logs `DB already at head — no-op` and exits 0, or runs `alembic
   upgrade head` and exits with alembic's exit code.

The image is built from `./migrator/Dockerfile`. Build context is one level up
(`..`) so it can `COPY` from `qontinui-web/backend/` (alembic chain + model
graph) and `qontinui-schemas/` (the pydantic-only path dep that models
import for `utc_now` etc.).

The migrator deliberately does **not** install qontinui-web/backend's full
Poetry env. That env transitively pulls the heavy `qontinui` ML library
(torch + CUDA from the pytorch-cu128 wheel index, ~3 GB) which env.py and the
model graph don't need. Instead, `migrator/pyproject.toml` lists the curated
dep set — alembic, sqlalchemy, psycopg2-binary, pydantic + pydantic-settings,
fastapi-users[sqlalchemy], pgvector, email-validator, python-dotenv, plus
the `qontinui-schemas` path dep. Final image is ~110 MB content / ~460 MB
on-disk. See the Dockerfile header for the full rationale.

If you add a model that imports a third-party package not in
`migrator/pyproject.toml`, the build-time smoke test (`python -c 'from
app.db.base_class import *'`) catches it before the migrator ever ships —
add the dep to `migrator/pyproject.toml` and rebuild.

### Historical note: pre-consolidation migrator failure

Pre-2026-04-30, the migrator failed against existing DBs because the alembic chain had revisions past `e8a3c5b9d142` that depended on tables created by the runner-native `MIGRATIONS` array — a dual-migration-system drift. **Resolved by the migration consolidation** (qontinui-web PR #11, merged 2026-04-30). The alembic chain now owns every table; the migrator runs clean against fresh and seeded canonical DBs. Kept here as historical context for anyone who finds an old branch with the old behavior.

## Promotion path

This stack runs identically (different infrastructure, same shape) on AWS for
staging/prod and on Neon/Upstash/R2 for `qontinui.cloud`. Profile switching
in the runner is the only thing that changes between environments — see
`tmp_canonical_db_topology_plan.md` §3.

## License

Licensed under the GNU Affero General Public License v3.0 or later. See [LICENSE](LICENSE) for the full text. Contributions require the CLA — see [CONTRIBUTING.md](CONTRIBUTING.md).
