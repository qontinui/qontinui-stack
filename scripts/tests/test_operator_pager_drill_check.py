"""Tests for scripts/operator_pager_drill_check.py (operator pager drill verdict, plan Phase 1)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import operator_pager_drill_check as check  # noqa: E402

FIVE = ["train_stalled", "leader_crash_loop", "deploy_failed", "storage_or_iops", "github_budget_exhausted"]


def events(*records, extra_lines=()):
    msgs = [json.dumps(r) for r in records] + list(extra_lines)
    return {"events": [{"message": m} for m in msgs]}


def rec(condition, outcome="accepted", drill_id="d-1"):
    return {"record": "operator_pager_relay", "condition": condition, "outcome": outcome, "drill_id": drill_id}


def test_all_five_accepted_passes():
    doc = events(*(rec(c) for c in FIVE))
    ok, best = check.verdict(check.relay_records(doc), "d-1", FIVE)
    assert ok is True
    assert set(best.values()) == {"accepted"}


def test_a_missing_condition_fails_as_missing():
    doc = events(*(rec(c) for c in FIVE[:4]))
    ok, best = check.verdict(check.relay_records(doc), "d-1", FIVE)
    assert ok is False
    assert best["github_budget_exhausted"] == "missing"


def test_fallback_fails_unless_allowed():
    doc = events(*(rec(c, "fallback") for c in FIVE))
    assert check.verdict(check.relay_records(doc), "d-1", FIVE)[0] is False
    assert check.verdict(check.relay_records(doc), "d-1", FIVE, allow_fallback=True)[0] is True


def test_records_of_another_drill_do_not_count():
    doc = events(*(rec(c, drill_id="other") for c in FIVE))
    ok, best = check.verdict(check.relay_records(doc), "d-1", FIVE)
    assert ok is False and set(best.values()) == {"missing"}


def test_a_later_accepted_record_beats_an_earlier_rejection():
    doc = events(rec("deploy_failed", "rejected"), rec("deploy_failed", "accepted"))
    ok, best = check.verdict(check.relay_records(doc), "d-1", ["deploy_failed"])
    assert ok is True and best["deploy_failed"] == "accepted"


def test_non_json_lines_and_emf_lines_are_ignored():
    emf = {"_aws": {"CloudWatchMetrics": []}, "VendorAccepted": 1}
    doc = events(rec("deploy_failed"), extra_lines=["START RequestId: x", json.dumps(emf)])
    assert check.verdict(check.relay_records(doc), "d-1", ["deploy_failed"])[0] is True


def test_summary_record_has_no_condition():
    doc = events({"record": "operator_pager_relay", "condition": None, "outcome": "accepted", "drill_id": "d-1"})
    assert check.verdict(check.relay_records(doc), "d-1", ["summary"])[0] is True


def test_main_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(events(*(rec(c) for c in FIVE)))))
    assert check.main(["--drill-id", "d-1", "--conditions", ",".join(FIVE)]) == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(events(rec("deploy_failed", "dropped")))))
    assert check.main(["--drill-id", "d-1", "--conditions", "deploy_failed"]) == 1
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    assert check.main(["--drill-id", "d-1", "--conditions", "deploy_failed"]) == 2
