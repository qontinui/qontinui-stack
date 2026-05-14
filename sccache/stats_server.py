#!/usr/bin/env python3
"""qontinui canonical sccache stats HTTP server.

Tiny stdlib-only HTTP server that exposes two endpoints:

  GET /stats        -> JSON: { "local": <sccache --show-stats output>,
                              "bucket": { "objects": N, "size_bytes": M } }
  GET /health       -> 200 OK if `sccache --show-stats` works.
  GET /metrics      -> Prometheus text format of the above.

This is a Phase 2 baseline-telemetry surface. Coord scrapes it on its
/metrics endpoint. The bucket counters reflect cross-fleet usage (every
agent + build machine writes to the same bucket); the local sccache
counters reflect only in-container compiles, which today is zero (the
sccache server here is dormant until Phase 6 cloud-burst).
"""
from __future__ import annotations

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

BUCKET = os.environ.get("SCCACHE_BUCKET", "qontinui-sccache")


def sccache_stats_json() -> dict:
    """Return parsed `sccache --show-stats --stats-format=json` output, or
    a `{"error": "..."}` payload on failure. Never raises."""
    try:
        out = subprocess.run(
            ["sccache", "--show-stats", "--stats-format=json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return {"error": out.stderr.strip() or "non-zero exit"}
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def bucket_stats() -> dict:
    """Return { objects, size_bytes } for the sccache bucket. Uses `mc du`
    against the `canonical` alias the entrypoint has already configured."""
    try:
        # `mc du --json canonical/<bucket>` returns one JSON object per
        # depth level; the top-level one (the bucket root) is what we want.
        out = subprocess.run(
            ["mc", "du", "--json", f"canonical/{BUCKET}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if out.returncode != 0:
            return {"error": out.stderr.strip() or "non-zero exit"}
        last = None
        for line in out.stdout.strip().splitlines():
            if line:
                last = json.loads(line)
        if not last:
            return {"objects": 0, "size_bytes": 0}
        return {
            "objects": last.get("objects", 0),
            "size_bytes": last.get("size", 0),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _stats_payload() -> dict:
    local = sccache_stats_json()
    bucket = bucket_stats()
    return {"local": local, "bucket": bucket, "bucket_name": BUCKET}


def _prometheus_payload(stats: dict) -> str:
    """Emit a small Prometheus text payload. Only the bucket-side counters
    are exported here — per-machine local sccache stats are aggregated at
    coord scrape time so all clients show up in one place."""
    lines: list[str] = []
    bucket = stats.get("bucket", {})
    objects = bucket.get("objects")
    size = bucket.get("size_bytes")
    if isinstance(objects, int):
        lines.append("# HELP sccache_bucket_objects Object count in the shared sccache MinIO bucket.")
        lines.append("# TYPE sccache_bucket_objects gauge")
        lines.append(f'sccache_bucket_objects{{bucket="{BUCKET}"}} {objects}')
    if isinstance(size, int):
        lines.append("# HELP sccache_bucket_size_bytes Total byte size of the shared sccache MinIO bucket.")
        lines.append("# TYPE sccache_bucket_size_bytes gauge")
        lines.append(f'sccache_bucket_size_bytes{{bucket="{BUCKET}"}} {size}')
    local = stats.get("local", {})
    # sccache 0.8 json shape: top-level "stats" map. Defensive: tolerate
    # either {"stats": {...}} or the flat-keys variant some versions emit.
    flat = local.get("stats", local)
    if isinstance(flat, dict):
        # Scalar counters.
        for key in ("compile_requests", "cache_writes", "cache_timeouts",
                    "cache_read_errors", "non_cacheable_compilations",
                    "forced_recaches", "cache_write_errors", "compile_fails"):
            v = flat.get(key)
            if isinstance(v, (int, float)):
                metric = f"sccache_local_{key}_total"
                lines.append(f"# TYPE {metric} counter")
                lines.append(f"{metric} {int(v)}")
        # Per-compiler bucketed counters; sum the `counts` dict for a
        # scalar total. sccache 0.8 emits these as
        # `{"counts": {"rust": N, ...}, "adv_counts": {...}}`.
        for key in ("cache_hits", "cache_misses", "cache_errors"):
            v = flat.get(key)
            if isinstance(v, dict):
                counts = v.get("counts") or {}
                if isinstance(counts, dict):
                    total = sum(int(x) for x in counts.values()
                                if isinstance(x, (int, float)))
                    metric = f"sccache_local_{key}_total"
                    lines.append(f"# TYPE {metric} counter")
                    lines.append(f"{metric} {total}")
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib API)
        if self.path == "/health":
            local = sccache_stats_json()
            if "error" in local:
                self._respond(503, b"sccache server unreachable\n", "text/plain")
            else:
                self._respond(200, b"ok\n", "text/plain")
            return
        if self.path == "/stats":
            payload = json.dumps(_stats_payload()).encode("utf-8")
            self._respond(200, payload, "application/json")
            return
        if self.path == "/metrics":
            stats = _stats_payload()
            body = _prometheus_payload(stats).encode("utf-8")
            self._respond(200, body, "text/plain; version=0.0.4")
            return
        self._respond(404, b"not found\n", "text/plain")

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Quieter than default access-log spam.
        return


def main() -> None:
    port = int(os.environ.get("SCCACHE_STATS_PORT", "4227"))
    srv = HTTPServer(("0.0.0.0", port), Handler)
    print(f"[sccache-stats] listening on :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
