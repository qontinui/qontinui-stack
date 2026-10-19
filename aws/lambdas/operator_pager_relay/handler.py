"""Operator pager relay: the one subscriber of ``qontinui-<env>-operator-pager``.

Plan: qontinui-dev-notes ``plans/2026-10-14-coord-pages-the-operator-for-five-
conditions-and-names-the-agent-fixing-each.md`` (Phases 1 and 2; D2, D7, D16).

What it does, per SNS record:

1. **Parse** one of three shapes:

   * a CloudWatch alarm state-change notification (metric or composite alarm).
     The alarm name is looked up in ``PAGER_ALARM_MAP`` (rendered by Terraform),
     which gives the condition and the automatic remediation in force;
   * ``page_version: 1`` JSON from an EventBridge input transformer;
   * ``page_version: 1`` JSON published by coord (Phase 3) or by
     ``scripts/operator-pager-drill.sh``.

2. **Enforce the cap.** Any message whose condition is not one of the five in
   :data:`CONDITIONS` is DROPPED. A new condition pages only after a deliberate
   edit of that tuple (plan D7).

3. **Decide drill or real** (plan D16): a composite alarm whose triggering
   child is its ``-drill`` child (the drill id is read from that child's
   history, and the composite's return to OK at the end of a drill is
   suppressed, never paged); an EventBridge event with
   ``source = qontinui.drill``; or a ``page_version`` message whose kind is
   ``drill``/``drill_summary``. Every pager alarm is a composite.

4. **Render** one title (<= 250 chars) and body (<= 1,024 chars) that says what
   happened and which remediation is in force, and ends with
   ``No action needed from you.`` A page informs; it never asks. The rendered
   text is checked against :data:`_ASKING_PATTERNS`; source text that would make
   the page ask is withheld and the check result is logged as ``asks``.

5. **Deliver** through the configured adapter. Phase 1 ships one adapter,
   ``sns_forward``, which forwards the page to the existing email topic under a
   ``[PAGE]`` subject prefix. A 4xx is never retried; a 5xx (or a transport
   error) is retried once after at least 5 s; a vendor adapter that still fails
   falls back to the email topic.

6. **Log one JSON record per message** to stdout. The record doubles as a
   CloudWatch Embedded Metric Format document, so the same line emits
   ``VendorAccepted`` / ``VendorRejected`` / ``FallbackForwarded`` / ``Dropped``
   in namespace ``Qontinui/OperatorPager``. The record carries a fixed set of
   fields; it never carries a credential, a request body or an exception
   message (a vendor error body can echo the token).

boto3 is imported lazily, inside the adapter, so every pure function here is
testable with the standard library and pytest alone.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

# ─── The closed set ──────────────────────────────────────────────────────

# Exactly five. This tuple is the relay half of the cap; coord's
# `PagerCondition` enum is the other half (claim r05-operator-pager-delivers).
CONDITIONS: tuple[str, ...] = (
    "train_stalled",
    "leader_crash_loop",
    "deploy_failed",
    "storage_or_iops",
    "github_budget_exhausted",
)

CONDITION_LABELS: dict[str, str] = {
    "train_stalled": "The merge train has stopped landing",
    "leader_crash_loop": "coord is crash-looping",
    "deploy_failed": "A deploy failed",
    "storage_or_iops": "Database storage or IOPS is at risk",
    "github_budget_exhausted": "GitHub API budget is exhausted",
}

KINDS: tuple[str, ...] = ("page", "update", "resolved", "drill", "drill_summary")

CLOSING_LINE = "No action needed from you."
TITLE_MAX = 250
BODY_MAX = 1024
SNS_SUBJECT_MAX = 100
SUBJECT_PREFIX = "[PAGE] "

RETRY_DELAY_SECS = 5.0

METRIC_NAMESPACE = "Qontinui/OperatorPager"
METRIC_NAMES: tuple[str, ...] = (
    "VendorAccepted",
    "VendorRejected",
    "FallbackForwarded",
    "Dropped",
)

AWS_AGENT_LINE = "Agent dispatch follows from coord; the agent state is not known yet."
WITHHELD_SOURCE_TEXT = "(source text withheld: it was worded as a request)"
WITHHELD_FIELD = "(withheld)"

# A page informs; it never asks (plan D16, decision_record
# operational-work-is-autonomous). Word-bounded so "responder" and
# "--respond-to-alert" do not trip it, and the question mark only counts at
# the end of a sentence so a URL query string does not.
_ASKING_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (label, re.compile(rx, re.IGNORECASE))
    for label, rx in (
        ("please", r"\bplease\b"),
        ("approve", r"\bapprov(?:e|al|ed)\b"),
        ("confirm", r"\bconfirm\b"),
        ("acknowledge", r"\back(?:nowledge)?\b"),
        ("reply", r"\breply\b"),
        ("decide", r"\bdecide\b"),
        ("you-question", r"\b(?:can|could|would|will|should|do) you\b"),
        ("action-required", r"\baction (?:is )?required\b"),
        ("needs-you", r"\bneeds? (?:you|your)\b"),
        ("question-mark", r"\?(?=\s|$)"),
    )
)

_DRILL_REASON = re.compile(r"^\s*DRILL\s+(\S+)")
_ALARM_ARN_NAME = re.compile(r"arn:aws[\w-]*:cloudwatch:[^:]*:\d*:alarm:([^\s,\"]+)")


def contains_asking_words(text: str) -> list[str]:
    """Return the labels of every asking pattern present in ``text``."""
    return [label for label, rx in _ASKING_PATTERNS if rx.search(text)]


# ─── Parsed message ──────────────────────────────────────────────────────


@dataclass
class Page:
    """One message the relay will render and deliver."""

    condition: Optional[str]
    kind: str
    origin: str  # cloudwatch | eventbridge | coord | drill-script | ci
    source: str  # aws.cloudwatch | aws.ecs | aws.rds | qontinui.drill | coord | ...
    signal: str  # alarm name, rule signal, or coord alert key
    what: str = ""
    remediation: str = ""
    remediation_state: str = ""
    agents: str = ""
    dedup_key: str = ""
    alert_ids: list[str] = field(default_factory=list)
    drill: bool = False
    drill_id: Optional[str] = None
    drill_detection: str = "n/a"  # how drill-or-real was decided
    conditions: list[str] = field(default_factory=list)  # drill_summary only


@dataclass
class Drop:
    """A message the relay does not deliver, and why.

    ``suppressed`` marks the one expected non-delivery: a composite returning to
    OK because its drill child did. It is not an error, so it emits no
    ``Dropped`` metric; every other Drop is something that should not have
    reached the pager.
    """

    reason: str
    condition: Optional[str] = None
    kind: Optional[str] = None
    origin: str = "unknown"
    source: str = "unknown"
    signal: str = ""
    drill_id: Optional[str] = None
    alert_ids: list[str] = field(default_factory=list)
    suppressed: bool = False


# Returns the recent state-history items of one alarm (DescribeAlarmHistory's
# `AlarmHistoryItems` shape) around a state-change time, or None when the read
# failed.
DrillHistory = Callable[[str, str], Optional[list]]


def load_alarm_map(raw: Optional[str]) -> dict[str, dict[str, Any]]:
    """Parse the Terraform-rendered ``PAGER_ALARM_MAP``; malformed means empty.

    Empty is the safe reading: every alarm is then unmapped and dropped, the
    ``Dropped`` alarm fires on the email topic, and nothing pages under a guessed
    condition.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_message(
    message: str,
    alarm_map: dict[str, dict[str, Any]],
    drill_history: Optional[DrillHistory] = None,
) -> "Page | Drop":
    """Turn one SNS ``Message`` string into a :class:`Page` or a :class:`Drop`."""
    try:
        doc = json.loads(message)
    except (TypeError, ValueError):
        return Drop(reason="unparseable")
    if not isinstance(doc, dict):
        return Drop(reason="unparseable")
    if "AlarmName" in doc and "NewStateValue" in doc:
        return parse_alarm(doc, alarm_map, drill_history)
    if doc.get("page_version") == 1:
        return parse_page_v1(doc)
    return Drop(reason="unparseable")


