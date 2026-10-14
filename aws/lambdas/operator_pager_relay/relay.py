"""Operator pager relay — the one subscriber of ``qontinui-<env>-operator-pager``.

Plan ``2026-10-14-coord-pages-the-operator-for-five-conditions-and-names-the-agent-fixing-each``,
Phases 1 and 2 (decisions D2, D4, D7, D10 and D16).

What it does
------------
Every pager source publishes to one SNS topic: CloudWatch alarms, EventBridge rules (through an input
transformer), coord's operator leg and coord's deploy workflow. This function is the topic's only
subscriber. For each message it:

1. parses one of two shapes:

   * a CloudWatch alarm state change. The alarm NAME is looked up in ``PAGER_ALARM_MAP``, a JSON
     object Terraform renders with each alarm's condition, title and automatic remediation;
   * a ``page_version: 1`` document, written by an EventBridge input transformer, by coord, by the CI
     rollback step or by the drill script;

2. drops it unless its condition is one of the FIVE (D7). This is the cap's second layer: coord
   enforces it first, and the topic policy admits only ``qontinui-<env>-pager-*`` alarms and rules;
3. decides whether it is a drill (D16): ``DRILL <id>`` in a metric alarm's state reason; for a
   composite alarm, its ``-drill`` child being the only child that triggered it (read from the
   notification, else from that child's alarm history); a ``qontinui.drill`` event source; or a
   document that says ``kind: drill``;
4. renders one page (title <= 250 characters, body <= 1,024) that always ends with "No action needed
   from you." A page informs; it never asks (``decision_record/operational-work-is-autonomous``);
5. delivers through the configured adapter, retrying a transient failure once after at least five
   seconds and falling back to the email topic when the adapter cannot deliver;
6. prints ONE JSON outcome record per message (``record = operator_pager_relay``), which coord
   ingests in Phase 5, plus Embedded Metric Format lines in namespace ``Qontinui/OperatorPager``.

What it never does
------------------
It never logs the vendor credential, a request body or an adapter's raw error text: an error is
recorded by class name and status only, because an HTTP client's message can carry a URL with a key
in it. The credential is read at run time from Parameter Store in eu-central-1 (D4), and only when
the configured adapter needs one. In Phase 1 the only adapter is ``sns_forward``, which needs none.

boto3 is imported lazily (inside the AWS-facing helpers), so every decision in this file is testable
with the standard library and pytest alone. CI installs nothing else.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# The closed vocabulary
# ---------------------------------------------------------------------------

#: The five operator-page conditions (plan D7). A sixth is a change to the claim's target, which is
#: the operator's call; it is never a mapping. ``test_exactly_five_conditions`` pins this tuple.
CONDITIONS: tuple[str, ...] = (
    "train_stalled",
    "leader_crash_loop",
    "deploy_failed",
    "storage_or_iops",
    "github_budget_exhausted",
)

CONDITION_LABELS: dict[str, str] = {
    "train_stalled": "Merge train stalled",
    "leader_crash_loop": "coord crash loop",
    "deploy_failed": "Deploy failed",
    "storage_or_iops": "Database storage or IOPS",
    "github_budget_exhausted": "GitHub API budget exhausted",
}

KINDS: tuple[str, ...] = ("page", "update", "resolved", "drill", "drill_summary")

KIND_LABELS: dict[str, str] = {
    "page": "PAGE",
    "update": "UPDATE",
    "resolved": "RESOLVED",
    "drill": "DRILL",
    "drill_summary": "DRILL SUMMARY",
}

NO_ACTION = "No action needed from you."
TITLE_MAX = 250
BODY_MAX = 1024
#: SNS requires an email Subject of FEWER than 100 characters, single line: a 100-character subject
#: is a 400, and that would send a page to the dead-letter queue instead of the inbox.
SNS_SUBJECT_MAX = 99
#: Every forwarded email's subject starts with this, so the operator can filter pages out of the
#: alarm noise on the same inbox (plan Phase 1).
EMAIL_SUBJECT_PREFIX = "[PAGE]"

#: Phrases that turn information into a request. A page that contains one is still delivered (a
#: lost page is worse than a badly worded one) but its record says ``asks: true``.
BANNED_ASK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bplease\b",
        r"\bapprove\b",
        r"\bapproval\b",
        r"\baction required\b",
        r"\byou need to\b",
        r"\byou must\b",
        r"\bclick\b",
        r"\backnowledge\b",
    )
)

RECORD_NAME = "operator_pager_relay"
EMF_NAMESPACE = "Qontinui/OperatorPager"

DRILL_REASON_RE = re.compile(r"\bDRILL\s+(?P<id>[A-Za-z0-9][A-Za-z0-9_.:-]{0,95})")
DRILL_EVENT_SOURCE = "qontinui.drill"
#: The ``source`` the drill script writes on every document it publishes.
DRILL_DOCUMENT_SOURCE = "drill"

#: The minimum wait before the one retry of a transient failure (plan Phase 1).
MIN_RETRY_DELAY_SECONDS = 5.0

#: The SNS client makes ONE attempt per call, with bounded timeouts. botocore's default retries would
#: stack on the relay's own retry (so ``attempts`` in the record would understate the publishes),
#: and its 60 s default timeouts outlast the function, which would end a hung publish with no
#: outcome record and hand the event to Lambda's async retry, a likely duplicate page.
SNS_CLIENT_CONFIG: dict[str, Any] = {
    "retries": {"max_attempts": 1, "mode": "standard"},
    "connect_timeout": 5,
    "read_timeout": 8,
}
#: The CloudWatch alarm-history read (Phase 2's drill lookup) is bounded the same way.
CLOUDWATCH_CLIENT_CONFIG: dict[str, Any] = SNS_CLIENT_CONFIG
#: The longest one message can take: at most one alarm-history read, then ``deliver``'s primary
#: twice and fallback once, plus one retry wait. The function timeout in
#: ``aws/modules/operator-pager/main.tf`` must exceed it (a test reads both).
WORST_CASE_DELIVERY_SECONDS = 4 * (SNS_CLIENT_CONFIG["connect_timeout"] + SNS_CLIENT_CONFIG["read_timeout"]) + MIN_RETRY_DELAY_SECONDS

#: Alarm names inside a composite alarm's state reason (``...:alarm:<name> transitioned to ALARM``).
ALARM_ARN_NAME_RE = re.compile(r"alarm:(?P<name>[A-Za-z0-9_.\-]+)")

#: The CloudWatch notification fields a record keeps (plan D16: Phase 2 captures one real composite
#: notification and pins the drill parser to it). Alarm names, states, reasons and child ARNs: no
#: secret, and nothing else from the message.
NOTIFICATION_KEYS: tuple[str, ...] = (
    "AlarmName",
    "AlarmRule",
    "NewStateValue",
    "OldStateValue",
    "NewStateReason",
    "StateChangeTime",
    "TriggeringChildren",
)

#: How far back the relay reads a drill child's history for the ``DRILL <id>`` transition.
DRILL_HISTORY_WINDOW_SECONDS = 900

#: RDS storage events the pager rule admits (plan D10), in the relay's own words. AWS's message text
#: is imperative ("Increase the maximum storage threshold.") and a page never asks, so the page
#: carries these descriptions, never AWS's text.
RDS_STORAGE_EVENTS: dict[str, str] = {
    "RDS-EVENT-0225": "allocated storage is approaching the storage autoscaling maximum (raised at 80% of the maximum)",
    "RDS-EVENT-0224": "storage autoscaling started a scale-up that will reach or exceed the maximum storage threshold",
    "RDS-EVENT-0223": "storage autoscaling could not scale the storage",
    "RDS-EVENT-0007": "allocated storage is exhausted",
    "RDS-EVENT-0089": "free storage is below 10% of the provisioned storage",
    "RDS-EVENT-0222": "free storage is low, and RDS shuts the database down to prevent corruption below its storage-full threshold",
    "RDS-EVENT-0221": "the database reached the storage-full threshold and RDS shut it down",
}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Page:
    """One message, parsed and classified, before rendering."""

    kind: str
    #: One of CONDITIONS. ``None`` only for a ``drill_summary``, which reports on a drill rather
    #: than on a condition.
    condition: Optional[str]
    title: str
    what: str
    source: str
    remediation: str = ""
    link: str = ""
    drill_id: Optional[str] = None
    dedup_key: Optional[str] = None
    alert_ids: list[Any] = field(default_factory=list)
    alarm: Optional[str] = None
    #: For a composite alarm page: how its drill status was decided (``notification``, ``history``,
    #: or ``failed`` when the drill child's history could not be read, in which case it pages as real).
    drill_lookup: Optional[str] = None


@dataclass
class Dropped:
    """A message the relay did not deliver, and why.

    ``benign`` marks a notification that was never a page: an alarm reaching INSUFFICIENT_DATA, or OK
    after INSUFFICIENT_DATA when it is created. Its record says ``ignored`` and it counts toward no
    metric. Anything else (an unmapped alarm, a sixth condition, an unknown shape) is ``dropped`` and
    counts toward ``Dropped``, whose alarm emails the operator."""

    reason: str
    condition: Optional[str] = None
    source: str = "unknown"
    alarm: Optional[str] = None
    benign: bool = False


@dataclass
class Rendered:
    title: str
    body: str
    asks: bool


@dataclass
class SendResult:
    """One adapter attempt. ``status`` is ``accepted``, ``rejected`` (permanent, 4xx-like) or
    ``retryable`` (transient, 5xx-like or a timeout)."""

    status: str
    vendor_status: Optional[str] = None
    vendor_ref: Optional[str] = None
    error_class: Optional[str] = None


@dataclass
class Outcome:
    outcome: str  # accepted | fallback | rejected
    adapter: str
    attempts: int
    vendor_status: Optional[str] = None
    vendor_ref: Optional[str] = None
    error_class: Optional[str] = None
    fallback_adapter: Optional[str] = None


# ---------------------------------------------------------------------------
# Parsing and classification (pure)
# ---------------------------------------------------------------------------


def _clean(text: Any, limit: int) -> str:
    """Collapse whitespace and cut at ``limit`` characters."""
    if text is None:
        return ""
    s = re.sub(r"\s+", " ", str(text)).strip()
    if len(s) > limit:
        s = s[: max(0, limit - 1)].rstrip() + "…"
    return s


def drill_id_from_reason(reason: Any) -> Optional[str]:
    """``DRILL <id>`` in an alarm's state reason marks a drill (plan D16). Returns the id or None."""
    if not isinstance(reason, str):
        return None
    m = DRILL_REASON_RE.search(reason)
    return m.group("id") if m else None


