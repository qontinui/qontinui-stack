#!/usr/bin/env python3
"""Query coord for symbol claims overlapping the current worktree's files.

Phase 4.3 of the ``2026-05-21-coordination-improvements`` plan. Helper for
the ``/symbol-claims-warn`` skill and the ``symbol-conflict-warn`` PreToolUse
hook in ``qontinui-claude-config``.

Phase 4.1 (qontinui-supervisor PR #49) ships a tree-sitter ``symbol_watcher``
daemon that posts ``ClaimKind::Symbol`` claims to ``/claims/acquire`` with
resource_key shape ``<repo>:<rel-file>:<symbol>``. This script queries coord
for claims with prefix ``<repo>:<rel-file>:`` and filters out the ones held
by the current machine — anything left is another machine actively editing a
symbol the operator is about to touch.

Endpoint
--------

``GET /coord/claims/list?kind=symbol&prefix=<repo>:<rel-file>:`` returns

    {
        "holders": [
            {"kind": "symbol", "machine_id": "<uuid>",
             "resource_key": "<repo>:<file>:<symbol>", "ttl_seconds": 119},
            ...
        ],
        "kind": "symbol", "prefix": "...", "truncated": false
    }

Empty list = nobody holds anything overlapping. The endpoint is read-only.

Auth (2026-06-11-claims-read-auth-hardening, Phase 3)
-----------------------------------------------------

Coord's claims read endpoints (``/coord/claims/list`` and
``/coord/claims/by-resource``) historically had no auth gate; enforcement
is coming behind ``COORD_CLAIMS_READ_AUTH_REQUIRED``. This script now
credentials its requests by reading the workspace ``.mcp.json`` (pure
local file read — no network discovery) and branching on the coord-mcp
server entry shape:

* **Proxy shape** (device-provisioned sessions): ``url`` is a loopback
  ``http://127.0.0.1:<port>/coord-mcp`` plus an ``X-Coord-Mcp-Proxy-Key``
  header. The claims request is rewritten onto the runner's nonce-gated
  read passthrough — ``<proxy_base>/claims/list?<query>`` — which injects
  a live device JWT (no token on disk, never stale).
* **Static-bearer shape** (agent-spawn sessions): a real coord ``url``
  plus an ``Authorization: Bearer`` header. The claims URL stays pointed
  direct at coord as before, with the bearer attached. (A narrower agent
  identity must NOT be elevated through the device proxy.)
* **Neither shape / missing / malformed ``.mcp.json``**: anonymous
  direct-coord call (today's behavior, until enforcement arms).

Every credentialed failure (loopback refused, non-200, …) fails open to
the anonymous direct-coord call; if that also fails, the existing
silent-fail (warn + exit 2) behavior applies. No retries beyond that
single fallback; every request keeps ``timeout=2.5``.

Usage
-----

    $ python scripts/symbol-claims-by-machine.py
    $ python scripts/symbol-claims-by-machine.py --file path/to/foo.rs
    $ python scripts/symbol-claims-by-machine.py --repo qontinui-runner \\
          --file src-tauri/src/main.rs
    $ python scripts/symbol-claims-by-machine.py --json

Without ``--file``, the script enumerates ``git diff --name-only HEAD``
(plus untracked files via ``git ls-files --others --exclude-standard``) in
the current worktree.

Exit codes:
    0   No conflicting claims (no claims at all, or all claims belong to
        the local machine).
    1   At least one conflicting claim found. Stdout has the table (or JSON
        with ``--json``).
    2   Input/setup error (machine.json missing, coord unreachable, etc.).
        These are also reported on stderr with a single-line explanation.

Environment overrides:
    COORD_HTTP_URL                  (default ``http://localhost:9870``)
    QONTINUI_MACHINE_JSON_PATH      (default ``~/.qontinui/machine.json``)
    QONTINUI_MCP_JSON               (default: walk up from cwd for ``.mcp.json``)

Design choices
--------------

* No tree-sitter dependency. We use prefix-list (Phase 5 admin endpoint
  ``/coord/claims/list``) so this script is pure-stdlib + ``urllib``.
* Best-effort: a non-2xx response or a network failure prints a one-line
  warning to stderr and exits 2 (no false-positives). The PreToolUse hook
  uses ``|| true`` so this never blocks an Edit.
* No-self-only suppression: if every claim is held by the local machine,
  the script reports "no conflicting symbol claims" — re-acquiring your
  own claim is the watcher's normal idle behavior, not a conflict.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------------


def _coord_url() -> str:
    return os.environ.get("COORD_HTTP_URL", "http://localhost:9870").rstrip("/")


def _machine_json_path() -> Path:
    override = os.environ.get("QONTINUI_MACHINE_JSON_PATH")
    if override:
        return Path(override)
    return Path.home() / ".qontinui" / "machine.json"


def _read_local_machine_id() -> str | None:
    """Return the local machine UUID, or ``None`` if not configured.

    Phase 1.4's finding: the canonical field is ``device_id`` on newer
    installs, with ``machine_id`` as a legacy fallback. Either is fine for
    our self-filter step; we just need to know "is this us?" The watcher
    daemon (PR #49) writes the same field, so the values match.
    """
    path = _machine_json_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("device_id") or data.get("machine_id")


# ---------------------------------------------------------------------------
# Claims read auth (.mcp.json dual-shape; see module docstring)
# ---------------------------------------------------------------------------


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclasses.dataclass
class ClaimsAuth:
    """How to credential claims read requests.

    ``mode`` is one of:

    * ``"proxy"``     — runner loopback passthrough; ``proxy_base`` is the
      ``http://127.0.0.1:<port>/coord-mcp`` base and ``headers`` carries
      the ``X-Coord-Mcp-Proxy-Key`` nonce.
    * ``"bearer"``    — direct coord with ``headers`` carrying
      ``Authorization: Bearer <jwt>``.
    * ``"anonymous"`` — today's uncredentialed direct-coord call.
    """

    mode: str = "anonymous"
    proxy_base: str | None = None
    headers: dict[str, str] = dataclasses.field(default_factory=dict)


_ANONYMOUS = ClaimsAuth()


def _find_mcp_json(start: Path) -> Path | None:
    """Walk from ``start`` upward looking for a ``.mcp.json`` file."""
    try:
        start = start.resolve()
    except OSError:
        return None
    for d in (start, *start.parents):
        candidate = d / ".mcp.json"
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def resolve_claims_auth(cwd: Path, mcp_json_override: str | None = None) -> ClaimsAuth:
    """Classify the workspace ``.mcp.json`` coord-mcp entry (file read only).

    Resolution order for the file: explicit override (``--mcp-json``) →
    ``$QONTINUI_MCP_JSON`` → walk up from ``cwd``. Anything missing,
    malformed, or shaped unexpectedly degrades to anonymous (fail open).
    """
    override = mcp_json_override or os.environ.get("QONTINUI_MCP_JSON")
    path = Path(override) if override else _find_mcp_json(cwd)
    if path is None:
        return _ANONYMOUS
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _ANONYMOUS
    if not isinstance(data, dict):
        return _ANONYMOUS
    servers = data.get("mcpServers")
    server = servers.get("coord-mcp") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        return _ANONYMOUS
    url = server.get("url")
    headers = server.get("headers")
    if not isinstance(url, str) or not isinstance(headers, dict):
        return _ANONYMOUS
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return _ANONYMOUS

    nonce = headers.get("X-Coord-Mcp-Proxy-Key")
    bearer = headers.get("Authorization")

    if (
        isinstance(nonce, str)
        and nonce
        and host in _LOOPBACK_HOSTS
        and parsed.path.rstrip("/").endswith("/coord-mcp")
    ):
        # Device-provisioned session: runner loopback proxy + nonce.
        return ClaimsAuth(
            mode="proxy",
            proxy_base=url.rstrip("/"),
            headers={"X-Coord-Mcp-Proxy-Key": nonce},
        )

    if isinstance(bearer, str) and bearer and host not in _LOOPBACK_HOSTS:
        # Agent-spawn session: static bearer, direct coord. Never route a
        # narrower agent identity through the device proxy.
        return ClaimsAuth(mode="bearer", headers={"Authorization": bearer})

    return _ANONYMOUS


# ---------------------------------------------------------------------------
# Git scope helpers
# ---------------------------------------------------------------------------


def _git_output(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return result.stdout.strip()


def _repo_root(cwd: Path) -> Path | None:
    out = _git_output(["rev-parse", "--show-toplevel"], cwd)
    if not out:
        return None
    return Path(out)


def _repo_name(root: Path) -> str:
    """Match ``qontinui-supervisor::symbol_watcher::mod::find_repo_root``."""
    return root.name


def _scope_files(cwd: Path) -> tuple[Path | None, list[str]]:
    """Return ``(repo_root, [rel_file, ...])`` for the current worktree.

    Files are collected from:

    - ``git diff --name-only HEAD`` (modified, tracked)
    - ``git ls-files --others --exclude-standard`` (new, not yet added)

    Both lists give POSIX-separator paths relative to repo root, which
    matches the watcher's ``relative_repo_path`` (forward-slashes for
    cross-OS resource_key stability).
    """
    root = _repo_root(cwd)
    if root is None:
        return None, []

    changed = _git_output(["diff", "--name-only", "HEAD"], cwd) or ""
    untracked = _git_output(
        ["ls-files", "--others", "--exclude-standard"], cwd
    ) or ""

    seen: set[str] = set()
    out: list[str] = []
    for line in (changed + "\n" + untracked).splitlines():
        rel = line.strip()
        if not rel:
            continue
        if rel in seen:
            continue
        seen.add(rel)
        out.append(rel)
    return root, out


# ---------------------------------------------------------------------------
# Coord HTTP query
# ---------------------------------------------------------------------------


def _request_holders(url: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Single GET → parsed ``holders`` list. RuntimeError on any failure."""
    req = urllib.request.Request(url, method="GET")
    for key, value in headers.items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"claims endpoint {url.split('?')[0]} unreachable: {e}") from e

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"coord returned non-JSON: {e}") from e

    holders = parsed.get("holders")
    if not isinstance(holders, list):
        raise RuntimeError("coord response missing 'holders' list")
    return holders


