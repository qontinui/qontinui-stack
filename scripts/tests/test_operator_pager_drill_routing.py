"""Tests for `scripts/operator-pager-drill.sh --target routing`.

Plan `2026-10-14-coord-pages-the-operator-for-five-conditions-and-names-the-
agent-fixing-each`, Phase 2 verify step: "`describe-alarms` confirms each pager
alarm's `AlarmActions` contains the pager topic: that is the claim's routing
check". The check is read-only, so it is the one target that may run without
announcing a drill.

What is pinned:

* A correctly routed stack passes: the three composites name the pager topic in
  BOTH AlarmActions and OKActions, no child alarm carries any action (one
  episode, one page), the deploy-failed and RDS-storage rules target the topic,
  and the task-exit rule targets its log group.
* Each of those properties, broken alone, fails the check (exit 1).
* The target writes nothing: no `sns publish`, `set-alarm-state` or
  `put-events` reaches `aws`.

`aws` is STUBBED on PATH, answering from fixture files, so the suite is
credential-free and offline like its siblings in this directory.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DRILL = REPO_ROOT / "scripts" / "operator-pager-drill.sh"

# Resolve the interpreter rather than spawning bare `"bash"`: on Windows a bare
# `"bash"` resolves to the WSL launcher in System32 before $PATH is searched.
_BASH = shutil.which("bash") or "bash"

ACCOUNT = "047719635665"
REGION = "us-east-1"
PREFIX = "qontinui-staging"
PAGER = f"{PREFIX}-pager-"
TOPIC = f"arn:aws:sns:{REGION}:{ACCOUNT}:{PREFIX}-operator-pager"
EXITS_LOG_ARN = f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/events/{PREFIX}-coord-task-exits"

COMPOSITES = ["coord-crash-loop", "rds-free-storage", "rds-saturation"]
CHILDREN = [
    "coord-abnormal-exits", "coord-no-healthy-hosts", "coord-crash-loop-drill",
    "rds-free-storage-low", "rds-free-storage-drill",
    "rds-read-latency-acute", "rds-write-latency-acute", "rds-queue-depth-acute",
    "rds-iops-saturated", "coord-latency-sustained", "rds-saturation-drill",
]

AWS_STUB = r"""#!/usr/bin/env bash
set -u
d="$AWS_STUB_DIR"
printf '%s\n' "$*" >> "$d/calls.log"
args=("$@")
rule=""
for ((i = 0; i < ${#args[@]}; i++)); do
  [ "${args[$i]}" = "--rule" ] && rule="${args[$((i+1))]}"
done
case "$*" in
  *"sts get-caller-identity"*) echo "047719635665" ;;
  *"ecs describe-clusters"*) echo "arn:aws:ecs:us-east-1:047719635665:cluster/qontinui-staging" ;;
  *"ecs describe-services"*) echo "arn:aws:ecs:us-east-1:047719635665:service/qontinui-staging/coord" ;;
  *"cloudwatch describe-alarms"*) cat "$d/alarms.json" ;;
  *"events list-targets-by-rule"*) cat "$d/rule-$rule.json" ;;
  *) echo "unexpected aws call: $*" >&2; exit 99 ;;
