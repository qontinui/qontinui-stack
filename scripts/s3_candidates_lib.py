#!/usr/bin/env python3
"""Helpers for scripts/s3-candidates.sh (stdlib only unless a subcommand says otherwise).

Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 1. The shell script
owns process lifecycle (start, setup, stop); this module owns everything that
is data: reading the pin file, the cooldown and licence screens, free ports,
idle RSS of a process tree, the two client round trips' S3 reads, and D8's
decision rule.

Subcommands (``python3 -I s3_candidates_lib.py <cmd> ...``):

  pin-env --pins F --section candidate|client --name N   shell-quoted PIN_* assignments
  names --pins F --section candidate                     one name per line
  cooldown --pins F --section S --name N [--today D]     exit 1 inside the cooldown
  licence --pins F --name N --file LICENSE --out J       licence screen record
  ports N                                                N free loopback TCP ports
  rss --pid P --out J                                    idle RSS + process count of P's tree
  wait-port HOST PORT SECS                               exit 0 once HOST:PORT accepts
  http-put URL FILE / http-get URL OUT                   plain-HTTP helpers (print status)
  s3-wait-key (needs boto3)                              poll until a key under a prefix exists
  s3-count (needs boto3)                                 count objects under a prefix
  decide --pins F --results-dir D --out J                apply D8's rule to the run's records
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

PAGE_KIB = os.sysconf("SC_PAGE_SIZE") // 1024

# Source-available licences are excluded at the screen (D8). The markers are
# phrases each licence's own text carries.
SOURCE_AVAILABLE_MARKERS = {
    "BUSL": "Business Source License",
    "SSPL": "Server Side Public License",
    "Elastic": "Elastic License",
    "Commons Clause": "Commons Clause",
}
OSI_MARKERS = [
    ("AGPL-3.0", "GNU AFFERO GENERAL PUBLIC LICENSE"),
    ("GPL-3.0", "GNU GENERAL PUBLIC LICENSE\n                       Version 3"),
    ("LGPL", "GNU LESSER GENERAL PUBLIC LICENSE"),
    ("Apache-2.0", "Apache License\n                           Version 2.0"),
    ("Apache-2.0", "Apache License, Version 2.0"),
    ("MIT", "Permission is hereby granted, free of charge"),
]


def _load(pins: str) -> dict:
    with open(pins, "rb") as fh:
        return tomllib.load(fh)


def _entry(doc: dict, section: str, name: str) -> dict:
    for entry in doc.get(section, []):
        if entry.get("name") == name:
            return entry
    raise SystemExit(f"s3-candidates: no [[{section}]] named {name!r} in the pin file")


def cmd_pin_env(a: argparse.Namespace) -> int:
    entry = _entry(_load(a.pins), a.section, a.name)
    for key, value in entry.items():
        if isinstance(value, (str, int, float)):
            print(f"PIN_{key.upper()}={shlex.quote(str(value))}")
    return 0


def cmd_names(a: argparse.Namespace) -> int:
    for entry in _load(a.pins).get(a.section, []):
        print(entry["name"])
    return 0


def cooldown_verdict(entry: dict, days: int, today: dt.date) -> tuple[bool, str]:
    released = dt.date.fromisoformat(entry["released_at"])
    pinned = dt.date.fromisoformat(entry["pinned_at"])
    if entry.get("cooldown_waiver"):
        return True, f"waived: {entry['cooldown_waiver']}"
    if (pinned - released).days < days:
        return False, f"pinned {(pinned - released).days} days after release (< {days})"
    if (today - released).days < days:
        return False, f"released {(today - released).days} days ago (< {days})"
    return True, f"released {entry['released_at']}, pinned {entry['pinned_at']} ({(pinned - released).days} days)"


def cmd_cooldown(a: argparse.Namespace) -> int:
    doc = _load(a.pins)
    entry = _entry(doc, a.section, a.name)
    today = dt.date.fromisoformat(a.today) if a.today else dt.datetime.now(dt.timezone.utc).date()
    ok, why = cooldown_verdict(entry, int(doc.get("cooldown_days", 14)), today)
    print(why)
    return 0 if ok else 1


def classify_licence(text: str) -> tuple[str, list[str]]:
    hits = [name for name, phrase in SOURCE_AVAILABLE_MARKERS.items() if phrase.lower() in text.lower()]
    norm = re.sub(r"\s+", " ", text)
    for spdx, phrase in OSI_MARKERS:
        if re.sub(r"\s+", " ", phrase) in norm:
            return spdx, hits
    return "UNKNOWN", hits


def cmd_licence(a: argparse.Namespace) -> int:
    entry = _entry(_load(a.pins), "candidate", a.name)
    data = Path(a.file).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    spdx, source_available = classify_licence(data.decode("utf-8", "replace"))
    pinned_ok = digest == entry.get("license_sha256")
    excluded = bool(source_available) or spdx == "UNKNOWN" or not pinned_ok
    record = {
        "name": a.name,
        "license_url": entry.get("license_url"),
        "license_sha256": digest,
        "license_sha256_pinned": entry.get("license_sha256"),
        "license_sha256_matches_pin": pinned_ok,
        "license_detected": spdx,
        "source_available_markers": source_available,
        "screen": "EXCLUDED" if excluded else "PASS",
        "governance": entry.get("governance"),
        "governance_note": entry.get("governance_note"),
    }
    Path(a.out).write_text(json.dumps(record, indent=2) + "\n")
    print(f"licence {a.name}: {spdx} screen={record['screen']}")
    return 0 if not excluded else 1


def cmd_ports(a: argparse.Namespace) -> int:
    socks = []
    try:
        for _ in range(a.count):
            s = socket.socket()
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        print(" ".join(str(s.getsockname()[1]) for s in socks))
    finally:
        for s in socks:
            s.close()
    return 0


def _children(pid: int) -> list[int]:
    out: list[int] = []
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = [t.name for t in task_dir.iterdir()]
    except FileNotFoundError:
        return out
    for tid in tids:
        try:
            out += [int(c) for c in Path(f"/proc/{pid}/task/{tid}/children").read_text().split()]
        except FileNotFoundError:
            continue
    return out


def _status(pid: int) -> dict[str, str]:
    fields = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, value = line.partition(":")
        fields[key] = value.strip()
    return fields


def cmd_rss(a: argparse.Namespace) -> int:
    tree, todo = [], [a.pid]
    while todo:
        pid = todo.pop()
        tree.append(pid)
        todo += _children(pid)
    procs = []
    total_kib = 0
    for pid in tree:
        try:
            st = _status(pid)
        except FileNotFoundError:
            continue
        rss_kib = int(st.get("VmRSS", "0 kB").split()[0])
        total_kib += rss_kib
        procs.append({"pid": pid, "name": st.get("Name"), "vmrss_kib": rss_kib, "threads": int(st.get("Threads", "0"))})
    record = {
        "root_pid": a.pid,
        "processes": len(procs),
        "idle_rss_kib": total_kib,
        "idle_rss_mib": round(total_kib / 1024, 1),
        "per_process": procs,
        "instrument": "/proc/<pid>/status VmRSS, summed over the process tree",
        "measured_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }
    Path(a.out).write_text(json.dumps(record, indent=2) + "\n")
    print(f"idle rss {record['idle_rss_mib']} MiB over {len(procs)} process(es)")
    return 0 if procs else 1


def cmd_wait_port(a: argparse.Namespace) -> int:
    deadline = time.monotonic() + a.secs
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((a.host, a.port), timeout=1):
                return 0
        except OSError:
            time.sleep(0.25)
    return 1


def cmd_http_put(a: argparse.Namespace) -> int:
    data = Path(a.file).read_bytes()
    req = urllib.request.Request(a.url, data=data, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (loopback)
            print(resp.status)
    except urllib.error.HTTPError as err:
        print(err.code)
    return 0


def cmd_http_get(a: argparse.Namespace) -> int:
    try:
        with urllib.request.urlopen(a.url, timeout=60) as resp:  # noqa: S310 (loopback)
            Path(a.out).write_bytes(resp.read())
            print(resp.status)
    except urllib.error.HTTPError as err:
        print(err.code)
    return 0


def _s3(a: argparse.Namespace):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=a.endpoint,
        aws_access_key_id=os.environ[a.access_key_env],
        aws_secret_access_key=os.environ[a.secret_key_env],
        region_name=a.region,
        config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


def _list(client, bucket: str, prefix: str) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        out += page.get("Contents", [])
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def cmd_s3_wait_key(a: argparse.Namespace) -> int:
    client = _s3(a)
    deadline = time.monotonic() + a.secs
    while time.monotonic() < deadline:
        hits = [o["Key"] for o in _list(client, a.bucket, a.prefix) if a.contains in o["Key"]]
        if hits:
            print(hits[0])
            return 0
        time.sleep(0.5)
    return 1


def cmd_s3_count(a: argparse.Namespace) -> int:
    objs = _list(_s3(a), a.bucket, a.prefix)
    print(json.dumps({"objects": len(objs), "bytes": sum(o.get("Size", 0) for o in objs)}))
    return 0


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


CONTROL_BROKEN_OPS = ["create_if_absent", "presign"]


def controls_are_valid(controls: dict) -> bool:
    """D8's negative control: the run counts only when both controls read as designed."""
    broken, good = controls.get("broken") or {}, controls.get("conforming") or {}
    failed = sorted(op for op, v in (broken.get("operations") or {}).items() if v != "PASS")
    broken_ok = (
        failed == CONTROL_BROKEN_OPS
        and len(broken.get("operations") or {}) == 7
        and isinstance(broken.get("exit_code"), int)
        and broken["exit_code"] != 0
    )
    good_ops = good.get("operations") or {}
    good_ok = len(good_ops) == 7 and all(v == "PASS" for v in good_ops.values()) and good.get("exit_code") == 0
    return broken_ok and good_ok


