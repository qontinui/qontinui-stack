"""Tests for ``sccache/stats_server.py``'s S3 bucket metrics.

Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 2. The stats server
used to shell out to ``mc du --json``; it now walks the bucket with a
SigV4-signed ListObjectsV2 (stdlib only), and coord's ``sccache_metrics.rs``
scrapes the resulting ``sccache_bucket_objects`` / ``sccache_bucket_size_bytes``
gauges. Two arms:

* **Stub arm (always runs).** An in-process S3 endpoint that VERIFIES the
  header signature and paginates. The walk must count every object across
  pages; a wrong secret, a truncated listing with no continuation token and an
  unreachable store must each come back as ``{"error": ...}``, never as a
  number — a metrics surface that turns a failed read into ``0`` would tell
  coord the shared cache is empty [policy: verification-and-evidence/
  silent-empty-is-unknown].
* **Live arm (the conformance store).** When ``QONTINUI_S3_TEST_ENDPOINT`` is
  set it puts three objects into a fresh bucket on that store (boto3) and
  asserts the object and size counts are non-empty and exact. The CI job
  ``s3 store conformance`` runs it against versitygw and against the SeaweedFS
  fallback with ``QONTINUI_S3_TEST_REQUIRED=1``, which turns a skip into a
  failure so the arm cannot pass by not running.

Locally::

    uv run --no-project --with boto3==1.43.126 --with pytest \
        python -m pytest scripts/tests/test_sccache_stats_server.py -q
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import socket
import sys
import threading
import unittest
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_MODULE_PATH = Path(__file__).resolve().parents[2] / "sccache" / "stats_server.py"
_spec = importlib.util.spec_from_file_location("sccache_stats_server", _MODULE_PATH)
assert _spec and _spec.loader
stats = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = stats
_spec.loader.exec_module(stats)

ACCESS = "stats-stub-access"
SECRET = "stats-stub-secret-not-a-real-secret"
REGION = "us-east-1"


def _uri_encode(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def _header_signature_valid(handler: BaseHTTPRequestHandler) -> bool:
    """Recompute the SigV4 header signature of a request with the stub's secret."""
    auth = handler.headers.get("Authorization", "")
    if not auth.startswith("AWS4-HMAC-SHA256 "):
        return False
    fields = dict(
        item.strip().split("=", 1) for item in auth[len("AWS4-HMAC-SHA256 "):].split(",")
    )
    try:
        access, date, region, service, terminal = fields["Credential"].split("/")
    except (KeyError, ValueError):
        return False
    if access != ACCESS or terminal != "aws4_request":
        return False
    parts = urllib.parse.urlsplit(handler.path)
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    canonical_query = "&".join(f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(pairs))
    signed = fields.get("SignedHeaders", "").split(";")
    canonical_headers = "".join(f"{h}:{(handler.headers.get(h) or '').strip()}\n" for h in signed)
    payload_hash = handler.headers.get("x-amz-content-sha256", "")
    canonical_request = "\n".join(
        [handler.command, parts.path, canonical_query, canonical_headers, ";".join(signed), payload_hash]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            handler.headers.get("x-amz-date", ""),
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + SECRET).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, fields.get("Signature", ""))


