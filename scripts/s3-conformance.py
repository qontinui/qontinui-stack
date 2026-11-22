#!/usr/bin/env python3
"""S3 conformance subset (D7) for choosing and guarding the stack's object store.

Plan 2026-11-22-fetches-stop-depending-on-upstreams, D8 and Phase 1. Runs the
seven operations the stack's clients rely on against one S3 endpoint and writes
one JSON record per operation:

  put               PutObject, then the stored ETag is read back
  get               GetObject, body compared byte for byte with what was put
  head              HeadObject, Content-Length equals the put size
  delete            DeleteObject, then HeadObject must answer 404
  multipart         two 5 MiB parts plus a 1 MiB tail, complete, get byte-equal
  presign           a presigned PUT and a presigned GET exercised over plain
                    HTTP (urllib, no SDK), plus the same GET with a tampered
                    signature, which MUST be refused (403). A store that serves
                    a presigned URL without checking its signature FAILS.
  create_if_absent  PutObject with ``If-None-Match: *`` twice on a fresh key;
                    the first must succeed and the second MUST answer 412.

Verdicts are PASS, FAIL or UNSUPPORTED (the server answered 501
NotImplemented). The process exits 0 only when at least seven operations
EXECUTED (got an answer from the server) and every one of them passed. A run
that executed nothing exits 2, never 0, so a misconfigured endpoint cannot read
as green [policy: testing/a-green-run-must-prove-it-ran].

Credentials are read from the environment variables NAMED by
``--access-key-env`` / ``--secret-key-env``, so a secret never reaches argv or
the JSON output.

Usage (locally, boto3 pinned):

  uv run --no-project --with boto3==1.43.126 python scripts/s3-conformance.py \\
      --endpoint http://127.0.0.1:9100 --access-key-env S3_ACCESS_KEY \\
      --secret-key-env S3_SECRET_KEY --bucket conformance --json-out out.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

SCHEMA = "s3-conformance/v1"
ALL_OPS = ("put", "get", "head", "delete", "multipart", "presign", "create_if_absent")
REQUIRED_EXECUTED = len(ALL_OPS)
MIB = 1024 * 1024

PASS = "PASS"
FAIL = "FAIL"
UNSUPPORTED = "UNSUPPORTED"


@dataclass
class OpRecord:
    """One operation's outcome. ``executed`` is true once the server answered."""

    op: str
    verdict: str = FAIL
    executed: bool = False
    http_status: list[int] = field(default_factory=list)
    bytes_compared: int = 0
    detail: str = ""


def _payload(size: int, seed: str) -> bytes:
    """Deterministic, non-repeating-looking bytes so a truncation is visible."""
    out = bytearray()
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        counter += 1
    return bytes(out[:size])


def _status_of(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(status, int):
            return status
    return None


def _error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        return str(resp.get("Error", {}).get("Code", ""))
    return ""


def _http(method: str, url: str, data: bytes | None = None, timeout: float = 30.0) -> tuple[int, bytes]:
    """Plain-HTTP request with no SDK; returns (status, body). Never raises on 4xx/5xx."""
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Length", str(len(data)))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-given endpoint)
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def _tamper_signature(url: str) -> str:
    """Flip the last hex digit of X-Amz-Signature (or Signature for SigV2)."""
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    out = []
    tampered = False
    for key, value in query:
        if key in ("X-Amz-Signature", "Signature") and value:
            last = value[-1]
            value = value[:-1] + ("0" if last != "0" else "1")
            tampered = True
        out.append((key, value))
    if not tampered:
        raise ValueError("presigned URL carries no signature parameter")
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(out, quote_via=urllib.parse.quote)))