def _triggering_children(msg: dict[str, Any]) -> set[str]:
    """Child alarm names a composite notification names: its ``TriggeringChildren`` ARNs when it
    carries them, and any ``alarm:<name>`` inside its state reason."""
    names: set[str] = set()
    children = msg.get("TriggeringChildren")
    if isinstance(children, list):
        for child in children:
            arn = child.get("Arn") if isinstance(child, dict) else None
            if isinstance(arn, str):
                m = ALARM_ARN_NAME_RE.search(arn)
                if m:
                    names.add(m.group("name"))
    reason = msg.get("NewStateReason")
    if isinstance(reason, str):
        names.update(m.group("name") for m in ALARM_ARN_NAME_RE.finditer(reason))
    names.discard(msg.get("AlarmName"))
    return names


def parse_alarm(
    msg: dict[str, Any],
    alarm_map: dict[str, Any],
    history: Optional[Callable[[str], Optional[str]]] = None,
    last_alarm_drill: Optional[Callable[[str], Optional[str]]] = None,
) -> Page | Dropped:
    """A CloudWatch alarm notification. Only an alarm named in ``alarm_map`` may page.

    A COMPOSITE is never drilled directly (AWS: a composite set by SetAlarmState "is not guaranteed to
    return to its actual state"). Its map entry names a ``drill_child``, and a drill sets that child.
    The composite page is a drill when the drill child is the only child that triggered it. When the
    notification names no child, the drill child's own history decides (``history(name)`` returns the
    ``DRILL <id>`` id of a recent ALARM transition, or None). When that history cannot be read, the
    page goes out as REAL: a drill paged as real is noise; a real page withheld as a drill is a miss.

    A METRIC alarm's return to OK is checked against its own last ALARM transition
    (``last_alarm_drill(name)``): when that transition was a drill, the return is the drill ending,
    recorded as ``ignored`` and not sent. An unreadable history sends the resolved notice."""
    name = msg.get("AlarmName")
    entry = alarm_map.get(name) if isinstance(name, str) else None
    if not isinstance(entry, dict):
        return Dropped(reason="unmapped_alarm", source="cloudwatch", alarm=name if isinstance(name, str) else None)
    condition = entry.get("condition")
    if condition not in CONDITIONS:
        return Dropped(reason="condition_not_in_five", condition=str(condition), source="cloudwatch", alarm=name)

    new_state = msg.get("NewStateValue")
    old_state = msg.get("OldStateValue")
    reason = msg.get("NewStateReason")
    drill_id = drill_id_from_reason(reason)
    drill_child = entry.get("drill_child")
    drill_lookup: Optional[str] = None

    if new_state == "ALARM" and isinstance(drill_child, str) and drill_child:
        children = _triggering_children(msg)
        if children:
            drill_lookup = "notification"
            if children == {drill_child}:
                drill_id = _lookup_drill_id(history, drill_child) or "unknown"
            else:
                drill_id = None
        else:
            try:
                drill_id = history(drill_child) if history is not None else None
                drill_lookup = "history"
            except Exception:  # noqa: BLE001 - an unreadable history pages as real
                drill_id = None
                drill_lookup = "failed"

    if new_state == "ALARM":
        kind = "drill" if drill_id else "page"
    elif new_state == "OK" and old_state == "ALARM":
        # The OK reason of a drill's return carries no marker, so the alarm's own last ALARM
        # transition decides. A composite's return is still sent as resolved: the composite's own
        # history names children, not a DRILL marker (the PR body records this).
        if not drill_child and last_alarm_drill is not None:
            try:
                returning_from = last_alarm_drill(name)
            except Exception:  # noqa: BLE001 - unreadable: the resolved notice goes out
                returning_from = None
            if returning_from:
                return Dropped(reason="drill_returned_to_ok", condition=condition, source="cloudwatch", alarm=name, benign=True)
        kind = "resolved"
    else:
        # INSUFFICIENT_DATA, or OK after INSUFFICIENT_DATA (creation): nothing was paged, so nothing
        # resolves. Dropped with a reason, not delivered.
        return Dropped(reason=f"state_{new_state}_from_{old_state}", condition=condition, source="cloudwatch", alarm=name, benign=True)

    what = f"CloudWatch alarm {name} is {new_state}: {_clean(reason, 400)}"
    if drill_lookup == "failed":
        what += " (Whether this was a drill could not be read, so it is sent as real.)"
    return Page(
        kind=kind,
        condition=condition,
        title=_clean(entry.get("title") or CONDITION_LABELS[condition], 160),
        what=what,
        source="cloudwatch",
        remediation=_clean(entry.get("remediation"), 300),
        drill_id=drill_id,
        dedup_key=f"{condition}:{name}",
        alarm=name,
        drill_lookup=drill_lookup,
    )


