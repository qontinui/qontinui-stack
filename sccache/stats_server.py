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

import datetime as _dt
import hashlib
import hmac
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BUCKET = os.environ.get("SCCACHE_BUCKET", "qontinui-sccache")

# SHA-256 of the empty body: every request this module signs is a bodiless GET.
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
# Stop a runaway walk (a store that never clears IsTruncated) from pinning the
# scrape forever; a bucket bigger than this reports an error, not a number.
MAX_LIST_PAGES = 10_000
# Wall-clock budget for one whole walk (every page together). A slow or huge
# bucket answers {"error": ...} after this, rather than holding the scrape.
LIST_DEADLINE_SECS = float(os.environ.get("SCCACHE_STATS_LIST_DEADLINE_SECS", "20"))
# A walk's result is reused for this long, so N concurrent or rapid scrapes of
# /stats and /metrics cost one walk, not N.
CACHE_TTL_SECS = float(os.environ.get("SCCACHE_STATS_CACHE_TTL_SECS", "45"))


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


def _endpoint_url() -> str:
    """The store's base URL from the same env sccache itself reads:
    SCCACHE_ENDPOINT is host:port (or a full URL), SCCACHE_S3_USE_SSL picks
    the scheme for the bare form."""
    endpoint = os.environ.get("SCCACHE_ENDPOINT", "object-store:9000")
    if "://" in endpoint:
        return endpoint.rstrip("/")
    use_ssl = os.environ.get("SCCACHE_S3_USE_SSL", "off").lower() in ("on", "true", "1", "yes")
    return f"{'https' if use_ssl else 'http'}://{endpoint}"


def _uri_encode(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def _sign_get(url: str, query: dict[str, str], access_key: str, secret_key: str,
              region: str, now: _dt.datetime) -> tuple[str, dict[str, str]]:
    """Build a SigV4-signed (header auth) path-style GET. Returns the full URL
    and the headers to send. Stdlib only, so the image needs no SDK."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc
    path = parts.path or "/"
    canonical_query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items()))
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    headers = {"host": host, "x-amz-content-sha256": _EMPTY_SHA256, "x-amz-date": amz_date}
    signed_headers = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical_request = "\n".join(
        ["GET", path, canonical_query, canonical_headers, signed_headers, _EMPTY_SHA256]
    )
    scope = f"{date}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = ("AWS4" + secret_key).encode()
    for part in (date, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    out = {
        "x-amz-content-sha256": _EMPTY_SHA256,
        "x-amz-date": amz_date,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    return f"{parts.scheme}://{host}{path}?{canonical_query}", out


def _local(tag: str) -> str:
    """Element tag without its XML namespace (stores differ on emitting one)."""
    return tag.rsplit("}", 1)[-1]


def list_bucket_totals(endpoint_url: str, bucket: str, access_key: str, secret_key: str,
                       region: str = "us-east-1", timeout: float = 10.0,
                       page_size: int = 1000, deadline_secs: float | None = None) -> dict:
    """Walk the bucket with S3 ListObjectsV2 and return
    ``{"objects": N, "size_bytes": M}``, or ``{"error": "..."}``. Never raises.

    Counts every object in the bucket (all prefixes): the whole-bucket totals
    coord's sccache_metrics scrape has always received. The whole walk is
    bounded by ``deadline_secs`` (default LIST_DEADLINE_SECS); each request's
    socket timeout is capped by what is left of it."""
    budget = LIST_DEADLINE_SECS if deadline_secs is None else deadline_secs
    give_up_at = time.monotonic() + budget
    objects = 0
    size = 0
    token: str | None = None
    base = f"{endpoint_url.rstrip('/')}/{urllib.parse.quote(bucket, safe='')}"
    try:
        for _page in range(MAX_LIST_PAGES):
            left = give_up_at - time.monotonic()
            if left <= 0:
                return {"error": f"listing exceeded its {budget:g}s deadline after {objects} objects"}
            query = {"list-type": "2", "max-keys": str(page_size)}
            if token:
                query["continuation-token"] = token
            url, headers = _sign_get(base, query, access_key, secret_key, region,
                                     _dt.datetime.now(_dt.timezone.utc))
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=min(timeout, left)) as resp:  # noqa: S310 (configured endpoint)
                root = ET.fromstring(resp.read())
            truncated = False
            token = None
            for child in root:
                name = _local(child.tag)
                if name == "Contents":
                    objects += 1
                    for field in child:
                        if _local(field.tag) == "Size" and field.text:
                            size += int(field.text)
                elif name == "IsTruncated":
                    truncated = (child.text or "").strip().lower() == "true"
                elif name == "NextContinuationToken":
                    token = child.text
            if not truncated:
                return {"objects": objects, "size_bytes": size}
            if not token:
                return {"error": "listing truncated without a NextContinuationToken"}
        return {"error": f"listing exceeded {MAX_LIST_PAGES} pages"}
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        return {"error": f"HTTP {e.code} from the store: {body}"}
    except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as e:
        if time.monotonic() >= give_up_at:
            return {"error": f"listing exceeded its {budget:g}s deadline after {objects} objects ({type(e).__name__})"}
        return {"error": f"{type(e).__name__}: {e}"}


_cache_lock = threading.Lock()
_cache: dict = {"at": None, "value": None}


def bucket_stats() -> dict:
    """Return { objects, size_bytes } for the sccache bucket by listing it
    over S3, with the same endpoint and key pair the entrypoint gave sccache.

    Cached for CACHE_TTL_SECS (errors too, so a failing store is not hammered);
    the lock makes concurrent scrapes wait for one walk instead of starting
    their own."""
    with _cache_lock:
        now = time.monotonic()
        if _cache["at"] is not None and now - _cache["at"] < CACHE_TTL_SECS:
            return dict(_cache["value"])
        value = _bucket_stats_uncached()
        _cache["at"], _cache["value"] = time.monotonic(), value
        return dict(value)


def _bucket_stats_uncached() -> dict:
    access_key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    if not access_key or not secret_key:
        return {"error": "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not set"}
    region = os.environ.get("SCCACHE_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    return list_bucket_totals(_endpoint_url(), BUCKET, access_key, secret_key, region)


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
        lines.append("# HELP sccache_bucket_objects Object count in the shared sccache object-store bucket.")
        lines.append("# TYPE sccache_bucket_objects gauge")
        lines.append(f'sccache_bucket_objects{{bucket="{BUCKET}"}} {objects}')
    if isinstance(size, int):
        lines.append("# HELP sccache_bucket_size_bytes Total byte size of the shared sccache object-store bucket.")
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
    # Threaded, so a slow bucket walk on one request never blocks /health.
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    srv.daemon_threads = True
    print(f"[sccache-stats] listening on :{port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
