"""Unit tests for the operator pager relay (plan 2026-10-14-coord-pages-the-operator-for-five-conditions-
and-names-the-agent-fixing-each, Phase 1).

Standard library and pytest only: boto3 is never imported, because every AWS client is injected.

The source is ``relay.py``, not the ``handler.py`` the presignup lambda uses: pytest puts each test
directory on ``sys.path``, so a second ``handler.py`` would answer the presignup test's ``import handler``.
It is loaded by FILE PATH under a unique name for the same reason. ``OPERATOR_PAGER_RELAY_PATH`` points the suite at another
copy of the file; the mutation runs use it to prove each guard's test fails when the guard is broken.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_PATH = Path(os.environ.get("OPERATOR_PAGER_RELAY_PATH") or Path(__file__).with_name("relay.py"))
_SPEC = importlib.util.spec_from_file_location("operator_pager_relay_handler_under_test", _PATH)
relay = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
# dataclasses resolve string annotations through sys.modules, so the module must be registered first.
sys.modules[_SPEC.name] = relay
_SPEC.loader.exec_module(relay)

FIVE = ["train_stalled", "leader_crash_loop", "deploy_failed", "storage_or_iops", "github_budget_exhausted"]
ALARM = "qontinui-staging-pager-rds-free-storage"
ALARM_MAP = {
    ALARM: {
        "condition": "storage_or_iops",
        "title": "Database free storage below 5 GB",
        "remediation": "RDS storage autoscaling, up to the 200 GiB maximum",
    }
}


def alarm_msg(name=ALARM, new="ALARM", old="OK", reason="Threshold Crossed: 3 datapoints were less than 5.0E9"):
    return json.dumps(
        {
            "AlarmName": name,
            "NewStateValue": new,
            "OldStateValue": old,
            "NewStateReason": reason,
            "StateChangeTime": "2026-10-14T12:00:00.000+0000",
        }
    )


def doc(**kw):
    base = {"page_version": 1, "kind": "page", "condition": "deploy_failed", "source": "coord", "title": "coord"}
    base.update(kw)
    return json.dumps(base)


class FakeAdapter(relay.Adapter):
    """Returns scripted results in order and records each call."""

    def __init__(self, name, results, token=None):
        self.name = name
        self.results = list(results)
        self.calls = 0
        self.token = token

    def send(self, page, rendered):
        self.calls += 1
        return self.results.pop(0)


def accepted(ref="ref-1"):
    return relay.SendResult(status="accepted", vendor_status="200", vendor_ref=ref)


def rejected(status="400"):
    return relay.SendResult(status="rejected", vendor_status=status, error_class="HTTPError")


def retryable(status="503"):
    return relay.SendResult(status="retryable", vendor_status=status, error_class="HTTPError")


# --- the cap -------------------------------------------------------------------------------------


def test_exactly_five_conditions():
    # Literal, not relay.CONDITIONS compared with itself: adding a sixth must redden this test.
    # A sixth condition changes the claim r05-operator-pager-delivers' target; it is the operator's call.
    assert list(relay.CONDITIONS) == FIVE


def test_unmapped_alarm_is_dropped():
    out = relay.parse_message(alarm_msg(name="qontinui-staging-coord-cpu"), ALARM_MAP)
    assert isinstance(out, relay.Dropped)
    assert out.reason == "unmapped_alarm"


def test_document_with_a_sixth_condition_is_dropped():
    out = relay.parse_message(doc(condition="backup_missing"), ALARM_MAP)
    assert isinstance(out, relay.Dropped)
    assert out.reason == "condition_not_in_five"


def test_alarm_map_entry_naming_a_sixth_condition_is_dropped():
    bad = {ALARM: {"condition": "backup_missing", "title": "x"}}
    out = relay.parse_message(alarm_msg(), bad)
    assert isinstance(out, relay.Dropped)
    assert out.reason == "condition_not_in_five"


@pytest.mark.parametrize("condition", FIVE)
def test_each_of_the_five_is_admitted(condition):
    out = relay.parse_message(doc(condition=condition), ALARM_MAP)
    assert isinstance(out, relay.Page)
    assert out.condition == condition


def test_only_a_drill_summary_may_name_no_condition():
    summary = json.loads(doc(kind="drill_summary", drill_id="d-1", source="drill"))
    del summary["condition"]
    out = relay.parse_message(json.dumps(summary), ALARM_MAP)
    assert isinstance(out, relay.Page) and out.condition is None
    assert relay.render(out).title.startswith("DRILL SUMMARY — Pager drill")
    page = json.loads(doc(kind="page"))
    del page["condition"]
    assert relay.parse_message(json.dumps(page), ALARM_MAP).reason == "condition_not_in_five"
    assert relay.parse_message(doc(kind="drill_summary", condition="sixth"), ALARM_MAP).reason == "condition_not_in_five"


def _summary(**kw):
    body = json.loads(doc(kind="drill_summary", **kw))
    del body["condition"]
    return json.dumps(body)


def test_a_condition_less_drill_summary_needs_a_drill_id_and_the_drill_source():
    # The free-text path past the five-condition cap is open only in the drill script's own shape.
    no_id = relay.parse_message(_summary(source="drill", what="free text"), ALARM_MAP)
    assert isinstance(no_id, relay.Dropped) and no_id.reason == "drill_summary_not_from_a_drill"
    not_drill = relay.parse_message(_summary(source="coord", drill_id="d-1", what="free text"), ALARM_MAP)
    assert isinstance(not_drill, relay.Dropped) and not_drill.reason == "drill_summary_not_from_a_drill"
    via_event = relay.parse_message(_summary(source="eventbridge", event_source="qontinui.drill", drill_id="d-2"), ALARM_MAP)
    assert isinstance(via_event, relay.Page) and via_event.drill_id == "d-2"


def test_unknown_shape_and_non_json_are_dropped():
    assert relay.parse_message("not json", ALARM_MAP).reason == "not_json_object"
    assert relay.parse_message(json.dumps({"hello": 1}), ALARM_MAP).reason == "unrecognised_shape"
    assert relay.parse_message(doc(kind="approve_me"), ALARM_MAP).reason == "unknown_kind"


# --- alarm states and drill recognition ----------------------------------------------------------


def test_real_alarm_is_a_page_not_a_drill():
    out = relay.parse_message(alarm_msg(), ALARM_MAP)
    assert isinstance(out, relay.Page)
    assert (out.kind, out.drill_id, out.condition) == ("page", None, "storage_or_iops")


def test_drill_reason_on_a_metric_alarm_is_a_drill():
    out = relay.parse_message(alarm_msg(reason="DRILL drill-20261014T120000Z-42"), ALARM_MAP)
    assert out.kind == "drill"
    assert out.drill_id == "drill-20261014T120000Z-42"


def test_drill_event_source_is_a_drill_whatever_the_kind():
    out = relay.parse_message(doc(kind="page", event_source="qontinui.drill", event_id="evt-7"), ALARM_MAP)
    assert out.kind == "drill"
    assert out.drill_id == "evt-7"


def test_aws_event_source_is_not_a_drill():
    out = relay.parse_message(doc(kind="page", event_source="aws.ecs", event_id="evt-8"), ALARM_MAP)
    assert out.kind == "page"
    assert out.drill_id is None


def test_ok_after_alarm_is_resolved_and_ok_after_insufficient_data_is_ignored():
    assert relay.parse_message(alarm_msg(new="OK", old="ALARM"), ALARM_MAP).kind == "resolved"
    for new, old in (("OK", "INSUFFICIENT_DATA"), ("INSUFFICIENT_DATA", "OK")):
        out = relay.parse_message(alarm_msg(new=new, old=old), ALARM_MAP)
        assert isinstance(out, relay.Dropped) and out.benign is True


def test_an_ignored_notification_is_recorded_but_counts_toward_no_metric():
    # An alarm's creation (INSUFFICIENT_DATA -> OK) reaches the relay through its OK action. That is
    # not a page and must not trip the Dropped health alarm on every apply.
    emitted = []
    primary = FakeAdapter("sns_forward", [])
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-9", "Message": alarm_msg(new="OK", old="INSUFFICIENT_DATA")}},
        {"PAGER_ALARM_MAP": json.dumps(ALARM_MAP)},
        primary,
        primary,
        now=lambda: 1_760_000_000.0,
        emit=emitted.append,
    )
    assert rec["outcome"] == "ignored" and primary.calls == 0
    assert [e for e in emitted if "_aws" in e] == []


# --- rendering: informs, never asks ----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["page", "update", "resolved", "drill", "drill_summary"])
@pytest.mark.parametrize("condition", FIVE)
def test_rendered_text_never_asks_and_ends_with_no_action(kind, condition):
    page = relay.Page(kind=kind, condition=condition, title="t", what="w", source="coord", remediation="dev-ops steward — claimed")
    r = relay.render(page)
    assert r.asks is False
    assert r.body.endswith("No action needed from you.")
    for banned in ("please", "approve", "action required", "you need to", "you must", "click", "acknowledge"):
        assert banned not in (r.title + r.body).lower()


def test_body_and_title_fit_and_keep_the_no_action_line():
    page = relay.Page(kind="page", condition="train_stalled", title="x" * 600, what="y" * 5000, source="coord", remediation="z" * 900)
    r = relay.render(page)
    assert len(r.title) <= 250
    assert len(r.body) <= 1024
    assert r.body.endswith("\nNo action needed from you.")


def test_inbound_asking_words_are_flagged_not_dropped():
    out = relay.parse_message(doc(what="Please approve the rollback."), ALARM_MAP)
    r = relay.render(out)
    assert r.asks is True


def test_email_subject_starts_with_page_prefix_and_fits_sns():
    page = relay.Page(kind="drill", condition="deploy_failed", title="t\nwith newline" * 20, what="", source="drill")
    s = relay.email_subject(page)
    assert s.startswith("[PAGE] DRILL: Deploy failed: ")
    assert len(s) < 100  # SNS rejects a subject of 100 characters or more
    assert len(s) == relay.SNS_SUBJECT_MAX == 99
    assert "\n" not in s


# --- delivery policy --------------------------------------------------------------------------------


def _page():
    return relay.Page(kind="page", condition="deploy_failed", title="coord", what="w", source="coord")


def test_permanent_failure_is_not_retried_and_falls_back():
    primary = FakeAdapter("vendor", [rejected("400")])
    fallback = FakeAdapter("sns_forward", [accepted("mid-1")])
    sleeps = []
    out = relay.deliver(_page(), relay.render(_page()), primary, fallback, sleep=sleeps.append)
    assert primary.calls == 1
    assert sleeps == []
    assert (out.outcome, out.fallback_adapter, out.vendor_ref) == ("fallback", "sns_forward", "mid-1")


def test_transient_failure_is_retried_once_after_at_least_five_seconds_then_falls_back():
    primary = FakeAdapter("vendor", [retryable("503"), retryable("503")])
    fallback = FakeAdapter("sns_forward", [accepted("mid-2")])
    sleeps = []
    out = relay.deliver(_page(), relay.render(_page()), primary, fallback, sleep=sleeps.append, retry_delay=0.1)
    assert primary.calls == 2
    assert len(sleeps) == 1 and sleeps[0] >= 5
    assert out.outcome == "fallback" and out.attempts == 2


def test_transient_failure_then_success_is_accepted_without_fallback():
    primary = FakeAdapter("vendor", [retryable(), accepted("vend-9")])
    fallback = FakeAdapter("sns_forward", [])
    out = relay.deliver(_page(), relay.render(_page()), primary, fallback, sleep=lambda s: None)
    assert (out.outcome, out.vendor_ref, fallback.calls) == ("accepted", "vend-9", 0)


def test_no_separate_fallback_means_rejected():
    primary = FakeAdapter("sns_forward", [rejected("403")])
    out = relay.deliver(_page(), relay.render(_page()), primary, FakeAdapter("sns_forward", []), sleep=lambda s: None)
    assert out.outcome == "rejected"


def test_handler_raises_when_nothing_delivered(monkeypatch):
    primary = FakeAdapter("sns_forward", [rejected("403")])
    monkeypatch.setattr(relay, "build_adapters", lambda env: (primary, primary))
    monkeypatch.setattr(relay, "_emit", lambda obj: None)
    event = {"Records": [{"Sns": {"MessageId": "m-1", "Message": doc()}}]}
    with pytest.raises(RuntimeError):
        relay.handler(event, None)


def test_unknown_adapter_name_still_reaches_the_email_fallback():
    calls = []

    class FakeSnsClient:
        def publish(self, **kw):
            calls.append(kw)
            return {"MessageId": "mid-77"}

    primary, fallback = relay.build_adapters({"PAGER_ADAPTER": "pushover", "EMAIL_TOPIC_ARN": "arn:aws:sns:us-east-1:1:t"})
    fallback._client_factory = FakeSnsClient
    out = relay.deliver(_page(), relay.render(_page()), primary, fallback, sleep=lambda s: None)
    assert out.outcome == "fallback"
    assert out.adapter == "pushover"
    assert calls and calls[0]["Subject"].startswith("[PAGE] ")


# --- the SNS forward adapter ----------------------------------------------------------------------------


class _ClientError(Exception):
    def __init__(self, status):
        super().__init__(f"An error occurred (X) when calling Publish: secret-in-message?token=tok-SECRET-123 status {status}")
        self.response = {"ResponseMetadata": {"HTTPStatusCode": status}}


def test_sns_forward_publishes_subject_and_body():
    sent = []

    class C:
        def publish(self, **kw):
            sent.append(kw)
            return {"MessageId": "mid-3"}

    a = relay.SnsForwardAdapter("arn:aws:sns:us-east-1:1:budget", client_factory=C)
    page = _page()
    res = a.send(page, relay.render(page))
    assert (res.status, res.vendor_ref) == ("accepted", "mid-3")
    assert sent[0]["TopicArn"] == "arn:aws:sns:us-east-1:1:budget"
    assert sent[0]["Subject"].startswith("[PAGE] Deploy failed: ")
    assert sent[0]["Message"].endswith("No action needed from you.")


def test_sns_client_makes_one_attempt_with_bounded_timeouts(monkeypatch):
    import types

    made = {}

    class Config:
        def __init__(self, **kw):
            self.kw = kw

    def client(service, **kw):
        made["service"], made["config"] = service, kw.get("config")
        return object()

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = Config
    boto3 = types.ModuleType("boto3")
    boto3.client = client
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)

    relay.SnsForwardAdapter("arn:aws:sns:us-east-1:1:budget")._sns()
    assert made["service"] == "sns"
    assert isinstance(made["config"], Config)
    assert made["config"].kw == {"retries": {"max_attempts": 1, "mode": "standard"}, "connect_timeout": 5, "read_timeout": 8}


def test_function_timeout_exceeds_the_worst_case_delivery():
    import re

    tf = (Path(__file__).resolve().parents[2] / "modules/operator-pager/main.tf").read_text()
    timeout = int(re.search(r"(?m)^\s*timeout\s*=\s*(\d+)", tf).group(1))
    # One alarm-history read (Phase 2), the primary twice, the fallback once, one retry wait.
    assert relay.WORST_CASE_DELIVERY_SECONDS == 4 * (5 + 8) + 5
    assert timeout > relay.WORST_CASE_DELIVERY_SECONDS


@pytest.mark.parametrize("status,expected", [(403, "rejected"), (400, "rejected"), (429, "retryable"), (500, "retryable")])
def test_sns_forward_classifies_errors_by_status(status, expected):
    class C:
        def publish(self, **kw):
            raise _ClientError(status)

    res = relay.SnsForwardAdapter("arn:x", client_factory=C).send(_page(), relay.render(_page()))
    assert res.status == expected
    assert res.error_class == "_ClientError"


# --- secrets never reach a log line ---------------------------------------------------------------------


def test_a_token_never_appears_in_emitted_lines():
    token = "tok-SECRET-123"

    class C:
        def publish(self, **kw):
            raise _ClientError(500)

    emitted = []
    primary = FakeAdapter("vendor", [relay.SendResult(status="rejected", vendor_status="401", error_class="HTTPError")], token=token)
    fallback = relay.SnsForwardAdapter("arn:x", client_factory=C)
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-2", "Message": doc(what="deploy failed")}},
        {"RETRY_DELAY_SECONDS": "5"},
        primary,
        fallback,
        sleep=lambda s: None,
        now=lambda: 1_760_000_000.0,
        emit=emitted.append,
    )
    blob = json.dumps(emitted)
    assert token not in blob
    assert "secret-in-message" not in blob
    assert rec["outcome"] == "rejected"


def test_vendor_secret_is_read_in_eu_central_1_with_decryption_and_not_emitted(capsys):
    seen = {}

    class Ssm:
        def get_parameter(self, Name, WithDecryption):  # noqa: N803 - boto3 casing
            seen.update(name=Name, decrypt=WithDecryption)
            return {"Parameter": {"Value": "tok-SECRET-456"}}

    def factory(region):
        seen["region"] = region
        return Ssm()

    value = relay.load_vendor_secret("/qontinui/ops/operator-pager/pushover", client_factory=factory)
    assert value == "tok-SECRET-456"
    assert seen == {"region": "eu-central-1", "name": "/qontinui/ops/operator-pager/pushover", "decrypt": True}
    assert "tok-SECRET-456" not in capsys.readouterr().out


# --- records and metrics -----------------------------------------------------------------------------


def test_process_record_emits_one_record_and_one_metric():
    emitted = []
    primary = FakeAdapter("sns_forward", [accepted("mid-5")])
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-3", "Message": doc(kind="drill", drill_id="drill-x", condition="train_stalled")}},
        {},
        primary,
        primary,
        now=lambda: 1_760_000_000.0,
        emit=emitted.append,
    )
    assert rec["record"] == "operator_pager_relay"
    assert (rec["outcome"], rec["kind"], rec["drill"], rec["drill_id"]) == ("accepted", "drill", True, "drill-x")
    metrics = [e for e in emitted if "_aws" in e]
    assert len(metrics) == 1
    m = metrics[0]
    assert m["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "Qontinui/OperatorPager"
    assert m["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[], ["Adapter", "Condition"]]
    assert m["VendorAccepted"] == 1


def test_the_page_text_never_appears_in_the_record_or_any_emitted_line():
    marker = "PAGE-TEXT-MARKER-7f3a"
    message = doc(condition="deploy_failed", what=f"deploy {marker} failed")
    emitted = []
    primary = FakeAdapter("sns_forward", [accepted("mid-6")])
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-4", "Message": message}},
        {},
        primary,
        primary,
        now=lambda: 1_760_000_000.0,
        emit=emitted.append,
    )
    assert rec["outcome"] == "accepted"
    assert marker in relay.render(relay.parse_message(message, ALARM_MAP)).body  # it is in the page ...
    assert marker not in json.dumps(rec)  # ... and recorded nowhere
    assert all(marker not in json.dumps(e) for e in emitted)


def test_dropped_message_emits_a_dropped_metric_and_no_delivery():
    emitted = []
    primary = FakeAdapter("sns_forward", [])
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-4", "Message": doc(condition="sixth")}},
        {},
        primary,
        primary,
        now=lambda: 1_760_000_000.0,
        emit=emitted.append,
    )
    assert rec["outcome"] == "dropped" and primary.calls == 0
    metric = [e for e in emitted if "_aws" in e][0]
    assert metric["Dropped"] == 1 and metric["Reason"] == "condition_not_in_five"


# --- Phase 2: composite drills, EventBridge documents --------------------------------------------------

COMPOSITE = "qontinui-staging-pager-coord-crash-loop"
DRILL_CHILD = COMPOSITE + "-drill"
REAL_CHILD = "qontinui-staging-pager-coord-abnormal-exits"
COMPOSITE_MAP = {
    COMPOSITE: {
        "condition": "leader_crash_loop",
        "title": "coord tasks are crash-looping",
        "remediation": "ECS restarts tasks",
        "drill_child": DRILL_CHILD,
    }
}


def composite_msg(children=None, reason="", new="ALARM", old="OK"):
    """The composite notification shape the relay parses. Phase 2's post-apply drill captures a real
    one and pins this fixture to it (plan D16); until then it follows the documented fields."""
    msg = {
        "AlarmName": COMPOSITE,
        "AlarmRule": f'ALARM("{REAL_CHILD}") OR ALARM("{DRILL_CHILD}")',
        "NewStateValue": new,
        "OldStateValue": old,
        "NewStateReason": reason,
        "StateChangeTime": "2026-10-14T12:00:00.000+0000",
    }
    if children is not None:
        msg["TriggeringChildren"] = [
            {"Arn": f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{c}", "State": {"Value": "ALARM"}} for c in children
        ]
    return json.dumps(msg)


def test_composite_triggered_only_by_its_drill_child_is_a_drill_with_the_id_from_history():
    out = relay.parse_message(composite_msg(children=[DRILL_CHILD]), COMPOSITE_MAP, history=lambda n: "drill-77" if n == DRILL_CHILD else None)
    assert (out.kind, out.drill_id, out.drill_lookup) == ("drill", "drill-77", "notification")


def test_composite_with_a_real_child_triggering_is_a_real_page_even_during_a_drill():
    out = relay.parse_message(composite_msg(children=[DRILL_CHILD, REAL_CHILD]), COMPOSITE_MAP, history=lambda n: "drill-77")
    assert (out.kind, out.drill_id) == ("page", None)


def test_composite_drill_child_named_in_the_reason_is_a_drill():
    reason = f"arn:aws:cloudwatch:us-east-1:047719635665:alarm:{DRILL_CHILD} transitioned to ALARM at 12:00"
    out = relay.parse_message(composite_msg(reason=reason), COMPOSITE_MAP, history=lambda n: None)
    assert (out.kind, out.drill_id) == ("drill", "unknown")


def test_composite_naming_no_child_asks_the_drill_childs_history():
    seen = []

    def history(name):
        seen.append(name)
        return "drill-88"

    out = relay.parse_message(composite_msg(), COMPOSITE_MAP, history=history)
    assert seen == [DRILL_CHILD]
    assert (out.kind, out.drill_id, out.drill_lookup) == ("drill", "drill-88", "history")
    out = relay.parse_message(composite_msg(), COMPOSITE_MAP, history=lambda n: None)
    assert (out.kind, out.drill_lookup) == ("page", "history")


def test_composite_with_unreadable_history_pages_as_real_and_says_so():
    def history(name):
        raise RuntimeError("AccessDenied")

    out = relay.parse_message(composite_msg(), COMPOSITE_MAP, history=history)
    assert (out.kind, out.drill_lookup) == ("page", "failed")
    assert "could not be read" in out.what


def test_composite_ok_after_alarm_is_resolved_without_a_history_read():
    calls = []
    out = relay.parse_message(composite_msg(new="OK", old="ALARM"), COMPOSITE_MAP, history=lambda n: calls.append(n))
    assert out.kind == "resolved" and calls == []


class _HistoryClient:
    def __init__(self, items):
        self.items = items
        self.kwargs = None

    def describe_alarm_history(self, **kw):
        self.kwargs = kw
        return {"AlarmHistoryItems": self.items}


def _item(new_value, reason):
    return {"HistoryData": json.dumps({"oldState": {"stateValue": "OK"}, "newState": {"stateValue": new_value, "stateReason": reason}})}


def test_drill_id_from_history_reads_the_newest_drill_alarm_transition():
    client = _HistoryClient([_item("OK", "Threshold Crossed: no datapoints"), _item("ALARM", "DRILL drill-99"), _item("ALARM", "DRILL drill-old")])
    assert relay.drill_id_from_history(DRILL_CHILD, client_factory=lambda: client, now=lambda: 1_760_000_000.0) == "drill-99"
    assert client.kwargs["AlarmName"] == DRILL_CHILD and client.kwargs["HistoryItemType"] == "StateUpdate"


def test_drill_id_from_history_without_a_drill_transition_is_none():
    client = _HistoryClient([_item("ALARM", "Threshold Crossed: 3 datapoints"), _item("OK", "back")])
    assert relay.drill_id_from_history(DRILL_CHILD, client_factory=lambda: client, now=lambda: 1_760_000_000.0) is None


def test_latest_alarm_only_reads_the_newest_alarm_transition_alone():
    client = _HistoryClient([_item("ALARM", "Threshold Crossed: 3 datapoints"), _item("ALARM", "DRILL drill-old")])
    assert relay.drill_id_from_history(ALARM, client_factory=lambda: client, now=lambda: 1.0, latest_alarm_only=True) is None
    assert relay.drill_id_from_history(ALARM, client_factory=lambda: client, now=lambda: 1.0) == "drill-old"
    client = _HistoryClient([_item("OK", "back"), _item("ALARM", "DRILL drill-5")])
    assert relay.drill_id_from_history(ALARM, client_factory=lambda: client, now=lambda: 1.0, latest_alarm_only=True) == "drill-5"


def test_a_drilled_metric_alarm_returning_to_ok_is_ignored_not_sent():
    out = relay.parse_message(alarm_msg(new="OK", old="ALARM", reason="Threshold Crossed: back"), ALARM_MAP, last_alarm_drill=lambda n: "drill-5" if n == ALARM else None)
    assert isinstance(out, relay.Dropped) and out.reason == "drill_returned_to_ok" and out.benign


def test_a_metric_alarm_returning_from_a_real_alarm_or_an_unreadable_history_is_resolved():
    real = relay.parse_message(alarm_msg(new="OK", old="ALARM"), ALARM_MAP, last_alarm_drill=lambda n: None)
    assert isinstance(real, relay.Page) and real.kind == "resolved"

    def unreadable(name):
        raise RuntimeError("AccessDenied")

    out = relay.parse_message(alarm_msg(new="OK", old="ALARM"), ALARM_MAP, last_alarm_drill=unreadable)
    assert isinstance(out, relay.Page) and out.kind == "resolved"


def test_a_composite_returning_to_ok_does_not_read_the_metric_history():
    calls = []
    out = relay.parse_message(composite_msg(new="OK", old="ALARM"), COMPOSITE_MAP, history=lambda n: None, last_alarm_drill=lambda n: calls.append(n) or "drill-1")
    assert isinstance(out, relay.Page) and out.kind == "resolved" and calls == []


def test_process_record_checks_a_metric_return_against_the_newest_alarm_transition(monkeypatch):
    seen = []

    def fake_history(name, latest_alarm_only=False, **kw):
        seen.append((name, latest_alarm_only))
        return "drill-5"

    monkeypatch.setattr(relay, "drill_id_from_history", fake_history)
    primary = FakeAdapter("sns_forward", [])
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-r", "Message": alarm_msg(new="OK", old="ALARM")}},
        {"PAGER_ALARM_MAP": json.dumps(ALARM_MAP)},
        primary,
        primary,
        now=lambda: 1.0,
        emit=lambda e: None,
    )
    assert (rec["outcome"], rec["reason"]) == ("ignored", "drill_returned_to_ok")
    assert seen == [(ALARM, True)] and primary.calls == 0


def test_cloudwatch_client_makes_one_attempt_with_bounded_timeouts(monkeypatch):
    import types

    made = {}

    class Config:
        def __init__(self, **kw):
            self.kw = kw

    class Client:
        def describe_alarm_history(self, **kw):
            return {"AlarmHistoryItems": []}

    def client(service, **kw):
        made["service"], made["config"] = service, kw.get("config")
        return Client()

    botocore = types.ModuleType("botocore")
    botocore_config = types.ModuleType("botocore.config")
    botocore_config.Config = Config
    boto3 = types.ModuleType("boto3")
    boto3.client = client
    monkeypatch.setitem(sys.modules, "boto3", boto3)
    monkeypatch.setitem(sys.modules, "botocore", botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", botocore_config)

    assert relay.drill_id_from_history(DRILL_CHILD, now=lambda: 1.0) is None
    assert made["service"] == "cloudwatch" and isinstance(made["config"], Config)
    assert made["config"].kw == {"retries": {"max_attempts": 1, "mode": "standard"}, "connect_timeout": 5, "read_timeout": 8}


def test_drill_id_from_history_ignores_a_marker_on_a_non_alarm_transition():
    client = _HistoryClient([_item("OK", "DRILL drill-on-ok")])
    assert relay.drill_id_from_history(DRILL_CHILD, client_factory=lambda: client, now=lambda: 1_760_000_000.0) is None


def deploy_doc(event_source="aws.ecs", reason="ECS deployment circuit breaker: tasks failed to start."):
    return json.dumps(
        {
            "page_version": 1,
            "kind": "page",
            "condition": "deploy_failed",
            "source": "eventbridge",
            "event_source": event_source,
            "event_id": "evt-1",
            "event_time": "2026-10-14T12:00:00Z",
            "aws": {
                "service": "arn:aws:ecs:us-east-1:047719635665:service/qontinui-staging/coord",
                "deployment": "ecs-svc/123",
                "event": "SERVICE_DEPLOYMENT_FAILED",
                "reason": reason,
            },
            "title": "ECS deployment failed",
            "remediation": {"name": "ECS deployment circuit breaker", "state": "rolls back to the last good task set"},
        }
    )


def test_ecs_deployment_failure_renders_what_happened_from_structured_fields():
    out = relay.parse_message(deploy_doc(), {})
    assert out.kind == "page" and out.condition == "deploy_failed"
    assert "SERVICE_DEPLOYMENT_FAILED for service coord (deployment ecs-svc/123)" in out.what
    assert "circuit breaker" in relay.render(out).body


def test_ecs_deployment_drill_takes_its_id_from_the_drill_reason():
    out = relay.parse_message(deploy_doc(event_source="qontinui.drill", reason="DRILL drill-55"), {})
    assert (out.kind, out.drill_id) == ("drill", "drill-55")


def rds_doc(event_id="RDS-EVENT-0225", event_source="aws.rds", message="Allocated storage size 160 GB is approaching the maximum storage threshold 200 GB. Increase the maximum storage threshold."):
    return json.dumps(
        {
            "page_version": 1,
            "kind": "page",
            "condition": "storage_or_iops",
            "source": "eventbridge",
            "event_source": event_source,
            "event_id": "evt-2",
            "event_time": "2026-10-14T12:00:00Z",
            "aws": {"rds_event_id": event_id, "instance": "qontinui-staging", "message": message},
            "title": "RDS storage event",
        }
    )


def test_rds_storage_event_renders_the_relays_neutral_words_not_awss_imperative():
    out = relay.parse_message(rds_doc(), {})
    r = relay.render(out)
    assert "approaching the storage autoscaling maximum" in r.body
    assert "Increase" not in r.body
    assert r.asks is False


@pytest.mark.parametrize("event_id", ["RDS-EVENT-0225", "RDS-EVENT-0224", "RDS-EVENT-0223", "RDS-EVENT-0007", "RDS-EVENT-0089", "RDS-EVENT-0222", "RDS-EVENT-0221"])
def test_every_admitted_rds_event_has_a_description(event_id):
    assert event_id in relay.RDS_STORAGE_EVENTS
    out = relay.parse_message(rds_doc(event_id=event_id), {})
    assert "an RDS storage event the pager does not describe" not in out.what


def test_rds_drill_takes_its_id_from_the_message():
    out = relay.parse_message(rds_doc(event_source="qontinui.drill", message="DRILL drill-66"), {})
    assert (out.kind, out.drill_id) == ("drill", "drill-66")


def test_an_alarm_record_keeps_the_notification_fields_for_the_composite_capture():
    emitted = []
    primary = FakeAdapter("sns_forward", [accepted("mid-c")])
    raw = json.loads(composite_msg(children=[DRILL_CHILD]))
    raw["AlarmDescription"] = "not kept"
    raw["AWSAccountId"] = "047719635665"
    rec = relay.process_record(
        {"Sns": {"MessageId": "m-c", "Message": json.dumps(raw)}},
        {"PAGER_ALARM_MAP": json.dumps(COMPOSITE_MAP)},
        primary,
        primary,
        now=lambda: 1_760_000_000.0,
        emit=emitted.append,
        history=lambda n: "drill-c",
    )
    assert rec["notification"]["TriggeringChildren"][0]["Arn"].endswith(DRILL_CHILD)
    assert set(rec["notification"]) <= set(relay.NOTIFICATION_KEYS)
    assert "AlarmDescription" not in rec["notification"]
    assert (rec["kind"], rec["drill_id"]) == ("drill", "drill-c")


def test_a_document_record_carries_no_notification():
    emitted = []
    primary = FakeAdapter("sns_forward", [accepted()])
    rec = relay.process_record({"Sns": {"MessageId": "m-d", "Message": doc()}}, {}, primary, primary, now=lambda: 1.0, emit=emitted.append)
    assert "notification" not in rec