def _lookup_drill_id(history: Optional[Callable[[str], Optional[str]]], child: str) -> Optional[str]:
    if history is None:
        return None
    try:
        return history(child)
    except Exception:  # noqa: BLE001 - the drill is known from the notification; only its id is lost
        return None


def parse_document(msg: dict[str, Any]) -> Page | Dropped:
    """A ``page_version: 1`` document from EventBridge, coord, CI or the drill script."""
    source = _clean(msg.get("source") or "document", 64)
    condition = msg.get("condition")
    kind = msg.get("kind", "page")
    if kind not in KINDS:
        return Dropped(reason="unknown_kind", condition=None if condition is None else str(condition), source=source)
    event_source = msg.get("event_source")
    from_drill = source == DRILL_DOCUMENT_SOURCE or event_source == DRILL_EVENT_SOURCE
    if kind == "drill_summary" and condition is None:
        # The one document that may name no condition, so the one that could carry free text past
        # the five-condition cap. It is admitted only in the shape the drill script sends: a drill id
        # and the drill source.
        if not (msg.get("drill_id") and from_drill):
            return Dropped(reason="drill_summary_not_from_a_drill", condition=None, source=source)
    elif condition not in CONDITIONS:
        return Dropped(reason="condition_not_in_five", condition=None if condition is None else str(condition), source=source)

    drill_id = msg.get("drill_id")
    drill_id = _clean(drill_id, 96) if drill_id else None
    # A qontinui.drill event source is a drill whatever kind the transformer wrote: PutEvents cannot
    # forge aws.* sources, so the drill script reaches the real rules through this source (plan D16).
    aws = msg.get("aws") if isinstance(msg.get("aws"), dict) else {}
    if event_source == DRILL_EVENT_SOURCE or msg.get("drill") is True:
        if kind in ("page", "update", "resolved"):
            kind = "drill"
        if not drill_id:
            drill_id = drill_id_from_reason(aws.get("reason")) or drill_id_from_reason(aws.get("message"))
        if not drill_id:
            drill_id = _clean(msg.get("event_id") or "unnamed", 96)

    alert_ids = msg.get("alert_ids")
    return Page(
        kind=kind,
        condition=condition,
        title=_clean(msg.get("title") or _condition_label(condition), 160),
        what=_clean(msg.get("what") or msg.get("body") or _aws_what(condition, aws, msg.get("event_time")), 600),
        source=source,
        remediation=_remediation_text(msg.get("remediation")),
        link=_clean(msg.get("link"), 200),
        drill_id=drill_id,
        dedup_key=_clean(msg.get("dedup_key"), 160) or None,
        alert_ids=alert_ids if isinstance(alert_ids, list) else [],
    )