def fetch_holders(
    repo: str,
    rel_file: str,
    *,
    coord_url: str | None = None,
    auth: ClaimsAuth | None = None,
) -> list[dict[str, Any]]:
    """GET ``claims/list?kind=symbol&prefix=<repo>:<rel-file>:``.

    ``auth`` (from :func:`resolve_claims_auth`) picks the credentialed
    route: proxy mode rewrites onto the runner loopback passthrough with
    the nonce header; bearer mode goes direct to coord with the
    Authorization header; anonymous (or ``None``) is today's plain call.
    A credentialed failure falls open ONCE to the anonymous direct call
    (no further retries); if that fails too the last error is raised as
    ``RuntimeError`` with a single-sentence message suitable for stderr.
    Returns the raw ``holders`` list on success (may be empty).
    """
    base = (coord_url or _coord_url()).rstrip("/")
    prefix = f"{repo}:{rel_file}:"
    qs = urllib.parse.urlencode({"kind": "symbol", "prefix": prefix})
    direct_url = f"{base}/coord/claims/list?{qs}"

    attempts: list[tuple[str, dict[str, str]]] = []
    if auth is not None and auth.mode == "proxy" and auth.proxy_base:
        attempts.append((f"{auth.proxy_base}/claims/list?{qs}", auth.headers))
    elif auth is not None and auth.mode == "bearer":
        attempts.append((direct_url, auth.headers))
    attempts.append((direct_url, {}))

    last_err: RuntimeError | None = None
    for url, headers in attempts:
        try:
            return _request_holders(url, headers)
        except RuntimeError as e:
            last_err = e
    assert last_err is not None  # attempts is never empty
    raise last_err