esac
"""


def _alarm(name: str, actions: list[str]) -> dict:
    return {"AlarmName": PAGER + name, "AlarmActions": list(actions),
            "OKActions": list(actions), "InsufficientDataActions": []}


def _good_fixture() -> dict:
    return {
        "alarms": {
            "CompositeAlarms": [_alarm(c, [TOPIC]) for c in COMPOSITES],
            "MetricAlarms": [_alarm(c, []) for c in CHILDREN],
        },
        "rules": {
            "deploy-failed": {"Targets": [{"Id": "pager", "Arn": TOPIC}]},
            "rds-storage-events": {"Targets": [{"Id": "pager", "Arn": TOPIC}]},
            "coord-task-exits": {"Targets": [{"Id": "log", "Arn": EXITS_LOG_ARN}]},
        },
    }


def _run(tmp_path: Path, fixture: dict) -> tuple[subprocess.CompletedProcess, str]:
    stub_dir = tmp_path / "stub"
    bin_dir = tmp_path / "bin"
    stub_dir.mkdir()
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(AWS_STUB, newline="\n")
    stub.chmod(0o755)
    (stub_dir / "alarms.json").write_text(json.dumps(fixture["alarms"]))
    for rule, doc in fixture["rules"].items():
        (stub_dir / f"rule-{PAGER}{rule}.json").write_text(json.dumps(doc))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "AWS_STUB_DIR": str(stub_dir),
    }
    # Generous: one run is ~10 process spawns, which take seconds each on a
    # loaded Windows box (Git Bash), and well under a second on CI's Linux.
    proc = subprocess.run(
        [_BASH, str(DRILL), "--target", "routing"],
        env=env, capture_output=True, text=True, timeout=600,
    )
    calls_log = stub_dir / "calls.log"
    calls = calls_log.read_text() if calls_log.exists() else ""
    return proc, calls


def test_correct_routing_passes_and_writes_nothing(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, _good_fixture())
    assert proc.returncode == 0, proc.stderr
    assert "OK: composites route ALARM and OK to the pager topic" in proc.stdout
    assert "checked 3 composites, 11 other pager alarms, 3 rules" in proc.stderr
    assert "describe-alarms" in calls and "list-targets-by-rule" in calls
    for write in ("sns publish", "set-alarm-state", "put-events"):
        assert write not in calls, f"routing must be read-only, saw {write!r}"


def test_log_group_target_with_wildcard_suffix_passes(tmp_path: Path) -> None:
    fixture = _good_fixture()
    fixture["rules"]["coord-task-exits"]["Targets"][0]["Arn"] = EXITS_LOG_ARN + ":*"
    proc, _ = _run(tmp_path, fixture)
    assert proc.returncode == 0, proc.stderr


def test_unreadable_rule_is_a_routing_fail_not_an_abort(tmp_path: Path) -> None:
    fixture = _good_fixture()
    del fixture["rules"]["rds-storage-events"]  # the stub's `cat` then fails
    proc, _ = _run(tmp_path, fixture)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL: pager routing (could not read" in proc.stderr


def _composite(f: dict, name: str) -> dict:
    return next(a for a in f["alarms"]["CompositeAlarms"] if a["AlarmName"] == PAGER + name)


def _drop_ok_action(f: dict) -> str:
    _composite(f, "rds-free-storage")["OKActions"] = []
    return "rds-free-storage: OKActions"


def _drop_alarm_action(f: dict) -> str:
    _composite(f, "coord-crash-loop")["AlarmActions"] = ["arn:aws:sns:us-east-1:1:other"]
    return "coord-crash-loop: AlarmActions"


def _missing_composite(f: dict) -> str:
    f["alarms"]["CompositeAlarms"] = [
        a for a in f["alarms"]["CompositeAlarms"] if a["AlarmName"] != PAGER + "rds-saturation"
    ]
    return "rds-saturation: missing"


def _child_pages(f: dict) -> str:
    f["alarms"]["MetricAlarms"][0]["AlarmActions"] = [TOPIC]
    return "a child alarm carries actions"


def _child_insufficient_data_action(f: dict) -> str:
    f["alarms"]["MetricAlarms"][-1]["InsufficientDataActions"] = [TOPIC]
    return "a child alarm carries actions"


def _deploy_rule_mistargeted(f: dict) -> str:
    f["rules"]["deploy-failed"]["Targets"] = [{"Id": "x", "Arn": "arn:aws:sns:us-east-1:1:other"}]
    return "rule deploy-failed"


def _rds_rule_untargeted(f: dict) -> str:
    f["rules"]["rds-storage-events"]["Targets"] = []
    return "rule rds-storage-events"


def _exits_rule_wrong_log_group(f: dict) -> str:
    f["rules"]["coord-task-exits"]["Targets"][0]["Arn"] = EXITS_LOG_ARN + "-old"
    return "rule coord-task-exits"


@pytest.mark.parametrize("breakage", [
    _drop_ok_action, _drop_alarm_action, _missing_composite, _child_pages,
    _child_insufficient_data_action, _deploy_rule_mistargeted,
    _rds_rule_untargeted, _exits_rule_wrong_log_group,
], ids=lambda b: b.__name__.lstrip("_"))
def test_each_broken_route_fails(tmp_path: Path, breakage) -> None:
    fixture = copy.deepcopy(_good_fixture())
    expected = breakage(fixture)
    proc, _ = _run(tmp_path, fixture)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FAIL: pager routing" in proc.stderr
    assert expected in proc.stderr