def _aws_what(condition: Optional[str], aws: dict[str, Any], event_time: Any) -> str:
    """The page's first line for an EventBridge-sourced document, built from structured fields the
    input transformer passed as JSON values (never pasted inside a JSON string, so any text is safe)."""
    when = _clean(event_time, 40)
    if condition == "deploy_failed" and aws:
        service = str(aws.get("service") or "").rsplit("/", 1)[-1] or "an ECS service"
        return (
            f"ECS reported {_clean(aws.get('event'), 60) or 'a failed deployment'} for service {service} "
            f"(deployment {_clean(aws.get('deployment'), 80) or 'unknown'}) at {when}: {_clean(aws.get('reason'), 300)}"
        )
    if condition == "storage_or_iops" and aws.get("rds_event_id"):
        event_id = str(aws.get("rds_event_id"))
        text = RDS_STORAGE_EVENTS.get(event_id, "an RDS storage event the pager does not describe")
        return f"{event_id} on {_clean(aws.get('instance'), 80) or 'the database'} at {when}: {text}."
    return ""


def _remediation_text(value: Any) -> str:
    if isinstance(value, dict):
        name = _clean(value.get("name"), 120)
        state = _clean(value.get("state"), 160)
        return f"{name} — {state}" if name and state else (name or state)
    return _clean(value, 300)


