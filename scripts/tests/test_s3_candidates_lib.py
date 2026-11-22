"""Tests for ``s3_candidates_lib.py``: D8's decision rule and its void-control refusal.

Plan 2026-11-22-fetches-stop-depending-on-upstreams, Phase 1. Stdlib only.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "s3_candidates_lib.py"
_spec = importlib.util.spec_from_file_location("s3_candidates_lib", _MODULE_PATH)
assert _spec and _spec.loader
lib = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lib
_spec.loader.exec_module(lib)

OPS = ["put", "get", "head", "delete", "multipart", "presign", "create_if_absent"]
PINS = {
    "candidate": [
        {"name": "alpha", "version": "1", "governance": "forkable"},
        {"name": "beta", "version": "2", "governance": "forkable"},
    ]
}


def _conformance(failing: tuple[str, ...] = (), exit_code: int | None = None) -> dict:
    ops = [{"op": op, "verdict": "FAIL" if op in failing else "PASS"} for op in OPS]
    passed = sum(1 for o in ops if o["verdict"] == "PASS")
    rec = {"operations": ops, "executed": 7, "passed": passed, "verdict": "PASS" if passed == 7 else "FAIL"}
    if exit_code is not None:
        rec["exit_code"] = exit_code
    return rec


def _candidate(name: str, rss: float, failing: tuple[str, ...] = ()) -> dict:
    return {
        "name": name,
        "licence": {"license_detected": "Apache-2.0", "screen": "PASS"},
        "idle": {"idle_rss_mib": rss, "processes": 1},
        "conformance": _conformance(failing),
        "clients": {"bazel-remote": {"verdict": "PASS"}, "sccache": {"verdict": "PASS"}},
    }


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _write(self, name: str, rec: dict) -> None:
        (self.dir / f"{name}.json").write_text(json.dumps(rec))

    def _controls(self, broken_failing=("create_if_absent", "presign"), broken_exit=1, good_exit=0) -> None:
        self._write("control-broken.conformance", _conformance(broken_failing, broken_exit))
        self._write("control-conforming.conformance", _conformance((), good_exit))

    def test_valid_controls_rank_by_rss_among_seven_of_seven(self) -> None:
        self._controls()
        self._write("alpha", _candidate("alpha", 30.0, failing=("create_if_absent",)))
        self._write("beta", _candidate("beta", 50.0))
        result = lib.decide(PINS, self.dir)
        self.assertTrue(result["controls_valid"])
        self.assertEqual(result["eligible_after_rule_1"], ["beta"])
        self.assertEqual(result["pick"], "beta")
        self.assertFalse(result["premise_falsified"])

    def test_missing_controls_void_the_run(self) -> None:
        self._write("alpha", _candidate("alpha", 30.0))
        result = lib.decide(PINS, self.dir)
        self.assertTrue(result["void"])
        self.assertIsNone(result["pick"])
        self.assertFalse(result["premise_falsified"])

    def test_a_broken_stub_that_passes_presign_voids_the_run(self) -> None:
        self._controls(broken_failing=("create_if_absent",))
        self._write("alpha", _candidate("alpha", 30.0))
        self.assertTrue(lib.decide(PINS, self.dir)["void"])

    def test_a_broken_stub_that_exits_zero_voids_the_run(self) -> None:
        self._controls(broken_exit=0)
        self.assertTrue(lib.decide(PINS, self.dir)["void"])

    def test_a_failing_conforming_stub_voids_the_run(self) -> None:
        self._controls(good_exit=1)
        self.assertTrue(lib.decide(PINS, self.dir)["void"])

    def test_cmd_decide_exits_one_on_void_and_four_on_falsified(self) -> None:
        pins = self.dir / "pins.toml"
        pins.write_text('[[candidate]]\nname = "alpha"\nversion = "1"\ngovernance = "forkable"\n')
        out = self.dir / "decision.json"
        args = ["decide", "--pins", str(pins), "--results-dir", str(self.dir), "--out", str(out)]
        self._write("alpha", _candidate("alpha", 30.0, failing=("presign",)))
        self.assertEqual(lib.main(args), 1)  # no controls yet: void
        self._controls()
        self.assertEqual(lib.main(args), 4)  # valid controls, nobody 7/7: falsified
        self._write("alpha", _candidate("alpha", 30.0))
        self.assertEqual(lib.main(args), 0)
        self.assertEqual(json.loads(out.read_text())["pick"], "alpha")


class CooldownTests(unittest.TestCase):
    def test_cooldown(self) -> None:
        entry = {"released_at": "2026-11-01", "pinned_at": "2026-11-20"}
        self.assertTrue(lib.cooldown_verdict(entry, 14, dt.date(2026, 11, 22))[0])
        entry = {"released_at": "2026-11-10", "pinned_at": "2026-11-20"}
        self.assertFalse(lib.cooldown_verdict(entry, 14, dt.date(2026, 11, 30))[0])
        entry["cooldown_waiver"] = "CVE-0000-0000"
        self.assertTrue(lib.cooldown_verdict(entry, 14, dt.date(2026, 11, 30))[0])


class LicenceTests(unittest.TestCase):
    def test_source_available_markers_are_found(self) -> None:
        spdx, hits = lib.classify_licence("Business Source License 1.1\nLicensor: x")
        self.assertIn("BUSL", hits)
        spdx, hits = lib.classify_licence("   Apache License\n      Version 2.0, January 2004")
        self.assertEqual((spdx, hits), ("Apache-2.0", []))


if __name__ == "__main__":
    unittest.main()
