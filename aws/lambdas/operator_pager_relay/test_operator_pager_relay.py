"""Tests for the operator pager relay (stdlib + pytest only; no AWS calls).

Run with the repo's CI command:

    python -m pytest scripts/tests/ aws/lambdas/ -q

Most tests enter through ``handler`` with fake adapters, so they exercise the
same path a real SNS invocation takes. Each test names the behaviour it pins;
flipping that behaviour in ``handler.py`` turns it red (testing
``a-test-must-be-able-to-fail``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("operator_pager_relay_handler", _HERE / "handler.py")
relay = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
# dataclasses resolve annotations through sys.modules, so register first.
sys.modules[_spec.name] = relay
_spec.loader.exec_module(relay)

ENV = "staging"
CRASH = f"qontinui-{ENV}-pager-coord-crash-loop"
CRASH_DRILL = f"{CRASH}-drill"
FREE = f"qontinui-{ENV}-pager-rds-free-storage"
SAT = f"qontinui-{ENV}-pager-rds-saturation"

ALARM_MAP = {
    CRASH: {
        "condition": "leader_crash_loop",
        "remediation": "ECS replaces stopped coord tasks and the leader lease fails over",
        "drill_child": CRASH_DRILL,
    },
    FREE: {
        "condition": "storage_or_iops",
        "remediation": "RDS storage autoscaling up to the maximum",
        "drill_child": f"{FREE}-drill",
    },
    SAT: {
        "condition": "storage_or_iops",
        "remediation": "none automatic; chronic saturation is agent work",
        "drill_child": f"{SAT}-drill",
    },
}

SECRET = "tok_SUPERSECRET_9f8e7d6c5b4a"


# ─── Fakes ───────────────────────────────────────────────────────────────


class FakeAdapter:
    def __init__(self, name, statuses, ref="vendor-ref-1"):
        self.name = name
        self.statuses = list(statuses)
        self.calls = []
        self.ref = ref
        self.token = SECRET  # a vendor adapter holds its credential
        self.statuses_last = self.statuses[-1] if self.statuses else None

    def send(self, rendered, page):
        self.calls.append((rendered, page))
        status = self.statuses.pop(0) if self.statuses else self.statuses_last
        self.statuses_last = status
        return relay.AdapterResponse(status=status, ref=self.ref if status and 200 <= status < 300 else None)


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, secs):
        self.calls.append(secs)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", ENV)
    monkeypatch.setenv("EMAIL_TOPIC_ARN", "arn:aws:sns:us-east-1:047719635665:qontinui-staging-budget-alerts")
    monkeypatch.setenv("PAGER_ALARM_MAP", json.dumps(ALARM_MAP))
    monkeypatch.setenv("PAGER_ADAPTER", "sns_forward")


def sns_event(message):
    if not isinstance(message, str):
        message = json.dumps(message)
    return {"Records": [{"Sns": {"MessageId": "mid-1", "Message": message, "TopicArn": "arn:x"}}]}


def no_history(_name, _when):
    """The history read failed (or there is none): UNKNOWN."""
    return None


def history_with(*reasons, state="ALARM"):
    """A DescribeAlarmHistory fake returning one StateUpdate per reason."""
    calls = []

    def read(name, when):
        calls.append((name, when))
        return [
            {
                "AlarmName": name,
                "Timestamp": f"2026-10-19T09:5{i}:00Z",
                "HistoryItemType": "StateUpdate",
                "HistoryData": json.dumps({"newState": {"stateValue": state, "stateReason": r}}),
            }
            for i, r in enumerate(reasons)
        ]

    read.calls = calls
    return read


def run(message, *, adapter=None, fallback=None, sleep=None, drill_history=no_history):
    lines = []
    adapter = adapter or FakeAdapter("sns_forward", [200])
    fallback = fallback or FakeAdapter("sns_forward", [200])
    result = relay.handler(
        sns_event(message),
        None,
        adapter=adapter,
        fallback=fallback,
        sleep=sleep or Sleeps(),
        emit=lines.append,
        now_ms=lambda: 1_700_000_000_000,
        drill_history=drill_history,
    )
    assert len(lines) == 1, "exactly one record per message"
    return result, json.loads(lines[0]), adapter, fallback


def alarm(name, state="ALARM", reason="Threshold Crossed: 3 datapoints were less than the threshold (5.0E9).", **extra):
    doc = {
        "AlarmName": name,
        "NewStateValue": state,
        "OldStateValue": "OK" if state == "ALARM" else "ALARM",
        "NewStateReason": reason,
        "StateChangeTime": "2026-10-19T10:00:00.000+0000",
    }
    doc.update(extra)
    return doc


def v1(**fields):
    doc = {"page_version": 1, "kind": "page", "origin": "coord", "source": "coord", "signal": "coord_leader_election_flap"}
    doc.update(fields)
    return doc


# ─── The cap ─────────────────────────────────────────────────────────────


def test_exactly_five_conditions():
    # Claim r05-operator-pager-delivers: the pager pages for at most five
    # conditions. A sixth needs a deliberate edit of this pin AND the claim.
    assert relay.CONDITIONS == (
        "train_stalled",
        "leader_crash_loop",
        "deploy_failed",
        "storage_or_iops",
        "github_budget_exhausted",
    ), "r05-operator-pager-delivers caps the pager at exactly five conditions"
    assert set(relay.CONDITION_LABELS) == set(relay.CONDITIONS)


def test_unmapped_alarm_is_dropped():
    _, rec, adapter, fallback = run(alarm("qontinui-staging-coord-cpu"))
    assert rec["outcome"] == "dropped"
    assert rec["drop_reason"] == "unmapped_alarm"
    assert rec["Dropped"] == 1 and rec["VendorAccepted"] == 0
    assert adapter.calls == [] and fallback.calls == []


def test_sixth_condition_is_rejected():
    _, rec, adapter, _ = run(v1(condition="backup_staleness"))
    assert rec["outcome"] == "dropped"
    assert rec["drop_reason"] == "condition_not_in_the_five"
    assert rec["condition"] == "backup_staleness"
    assert adapter.calls == []


def test_alarm_mapped_to_a_sixth_condition_is_rejected():
    bad_map = dict(ALARM_MAP)
    bad_map["qontinui-staging-pager-backups"] = {"condition": "backup_staleness", "remediation": "x"}
    parsed = relay.parse_message(json.dumps(alarm("qontinui-staging-pager-backups")), bad_map)
    assert isinstance(parsed, relay.Drop) and parsed.reason == "condition_not_in_the_five"


def test_drill_summary_with_a_sixth_condition_is_rejected():
    _, rec, adapter, _ = run(
        v1(kind="drill_summary", source="qontinui.drill", drill_id="d1", conditions=["deploy_failed", "backups"])
    )
    assert rec["outcome"] == "dropped" and adapter.calls == []


def test_malformed_alarm_map_drops_everything():
    assert relay.load_alarm_map("{not json") == {}
    parsed = relay.parse_message(json.dumps(alarm(CRASH)), relay.load_alarm_map("{not json"))
    assert isinstance(parsed, relay.Drop) and parsed.reason == "unmapped_alarm"


def test_unparseable_message_is_dropped():
    _, rec, adapter, _ = run("this is not json")
    assert rec["outcome"] == "dropped" and rec["drop_reason"] == "unparseable"
    assert adapter.calls == []


def test_insufficient_data_is_not_paged():
    _, rec, adapter, _ = run(alarm(FREE, state="INSUFFICIENT_DATA"))
    assert rec["outcome"] == "dropped" and rec["drop_reason"] == "state_not_paged"
    assert adapter.calls == []


# ─── Wording ─────────────────────────────────────────────────────────────


def _every_page():
    for cond in relay.CONDITIONS:
        for kind in ("page", "update", "resolved", "drill"):
            yield relay.Page(
                condition=cond,
                kind=kind,
                origin="coord",
                source="coord",
                signal="sig",
                what="Something happened at 10:00.",
                remediation="automatic remediation text",
                remediation_state="in force",
                agents="responder agent-123 dispatched (run /dev-ops-steward --respond-to-alert 42 --once)",
                drill=kind == "drill",
                drill_id="d1" if kind == "drill" else None,
            )
    yield relay.Page(
        condition=None,
        kind="drill_summary",
        origin="drill-script",
        source="qontinui.drill",
        signal="drill-summary",
        drill=True,
        drill_id=None,
        conditions=list(relay.CONDITIONS),
    )


def test_rendered_text_contains_no_asking_words_and_ends_with_closing_line():
    for page in _every_page():
        r = relay.render(page)
        assert relay.contains_asking_words(r.title + "\n" + r.body) == [], (page.kind, page.condition, r.body)
        assert r.asks is False
        assert r.body.endswith(relay.CLOSING_LINE)
        assert len(r.title) <= relay.TITLE_MAX and len(r.body) <= relay.BODY_MAX


def test_banned_word_check_catches_asking_text():
    for text in (
        "Please restart the service.",
        "Can you approve the rollback",
        "Acknowledge this page.",
        "Is the database OK? ",
        "Action required: reply to this.",
    ):
        assert relay.contains_asking_words(text), text
    # Not asking: a responder, a steward flag, a URL query string.
    assert relay.contains_asking_words("responder dispatched --respond-to-alert https://x.io/a?id=1") == []


def test_source_text_that_asks_is_withheld():
    _, rec, adapter, _ = run(v1(condition="leader_crash_loop", what="Please approve the restart?"))
    rendered = adapter.calls[0][0]
    assert "Please" not in rendered.body and "approve" not in rendered.body
    assert relay.WITHHELD_SOURCE_TEXT in rendered.body
    assert rec["asks"] is False and rec["source_text_withheld"] is True


def test_asking_text_in_any_source_field_is_withheld():
    msg = v1(
        condition="deploy_failed",
        what="Deploy failed.",
        remediation="Please approve the rollback",
        remediation_state="needs your call",
        agents="could you check",
        signal="please-ack",
    )
    _, rec, adapter, _ = run(msg)
    rendered = adapter.calls[0][0]
    assert relay.contains_asking_words(rendered.title + "\n" + rendered.body) == []
    assert rec["outcome"] == "accepted" and rec["asks"] is False and rec["source_text_withheld"] is True


def test_a_page_that_still_asks_after_withholding_is_dropped(monkeypatch):
    monkeypatch.setitem(relay.CONDITION_LABELS, "deploy_failed", "Please confirm the deploy")
    _, rec, adapter, _ = run(v1(condition="deploy_failed"))
    assert rec["outcome"] == "dropped" and rec["drop_reason"] == "asks"
    assert adapter.calls == []


def test_long_text_is_clipped_but_keeps_closing_line():
    r = relay.render(relay.Page(condition="deploy_failed", kind="page", origin="coord", source="coord", signal="s", what="x" * 5000))
    assert len(r.body) <= relay.BODY_MAX
    assert r.body.endswith(relay.CLOSING_LINE)


def test_sns_subject_is_prefixed_ascii_and_bounded():
    s = relay.sns_subject("coord is crash-looping — " + "é" * 300)
    assert s.startswith("[PAGE] ")
    assert len(s) <= relay.SNS_SUBJECT_MAX
    s.encode("ascii")
    assert "\n" not in relay.sns_subject("a\nb")


# ─── Delivery rules ──────────────────────────────────────────────────────


def test_4xx_is_not_retried_and_falls_back():
    vendor = FakeAdapter("pushover", [400])
    fallback = FakeAdapter("sns_forward", [200], ref="fb-mid")
    sleeps = Sleeps()
    _, rec, _, _ = run(v1(condition="deploy_failed"), adapter=vendor, fallback=fallback, sleep=sleeps)
    assert len(vendor.calls) == 1, "a 4xx must never be retried"
    assert sleeps.calls == []
    assert rec["outcome"] == "fallback" and rec["fallback_used"] is True
    assert rec["FallbackForwarded"] == 1 and rec["VendorRejected"] == 1
    assert rec["vendor_status"] == 400 and rec["vendor_ref"] == "fb-mid"
    assert len(fallback.calls) == 1


def test_5xx_is_retried_once_after_five_seconds_then_falls_back():
    vendor = FakeAdapter("pushover", [503, 502, 500])
    fallback = FakeAdapter("sns_forward", [200])
    sleeps = Sleeps()
    _, rec, _, _ = run(v1(condition="deploy_failed"), adapter=vendor, fallback=fallback, sleep=sleeps)
    assert len(vendor.calls) == 2, "a 5xx is retried exactly once"
    assert len(sleeps.calls) == 1 and sleeps.calls[0] >= 5
    assert rec["attempts"] == 2
    assert rec["outcome"] == "fallback" and len(fallback.calls) == 1


def test_5xx_then_success_is_accepted_without_fallback():
    vendor = FakeAdapter("pushover", [500, 200])
    fallback = FakeAdapter("sns_forward", [200])
    _, rec, _, _ = run(v1(condition="deploy_failed"), adapter=vendor, fallback=fallback)
    assert rec["outcome"] == "accepted" and rec["VendorAccepted"] == 1
    assert fallback.calls == []


def test_transport_error_is_retried_like_a_5xx():
    vendor = FakeAdapter("pushover", [None, None])
    fallback = FakeAdapter("sns_forward", [200])
    sleeps = Sleeps()
    _, rec, _, _ = run(v1(condition="deploy_failed"), adapter=vendor, fallback=fallback, sleep=sleeps)
    assert len(vendor.calls) == 2 and sleeps.calls == [relay.RETRY_DELAY_SECS]
    assert rec["outcome"] == "fallback"


def test_sns_forward_failure_does_not_fall_back_to_itself_and_raises():
    primary = FakeAdapter("sns_forward", [500, 500])
    fallback = FakeAdapter("sns_forward", [200])
    lines = []
    with pytest.raises(relay.DeliveryFailed):
        relay.handler(
            sns_event(v1(condition="deploy_failed")),
            None,
            adapter=primary,
            fallback=fallback,
            sleep=Sleeps(),
            emit=lines.append,
        )
    rec = json.loads(lines[0])
    assert rec["outcome"] == "rejected" and rec["VendorRejected"] == 1
    assert fallback.calls == []


def test_failed_fallback_raises_for_lambda_retry_and_dlq():
    with pytest.raises(relay.DeliveryFailed):
        relay.handler(
            sns_event(v1(condition="deploy_failed")),
            None,
            adapter=FakeAdapter("pushover", [400]),
            fallback=FakeAdapter("sns_forward", [500]),
            sleep=Sleeps(),
            emit=lambda _line: None,
        )


def test_token_never_appears_in_logs(capsys):
    class LeakyVendor(FakeAdapter):
        def send(self, rendered, page):
            self.calls.append(1)
            # A vendor error body that echoes the credential must not reach a log.
            return relay.AdapterResponse(status=401, ref=None)

    lines = []
    relay.handler(
        sns_event(v1(condition="github_budget_exhausted", what="core bucket at 0.5%")),
        None,
        adapter=LeakyVendor("pushover", []),
        fallback=FakeAdapter("sns_forward", [200]),
        sleep=Sleeps(),
        emit=lines.append,
    )
    out = capsys.readouterr()
    assert SECRET not in "".join(lines) + out.out + out.err
    # And the record never carries the request body.
    rec = json.loads(lines[0])
    assert "body" not in rec and "message" not in rec


def test_sns_forward_adapter_publishes_to_email_topic_with_page_prefix():
    class FakeSns:
        def __init__(self):
            self.kwargs = None

        def publish(self, **kwargs):
            self.kwargs = kwargs
            return {"MessageId": "m-123", "ResponseMetadata": {"HTTPStatusCode": 200}}

    sns = FakeSns()
    ad = relay.SnsForwardAdapter("arn:aws:sns:us-east-1:1:email", client=sns)
    page = relay.Page(condition="deploy_failed", kind="page", origin="coord", source="coord", signal="s")
    resp = ad.send(relay.render(page), page)
    assert resp.status == 200 and resp.ref == "m-123"
    assert sns.kwargs["TopicArn"] == "arn:aws:sns:us-east-1:1:email"
    assert sns.kwargs["Subject"].startswith("[PAGE] ")
    assert sns.kwargs["Message"].rstrip().endswith(relay.CLOSING_LINE)


def test_sns_forward_adapter_maps_client_error_status():
    class Boom(Exception):
        def __init__(self, code):
            super().__init__(f"denied {SECRET}")
            self.response = {"ResponseMetadata": {"HTTPStatusCode": code}}

    class FakeSns:
        def __init__(self, exc):
            self.exc = exc

        def publish(self, **kwargs):
            raise self.exc

    page = relay.Page(condition="deploy_failed", kind="page", origin="coord", source="coord", signal="s")
    r = relay.render(page)
    assert relay.SnsForwardAdapter("arn", client=FakeSns(Boom(403))).send(r, page).status == 403
    assert relay.SnsForwardAdapter("arn", client=FakeSns(RuntimeError("net"))).send(r, page).status is None


# ─── Drill or real (plan D16) ────────────────────────────────────────────


def _child_arn(name):
    return f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{name}"


def test_free_storage_drill_carries_the_drill_id_from_the_drill_childs_history():
    hist = history_with("Threshold Crossed: no datapoints", "DRILL d-42")
    doc = alarm(FREE, reason="x", TriggeringChildren=[{"Arn": _child_arn(f"{FREE}-drill")}])
    _, rec, adapter, _ = run(doc, drill_history=hist)
    assert rec["kind"] == "drill" and rec["drill"] is True and rec["drill_id"] == "d-42"
    assert rec["drill_detection"] == "composite_child"
    assert hist.calls == [(f"{FREE}-drill", "2026-10-19T10:00:00.000+0000")]
    assert adapter.calls[0][0].title.startswith("DRILL d-42")


def test_free_storage_real_child_is_a_page():
    doc = alarm(FREE, TriggeringChildren=[{"Arn": _child_arn(f"{FREE}-low")}])
    _, rec, _, _ = run(doc, drill_history=history_with("DRILL old-drill"))
    assert rec["kind"] == "page" and rec["drill"] is False and rec["drill_id"] is None
    assert rec["condition"] == "storage_or_iops"
    assert rec["remediation_named"] == "RDS storage autoscaling up to the maximum"


def test_ok_after_a_drill_delivers_nothing():
    # The drill child returns to OK and the composite follows. A "Resolved:"
    # page here would read as a real recovery, so nothing is delivered and no
    # Dropped metric fires (it is expected, not an error).
    for name in (FREE, CRASH, SAT):
        child = f"{name}-drill"
        doc = alarm(name, state="OK", reason="y", TriggeringChildren=[{"Arn": _child_arn(child)}])
        _, rec, adapter, fallback = run(doc, drill_history=history_with("DRILL d-5"))
        assert rec["outcome"] == "suppressed", name
        assert rec["drop_reason"] == "drill_returned_to_ok"
        assert rec["drill_id"] == "d-5"
        assert rec["Dropped"] == 0 and rec["VendorAccepted"] == 0
        assert adapter.calls == [] and fallback.calls == []


def test_ok_after_a_drill_with_no_named_child_is_decided_from_history():
    _, rec, adapter, _ = run(alarm(FREE, state="OK", reason="y"), drill_history=history_with("DRILL d-6"))
    assert rec["outcome"] == "suppressed" and adapter.calls == []


def test_real_ok_is_resolved_not_suppressed():
    doc = alarm(FREE, state="OK", reason="y", TriggeringChildren=[{"Arn": _child_arn(f"{FREE}-low")}])
    _, rec, adapter, _ = run(doc, drill_history=history_with("DRILL d-7"))
    assert rec["kind"] == "resolved" and rec["outcome"] == "accepted"
    assert adapter.calls[0][0].title.startswith("Resolved:")


def test_unnamed_child_with_no_drill_in_history_is_real():
    _, rec, _, _ = run(alarm(CRASH, reason="composite changed"), drill_history=history_with("Threshold Crossed"))
    assert rec["kind"] == "page" and rec["drill_detection"] == "composite_history"


def test_drill_id_from_history_reads_only_alarm_transitions_with_a_drill_reason():
    items = [
        {"Timestamp": "2026-10-19T09:50:00Z", "HistoryData": json.dumps({"newState": {"stateValue": "ALARM", "stateReason": "DRILL a"}})},
        {"Timestamp": "2026-10-19T09:55:00Z", "HistoryData": json.dumps({"newState": {"stateValue": "ALARM", "stateReason": "DRILL b"}})},
        {"Timestamp": "2026-10-19T09:58:00Z", "HistoryData": json.dumps({"newState": {"stateValue": "OK", "stateReason": "DRILL c"}})},
        {"Timestamp": "2026-10-19T09:59:00Z", "HistoryData": "not json"},
    ]
    assert relay.drill_id_from_history(items) == "b"
    assert relay.drill_id_from_history([]) is None
    assert relay.drill_id_from_history(None) is None


def test_cloudwatch_drill_history_windows_the_read_and_reads_failure_as_unknown():
    import datetime as dt

    class FakeCw:
        def __init__(self):
            self.kwargs = None

        def describe_alarm_history(self, **kwargs):
            self.kwargs = kwargs
            return {"AlarmHistoryItems": [{"x": 1}]}

    cw = FakeCw()
    items = relay.CloudWatchDrillHistory(client=cw)("a-drill", "2026-10-19T10:00:00.000+0000")
    assert items == [{"x": 1}]
    at = dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.timezone.utc)
    assert cw.kwargs["AlarmName"] == "a-drill" and cw.kwargs["HistoryItemType"] == "StateUpdate"
    assert cw.kwargs["StartDate"] == at - dt.timedelta(minutes=10)
    assert cw.kwargs["EndDate"] == at + dt.timedelta(minutes=1)

    class Broken:
        def describe_alarm_history(self, **kwargs):
            raise RuntimeError("denied")

    assert relay.CloudWatchDrillHistory(client=Broken())("a-drill", "x") is None


def test_composite_triggered_by_drill_child_is_a_drill():
    doc = alarm(
        CRASH,
        reason=f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{CRASH_DRILL} transitioned to ALARM at Sunday 19 October, 2026 10:00:00 UTC",
        AlarmRule=f'ALARM("x") OR ALARM("{CRASH_DRILL}")',
        TriggeringChildren=[
            {"Arn": f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{CRASH_DRILL}", "State": {"Value": "ALARM"}}
        ],
    )
    _, rec, _, _ = run(doc)
    assert rec["kind"] == "drill" and rec["drill"] is True
    assert rec["drill_detection"] == "composite_child"


def test_composite_drill_child_named_only_in_reason_is_a_drill():
    doc = alarm(
        SAT,
        reason=f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{SAT}-drill transitioned to ALARM at Sunday",
    )
    assert relay.triggering_children(doc) == [f"{SAT}-drill"]
    _, rec, _, _ = run(doc)
    assert rec["kind"] == "drill"


def test_composite_triggered_by_real_child_is_a_page():
    child = f"qontinui-{ENV}-pager-coord-abnormal-exits"
    doc = alarm(
        CRASH,
        reason=f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{child} transitioned to ALARM at Sunday",
        TriggeringChildren=[{"Arn": f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{child}"}],
    )
    _, rec, _, _ = run(doc)
    assert rec["kind"] == "page" and rec["drill"] is False
    assert rec["condition"] == "leader_crash_loop"


def test_composite_without_a_named_child_pages_and_says_detection_unknown():
    # A real page must never be silenced because the parser could not name a
    # child; the record says how drill-or-real was (not) decided.
    _, rec, _, _ = run(alarm(CRASH, reason="composite changed"))
    assert rec["kind"] == "page" and rec["drill_detection"] == "composite_child_unnamed"


def test_composite_ok_is_resolved():
    _, rec, _, _ = run(alarm(SAT, state="OK", reason="no child in ALARM"))
    assert rec["kind"] == "resolved"


def test_eventbridge_drill_source_is_a_drill_with_id_from_reason():
    msg = v1(
        condition="deploy_failed",
        origin="eventbridge",
        source="qontinui.drill",
        signal="ecs-deployment-failed",
        event_name="SERVICE_DEPLOYMENT_FAILED",
        reason="DRILL d-7 deployment failed (drill)",
        resources=["arn:aws:ecs:us-east-1:047719635665:service/qontinui-staging/coord"],
        remediation="ECS deployment circuit breaker rolls back (rollback = true)",
    )
    _, rec, adapter, _ = run(msg)
    assert rec["kind"] == "drill" and rec["drill_id"] == "d-7"
    assert rec["drill_detection"] == "event_source"
    assert relay.AWS_AGENT_LINE in adapter.calls[0][0].body


def test_eventbridge_real_event_is_a_page_naming_remediation():
    msg = v1(
        condition="storage_or_iops",
        origin="eventbridge",
        source="aws.rds",
        signal="rds-storage-event",
        event_id="RDS-EVENT-0225",
        message="Allocated storage is approaching the maximum storage threshold.",
        source_identifier="qontinui-staging",
        remediation="RDS storage autoscaling up to the maximum allocated storage",
    )
    _, rec, adapter, _ = run(msg)
    rendered = adapter.calls[0][0]
    assert rec["kind"] == "page" and rec["drill"] is False
    assert "RDS-EVENT-0225" in rendered.body and "qontinui-staging" in rendered.body
    assert "Automatic remediation: RDS storage autoscaling" in rendered.body


def test_drill_summary_is_accepted():
    _, rec, adapter, _ = run(
        v1(kind="drill_summary", origin="drill-script", source="qontinui.drill", drill_id="d1", conditions=list(relay.CONDITIONS))
    )
    assert rec["outcome"] == "accepted" and rec["kind"] == "drill_summary"
    assert rec["conditions"] == list(relay.CONDITIONS)


def test_unknown_kind_is_dropped():
    _, rec, adapter, _ = run(v1(condition="deploy_failed", kind="question"))
    assert rec["outcome"] == "dropped" and rec["drop_reason"] == "unknown_kind"
    assert adapter.calls == []


# ─── The record ──────────────────────────────────────────────────────────


def test_record_is_emf_shaped_with_fixed_metrics():
    _, rec, _, _ = run(v1(condition="train_stalled", alert_ids=["a1"], dedup_key="train_stalled:2026-10-19T10"))
    aws = rec["_aws"]
    cw = aws["CloudWatchMetrics"][0]
    assert cw["Namespace"] == "Qontinui/OperatorPager"
    assert cw["Dimensions"] == [["Environment"]]
    assert [m["Name"] for m in cw["Metrics"]] == ["VendorAccepted", "VendorRejected", "FallbackForwarded", "Dropped"]
    assert rec["Environment"] == ENV
    assert rec["VendorAccepted"] == 1 and rec["Dropped"] == 0
    assert rec["alert_ids"] == ["a1"] and rec["dedup_key"] == "train_stalled:2026-10-19T10"
    assert rec["adapter"] == "sns_forward" and rec["vendor_ref"] == "vendor-ref-1"
    assert rec["asks"] is False


# ─── Contract with the Terraform that publishes to the relay ─────────────

_MODULE = _HERE.parent.parent / "modules" / "operator-pager"


def _templates():
    import re

    text = (_MODULE / "events.tf").read_text(encoding="utf-8")
    found = re.findall(r"input_template = <<-EOT\n(.*?)\n\s*EOT", text, re.S)
    assert len(found) == 2, "deploy-failed and rds-storage templates"
    return [re.sub(r"\$\{aws_cloudwatch_event_rule\.(\w+)\.name\}", r"qontinui-staging-pager-\1", t.strip()) for t in found]


def _fill(template, values):
    import re

    # EventBridge inserts each unquoted <placeholder> as a JSON value.
    return re.sub(r"<(\w+)>", lambda m: json.dumps(values.get(m.group(1))), template)


def test_eventbridge_templates_render_pages_the_relay_accepts():
    deploy, rds = _templates()
    real = relay.parse_message(
        _fill(
            deploy,
            {
                "source": "aws.ecs",
                "time": "2026-10-19T10:00:00Z",
                "resources": ["arn:aws:ecs:us-east-1:047719635665:service/qontinui-staging/coord"],
                "event_name": "SERVICE_DEPLOYMENT_FAILED",
                "reason": 'ECS deployment circuit breaker: tasks failed to start. "quoted"',
                "deployment_id": "ecs-svc/123",
            },
        ),
        {},
    )
    assert isinstance(real, relay.Page), real
    assert real.condition == "deploy_failed" and real.kind == "page" and not real.drill
    assert "rollback" in real.remediation and real.agents == relay.AWS_AGENT_LINE

    drill = relay.parse_message(
        _fill(
            rds,
            {
                "source": "qontinui.drill",
                "time": "2026-10-19T10:00:00Z",
                "event_id": "RDS-EVENT-0225",
                "message": "DRILL d-9 rds-storage rule drill",
                "source_identifier": "qontinui-staging",
            },
        ),
        {},
    )
    assert isinstance(drill, relay.Page), drill
    assert drill.condition == "storage_or_iops" and drill.kind == "drill" and drill.drill_id == "d-9"


def _tf(name):
    return (_MODULE / name).read_text(encoding="utf-8")


def test_lambda_async_retries_are_off():
    # SNS invokes the relay asynchronously; Lambda's default of two retries
    # would re-send a 4xx three times and a 5xx six, and duplicate records.
    import re

    block = re.search(r'resource "aws_lambda_function_event_invoke_config" "relay" \{(.*?)\n\}', _tf("main.tf"), re.S)
    assert block, "the relay must carry an event invoke config"
    assert re.search(r"maximum_retry_attempts\s*=\s*0\b", block.group(1))
    assert "aws_lambda_function.relay.function_name" in block.group(1)


def test_lambda_timeout_covers_the_worst_case_delivery():
    import re

    fn = re.search(r'resource "aws_lambda_function" "relay" \{(.*?)\n\}', _tf("main.tf"), re.S).group(1)
    timeout = int(re.search(r"\btimeout\s*=\s*(\d+)", fn).group(1))
    per_call = relay.CONNECT_TIMEOUT_SECS + relay.READ_TIMEOUT_SECS
    # one history read + primary + retry + fallback, plus the retry sleep
    assert timeout >= 4 * per_call + relay.RETRY_DELAY_SECS + 10, timeout


def test_every_pager_alarm_with_actions_is_a_composite_with_a_drill_child():
    # A drilled metric alarm returns to OK with CloudWatch's own reason and
    # sends a real-looking "Resolved" page; only composites with a drill child
    # can be drilled state-neutrally (plan D16).
    import re

    alarms = _tf("alarms.tf")
    for m in re.finditer(r'resource "aws_cloudwatch_metric_alarm" "(\w+)" \{(.*?)\n\}', alarms, re.S):
        assert "alarm_actions" not in m.group(2) and "ok_actions" not in m.group(2), m.group(1)
    composites = re.findall(r'resource "aws_cloudwatch_composite_alarm" "(\w+)" \{(.*?)\n\}', alarms, re.S)
    assert len(composites) == 3
    for name, body in composites:
        assert "_drill.alarm_name" in body, name


def test_every_condition_named_in_terraform_is_one_of_the_five():
    import re

    named = set()
    for tf in _MODULE.glob("*.tf"):
        text = tf.read_text(encoding="utf-8")
        named |= set(re.findall(r'condition\s*=\s*"(\w+)"', text))
        named |= set(re.findall(r'"condition":\s*"(\w+)"', text))
    assert named, "the module names conditions"
    assert named <= set(relay.CONDITIONS), named - set(relay.CONDITIONS)