def parse_message(
    raw: Any,
    alarm_map: dict[str, Any],
    history: Optional[Callable[[str], Optional[str]]] = None,
    last_alarm_drill: Optional[Callable[[str], Optional[str]]] = None,
) -> Page | Dropped:
    """Classify one SNS ``Message`` string."""
    try:
        msg = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError):
        msg = None
    if not isinstance(msg, dict):
        return Dropped(reason="not_json_object")
    if "AlarmName" in msg and "NewStateValue" in msg:
        return parse_alarm(msg, alarm_map, history, last_alarm_drill)
    if msg.get("page_version") == 1:
        return parse_document(msg)
    return Dropped(reason="unrecognised_shape", source=_clean(msg.get("source") or "unknown", 64))


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------


def _condition_label(condition: Optional[str]) -> str:
    return CONDITION_LABELS[condition] if condition else "Pager drill"


def asks(text: str) -> bool:
    return any(p.search(text) for p in BANNED_ASK_PATTERNS)


def render(page: Page) -> Rendered:
    """One page format for every source. The body always ends with ``NO_ACTION``."""
    label = KIND_LABELS[page.kind]
    cond = _condition_label(page.condition)
    title = _clean(f"{label} — {cond}: {page.title}", TITLE_MAX)

    lines: list[str] = []
    if page.kind in ("drill", "drill_summary"):
        lines.append(f"Pager drill {page.drill_id or ''}".strip() + ". This is a test of the pager path.")
    if page.what:
        lines.append(page.what)
    if page.remediation:
        lines.append(f"Remediation: {page.remediation}")
    if page.link:
        lines.append(page.link)

    tail = "\n" + NO_ACTION
    room = BODY_MAX - len(tail)
    body = "\n".join(lines)
    if len(body) > room:
        body = body[: max(0, room - 1)].rstrip() + "…"
    body = body + tail
    return Rendered(title=title, body=body, asks=asks(title) or asks(body))


def email_subject(page: Page) -> str:
    """``[PAGE] ...`` for every forwarded message; one line, at most 100 characters (SNS)."""
    label = "" if page.kind == "page" else KIND_LABELS[page.kind] + ": "
    subject = f"{EMAIL_SUBJECT_PREFIX} {label}{_condition_label(page.condition)}: {page.title}"
    subject = re.sub(r"[\r\n]+", " ", subject)
    return _clean(subject, SNS_SUBJECT_MAX)


# ---------------------------------------------------------------------------
# Delivery policy (pure, with the adapters and the clock injected)
# ---------------------------------------------------------------------------