def triggering_children(doc: dict[str, Any]) -> list[str]:
    """Names of the child alarms a composite notification says triggered it.

    Reads ``TriggeringChildren[].Arn`` when the notification carries it, and
    otherwise every alarm ARN named in ``NewStateReason`` (the composite form
    is "arn:...:alarm:<child> transitioned to ALARM at ..."). Pinning this to a
    captured real composite notification is a post-apply step (plan D16).
    """
    names: list[str] = []
    children = doc.get("TriggeringChildren")
    if isinstance(children, list):
        for child in children:
            arn = child.get("Arn") if isinstance(child, dict) else None
            if isinstance(arn, str):
                m = _ALARM_ARN_NAME.search(arn)
                if m:
                    names.append(m.group(1))
    if not names:
        reason = doc.get("NewStateReason")
        if isinstance(reason, str):
            names = _ALARM_ARN_NAME.findall(reason)
    return names


def drill_id_from_history(items: Optional[list]) -> Optional[str]:
    """The drill id of the newest ``ALARM`` transition whose reason is ``DRILL <id>``.

    ``items`` is DescribeAlarmHistory's ``AlarmHistoryItems`` for a drill child,
    already windowed around the composite's state change. ``HistoryData`` is a
    JSON string whose ``newState.stateReason`` carries the reason the drill
    script set.
    """
    best: Optional[tuple[str, str]] = None
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            data = json.loads(item.get("HistoryData") or "{}")
        except (TypeError, ValueError):
            continue
        new = data.get("newState") if isinstance(data, dict) else None
        if not isinstance(new, dict) or new.get("stateValue") != "ALARM":
            continue
        m = _DRILL_REASON.match(str(new.get("stateReason") or ""))
        if not m:
            continue
        stamp = str(item.get("Timestamp") or "")
        if best is None or stamp > best[0]:
            best = (stamp, m.group(1))
    return best[1] if best else None


