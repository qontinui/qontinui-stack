# The tools container for local/postgres tests: the same PostgreSQL 16 +
# pgvector image the servers run (so psql is the server's own client), plus
# python3 for the qontinui-pg tools and curl for the notice path.
# Digest-pinned to the same build docker-compose.yml's postgres runs (plan
# 2026-11-22-fetches-stop-depending-on-upstreams D5; scripts/tests/test_image_pins.py).
# released: 2026-08-13 pinned: 2026-11-22
FROM pgvector/pgvector:0.8.6-pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 curl >/dev/null \
 && rm -rf /var/lib/apt/lists/*