class Conformance:
    def __init__(self, client: Any, bucket: str, prefix: str) -> None:
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    # Each op mutates ``rec`` as it goes, so a crash midway still reports what
    # was observed (statuses, executed) rather than an empty record.

    def op_put(self, rec: OpRecord) -> None:
        body = _payload(64 * 1024, "put")
        resp = self.client.put_object(Bucket=self.bucket, Key=self.key("put"), Body=body)
        rec.executed = True
        rec.http_status.append(resp["ResponseMetadata"]["HTTPStatusCode"])
        etag = resp.get("ETag", "").strip('"')
        expected = hashlib.md5(body).hexdigest()  # noqa: S324 (S3 ETag semantics, not security)
        if etag != expected:
            rec.detail = f"ETag {etag!r} != md5 {expected!r}"
            return
        rec.bytes_compared = len(body)
        rec.verdict = PASS

    def op_get(self, rec: OpRecord) -> None:
        body = _payload(256 * 1024 + 7, "get")
        key = self.key("get")
        put = self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        rec.executed = True
        rec.http_status.append(put["ResponseMetadata"]["HTTPStatusCode"])
        got = self.client.get_object(Bucket=self.bucket, Key=key)
        rec.http_status.append(got["ResponseMetadata"]["HTTPStatusCode"])
        data = got["Body"].read()
        rec.bytes_compared = len(data)
        if data != body:
            rec.detail = f"body differs: got {len(data)} bytes, put {len(body)}"
            return
        rec.verdict = PASS

    def op_head(self, rec: OpRecord) -> None:
        body = _payload(12345, "head")
        key = self.key("head")
        put = self.client.put_object(Bucket=self.bucket, Key=key, Body=body)
        rec.executed = True
        rec.http_status.append(put["ResponseMetadata"]["HTTPStatusCode"])
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        rec.http_status.append(head["ResponseMetadata"]["HTTPStatusCode"])
        length = head.get("ContentLength")
        if length != len(body):
            rec.detail = f"ContentLength {length} != {len(body)}"
            return
        rec.verdict = PASS

    def op_delete(self, rec: OpRecord) -> None:
        key = self.key("delete")
        put = self.client.put_object(Bucket=self.bucket, Key=key, Body=b"to-be-deleted")
        rec.executed = True
        rec.http_status.append(put["ResponseMetadata"]["HTTPStatusCode"])
        dele = self.client.delete_object(Bucket=self.bucket, Key=key)
        rec.http_status.append(dele["ResponseMetadata"]["HTTPStatusCode"])
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:  # botocore ClientError
            status = _status_of(exc)
            if status is None:
                raise
            rec.http_status.append(status)
            if status == 404:
                rec.verdict = PASS
            else:
                rec.detail = f"HEAD after delete answered {status}, want 404"
            return
        rec.http_status.append(head["ResponseMetadata"]["HTTPStatusCode"])
        rec.detail = "object still present after DeleteObject"

    def op_multipart(self, rec: OpRecord) -> None:
        key = self.key("multipart")
        parts_data = [_payload(5 * MIB, "mp1"), _payload(5 * MIB, "mp2"), _payload(1 * MIB, "mp3")]
        created = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)
        rec.executed = True
        rec.http_status.append(created["ResponseMetadata"]["HTTPStatusCode"])
        upload_id = created["UploadId"]
        completed = []
        try:
            for number, data in enumerate(parts_data, start=1):
                part = self.client.upload_part(
                    Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=data
                )
                rec.http_status.append(part["ResponseMetadata"]["HTTPStatusCode"])
                completed.append({"ETag": part["ETag"], "PartNumber": number})
            done = self.client.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": completed}
            )
            rec.http_status.append(done["ResponseMetadata"]["HTTPStatusCode"])
        except Exception:
            try:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
            except Exception:  # best effort; the original error is what matters
                pass
            raise
        got = self.client.get_object(Bucket=self.bucket, Key=key)
        rec.http_status.append(got["ResponseMetadata"]["HTTPStatusCode"])
        data = got["Body"].read()
        want = b"".join(parts_data)
        rec.bytes_compared = len(data)
        if data != want:
            rec.detail = f"assembled object differs: got {len(data)} bytes, want {len(want)}"
            return
        rec.verdict = PASS

    def op_presign(self, rec: OpRecord) -> None:
        key = self.key("presign")
        body = _payload(32 * 1024, "presign")
        put_url = self.client.generate_presigned_url(
            "put_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=300
        )
        status, _ = _http("PUT", put_url, data=body)
        rec.executed = True
        rec.http_status.append(status)
        if status != 200:
            rec.detail = f"presigned PUT answered {status}"
            return
        get_url = self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=300
        )
        status, data = _http("GET", get_url)
        rec.http_status.append(status)
        if status != 200:
            rec.detail = f"presigned GET answered {status}"
            return
        rec.bytes_compared = len(data)
        if data != body:
            rec.detail = f"presigned GET body differs: got {len(data)} bytes, want {len(body)}"
            return
        status, _ = _http("GET", _tamper_signature(get_url))
        rec.http_status.append(status)
        if status != 403:
            rec.detail = f"GET with a tampered signature answered {status}, want 403 (signature not checked)"
            return
        rec.verdict = PASS

    def op_create_if_absent(self, rec: OpRecord) -> None:
        key = self.key(f"cia-{uuid.uuid4().hex}")
        first = self.client.put_object(Bucket=self.bucket, Key=key, Body=b"first", IfNoneMatch="*")
        rec.executed = True
        rec.http_status.append(first["ResponseMetadata"]["HTTPStatusCode"])
        try:
            second = self.client.put_object(Bucket=self.bucket, Key=key, Body=b"second", IfNoneMatch="*")
        except Exception as exc:
            status = _status_of(exc)
            if status is None:
                raise
            rec.http_status.append(status)
            if status == 412:
                got = self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
                rec.bytes_compared = len(got)
                if got == b"first":
                    rec.verdict = PASS
                else:
                    rec.detail = f"412 answered but the object now holds {got!r}"
            elif status == 501:
                rec.verdict = UNSUPPORTED
                rec.detail = "second conditional put answered 501"
            else:
                rec.detail = f"second conditional put answered {status} ({_error_code(exc)}), want 412"
            return
        rec.http_status.append(second["ResponseMetadata"]["HTTPStatusCode"])
        rec.detail = "second PutObject with If-None-Match: * succeeded; the header was ignored"


