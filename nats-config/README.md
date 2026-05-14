# NATS JetStream cluster config (Row 9 Phase 1)

3-node JetStream cluster, paired to the canonical PG/Redis/MinIO
stack. Per `plans/2026-05-14-failure-modes-at-scale-design.md` §3.1.

## Port map (host-facing)

| Service        | Container  | Host port | Purpose                |
|----------------|-----------|-----------|------------------------|
| nats-1 client  | 4222      | 4223      | coord + subscribers    |
| nats-1 monitor | 8222      | 8223      | health + stream stats  |
| nats-2 / -3    | —         | —         | cluster-internal only  |

The cluster-route port (6222) is **not** exposed to the host. Cluster
traffic stays on the compose-default network.

Host port 4223 follows the "+1 offset" convention used for postgres
(5433→5432), redis (6380→6379), and minio (9100→9000) elsewhere in
this stack.

## Auth posture (Phase 1 vs Phase 2)

- **Phase 1 (this commit):** shared user/password for client auth +
  a separate cluster token. Same posture as Redis's `requirepass`.
- **Phase 2** (gated on Row 9 Phase 2 — coord-issued JWTs): NATS-native
  JWT auth replaces the static user. Per-agent subjects are scoped
  via the JWT claims documented in design §3.3.

## mTLS

Documented as the production target in the design doc. Deferred from
Phase 1 because CA generation + cert distribution + client trust-store
updates are a non-trivial chunk of work that does not affect the
behaviors under test in this phase (dual-publish + replay-on-reconnect).

## Verifying the cluster

```sh
# From the host, after `docker compose up -d`:
docker exec qontinui-canonical-nats-1 \
  nats --user "$NATS_CLIENT_USER" --password "$NATS_CLIENT_PASSWORD" \
       server report jetstream
```

A healthy cluster reports three peers, all `current` and in-sync.

## Stream declaration

Streams are declared by coord at startup via `nats_streams::ensure`
(see `qontinui-coord/src/nats_streams.rs`). This matches the
ensure_* dual-pattern documented in
`memory/proj_pg_dual_schema_runner_public.md` — schema lives next to
the code that uses it.