def parse_alarm(
    doc: dict[str, Any],
    alarm_map: dict[str, dict[str, Any]],
    drill_history: Optional[DrillHistory] = None,
) -> "Page | Drop":
    name = str(doc.get("AlarmName", ""))
    entry = alarm_map.get(name)
    if not isinstance(entry, dict):
        return Drop(reason="unmapped_alarm", origin="cloudwatch", source="aws.cloudwatch", signal=name)
    condition = entry.get("condition")
    if condition not in CONDITIONS:
        return Drop(
            reason="condition_not_in_the_five",
            condition=str(condition),
            origin="cloudwatch",
            source="aws.cloudwatch",
            signal=name,
        )
    state = doc.get("NewStateValue")
    reason = str(doc.get("NewStateReason") or "")
    if state == "ALARM":
        kind = "page"
    elif state == "OK":
        kind = "resolved"
    else:
        return Drop(
            reason="state_not_paged",
            condition=condition,
            origin="cloudwatch",
            source="aws.cloudwatch",
            signal=name,
        )

    # Every pager alarm is a composite with a dedicated `-drill` child (plan
    # D16): it is a drill iff the child that moved the composite is that drill
    # child. The notification names the child (TriggeringChildren, or the
    # reason). The drill ID lives only in the drill child's own state reason,
    # so it is read from that child's recent history. A composite naming no
    # child is decided from the same history read; if that read failed too it
    # pages as REAL, because a parser gap must never silence a real page.
    drill = False
    drill_id: Optional[str] = None
    drill_child = entry.get("drill_child")
    history: Optional[list] = None
    if drill_child and drill_history is not None:
        history = drill_history(str(drill_child), str(doc.get("StateChangeTime") or ""))
    hist_id = drill_id_from_history(history)
    children = triggering_children(doc)
    if not drill_child:
        detection = "no_drill_child"
    elif children:
        detection = "composite_child"
        drill = any(c == drill_child or c.endswith("-drill") for c in children)
        drill_id = hist_id if drill else None
    elif history is not None:
        detection = "composite_history"
        drill = hist_id is not None
        drill_id = hist_id
    else:
        detection = "composite_child_unnamed"
    if drill and state == "OK":
        # The drill child returned to OK and the composite followed: the end of
        # a drill, not a resolution. Deliver nothing.
        return Drop(
            reason="drill_returned_to_ok",
            condition=condition,
            kind="drill",
            origin="cloudwatch",
            source="aws.cloudwatch",
            signal=name,
            drill_id=drill_id,
            suppressed=True,
        )
    if drill:
        kind = "drill"

    when = doc.get("StateChangeTime") or ""
    what = f"Alarm {name} is {state}" + (f" since {when}" if when else "") + "."
    if reason and not drill:
        what += f" Reason: {reason}"
    return Page(
        condition=condition,
        kind=kind,
        origin="cloudwatch",
        source="aws.cloudwatch",
        signal=name,
        what=what,
        remediation=str(entry.get("remediation") or ""),
        remediation_state="in force",
        agents=AWS_AGENT_LINE,
        dedup_key=f"{condition}:{name}",
        drill=drill,
        drill_id=drill_id,
        drill_detection=detection,
    )


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v is not None]
    if value is None or value == "":
        return []
    return [str(value)]