class Adapter:
    """A delivery channel. ``send`` never raises: it returns a ``SendResult``."""

    name = "abstract"
    needs_secret = False

    def send(self, page: Page, rendered: Rendered) -> SendResult:  # pragma: no cover - interface
        raise NotImplementedError


def deliver(
    page: Page,
    rendered: Rendered,
    primary: Adapter,
    fallback: Optional[Adapter],
    sleep: Callable[[float], None] = time.sleep,
    retry_delay: float = MIN_RETRY_DELAY_SECONDS,
) -> Outcome:
    """A permanent failure is not retried. A transient one is retried once, after at least five
    seconds. Whatever the primary could not deliver goes to the fallback, unless the fallback IS the
    primary, in which case the outcome is ``rejected`` and the caller raises (async retry, then DLQ)."""
    attempts = 1
    result = primary.send(page, rendered)
    if result.status == "retryable":
        sleep(max(MIN_RETRY_DELAY_SECONDS, retry_delay))
        attempts += 1
        result = primary.send(page, rendered)
    if result.status == "accepted":
        return Outcome(
            outcome="accepted",
            adapter=primary.name,
            attempts=attempts,
            vendor_status=result.vendor_status,
            vendor_ref=result.vendor_ref,
        )
    if fallback is None or fallback.name == primary.name:
        return Outcome(
            outcome="rejected",
            adapter=primary.name,
            attempts=attempts,
            vendor_status=result.vendor_status,
            error_class=result.error_class,
        )
    fb = fallback.send(page, rendered)
    if fb.status == "accepted":
        return Outcome(
            outcome="fallback",
            adapter=primary.name,
            attempts=attempts,
            vendor_status=result.vendor_status,
            vendor_ref=fb.vendor_ref,
            error_class=result.error_class,
            fallback_adapter=fallback.name,
        )
    return Outcome(
        outcome="rejected",
        adapter=primary.name,
        attempts=attempts,
        vendor_status=result.vendor_status,
        error_class=result.error_class or fb.error_class,
        fallback_adapter=fallback.name,
    )


# ---------------------------------------------------------------------------
# Records and metrics (pure)
# ---------------------------------------------------------------------------


def notification_fields(raw: Any) -> Optional[dict[str, Any]]:
    """The kept fields of a CloudWatch alarm notification, or None for any other message."""
    try:
        msg = json.loads(raw) if isinstance(raw, str) else None
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict) or "AlarmName" not in msg:
        return None
    return {k: msg[k] for k in NOTIFICATION_KEYS if k in msg}


def outcome_record(page: Page | Dropped, outcome: Optional[Outcome], rendered: Optional[Rendered], sns_message_id: Optional[str], now: float) -> dict[str, Any]:
    """The one JSON record per message. Fields are values the relay decided, never a secret, never
    a request body, never an adapter's error text."""
    rec: dict[str, Any] = {
        "record": RECORD_NAME,
        "page_version": 1,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "sns_message_id": sns_message_id,
    }
    if isinstance(page, Dropped):
        rec.update(
            outcome="ignored" if page.benign else "dropped",
            reason=page.reason,
            condition=page.condition,
            source=page.source,
            alarm=page.alarm,
        )
        return rec
    rec.update(
        condition=page.condition,
        kind=page.kind,
        drill=page.kind in ("drill", "drill_summary"),
        drill_id=page.drill_id,
        source=page.source,
        alarm=page.alarm,
        dedup_key=page.dedup_key,
        alert_ids=page.alert_ids,
        remediation=page.remediation or None,
        drill_lookup=page.drill_lookup,
        asks=bool(rendered.asks) if rendered else None,
    )
    if outcome is not None:
        rec.update(
            outcome=outcome.outcome,
            adapter=outcome.adapter,
            attempts=outcome.attempts,
            vendor_status=outcome.vendor_status,
            vendor_ref=outcome.vendor_ref,
            error_class=outcome.error_class,
            fallback_adapter=outcome.fallback_adapter,
        )
    return rec


