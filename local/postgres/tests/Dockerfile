# The tools container for local/postgres tests: the same PostgreSQL 16 +
# pgvector image the servers run (so psql is the server's own client), plus
# python3 for the qontinui-pg tools and curl for the notice path.
FROM pgvector/pgvector:pg16
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends python3 curl >/dev/null \
 && rm -rf /var/lib/apt/lists/*
