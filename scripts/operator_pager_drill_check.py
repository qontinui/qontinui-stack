#!/usr/bin/env python3
"""Verdict for one operator-pager drill, from the relay's own outcome records.

Plan ``2026-10-14-coord-pages-the-operator-for-five-conditions-and-names-the-agent-fixing-each``,
Phase 1. ``scripts/operator-pager-drill.sh`` pipes ``aws logs filter-log-events --output json`` for the
relay's log group into this script.

The relay prints one JSON record per message (``record = operator_pager_relay``). A drill passes when
EVERY drilled condition has a record for this drill id whose outcome is ``accepted``. ``fallback`` means
the configured adapter failed and the email fallback delivered: a page reached a human, but the path
under test did not, so it passes only with ``--allow-fallback``. A condition with no record at all is
``missing``, which is UNKNOWN and never a pass.

Standard library only (qontinui-stack CI installs pytest and nothing else).

Exit codes: 0 every condition accepted; 1 at least one condition not accepted; 2 bad usage or input.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Iterable, Iterator

RECORD_NAME = "operator_pager_relay"
PASSING = ("accepted",)


def relay_records(doc: Any) -> Iterator[dict[str, Any]]:
    """The relay records inside a ``filter-log-events`` JSON document. Non-JSON lines (Lambda's own
    START/END/REPORT lines) and EMF metric lines are skipped."""
    events = doc.get("events", []) if isinstance(doc, dict) else []
    for event in events:
        message = event.get("message") if isinstance(event, dict) else None
        if not isinstance(message, str):
            continue
        try:
            rec = json.loads(message)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("record") == RECORD_NAME:
            yield rec


def verdict(records: Iterable[dict[str, Any]], drill_id: str, conditions: list[str], allow_fallback: bool = False) -> tuple[bool, dict[str, str]]:
    """Per condition: the best outcome seen for this drill id. ``missing`` when none was seen."""
    passing = PASSING + (("fallback",) if allow_fallback else ())
    rank = {"accepted": 3, "fallback": 2, "rejected": 1, "dropped": 0}
    best: dict[str, str] = {c: "missing" for c in conditions}
    for rec in records:
        if rec.get("drill_id") != drill_id:
            continue
        cond = rec.get("condition")
        key = cond if cond is not None else "summary"
        if key not in best:
            continue
        outcome = str(rec.get("outcome"))
        if rank.get(outcome, -1) > rank.get(best[key], -1):
            best[key] = outcome
    ok = all(v in passing for v in best.values())
    return ok, best


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drill-id", required=True)
    ap.add_argument("--conditions", required=True, help="comma-separated; 'summary' names the drill summary")
    ap.add_argument("--allow-fallback", action="store_true")
    args = ap.parse_args(argv)
    conditions = [c for c in (s.strip() for s in args.conditions.split(",")) if c]
    if not conditions:
        print("operator_pager_drill_check: no conditions", file=sys.stderr)
        return 2
    try:
        doc = json.load(sys.stdin)
    except ValueError as exc:
        print(f"operator_pager_drill_check: stdin is not JSON ({exc})", file=sys.stderr)
        return 2
    ok, best = verdict(relay_records(doc), args.drill_id, conditions, args.allow_fallback)
    for cond in conditions:
        print(f"{cond:26s} {best[cond]}")
    print(f"drill {args.drill_id}: {'PASS' if ok else 'NOT ACCEPTED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
