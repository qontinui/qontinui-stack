"""Tests for the branch-provenance observation in `hooks/pre-checkout-coord-guard.sh`.

Plan `2026-08-28-shared-checkout-branch-provenance-and-reclaim-signal`, Phase 3.

What is being pinned, and why each case is here rather than "obviously fine":

* The POST fires for a branch CREATE/SWITCH in a primary/shared checkout, and
  exactly once. A second event for one checkout would double-count a
  `(repo, branch)` row that Phase 4 joins to `repo_branches`/`pr_events`.
* It does NOT fire inside a coord-allocated worktree (`agent-worktrees/`).
  Those already report their branch through `POST /agents/allocate` ->
  `coord.agent_worktrees.branch`; emitting here too is a correctness bug (the
  plan's D5), not just noise.
* It does NOT fire for the shim's `tree_disturb` class (`git checkout -- <p>`,
  `git checkout .`, `pull`, `rebase`, `stash pop|apply`) or for
  `git reset --hard`: none of those creates or switches a branch.
* A POST that cannot reach coord still lets the guarded command proceed. The
  observation is telemetry; it has no verdict and no exit code of its own.

`curl` is STUBBED on PATH rather than pointed at a real coord: the guard's
claim GET and its provenance POST both go through it, so the stub is also what
keeps this suite credential-free and offline (the same property the terraform
drift tests in this directory hold).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "hooks" / "pre-checkout-coord-guard.sh"

DEVICE_ID = "11111111-2222-3333-4444-555555555555"
SESSION_ID = "sess-abc-123"

# The stub stands in for curl for BOTH calls the guard makes. A POST is
# recorded (argv + body) and answered 201; anything else is the claim GET, which
# is answered `null` (no live claim) so the guard takes its warn-only arm.
CURL_STUB = r"""#!/usr/bin/env bash
set -u
log="$CURL_STUB_LOG"
out=""
body=""
is_post=0
args=("$@")
i=0
while (( i < ${#args[@]} )); do
  case "${args[$i]}" in
    -o) out="${args[$((i+1))]}"; i=$((i+2)) ;;
    -X) [[ "${args[$((i+1))]}" == "POST" ]] && is_post=1; i=$((i+2)) ;;
    --data-binary) body="${args[$((i+1))]}"; i=$((i+2)) ;;
    *) i=$((i+1)) ;;
  esac
done
url="${args[$(( ${#args[@]} - 1 ))]}"
if (( is_post )); then
  printf '%s\n' "{\"url\":\"$url\",\"body\":$body}" >> "$log"
  if [[ "${CURL_STUB_POST_FAILS:-0}" == "1" ]]; then
    exit 7
  fi
  printf '201'
  exit 0
fi
[[ -n "$out" ]] && printf 'null' > "$out"
printf '200'
exit 0
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main", ".")
    _git(path, "config", "user.email", "t@example.invalid")
    _git(path, "config", "user.name", "t")
    (path / "a").write_text("x\n")
    _git(path, "add", "a")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture()
def env(tmp_path: Path):
    """A hermetic guard invocation: stubbed curl, private HOME, private log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "curl"
    stub.write_text(CURL_STUB)
    stub.chmod(0o755)

    home = tmp_path / "home"
    (home / ".qontinui").mkdir(parents=True)

    state = {
        "curl_log": tmp_path / "curl.jsonl",
        "guard_log": tmp_path / "coord-guard.log",
        "tmp_path": tmp_path,
    }
    state["base_env"] = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "CURL_STUB_LOG": str(state["curl_log"]),
        "COORD_HTTP_URL": "http://127.0.0.1:1/never-dialed",
        "QONTINUI_MACHINE_ID": DEVICE_ID,
        "QONTINUI_AGENT_SESSION_ID": SESSION_ID,
        "QONTINUI_COORD_GUARD_LOG": str(state["guard_log"]),
    }
    return state


def _run(env, cwd: Path, command: str | None, **extra) -> subprocess.CompletedProcess:
    e = dict(env["base_env"])
    e["GIT_GUARD_CWD"] = str(cwd)
    if command is not None:
        e["GIT_GUARD_COMMAND"] = command
    e.update(extra)
    return subprocess.run(
        ["bash", str(GUARD)], env=e, capture_output=True, text=True, timeout=60
    )


def _posts(env, *, expect: int, timeout: float = 10.0) -> list[dict]:
    """Read the recorded POSTs.

    The provenance POST is deliberately BACKGROUNDED (it must cost the session
    no wall-clock time), so poll rather than read once.

    `expect == 0` cannot poll for an arrival, so it settles briefly first —
    otherwise the assertion could pass merely by outrunning the child. The
    settle is short because it is belt-and-braces, not the real proof: on every
    zero-expect path the PARENT writes its skip breadcrumb and exits without
    ever forking a child, so process exit already establishes the absence.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if env["curl_log"].exists():
            lines = [ln for ln in env["curl_log"].read_text().splitlines() if ln.strip()]
            if len(lines) >= expect and expect > 0:
                return [json.loads(ln) for ln in lines]
        if expect == 0 and time.time() > deadline - timeout + 1.0:
            break
        time.sleep(0.05)
    if not env["curl_log"].exists():
        return []
    return [
        json.loads(ln)
        for ln in env["curl_log"].read_text().splitlines()
        if ln.strip()
    ]


def _breadcrumbs(env) -> str:
    p = env["guard_log"]
    return p.read_text() if p.exists() else ""


# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,branch",
    [
        ("git checkout -b feat/provenance", "feat/provenance"),
        ("git switch -c feat/two", "feat/two"),
        ("git checkout -B rel/1 origin/main", "rel/1"),
        ("git checkout main", "main"),
        ("git switch main", "main"),
        ("cd /somewhere && git switch -c feat/three", "feat/three"),
        ("git -C /somewhere checkout -b feat/four", "feat/four"),
        ("git fetch origin && git checkout -b feat/five origin/main", "feat/five"),
    ],
)
def test_branch_create_in_primary_tree_emits_exactly_one_event(env, command, branch):
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    proc = _run(env, repo, command)
    assert proc.returncode == 0, proc.stderr

    posts = _posts(env, expect=1)
    assert len(posts) == 1, f"expected exactly one event, got {posts}"

    assert posts[0]["url"].endswith("/coord/trees/branch-events")
    assert posts[0]["body"] == {
        "device_id": DEVICE_ID,
        "repo": "qontinui-stack",
        "branch": branch,
        "agent_session_id": SESSION_ID,
        "created_via": "checkout_guard_observed",
    }


def test_no_authorization_header_is_sent(env):
    """The route is bearer-less and device-scoped, mirroring /coord/trees/upsert.

    This hook has to work with no runner up, so there is no JWT to mint; a
    header added later would make the write fail closed on exactly the boxes it
    exists to cover.
    """
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    argv_log = env["tmp_path"] / "argv.txt"
    stub = env["tmp_path"] / "bin" / "curl"
    stub.write_text(
        CURL_STUB.replace(
            'url="${args[$(( ${#args[@]} - 1 ))]}"',
            'url="${args[$(( ${#args[@]} - 1 ))]}"\n'
            f'printf "%s\\n" "$*" >> "{argv_log}"',
        )
    )
    stub.chmod(0o755)

    proc = _run(env, repo, "git checkout -b feat/x")
    assert proc.returncode == 0
    _posts(env, expect=1)
    assert "Authorization" not in argv_log.read_text()


def test_allocated_worktree_emits_no_event(env):
    """A coord-allocated worktree already reports via POST /agents/allocate."""
    repo = _make_repo(
        env["tmp_path"] / "agent-worktrees" / "01a05f12-dead" / "qontinui-stack"
    )
    proc = _run(env, repo, "git checkout -b feat/provenance")
    assert proc.returncode == 0, proc.stderr

    assert _posts(env, expect=0) == []
    assert "branch-event-skipped-allocated-worktree" in _breadcrumbs(env)


@pytest.mark.parametrize(
    "command",
    [
        "git checkout -- a",
        "git checkout .",
        "git pull --rebase",
        "git rebase origin/main",
        "git stash pop",
        "git reset --hard origin/main",
        # A verb quoted inside another command is not that command.
        'git commit -m "git checkout -b nope"',
        # A raw object id detaches HEAD; it names no branch.
        "git checkout 9763836e",
        "git switch --detach",
    ],
)
def test_non_branch_creating_ops_emit_no_event(env, command):
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    proc = _run(env, repo, command)
    assert proc.returncode == 0, proc.stderr

    assert _posts(env, expect=0) == [], f"{command!r} should emit no event"
    assert "branch-event-posted" not in _breadcrumbs(env)


def test_no_command_supplied_emits_no_event(env):
    """The guard's legacy inline callers pass no command.

    A missing event then reads as UNKNOWN, never as "nobody checked anything
    out" — the plan's own risk note, and the fleet's `silent-empty-is-unknown`
    discipline.
    """
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    proc = _run(env, repo, None)
    assert proc.returncode == 0, proc.stderr
    assert _posts(env, expect=0) == []


def test_command_can_be_passed_as_an_argument(env):
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    e = dict(env["base_env"])
    e["GIT_GUARD_CWD"] = str(repo)
    proc = subprocess.run(
        ["bash", str(GUARD), "--command", "git checkout -b feat/argform"],
        env=e,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    posts = _posts(env, expect=1)
    assert len(posts) == 1
    assert posts[0]["body"]["branch"] == "feat/argform"


def test_unreachable_coord_still_lets_the_command_proceed(env):
    """A failed observation must never become a verdict."""
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    proc = _run(env, repo, "git checkout -b feat/offline", CURL_STUB_POST_FAILS="1")
    assert proc.returncode == 0, proc.stderr
    assert "branch-event" not in proc.stderr

    deadline = time.time() + 10.0
    while time.time() < deadline and "branch-event-posted" not in _breadcrumbs(env):
        time.sleep(0.05)
    assert "http=unreachable" in _breadcrumbs(env)


def test_session_id_is_omitted_when_unknown(env):
    """`agent_session_id` is self-reported and best-effort; omit, never fake."""
    repo = _make_repo(env["tmp_path"] / "qontinui-stack")
    e = dict(env["base_env"])
    e.pop("QONTINUI_AGENT_SESSION_ID", None)  # and HOME holds no agent_session_id
    e["GIT_GUARD_CWD"] = str(repo)
    e["GIT_GUARD_COMMAND"] = "git checkout -b feat/nosession"
    proc = subprocess.run(
        ["bash", str(GUARD)], env=e, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr

    posts = _posts(env, expect=1)
    assert len(posts) == 1
    assert "agent_session_id" not in posts[0]["body"]