class _ListStub:
    """A bucket of ``objects`` (key -> size) served by a signature-checking
    ListObjectsV2. ``drop_token`` makes it claim truncation without a token."""

    def __init__(self, objects: dict[str, int], drop_token: bool = False) -> None:
        self.objects = dict(sorted(objects.items()))
        self.drop_token = drop_token
        self.pages_served = 0
        store = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if not _header_signature_valid(self):
                    return self._send(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
                if query.get("list-type") != "2":
                    return self._send(400, b"<Error><Code>InvalidRequest</Code></Error>")
                keys = list(store.objects)
                start = int(query.get("continuation-token") or 0)
                size = int(query.get("max-keys") or 1000)
                page = keys[start:start + size]
                truncated = start + size < len(keys)
                store.pages_served += 1
                body = ['<?xml version="1.0" encoding="UTF-8"?>',
                        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                        f"<KeyCount>{len(page)}</KeyCount>",
                        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>"]
                if truncated and not store.drop_token:
                    body.append(f"<NextContinuationToken>{start + size}</NextContinuationToken>")
                for k in page:
                    body.append(f"<Contents><Key>{k}</Key><Size>{store.objects[k]}</Size></Contents>")
                body.append("</ListBucketResult>")
                self._send(200, "".join(body).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "_ListStub":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FIVE = {"sccache/a/1": 100, "sccache/a/2": 250, "sccache/b/3": 1, "sccache/c/4": 4096, "other/5": 7}


class StubListingTests(unittest.TestCase):
    def test_walk_counts_every_object_across_pages(self) -> None:
        with _ListStub(FIVE) as stub:
            got = stats.list_bucket_totals(stub.url, "qontinui-sccache", ACCESS, SECRET, REGION, page_size=2)
            self.assertEqual(got, {"objects": 5, "size_bytes": sum(FIVE.values())})
            self.assertEqual(stub.pages_served, 3, "five objects at two per page is three pages")

    def test_empty_bucket_is_a_real_zero(self) -> None:
        with _ListStub({}) as stub:
            got = stats.list_bucket_totals(stub.url, "qontinui-sccache", ACCESS, SECRET, REGION)
        self.assertEqual(got, {"objects": 0, "size_bytes": 0})

    def test_wrong_secret_is_an_error_not_a_zero(self) -> None:
        # The negative control for the signer: the stub checks signatures, so a
        # passing walk above proves the signing is right, and this proves the
        # stub would have caught it if it were not.
        with _ListStub(FIVE) as stub:
            got = stats.list_bucket_totals(stub.url, "qontinui-sccache", ACCESS, "wrong-secret", REGION)
        self.assertIn("error", got)
        self.assertIn("403", got["error"])
        self.assertNotIn("objects", got)

    def test_truncated_listing_without_a_token_is_an_error(self) -> None:
        with _ListStub(FIVE, drop_token=True) as stub:
            got = stats.list_bucket_totals(stub.url, "qontinui-sccache", ACCESS, SECRET, REGION, page_size=2)
        self.assertIn("error", got)
        self.assertNotIn("objects", got)

    def test_unreachable_store_is_an_error(self) -> None:
        got = stats.list_bucket_totals(f"http://127.0.0.1:{_closed_port()}", "b", ACCESS, SECRET, REGION, timeout=2)
        self.assertIn("error", got)

    def test_bucket_stats_reads_the_sccache_env(self) -> None:
        with _ListStub(FIVE) as stub:
            host = stub.url.removeprefix("http://")
            env = {"SCCACHE_ENDPOINT": host, "SCCACHE_S3_USE_SSL": "off", "SCCACHE_REGION": REGION,
                   "AWS_ACCESS_KEY_ID": ACCESS, "AWS_SECRET_ACCESS_KEY": SECRET}
            with mock.patch.dict(os.environ, env):
                got = stats.bucket_stats()
        self.assertEqual(got, {"objects": 5, "size_bytes": sum(FIVE.values())})

    def test_bucket_stats_without_credentials_is_an_error(self) -> None:
        with mock.patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""}):
            self.assertIn("error", stats.bucket_stats())

    def test_prometheus_payload_exports_the_counts_and_names_no_minio(self) -> None:
        text = stats._prometheus_payload({"bucket": {"objects": 5, "size_bytes": 4454}, "local": {}})
        self.assertIn('sccache_bucket_objects{bucket="qontinui-sccache"} 5', text)
        self.assertIn('sccache_bucket_size_bytes{bucket="qontinui-sccache"} 4454', text)
        self.assertNotIn("minio", text.lower())

    def test_prometheus_payload_omits_gauges_on_error(self) -> None:
        text = stats._prometheus_payload({"bucket": {"error": "HTTP 403"}, "local": {}})
        self.assertNotIn("sccache_bucket_objects", text)


LIVE_ENDPOINT = os.environ.get("QONTINUI_S3_TEST_ENDPOINT", "")
LIVE_REQUIRED = os.environ.get("QONTINUI_S3_TEST_REQUIRED", "") == "1"


class LiveStoreTests(unittest.TestCase):
    """Against a real store (the conformance store in CI)."""

    def setUp(self) -> None:
        if not LIVE_ENDPOINT:
            if LIVE_REQUIRED:
                self.fail("QONTINUI_S3_TEST_REQUIRED=1 but QONTINUI_S3_TEST_ENDPOINT is unset")
            self.skipTest("QONTINUI_S3_TEST_ENDPOINT not set")
        try:
            import boto3  # noqa: F401
        except ImportError:
            if LIVE_REQUIRED:
                self.fail("QONTINUI_S3_TEST_REQUIRED=1 but boto3 is not installed")
            self.skipTest("boto3 not installed")

    def test_counts_are_non_empty_and_exact_on_the_real_store(self) -> None:
        import boto3
        from botocore.config import Config

        access = os.environ["S3_ACCESS_KEY"]
        secret = os.environ["S3_SECRET_KEY"]
        client = boto3.client(
            "s3", endpoint_url=LIVE_ENDPOINT, region_name=REGION,
            aws_access_key_id=access, aws_secret_access_key=secret,
            config=Config(s3={"addressing_style": "path"},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
        bucket = f"stats-test-{uuid.uuid4().hex[:12]}"
        client.create_bucket(Bucket=bucket)
        bodies = {"sccache/x/one": b"a" * 10, "sccache/y/two": b"b" * 2000, "top": b"c" * 5}
        try:
            for key, body in bodies.items():
                client.put_object(Bucket=bucket, Key=key, Body=body)
            got = stats.list_bucket_totals(LIVE_ENDPOINT, bucket, access, secret, REGION, page_size=2)
            self.assertNotIn("error", got, got)
            self.assertGreater(got["objects"], 0)
            self.assertGreater(got["size_bytes"], 0)
            self.assertEqual(got, {"objects": 3, "size_bytes": 2015})
        finally:
            for key in bodies:
                client.delete_object(Bucket=bucket, Key=key)
            client.delete_bucket(Bucket=bucket)


if __name__ == "__main__":
    unittest.main()