def run_op(conf: Conformance, name: str) -> OpRecord:
    rec = OpRecord(op=name)
    fn: Callable[[OpRecord], None] = getattr(conf, f"op_{name}")
    try:
        fn(rec)
    except Exception as exc:  # report, never crash the whole run on one op
        status = _status_of(exc)
        if status is not None:
            rec.executed = True
            rec.http_status.append(status)
            if status == 501 or _error_code(exc) == "NotImplemented":
                rec.verdict = UNSUPPORTED
        rec.detail = rec.detail or f"{type(exc).__name__}: {exc}"[:500]
    return rec


def ensure_bucket(client: Any, bucket: str) -> str:
    """Create the bucket if it is absent. Returns a note for the JSON record."""
    try:
        client.head_bucket(Bucket=bucket)
        return "existing"
    except Exception as exc:
        if _status_of(exc) not in (404, 403, 400, None):
            raise
    try:
        client.create_bucket(Bucket=bucket)
        return "created"
    except Exception as exc:
        code = _error_code(exc)
        if code in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
            return "existing"
        return f"create failed: {code or type(exc).__name__}"


def build_client(endpoint: str, access_key: str, secret_key: str, region: str) -> Any:
    import boto3
    from botocore.config import Config

    # Checksums only when an operation requires them: the subset is what the
    # stack's clients (bazel-remote's minio-go, sccache's opendal) send, not
    # boto3's newer default trailing CRC32, which is a separate compatibility
    # axis and would fail stores the clients themselves work with.
    config = Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        retries={"max_attempts": 1, "mode": "standard"},
        connect_timeout=5,
        read_timeout=60,
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=config,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--endpoint", required=True, help="S3 endpoint URL, e.g. http://127.0.0.1:9100")
    p.add_argument("--access-key-env", required=True, help="NAME of the env var holding the access key")
    p.add_argument("--secret-key-env", required=True, help="NAME of the env var holding the secret key")
    p.add_argument("--bucket", required=True)
    p.add_argument("--json-out", required=True, help="path to write the JSON result")
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--label", default="", help="free-text label recorded in the JSON (e.g. candidate name)")
    p.add_argument(
        "--ops",
        default=",".join(ALL_OPS),
        help="comma-separated subset to run (default: all seven). Fewer than seven can never exit 0.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    access_key = os.environ.get(args.access_key_env, "")
    secret_key = os.environ.get(args.secret_key_env, "")
    if not access_key or not secret_key:
        print(
            f"s3-conformance: {args.access_key_env} / {args.secret_key_env} must both be set", file=sys.stderr
        )
        return 2
    ops = [o.strip() for o in args.ops.split(",") if o.strip()]
    unknown = [o for o in ops if o not in ALL_OPS]
    if unknown:
        print(f"s3-conformance: unknown operation(s) {unknown}; known: {list(ALL_OPS)}", file=sys.stderr)
        return 2

    started = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    try:
        import boto3

        boto3_version = boto3.__version__
    except ImportError:
        print("s3-conformance: boto3 is not installed (uv run --with boto3==1.43.126 ...)", file=sys.stderr)
        return 2

    client = build_client(args.endpoint, access_key, secret_key, args.region)
    records: list[OpRecord] = []
    bucket_note = "not attempted (no operations requested)"
    if ops:
        try:
            bucket_note = ensure_bucket(client, args.bucket)
        except Exception as exc:
            bucket_note = f"bucket check failed: {type(exc).__name__}: {exc}"[:300]
        conf = Conformance(client, args.bucket, prefix=f"s3-conformance/{uuid.uuid4().hex[:12]}")
        records = [run_op(conf, name) for name in ops]

    executed = sum(1 for r in records if r.executed)
    passed = sum(1 for r in records if r.executed and r.verdict == PASS)
    ok = executed >= REQUIRED_EXECUTED and passed == executed
    result = {
        "schema": SCHEMA,
        "label": args.label,
        "endpoint": args.endpoint,
        "bucket": args.bucket,
        "bucket_setup": bucket_note,
        "region": args.region,
        "boto3_version": boto3_version,
        "started_at": started,
        "operations": [asdict(r) for r in records],
        "executed": executed,
        "passed": passed,
        "required_executed": REQUIRED_EXECUTED,
        "verdict": PASS if ok else FAIL,
    }
    with open(args.json_out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
        fh.write("\n")
    for r in records:
        print(f"{r.op:17s} {r.verdict:11s} status={r.http_status} bytes={r.bytes_compared} {r.detail}")
    print(f"executed={executed} passed={passed} verdict={result['verdict']}")
    if executed == 0:
        print("s3-conformance: ZERO operations executed — this is not a pass", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