def _drill_id_from_text(*texts: Any) -> Optional[str]:
    for text in texts:
        if isinstance(text, str):
            m = _DRILL_REASON.match(text)
            if m:
                return m.group(1)
    return None


def parse_page_v1(doc: dict[str, Any]) -> "Page | Drop":
    """``page_version: 1`` from EventBridge, coord, CI or the drill script."""
    origin = str(doc.get("origin") or "unknown")
    source = str(doc.get("source") or origin)
    signal = str(doc.get("signal") or "")
    kind = doc.get("kind") or "page"
    alert_ids = _str_list(doc.get("alert_ids"))
    if kind not in KINDS:
        return Drop(reason="unknown_kind", kind=str(kind), origin=origin, source=source, signal=signal)

    is_drill_source = source == "qontinui.drill"
    drill_id = doc.get("drill_id") or _drill_id_from_text(
        doc.get("reason"), doc.get("message"), doc.get("what")
    )
    drill_id = str(drill_id) if drill_id else None

    if kind == "drill_summary":
        conditions = _str_list(doc.get("conditions"))
        bad = [c for c in conditions if c not in CONDITIONS]
        if not conditions or bad:
            return Drop(
                reason="condition_not_in_the_five",
                condition=",".join(bad) or None,
                kind=kind,
                origin=origin,
                source=source,
                signal=signal,
                drill_id=drill_id,
            )
        return Page(
            condition=None,
            kind=kind,
            origin=origin,
            source=source,
            signal=signal or "drill-summary",
            what=str(doc.get("what") or ""),
            dedup_key=f"drill_summary:{drill_id}",
            drill=True,
            drill_id=drill_id,
            drill_detection="kind",
            conditions=conditions,
        )

    condition = doc.get("condition")
    if condition not in CONDITIONS:
        return Drop(
            reason="condition_not_in_the_five",
            condition=None if condition is None else str(condition),
            kind=str(kind),
            origin=origin,
            source=source,
            signal=signal,
            drill_id=drill_id,
            alert_ids=alert_ids,
        )

    drill = kind == "drill" or is_drill_source
    if drill:
        kind = "drill"
    detection = "kind" if doc.get("kind") == "drill" else ("event_source" if is_drill_source else "n/a")

    what = str(doc.get("what") or "")
    if not what:
        # EventBridge templates carry the raw event fields; compose from them.
        parts = []
        event_name = doc.get("event_name") or doc.get("event_id")
        if event_name:
            parts.append(str(event_name))
        for key in ("reason", "message"):
            if doc.get(key):
                parts.append(str(doc[key]))
        resources = _str_list(doc.get("resources"))
        if doc.get("source_identifier"):
            resources.append(str(doc["source_identifier"]))
        if resources:
            parts.append("on " + ", ".join(resources))
        if doc.get("time"):
            parts.append(f"at {doc['time']}")
        what = " ".join(parts)

    agents = str(doc.get("agents") or "")
    if not agents and origin == "eventbridge":
        agents = AWS_AGENT_LINE

    return Page(
        condition=condition,
        kind=str(kind),
        origin=origin,
        source=source,
        signal=signal,
        what=what,
        remediation=str(doc.get("remediation") or ""),
        remediation_state=str(doc.get("remediation_state") or ""),
        agents=agents,
        dedup_key=str(doc.get("dedup_key") or f"{condition}:{signal}"),
        alert_ids=alert_ids,
        drill=drill,
        drill_id=drill_id,
        drill_detection=detection,
    )


