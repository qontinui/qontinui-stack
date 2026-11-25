"""Tests for ``s3-conformance.py`` against an in-process S3 stub.

Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 1 gate.

The stub has two modes:

* ``conforming`` — honours ``If-None-Match: *`` and verifies the SigV4 query
  signature of a presigned URL. This is the POSITIVE control: the script must
  report 7/7 PASS and exit 0 against it, which proves the stub is complete
  enough that the negative control's failures are attributable to the two
  defects and nothing else.
* ``broken`` — ignores ``If-None-Match`` and serves a presigned URL without
  checking its signature. This is the NEGATIVE control the plan's D8 requires
  [policy: testing/a-test-must-be-able-to-fail]: ``create_if_absent`` and
  ``presign`` MUST report FAIL and the run must exit non-zero, or the
  conformance run that picks a store is void.

A run that executed zero operations must exit non-zero
[policy: testing/a-green-run-must-prove-it-ran].

Needs boto3 (the CI job installs it; locally
``uv run --no-project --with boto3==1.43.126 --with pytest python -m pytest
scripts/tests/test_s3_conformance.py -q``). Written against
``unittest.TestCase`` so both runners work, matching the sibling tests.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

_MODULE_PATH = Path(__file__).resolve().parent.parent / "s3-conformance.py"
_spec = importlib.util.spec_from_file_location("s3_conformance", _MODULE_PATH)
assert _spec and _spec.loader
conformance = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = conformance  # dataclasses resolves annotations through sys.modules
_spec.loader.exec_module(conformance)

ACCESS = "stub-access-key"
SECRET = "stub-secret-key-not-a-real-secret"


def _uri_encode(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.~")


def _sigv4_query_valid(method: str, raw_path: str, raw_query: str, host: str) -> bool:
    """Verify a SigV4 presigned (query-string) signature with the stub's secret."""
    pairs = urllib.parse.parse_qsl(raw_query, keep_blank_values=True)
    params = dict(pairs)
    signature = params.get("X-Amz-Signature", "")
    credential = params.get("X-Amz-Credential", "")
    amz_date = params.get("X-Amz-Date", "")
    signed_headers = params.get("X-Amz-SignedHeaders", "")
    try:
        access, date, region, service, terminal = credential.split("/")
    except ValueError:
        return False
    if access != ACCESS or terminal != "aws4_request" or signed_headers != "host":
        return False
    canonical_query = "&".join(
        f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(pairs) if k != "X-Amz-Signature"
    )
    canonical_request = "\n".join(
        [method, raw_path, canonical_query, f"host:{host}\n", signed_headers, "UNSIGNED-PAYLOAD"]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = ("AWS4" + SECRET).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class _Store:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.buckets: set[str] = set()
        self.objects: dict[tuple[str, str], bytes] = {}
        self.uploads: dict[str, dict[int, bytes]] = {}
        self.lock = threading.Lock()


def _make_handler(store: _Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # keep test output clean
            pass

        # -- helpers ---------------------------------------------------------
        def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def _error(self, status: int, code: str) -> None:
            body = f"<Error><Code>{code}</Code><Message>{code}</Message></Error>".encode()
            self._send(status, body, {"Content-Type": "application/xml"})

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _parse(self) -> tuple[str, str, dict[str, str], str, str]:
            parts = urllib.parse.urlsplit(self.path)
            raw_path = parts.path
            segments = urllib.parse.unquote(raw_path).lstrip("/").split("/", 1)
            bucket = segments[0]
            key = segments[1] if len(segments) > 1 else ""
            query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            return bucket, key, query, raw_path, parts.query

        def _authorised(self, query: dict[str, str], raw_path: str, raw_query: str) -> bool:
            if "X-Amz-Signature" not in query:
                return True  # header auth is not what the stub models
            if store.mode == "broken":
                return True  # defect 1: presigned URLs served unchecked
            return _sigv4_query_valid(self.command, raw_path, raw_query, self.headers.get("Host", ""))

        # -- verbs -------------------------------------------------------------
        def do_HEAD(self) -> None:
            bucket, key, query, raw_path, raw_query = self._parse()
            if not self._authorised(query, raw_path, raw_query):
                return self._error(403, "SignatureDoesNotMatch")
            if not key:
                return self._send(200 if bucket in store.buckets else 404)
            data = store.objects.get((bucket, key))
            if data is None:
                return self._send(404)
            etag = hashlib.md5(data).hexdigest()  # noqa: S324
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", f'"{etag}"')
            self.end_headers()

        def do_GET(self) -> None:
            bucket, key, query, raw_path, raw_query = self._parse()
            if not self._authorised(query, raw_path, raw_query):
                return self._error(403, "SignatureDoesNotMatch")
            data = store.objects.get((bucket, key))
            if data is None:
                return self._error(404, "NoSuchKey")
            etag = hashlib.md5(data).hexdigest()  # noqa: S324
            self._send(200, data, {"ETag": f'"{etag}"', "Content-Type": "application/octet-stream"})

        def do_PUT(self) -> None:
            bucket, key, query, raw_path, raw_query = self._parse()
            body = self._body()
            if not self._authorised(query, raw_path, raw_query):
                return self._error(403, "SignatureDoesNotMatch")
            if not key:
                store.buckets.add(bucket)
                return self._send(200)
            if "uploadId" in query:
                with store.lock:
                    store.uploads[query["uploadId"]][int(query["partNumber"])] = body
                etag = hashlib.md5(body).hexdigest()  # noqa: S324
                return self._send(200, headers={"ETag": f'"{etag}"'})
            with store.lock:
                exists = (bucket, key) in store.objects
                conditional = self.headers.get("If-None-Match", "").strip() == "*"
                # defect 2 (broken mode): If-None-Match ignored
                if conditional and exists and store.mode == "conforming":
                    return self._error(412, "PreconditionFailed")
                store.objects[(bucket, key)] = body
            etag = hashlib.md5(body).hexdigest()  # noqa: S324
            self._send(200, headers={"ETag": f'"{etag}"'})

        def do_POST(self) -> None:
            bucket, key, query, _raw_path, _raw_query = self._parse()
            self._body()
            if "uploads" in query:
                upload_id = uuid.uuid4().hex
                store.uploads[upload_id] = {}
                xml = (
                    "<InitiateMultipartUploadResult>"
                    f"<Bucket>{bucket}</Bucket><Key>{key}</Key><UploadId>{upload_id}</UploadId>"
                    "</InitiateMultipartUploadResult>"
                ).encode()
                return self._send(200, xml, {"Content-Type": "application/xml"})
            if "uploadId" in query:
                parts = store.uploads.pop(query["uploadId"])
                data = b"".join(parts[n] for n in sorted(parts))
                store.objects[(bucket, key)] = data
                xml = (
                    "<CompleteMultipartUploadResult>"
                    f"<Bucket>{bucket}</Bucket><Key>{key}</Key><ETag>\"mp-{len(parts)}\"</ETag>"
                    "</CompleteMultipartUploadResult>"
                ).encode()
                return self._send(200, xml, {"Content-Type": "application/xml"})
            self._error(400, "InvalidRequest")

        def do_DELETE(self) -> None:
            bucket, key, query, _raw_path, _raw_query = self._parse()
            if "uploadId" in query:
                store.uploads.pop(query["uploadId"], None)
            else:
                store.objects.pop((bucket, key), None)
            self._send(204)

    return Handler


class _StubServer:
    def __init__(self, mode: str) -> None:
        self.store = _Store(mode)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.store))
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_StubServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ConformanceRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = os.path.join(self.tmp.name, "result.json")
        patcher = mock.patch.dict(os.environ, {"STUB_AK": ACCESS, "STUB_SK": SECRET})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, endpoint: str, *extra: str) -> tuple[int, dict]:
        code = conformance.main(
            [
                "--endpoint", endpoint,
                "--access-key-env", "STUB_AK",
                "--secret-key-env", "STUB_SK",
                "--bucket", "conformance",
                "--json-out", self.out,
                *extra,
            ]
        )
        result = json.loads(Path(self.out).read_text()) if os.path.exists(self.out) else {}
        return code, result

    @staticmethod
    def _verdicts(result: dict) -> dict[str, str]:
        return {r["op"]: r["verdict"] for r in result["operations"]}

    def test_positive_control_conforming_stub_passes_all_seven(self) -> None:
        with _StubServer("conforming") as stub:
            code, result = self._run(stub.endpoint)
        self.assertEqual(self._verdicts(result), {op: "PASS" for op in conformance.ALL_OPS}, result)
        self.assertEqual(result["executed"], 7)
        self.assertEqual(result["verdict"], "PASS")
        self.assertEqual(code, 0)
        multipart = next(r for r in result["operations"] if r["op"] == "multipart")
        self.assertEqual(multipart["bytes_compared"], 11 * 1024 * 1024)

    def test_negative_control_broken_stub_fails_presign_and_create_if_absent(self) -> None:
        with _StubServer("broken") as stub:
            code, result = self._run(stub.endpoint)
        verdicts = self._verdicts(result)
        self.assertEqual(verdicts["create_if_absent"], "FAIL", result)
        self.assertEqual(verdicts["presign"], "FAIL", result)
        for op in ("put", "get", "head", "delete", "multipart"):
            self.assertEqual(verdicts[op], "PASS", (op, result))
        presign = next(r for r in result["operations"] if r["op"] == "presign")
        self.assertEqual(presign["http_status"], [200, 200, 200])  # tampered GET was served
        self.assertIn("tampered", presign["detail"])
        self.assertEqual(result["verdict"], "FAIL")
        self.assertNotEqual(code, 0)

    def test_zero_operation_run_exits_non_zero(self) -> None:
        with _StubServer("conforming") as stub:
            code, result = self._run(stub.endpoint, "--ops", "")
        self.assertEqual(result["executed"], 0)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertNotEqual(code, 0)

    def test_unreachable_endpoint_executes_nothing_and_exits_non_zero(self) -> None:
        code, result = self._run(f"http://127.0.0.1:{_closed_port()}")
        self.assertEqual(result["executed"], 0)
        self.assertNotEqual(code, 0)

    def test_a_passing_subset_never_exits_zero(self) -> None:
        with _StubServer("conforming") as stub:
            code, result = self._run(stub.endpoint, "--ops", "put,get")
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["verdict"], "FAIL")
        self.assertNotEqual(code, 0)

    def test_missing_credentials_refuse(self) -> None:
        with mock.patch.dict(os.environ, {"STUB_SK": ""}):
            code, _ = self._run("http://127.0.0.1:1")
        self.assertEqual(code, 2)

    def test_secret_never_reaches_the_json(self) -> None:
        with _StubServer("conforming") as stub:
            self._run(stub.endpoint)
        self.assertNotIn(SECRET, Path(self.out).read_text())


class TamperTests(unittest.TestCase):
    def test_tamper_changes_only_the_signature(self) -> None:
        url = "http://h/b/k?X-Amz-Date=1&X-Amz-Signature=abc0"
        tampered = conformance._tamper_signature(url)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(tampered).query))
        self.assertEqual(q["X-Amz-Signature"], "abc1")
        self.assertEqual(q["X-Amz-Date"], "1")

    def test_tamper_refuses_an_unsigned_url(self) -> None:
        with self.assertRaises(ValueError):
            conformance._tamper_signature("http://h/b/k?x=1")


if __name__ == "__main__":
    unittest.main()