def emf_lines(record: dict[str, Any], now: float) -> list[dict[str, Any]]:
    """Embedded Metric Format: each metric once with no dimensions (what the health alarms watch)
    and once by condition/adapter (or by reason, for a drop)."""
    ts = int(now * 1000)
    outcome = record.get("outcome")
    if outcome == "dropped":
        name, dims = "Dropped", {"Reason": str(record.get("reason"))}
    elif outcome == "accepted":
        name, dims = "VendorAccepted", {"Condition": str(record.get("condition")), "Adapter": str(record.get("adapter"))}
    elif outcome == "fallback":
        name, dims = "FallbackForwarded", {"Condition": str(record.get("condition")), "Adapter": str(record.get("adapter"))}
    elif outcome == "rejected":
        name, dims = "VendorRejected", {"Condition": str(record.get("condition")), "Adapter": str(record.get("adapter"))}
    else:
        return []
    line: dict[str, Any] = {
        "_aws": {
            "Timestamp": ts,
            "CloudWatchMetrics": [
                {
                    "Namespace": EMF_NAMESPACE,
                    "Dimensions": [[], sorted(dims.keys())],
                    "Metrics": [{"Name": name, "Unit": "Count"}],
                }
            ],
        },
        name: 1,
    }
    line.update(dims)
    return [line]


# ---------------------------------------------------------------------------
# AWS-facing helpers (boto3 imported lazily)
# ---------------------------------------------------------------------------


def _client_error_status(exc: Exception) -> tuple[str, Optional[str]]:
    """Classify a botocore error as permanent or transient, by HTTP status, without its message."""
    response = getattr(exc, "response", None)
    status: Optional[int] = None
    if isinstance(response, dict):
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        return "rejected", str(status)
    return "retryable", str(status) if status is not None else None


class SnsForwardAdapter(Adapter):
    """Publishes the rendered page to the existing email topic (``qontinui-<env>-budget-alerts``).

    The default adapter until the operator's vendor decision, and the fallback after it."""

    name = "sns_forward"

    def __init__(self, topic_arn: str, client_factory: Optional[Callable[[], Any]] = None) -> None:
        self.topic_arn = topic_arn
        self._client_factory = client_factory
        self._client: Any = None

    def _sns(self) -> Any:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import boto3  # lazy: keeps the module importable without boto3
                from botocore.config import Config

                self._client = boto3.client("sns", config=Config(**SNS_CLIENT_CONFIG))
        return self._client

    def send(self, page: Page, rendered: Rendered) -> SendResult:
        if not self.topic_arn:
            return SendResult(status="rejected", vendor_status="no_topic", error_class="Unconfigured")
        try:
            resp = self._sns().publish(
                TopicArn=self.topic_arn,
                Subject=email_subject(page),
                Message=rendered.title + "\n\n" + rendered.body,
            )
        except Exception as exc:  # noqa: BLE001 - classified, never logged raw
            status, vendor_status = _client_error_status(exc)
            return SendResult(status=status, vendor_status=vendor_status, error_class=type(exc).__name__)
        return SendResult(status="accepted", vendor_status="200", vendor_ref=resp.get("MessageId"))


def load_vendor_secret(parameter_name: str, region: str = "eu-central-1", client_factory: Optional[Callable[[str], Any]] = None) -> str:
    """Read the vendor credential the operator saved in Parameter Store (plan D4).

    The parameter lives in eu-central-1 and the relay in us-east-1, so the client is built for
    eu-central-1 explicitly. The value is returned to the adapter and never logged."""
    if client_factory is not None:
        client = client_factory(region)
    else:
        import boto3  # lazy

        client = boto3.client("ssm", region_name=region)
    resp = client.get_parameter(Name=parameter_name, WithDecryption=True)
    return resp["Parameter"]["Value"]


def drill_id_from_history(
    alarm_name: str,
    client_factory: Optional[Callable[[], Any]] = None,
    now: Callable[[], float] = time.time,
    window_seconds: int = DRILL_HISTORY_WINDOW_SECONDS,
    latest_alarm_only: bool = False,
) -> Optional[str]:
    """The ``DRILL <id>`` of the drill child's most recent ALARM transition in the window, or None.

    With ``latest_alarm_only`` only the alarm's newest ALARM transition counts: an older drill is not
    evidence about the alarm that is now returning to OK.

    History, not DescribeAlarms: a metric alarm set by SetAlarmState returns to its actual state
    "often within seconds", so by the time the composite's notification arrives the child's CURRENT
    state reason is usually the OK evaluation, and the drill id is only in the history. Raises when the
    history cannot be read; the caller then pages as real."""
    from datetime import datetime, timezone

    if client_factory is not None:
        client = client_factory()
    else:
        import boto3  # lazy
        from botocore.config import Config

        client = boto3.client("cloudwatch", config=Config(**CLOUDWATCH_CLIENT_CONFIG))
    end = now()
    resp = client.describe_alarm_history(
        AlarmName=alarm_name,
        HistoryItemType="StateUpdate",
        StartDate=datetime.fromtimestamp(end - window_seconds, timezone.utc),
        EndDate=datetime.fromtimestamp(end + 60, timezone.utc),
        MaxRecords=20,
        ScanBy="TimestampDescending",
    )
    for item in resp.get("AlarmHistoryItems", []):
        try:
            data = json.loads(item.get("HistoryData") or "{}")
        except ValueError:
            continue
        new = data.get("newState") if isinstance(data, dict) else None
        if isinstance(new, dict) and new.get("stateValue") == "ALARM":
            found = drill_id_from_reason(new.get("stateReason"))
            if found or latest_alarm_only:
                return found
    return None