# ─── Rendering ───────────────────────────────────────────────────────────


@dataclass
class Rendered:
    title: str
    body: str
    asks: bool
    source_text_withheld: bool


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _title_for(page: Page) -> str:
    if page.kind == "drill_summary":
        return f"DRILL {page.drill_id or 'unidentified'} summary: {len(page.conditions)} conditions"
    label = CONDITION_LABELS.get(page.condition or "", page.condition or "")
    if page.kind == "drill":
        return f"DRILL {page.drill_id or 'unidentified'}: {label}"
    if page.kind == "resolved":
        return f"Resolved: {label}"
    if page.kind == "update":
        return f"Update: {label}"
    return label


def _lines_for(page: Page, what: str) -> list[str]:
    lines: list[str] = []
    if page.kind == "drill_summary":
        lines.append("Drill summary for: " + ", ".join(page.conditions) + ".")
        if what:
            lines.append(what)
        lines.append("This was a drill of the operator pager; nothing is wrong.")
        return lines
    if page.kind == "drill":
        lines.append("This is a drill of the operator pager; nothing is wrong.")
    if what:
        lines.append(f"What happened: {what}")
    if page.remediation:
        state = f" ({page.remediation_state})" if page.remediation_state else ""
        lines.append(f"Automatic remediation: {page.remediation}{state}.")
    if page.agents:
        lines.append(f"Agents: {page.agents}")
    lines.append(f"Signal: {page.signal or 'unnamed'} ({page.source}).")
    return lines


def _assemble(page: Page, what: str) -> tuple[str, str]:
    title = _clip(_title_for(page), TITLE_MAX)
    head = "\n".join(_lines_for(page, what))
    # The closing line is never truncated away: clip the head to fit it.
    room = BODY_MAX - len(CLOSING_LINE) - 1
    body = _clip(head, room) + "\n" + CLOSING_LINE
    return title, body


def render(page: Page) -> Rendered:
    """Render a page; withhold source text that would make it ask."""
    title, body = _assemble(page, page.what)
    withheld = False
    if contains_asking_words(title + "\n" + body):
        # Withhold EVERY field that came from outside the relay, not only
        # `what`: a page_version 1 publisher also supplies remediation,
        # remediation_state, agents, signal and drill_id. If the page still
        # asks after this, the handler drops it (reason "asks").
        withheld = True
        page = replace(
            page,
            what=WITHHELD_SOURCE_TEXT,
            remediation=WITHHELD_FIELD if page.remediation else "",
            remediation_state="",
            agents=WITHHELD_FIELD if page.agents else "",
            signal=WITHHELD_FIELD,
            drill_id=None,
        )
        title, body = _assemble(page, page.what)
    asks = bool(contains_asking_words(title + "\n" + body))
    return Rendered(title=title, body=body, asks=asks, source_text_withheld=withheld)


