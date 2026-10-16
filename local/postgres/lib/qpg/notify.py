"""Operator notices: always logged, posted to coord only when configured.

The default is log-only (``[notify] enabled = false``). When enabled, a notice
is POSTed to coord's agent-findings door (``<coord_url>/coord/agent-findings``)
with the bearer read from ``header_file`` — a file holding one
``Authorization: Bearer …`` line, passed to curl as ``-H @file`` so the token
never appears on a command line. The pager plan is paused, so these findings are
pull-based (plan Risks).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(kind: str, message: str, **fields) -> None:
    record = {"at": _now(), "kind": kind, "message": message}
    record.update(fields)
    print(json.dumps(record, default=str), file=sys.stderr, flush=True)


class Notifier:
    def __init__(self, config=None, force_dry_run: bool = False):
        self.enabled = False
        self.coord_url = None
        self.header_file = None
        self.topic = "operator-notify"
        self.kind = "status"
        self.resource_keys: list[str] = []
        if config is not None:
            self.enabled = config.getbool("notify", "enabled", False) and not force_dry_run
            self.coord_url = (config.get("notify", "coord_url") or "").rstrip("/") or None
            self.header_file = config.get("notify", "header_file") or None
            self.topic = config.get("notify", "topic", fallback=self.topic) or self.topic
            self.kind = config.get("notify", "kind", fallback=self.kind) or self.kind
            keys = config.get("notify", "resource_keys", fallback="") or ""
            self.resource_keys = [k.strip() for k in keys.split(",") if k.strip()]
        self.sent: list[dict] = []

    def notice(self, title: str, body: str, artifact: dict | None = None) -> bool:
        """Log the notice; post it when enabled. Returns True when posted."""
        payload = {
            "title": title,
            "topic": self.topic,
            "kind": self.kind,
            "body": body,
            "resource_keys": self.resource_keys,
            "artifact_refs": artifact or {},
        }
        self.sent.append(payload)
        log("notice", title, body=body, posted=self.enabled)
        if not self.enabled:
            return False
        if not self.coord_url or not self.header_file or not os.path.isfile(self.header_file):
            log("notice-not-posted", "notify enabled but coord_url/header_file missing", title=title)
            return False
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json") as fh:
            json.dump(payload, fh)
            body_path = fh.name
        try:
            proc = subprocess.run(
                ["curl", "-sS", "-m", "30", "-o", "/dev/null", "-w", "%{http_code}",
                 "-H", f"@{self.header_file}", "-H", "Content-Type: application/json",
                 "--data-binary", f"@{body_path}", f"{self.coord_url}/coord/agent-findings"],
                capture_output=True, text=True, timeout=60,
            )
        finally:
            os.unlink(body_path)
        code = proc.stdout.strip()
        ok = code.startswith("2")
        log("notice-posted" if ok else "notice-post-failed", title, http=code, stderr=proc.stderr.strip())
        return ok