# ---------------------------------------------------------------------------
# Collection + filtering
# ---------------------------------------------------------------------------


def _parse_symbol(resource_key: str) -> str:
    """Extract the symbol-name tail from a ``<repo>:<file>:<symbol>`` key.

    The symbol portion can itself contain ``:`` (e.g. ``Type::method`` →
    Rust qualifier), so we strip the leading ``<repo>:<file>:`` prefix
    rather than splitting on every colon.
    """
    # The caller knows repo + rel_file; the resource_key is the prefix +
    # symbol. We're called with the holder dict whose resource_key
    # matches the prefix; strip the prefix's separator count instead of
    # re-parsing.
    return resource_key.rsplit(":", 1)[-1] if ":" in resource_key else resource_key


def collect_conflicts(
    repo: str,
    rel_files: list[str],
    local_machine_id: str | None,
    *,
    coord_url: str | None = None,
    auth: ClaimsAuth | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """For each rel_file, fetch claims and filter out the local machine's.

    Returns ``(conflicts, errors)`` where ``conflicts`` is a list of
    holder dicts annotated with a ``symbol`` field, and ``errors`` is a
    list of per-file error strings (best-effort: a single failing file
    doesn't suppress the rest).
    """
    conflicts: list[dict[str, Any]] = []
    errors: list[str] = []
    for rel_file in rel_files:
        try:
            holders = fetch_holders(repo, rel_file, coord_url=coord_url, auth=auth)
        except RuntimeError as e:
            errors.append(f"{rel_file}: {e}")
            continue
        for h in holders:
            mid = h.get("machine_id")
            if local_machine_id and mid == local_machine_id:
                continue
            conflicts.append(
                {
                    "symbol": _parse_symbol(str(h.get("resource_key", ""))),
                    "file": rel_file,
                    "machine_id": mid,
                    "ttl_seconds": h.get("ttl_seconds"),
                    "resource_key": h.get("resource_key"),
                }
            )
    return conflicts, errors


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_text(conflicts: list[dict[str, Any]]) -> str:
    if not conflicts:
        return "No conflicting symbol claims."
    # Compute widths.
    headers = ("Symbol", "File", "Held by", "TTL")
    rows: list[tuple[str, str, str, str]] = []
    for c in conflicts:
        rows.append(
            (
                c.get("symbol") or "?",
                c.get("file") or "?",
                str(c.get("machine_id") or "?"),
                f"{c.get('ttl_seconds')}s" if c.get("ttl_seconds") is not None else "?",
            )
        )
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in rows))
        for i in range(4)
    ]
    sep = " | "
    out_lines = [
        sep.join(headers[i].ljust(widths[i]) for i in range(4)),
        sep.join("-" * widths[i] for i in range(4)),
    ]
    for row in rows:
        out_lines.append(sep.join(row[i].ljust(widths[i]) for i in range(4)))
    return "\n".join(out_lines)