def sns_subject(title: str) -> str:
    """SNS email subject: ASCII printable, no newlines, <= 100 chars."""
    ascii_title = title.encode("ascii", "replace").decode("ascii")
    ascii_title = re.sub(r"[\x00-\x1f\x7f]+", " ", ascii_title)
    return _clip(SUBJECT_PREFIX + ascii_title, SNS_SUBJECT_MAX).replace("…", ".")


# ─── Delivery ────────────────────────────────────────────────────────────


@dataclass
class AdapterResponse:
    """What an adapter observed. ``status`` None means a transport error."""

    status: Optional[int]
    ref: Optional[str] = None


CONNECT_TIMEOUT_SECS = 3
READ_TIMEOUT_SECS = 7


def boto_config() -> Any:
    """One attempt per AWS call, with short timeouts.

    The relay's own retry rule (4xx never; 5xx once, after at least 5 s) is
    the only retry policy. Worst case per message, every call at its timeout:
    one history read, two adapter calls and one fallback, each at most 3 + 7
    s, plus the 5 s sleep. That is 45 s, well inside the Lambda's 90 s
    timeout, so the message's one record is always written.
    """
    from botocore.config import Config

    return Config(
        retries={"max_attempts": 1, "mode": "standard"},
        connect_timeout=CONNECT_TIMEOUT_SECS,
        read_timeout=READ_TIMEOUT_SECS,
    )


class CloudWatchDrillHistory:
    """Read a drill child's recent state history (cloudwatch:DescribeAlarmHistory).

    The window runs from 10 minutes before the composite's state change to 1
    minute after it. Any failure returns None, which the parser reads as
    "unknown", never as "not a drill".
    """

    def __init__(self, client: Any = None):
        self._client = client

    def __call__(self, alarm_name: str, change_time: str) -> Optional[list]:
        import datetime as dt

        try:
            if self._client is None:
                import boto3

                self._client = boto3.client("cloudwatch", config=boto_config())
            try:
                at = dt.datetime.strptime(change_time, "%Y-%m-%dT%H:%M:%S.%f%z")
            except ValueError:
                at = dt.datetime.now(dt.timezone.utc)
            resp = self._client.describe_alarm_history(
                AlarmName=alarm_name,
                HistoryItemType="StateUpdate",
                StartDate=at - dt.timedelta(minutes=10),
                EndDate=at + dt.timedelta(minutes=1),
                MaxRecords=20,
                ScanBy="TimestampDescending",
            )
            items = resp.get("AlarmHistoryItems")
            return items if isinstance(items, list) else None
        except Exception:  # noqa: BLE001 — UNKNOWN, never "not a drill"
            return None


class SnsForwardAdapter:
    """Publish the page to an SNS topic (the existing email topic).

    Phase 1's only adapter, and the fallback for any later vendor adapter.
    """

    name = "sns_forward"

    def __init__(self, topic_arn: str, client: Any = None):
        self.topic_arn = topic_arn
        self._client = client

    def _sns(self) -> Any:
        if self._client is None:
            import boto3  # lazy: keeps the pure functions stdlib-testable

            self._client = boto3.client("sns", config=boto_config())
        return self._client

    def send(self, rendered: Rendered, page: Page) -> AdapterResponse:
        if not self.topic_arn:
            return AdapterResponse(status=None)
        try:
            resp = self._sns().publish(
                TopicArn=self.topic_arn,
                Subject=sns_subject(rendered.title),
                Message=rendered.title + "\n\n" + rendered.body,
            )
        except Exception as exc:  # noqa: BLE001 — classify, never log the message
            status = getattr(exc, "response", {}) or {}
            code = status.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(status, dict) else None
            return AdapterResponse(status=code if isinstance(code, int) else None)
        meta = resp.get("ResponseMetadata", {}) if isinstance(resp, dict) else {}
        code = meta.get("HTTPStatusCode", 200)
        return AdapterResponse(status=code if isinstance(code, int) else 200, ref=resp.get("MessageId"))