def decide(pins: dict, results_dir: Path) -> dict:
    """Apply D8's rule, in order, to every candidate record in ``results_dir``.

    1. All seven operations pass (an exclusion: a candidate failing it cannot be picked).
    2. Foundation-governed over forkable.
    3. Both real clients complete a put/get round trip.
    4. Lower idle RSS.
    5. Fewer processes.
    """
    rows = []
    for entry in pins.get("candidate", []):
        name = entry["name"]
        rec = _read(results_dir / f"{name}.json") or {}
        lic = rec.get("licence") or {}
        conf = rec.get("conformance") or {}
        rss = rec.get("idle") or {}
        clients = rec.get("clients") or {}
        client_verdicts = {c: (clients.get(c) or {}).get("verdict", "UNKNOWN") for c in ("bazel-remote", "sccache")}
        rows.append(
            {
                "name": name,
                "version": entry["version"],
                "licence": lic.get("license_detected", "UNKNOWN"),
                "licence_screen": lic.get("screen", "UNKNOWN"),
                "governance": entry.get("governance"),
                "conformance_verdict": conf.get("verdict", "UNKNOWN"),
                "conformance_passed": conf.get("passed"),
                "conformance_executed": conf.get("executed"),
                "operations": {o["op"]: o["verdict"] for o in conf.get("operations", [])},
                "client_round_trips": client_verdicts,
                "idle_rss_mib": rss.get("idle_rss_mib"),
                "processes": rss.get("processes"),
            }
        )

    eligible = [
        r for r in rows
        if r["licence_screen"] == "PASS" and r["conformance_verdict"] == "PASS" and r["conformance_executed"] == 7
    ]
    notes = []
    foundation = [r for r in eligible if r["governance"] == "foundation"]
    if not foundation:
        notes.append(
            "rule 2 (foundation-governed over forkable) is INERT for this run: with Ceph RGW screened out "
            "by footprint, no eligible candidate is foundation-governed"
        )
    both_clients = [r for r in eligible if all(v == "PASS" for v in r["client_round_trips"].values())]
    if eligible and not both_clients:
        notes.append("rule 3 could not discriminate: no eligible candidate has both client round trips PASS")
    if any("UNKNOWN" in r["client_round_trips"].values() for r in eligible):
        notes.append("at least one client round trip is UNKNOWN; UNKNOWN ranks below PASS and is never read as PASS")

    def key(r: dict) -> tuple:
        clients_pass = sum(1 for v in r["client_round_trips"].values() if v == "PASS")
        rss = r["idle_rss_mib"] if isinstance(r["idle_rss_mib"], (int, float)) else float("inf")
        procs = r["processes"] if isinstance(r["processes"], int) else 1 << 30
        return (0 if r["governance"] == "foundation" else 1, -clients_pass, rss, procs)

    ranked = sorted(eligible, key=key)
    falsified = not ranked
    controls = {}
    for mode in ("broken", "conforming"):
        ctrl = _read(results_dir / f"control-{mode}.conformance.json") or {}
        controls[mode] = {
            "verdict": ctrl.get("verdict", "UNKNOWN"),
            "exit_code": ctrl.get("exit_code"),
            "operations": {o["op"]: o["verdict"] for o in ctrl.get("operations", [])},
        }
    controls_valid = controls_are_valid(controls)
    if not controls_valid:
        # A void run decides nothing: no pick, no runner-up, and not a falsification either.
        ranked, falsified = [], False
        notes.insert(0, "controls are VOID (or missing): the broken stub must fail exactly create_if_absent "
                        "and presign with a non-zero exit, and the conforming stub must pass all seven with exit 0; "
                        "nothing is decided")
    return {
        "schema": "s3-candidates-decision/v1",
        "rule": [
            "1 all seven operations pass (exclusion)",
            "2 foundation-governed over forkable",
            "3 the stack's two real clients (bazel-remote S3, sccache opendal S3) complete a put/get round trip",
            "4 lower idle RSS",
            "5 fewer processes",
        ],
        "controls": controls,
        "screened_out": pins.get("screened_out", []),
        "candidates": rows,
        "eligible_after_rule_1": [r["name"] for r in eligible],
        "ranking": [r["name"] for r in ranked],
        "pick": ranked[0]["name"] if ranked else None,
        "runner_up": ranked[1]["name"] if len(ranked) > 1 else None,
        "notes": notes,
        "controls_valid": controls_valid,
        "void": not controls_valid,
        "premise_falsified": falsified,
        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }


def cmd_decide(a: argparse.Namespace) -> int:
    result = decide(_load(a.pins), Path(a.results_dir))
    Path(a.out).write_text(json.dumps(result, indent=2) + "\n")
    print(f"ranking={result['ranking']} pick={result['pick']} runner_up={result['runner_up']}")
    for note in result["notes"]:
        print(f"note: {note}")
    if result["void"]:
        print("controls VOID: this run decides nothing (D8)", file=sys.stderr)
        return 1
    if result["premise_falsified"]:
        print("NO candidate passes all seven operations: premise FALSIFIED (D8) — halt", file=sys.stderr)
        return 4
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    def s3_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--endpoint", required=True)
        sp.add_argument("--access-key-env", required=True)
        sp.add_argument("--secret-key-env", required=True)
        sp.add_argument("--region", default="us-east-1")
        sp.add_argument("--bucket", required=True)
        sp.add_argument("--prefix", default="")

    sp = sub.add_parser("pin-env"); sp.add_argument("--pins", required=True); sp.add_argument("--section", required=True); sp.add_argument("--name", required=True); sp.set_defaults(fn=cmd_pin_env)
    sp = sub.add_parser("names"); sp.add_argument("--pins", required=True); sp.add_argument("--section", required=True); sp.set_defaults(fn=cmd_names)
    sp = sub.add_parser("cooldown"); sp.add_argument("--pins", required=True); sp.add_argument("--section", required=True); sp.add_argument("--name", required=True); sp.add_argument("--today"); sp.set_defaults(fn=cmd_cooldown)
    sp = sub.add_parser("licence"); sp.add_argument("--pins", required=True); sp.add_argument("--name", required=True); sp.add_argument("--file", required=True); sp.add_argument("--out", required=True); sp.set_defaults(fn=cmd_licence)
    sp = sub.add_parser("ports"); sp.add_argument("count", type=int); sp.set_defaults(fn=cmd_ports)
    sp = sub.add_parser("rss"); sp.add_argument("--pid", type=int, required=True); sp.add_argument("--out", required=True); sp.set_defaults(fn=cmd_rss)
    sp = sub.add_parser("wait-port"); sp.add_argument("host"); sp.add_argument("port", type=int); sp.add_argument("secs", type=float); sp.set_defaults(fn=cmd_wait_port)
    sp = sub.add_parser("http-put"); sp.add_argument("url"); sp.add_argument("file"); sp.set_defaults(fn=cmd_http_put)
    sp = sub.add_parser("http-get"); sp.add_argument("url"); sp.add_argument("out"); sp.set_defaults(fn=cmd_http_get)
    sp = sub.add_parser("s3-wait-key"); s3_args(sp); sp.add_argument("--contains", required=True); sp.add_argument("--secs", type=float, default=30); sp.set_defaults(fn=cmd_s3_wait_key)
    sp = sub.add_parser("s3-count"); s3_args(sp); sp.set_defaults(fn=cmd_s3_count)
    sp = sub.add_parser("decide"); sp.add_argument("--pins", required=True); sp.add_argument("--results-dir", required=True); sp.add_argument("--out", required=True); sp.set_defaults(fn=cmd_decide)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