def render_json(conflicts: list[dict[str, Any]], errors: list[str]) -> str:
    return json.dumps(
        {"conflicts": conflicts, "errors": errors},
        indent=2,
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="symbol-claims-by-machine",
        description=(
            "Query coord for ClaimKind::Symbol claims overlapping the "
            "current worktree's files; report any held by other machines."
        ),
    )
    p.add_argument(
        "--repo",
        help=(
            "Override repo name (basename of git toplevel). "
            "Default: inferred from cwd."
        ),
    )
    p.add_argument(
        "--file",
        action="append",
        dest="files",
        help=(
            "Restrict scope to this file (relative to repo root, "
            "POSIX separators). Repeatable. Default: changed + untracked "
            "files from git."
        ),
    )
    p.add_argument(
        "--coord-url",
        help=(
            "Override $COORD_HTTP_URL. "
            "Default: $COORD_HTTP_URL or http://localhost:9870."
        ),
    )
    p.add_argument(
        "--mcp-json",
        dest="mcp_json",
        help=(
            "Path to the workspace .mcp.json used to credential claims "
            "reads (dual-shape: runner loopback proxy or static bearer). "
            "Default: $QONTINUI_MCP_JSON, else walk up from cwd. "
            "Missing/malformed = anonymous (fail open)."
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text table.",
    )
    p.add_argument(
        "--cwd",
        help="Override the worktree to inspect. Default: process cwd.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cwd = Path(args.cwd) if args.cwd else Path.cwd()

    # Determine repo + files.
    if args.repo and args.files:
        repo = args.repo
        rel_files = list(args.files)
    else:
        repo_root, scoped = _scope_files(cwd)
        if repo_root is None:
            print(
                f"symbol-claims: not inside a git worktree: {cwd}",
                file=sys.stderr,
            )
            return 2
        repo = args.repo or _repo_name(repo_root)
        rel_files = list(args.files) if args.files else scoped

    if not rel_files:
        # Nothing to query — render-empty + exit 0 (no conflicts).
        if args.json:
            print(render_json([], []))
        else:
            print("No conflicting symbol claims.")
        return 0

    local_id = _read_local_machine_id()
    if local_id is None:
        # Be defensive but functional: we can still show OTHER machines'
        # claims, we just can't filter ours out. Warn once.
        print(
            "symbol-claims: WARN: ~/.qontinui/machine.json missing or "
            "unreadable — listing ALL holders (cannot filter self).",
            file=sys.stderr,
        )

    conflicts, errors = collect_conflicts(
        repo,
        rel_files,
        local_id,
        coord_url=args.coord_url,
        auth=resolve_claims_auth(cwd, args.mcp_json),
    )

    if args.json:
        print(render_json(conflicts, errors))
    else:
        for err in errors:
            print(f"symbol-claims: WARN: {err}", file=sys.stderr)
        print(render_text(conflicts))

    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())