ADAPTERS: dict[str, Callable[..., Adapter]] = {
    "sns_forward": lambda env: SnsForwardAdapter(env.get("EMAIL_TOPIC_ARN", "")),
}


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def _emit(obj: dict[str, Any]) -> None:
    # print, not logging: a Lambda logging line carries a text prefix, and the CloudWatch Logs JSON
    # filter coord uses (`{ $.record = "operator_pager_relay" }`) needs the whole event to be JSON.
    print(json.dumps(obj, separators=(",", ":"), sort_keys=True, default=str))


def build_adapters(env: dict[str, str]) -> tuple[Adapter, Optional[Adapter]]:
    name = env.get("PAGER_ADAPTER", "sns_forward") or "sns_forward"
    factory = ADAPTERS.get(name)
    fallback = ADAPTERS["sns_forward"](env)
    if factory is None:
        # An adapter this build does not have is a configuration error. Every page still reaches
        # the email fallback, and the record names the missing adapter.
        class _Missing(Adapter):
            def __init__(self, n: str) -> None:
                self.name = n

            def send(self, page: Page, rendered: Rendered) -> SendResult:
                return SendResult(status="rejected", vendor_status="adapter_not_in_build", error_class="Unconfigured")

        return _Missing(name), fallback
    return factory(env), fallback


def process_record(
    record: dict[str, Any],
    env: dict[str, str],
    primary: Adapter,
    fallback: Optional[Adapter],
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
    emit: Callable[[dict[str, Any]], None] = _emit,
    history: Optional[Callable[[str], Optional[str]]] = None,
    last_alarm_drill: Optional[Callable[[str], Optional[str]]] = None,
) -> dict[str, Any]:
    sns = record.get("Sns", {}) if isinstance(record, dict) else {}
    try:
        alarm_map = json.loads(env.get("PAGER_ALARM_MAP") or "{}")
    except ValueError:
        alarm_map = {}
    if not isinstance(alarm_map, dict):
        alarm_map = {}
    parsed = parse_message(
        sns.get("Message"),
        alarm_map,
        history if history is not None else drill_id_from_history,
        last_alarm_drill if last_alarm_drill is not None else (lambda name: drill_id_from_history(name, latest_alarm_only=True)),
    )
    if isinstance(parsed, Dropped):
        rec = outcome_record(parsed, None, None, sns.get("MessageId"), now())
    else:
        rendered = render(parsed)
        try:
            delay = float(env.get("RETRY_DELAY_SECONDS", MIN_RETRY_DELAY_SECONDS))
        except ValueError:
            delay = MIN_RETRY_DELAY_SECONDS
        outcome = deliver(parsed, rendered, primary, fallback, sleep=sleep, retry_delay=delay)
        rec = outcome_record(parsed, outcome, rendered, sns.get("MessageId"), now())
    notification = notification_fields(sns.get("Message"))
    if notification is not None:
        rec["notification"] = notification
    emit(rec)
    for line in emf_lines(rec, now()):
        emit(line)
    return rec


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    env = dict(os.environ)
    primary, fallback = build_adapters(env)
    records = [process_record(r, env, primary, fallback) for r in event.get("Records", [])]
    rejected = [r for r in records if r.get("outcome") == "rejected"]
    if rejected:
        # Nothing delivered these. Raising hands the invocation to Lambda's async retries and then
        # to the dead-letter queue, whose depth alarm emails the operator (plan Phase 1).
        raise RuntimeError(f"operator pager relay: {len(rejected)} message(s) not delivered by any channel")
    return {"processed": len(records)}