def _accepted(r: AdapterResponse) -> bool:
    return r.status is not None and 200 <= r.status < 300


def _permanent(r: AdapterResponse) -> bool:
    return r.status is not None and 400 <= r.status < 500


@dataclass
class Delivery:
    outcome: str  # accepted | fallback | rejected
    adapter: str
    vendor_status: Optional[int]
    vendor_ref: Optional[str]
    attempts: int
    fallback_status: Optional[int] = None


def deliver(
    rendered: Rendered,
    page: Page,
    adapter: Any,
    fallback: Any,
    sleep: Callable[[float], None] = time.sleep,
) -> Delivery:
    """Send through ``adapter``; 4xx never retried, 5xx once after >= 5 s.

    When the primary adapter is not itself the email forward, a final failure
    falls back to ``fallback`` (the email topic).
    """
    attempts = 1
    resp = adapter.send(rendered, page)
    if not _accepted(resp) and not _permanent(resp):
        sleep(RETRY_DELAY_SECS)
        attempts = 2
        resp = adapter.send(rendered, page)
    if _accepted(resp):
        return Delivery("accepted", adapter.name, resp.status, resp.ref, attempts)
    if fallback is not None and fallback.name != adapter.name:
        fb = fallback.send(rendered, page)
        if _accepted(fb):
            return Delivery("fallback", adapter.name, resp.status, fb.ref, attempts, fb.status)
        return Delivery("rejected", adapter.name, resp.status, None, attempts, fb.status)
    return Delivery("rejected", adapter.name, resp.status, None, attempts)


# ─── The outcome record (one JSON line per message, EMF-shaped) ──────────


def outcome_record(
    *,
    environment: str,
    sns_message_id: Optional[str],
    page: Optional[Page],
    drop: Optional[Drop],
    rendered: Optional[Rendered],
    delivery: Optional[Delivery],
    now_ms: int,
) -> dict[str, Any]:
    """Build the record. Fixed fields only: no body, no secret, no exception text."""
    metrics = dict.fromkeys(METRIC_NAMES, 0)
    if drop is not None:
        outcome = "suppressed" if drop.suppressed else "dropped"
        if not drop.suppressed:
            metrics["Dropped"] = 1
        src: Any = drop
    else:
        assert page is not None and delivery is not None
        outcome = delivery.outcome
        src = page
        if outcome == "accepted":
            metrics["VendorAccepted"] = 1
        elif outcome == "fallback":
            metrics["VendorRejected"] = 1
            metrics["FallbackForwarded"] = 1
        else:
            metrics["VendorRejected"] = 1

    record: dict[str, Any] = {
        "record": "operator_pager_relay",
        "record_version": 1,
        "sns_message_id": sns_message_id,
        "condition": src.condition,
        "kind": src.kind,
        "origin": src.origin,
        "source": src.source,
        "signal": src.signal,
        "dedup_key": getattr(src, "dedup_key", None) or None,
        "alert_ids": list(src.alert_ids),
        "drill": bool(getattr(src, "drill", False)) or src.source == "qontinui.drill",
        "drill_id": src.drill_id,
        "outcome": outcome,
    }
    if drop is not None:
        record["drop_reason"] = drop.reason
        record["adapter"] = None
        record["vendor_status"] = None
        record["vendor_ref"] = None
    else:
        assert page is not None and delivery is not None and rendered is not None
        record["drill_detection"] = page.drill_detection
        record["conditions"] = list(page.conditions)
        record["adapter"] = delivery.adapter
        record["vendor_status"] = delivery.vendor_status
        record["vendor_ref"] = delivery.vendor_ref
        record["attempts"] = delivery.attempts
        record["fallback_used"] = delivery.outcome == "fallback"
        record["fallback_status"] = delivery.fallback_status
        record["remediation_named"] = page.remediation or None
        record["asks"] = rendered.asks
        record["source_text_withheld"] = rendered.source_text_withheld
        record["title"] = rendered.title

    record["Environment"] = environment
    record.update(metrics)
    record["_aws"] = {
        "Timestamp": now_ms,
        "CloudWatchMetrics": [
            {
                "Namespace": METRIC_NAMESPACE,
                "Dimensions": [["Environment"]],
                "Metrics": [{"Name": n, "Unit": "Count"} for n in METRIC_NAMES],
            }
        ],
    }
    return record


# ─── Entry point ─────────────────────────────────────────────────────────


class DeliveryFailed(RuntimeError):
    """Raised after logging when nothing accepted the page.

    The invocation then counts as a Lambda error (the relay-errors alarm on the
    email topic) and the message goes straight to the dead-letter queue.
    Lambda's own async retries are OFF (aws_lambda_function_event_invoke_config
    maximum_retry_attempts = 0): a retry would re-send a page a channel may
    already have taken, and write a second record for one message."""


def _default_adapter(name: str, email_topic_arn: str) -> Any:
    if name == "sns_forward":
        return SnsForwardAdapter(email_topic_arn)
    raise ValueError(f"unknown adapter {name!r}")


def handler(
    event: dict[str, Any],
    context: Any = None,
    *,
    adapter: Any = None,
    fallback: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[str], None] = print,
    now_ms: Optional[Callable[[], int]] = None,
    drill_history: Optional[DrillHistory] = None,
) -> dict[str, Any]:
    """Lambda entry point (``handler.handler``). Keyword args are test seams."""
    environment = os.environ.get("ENVIRONMENT", "unknown")
    email_topic_arn = os.environ.get("EMAIL_TOPIC_ARN", "")
    alarm_map = load_alarm_map(os.environ.get("PAGER_ALARM_MAP"))
    if adapter is None:
        adapter = _default_adapter(os.environ.get("PAGER_ADAPTER", "sns_forward"), email_topic_arn)
    if fallback is None:
        fallback = SnsForwardAdapter(email_topic_arn)
    clock = now_ms or (lambda: int(time.time() * 1000))
    if drill_history is None:
        drill_history = CloudWatchDrillHistory()

    results: list[dict[str, Any]] = []
    failed = False
    for rec in event.get("Records", []) or []:
        sns = rec.get("Sns", {}) if isinstance(rec, dict) else {}
        message_id = sns.get("MessageId")
        parsed = parse_message(sns.get("Message", ""), alarm_map, drill_history)
        rendered = render(parsed) if isinstance(parsed, Page) else None
        if isinstance(parsed, Page) and rendered is not None and rendered.asks:
            # Even with every source field withheld the page would ask: never send it.
            parsed = Drop(
                reason="asks",
                condition=parsed.condition,
                kind=parsed.kind,
                origin=parsed.origin,
                source=parsed.source,
                signal=parsed.signal,
                drill_id=parsed.drill_id,
                alert_ids=parsed.alert_ids,
            )
        if isinstance(parsed, Drop):
            record = outcome_record(
                environment=environment,
                sns_message_id=message_id,
                page=None,
                drop=parsed,
                rendered=None,
                delivery=None,
                now_ms=clock(),
            )
        else:
            assert rendered is not None
            delivery = deliver(rendered, parsed, adapter, fallback, sleep=sleep)
            record = outcome_record(
                environment=environment,
                sns_message_id=message_id,
                page=parsed,
                drop=None,
                rendered=rendered,
                delivery=delivery,
                now_ms=clock(),
            )
            failed = failed or delivery.outcome == "rejected"
        emit(json.dumps(record, sort_keys=True))
        results.append({"outcome": record["outcome"], "condition": record["condition"], "kind": record["kind"]})
    if failed:
        raise DeliveryFailed("no channel accepted the page; see the relay record")
    return {"results": results}
