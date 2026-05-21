"""Tests for ``symbol-claims-by-machine.py``.

Runnable via either:

    python -m pytest qontinui-stack/scripts/tests/test_symbol_claims_by_machine.py
    python -m unittest qontinui-stack.scripts.tests.test_symbol_claims_by_machine

The tests stub the coord HTTP layer at the module-level
``fetch_holders`` so we never hit a live coord. Each test owns its own
temp dir for the machine.json + cwd substrate.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


# ---------------------------------------------------------------------------
# Load symbol-claims-by-machine.py as a module (hyphenated filename → spec_from_file)
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_SCRIPT_PATH = _SCRIPTS_DIR / "symbol-claims-by-machine.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("symbol_claims_by_machine", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["symbol_claims_by_machine"] = mod
    spec.loader.exec_module(mod)
    return mod


scbm = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _holder(resource_key: str, machine_id: str, ttl: int = 200) -> dict:
    return {
        "kind": "symbol",
        "machine_id": machine_id,
        "resource_key": resource_key,
        "ttl_seconds": ttl,
    }


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class ParseSymbolTests(unittest.TestCase):
    def test_simple_three_part_key(self) -> None:
        self.assertEqual(
            scbm._parse_symbol("repo:src/foo.rs:bar"),
            "bar",
        )

    def test_rust_qualified_method(self) -> None:
        # The watcher emits keys like ``Type::method`` for impl methods.
        # rsplit(':', 1) gives the last segment.
        self.assertEqual(
            scbm._parse_symbol("repo:src/foo.rs:Type::method"),
            "Type::method".rsplit(":", 1)[-1],
        )

    def test_no_colon(self) -> None:
        self.assertEqual(scbm._parse_symbol("bare"), "bare")


class CollectConflictsTests(unittest.TestCase):
    def test_empty_holders_emits_nothing(self) -> None:
        with mock.patch.object(scbm, "fetch_holders", return_value=[]):
            conflicts, errors = scbm.collect_conflicts(
                "repo", ["src/foo.rs"], "local-uuid"
            )
        self.assertEqual(conflicts, [])
        self.assertEqual(errors, [])

    def test_filters_self(self) -> None:
        holders = [_holder("repo:src/foo.rs:bar", "local-uuid")]
        with mock.patch.object(scbm, "fetch_holders", return_value=holders):
            conflicts, errors = scbm.collect_conflicts(
                "repo", ["src/foo.rs"], "local-uuid"
            )
        self.assertEqual(conflicts, [])
        self.assertEqual(errors, [])

    def test_keeps_other_machines(self) -> None:
        holders = [
            _holder("repo:src/foo.rs:bar", "OTHER-uuid", ttl=120),
            _holder("repo:src/foo.rs:baz", "local-uuid"),  # filtered
        ]
        with mock.patch.object(scbm, "fetch_holders", return_value=holders):
            conflicts, errors = scbm.collect_conflicts(
                "repo", ["src/foo.rs"], "local-uuid"
            )
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["symbol"], "bar")
        self.assertEqual(conflicts[0]["machine_id"], "OTHER-uuid")
        self.assertEqual(conflicts[0]["ttl_seconds"], 120)
        self.assertEqual(conflicts[0]["file"], "src/foo.rs")
        self.assertEqual(errors, [])

    def test_no_local_id_keeps_everything(self) -> None:
        """When machine.json is missing, listing all holders is acceptable.

        The hook just surfaces information; the operator decides.
        """
        holders = [
            _holder("repo:src/foo.rs:bar", "any-uuid-1"),
            _holder("repo:src/foo.rs:baz", "any-uuid-2"),
        ]
        with mock.patch.object(scbm, "fetch_holders", return_value=holders):
            conflicts, _ = scbm.collect_conflicts(
                "repo", ["src/foo.rs"], None
            )
        self.assertEqual(len(conflicts), 2)

    def test_transport_failure_per_file_isolated(self) -> None:
        """One bad file shouldn't suppress the others."""

        def fake_fetch(repo: str, rel_file: str, **_) -> list[dict]:
            if rel_file == "src/broken.rs":
                raise RuntimeError("simulated 500")
            return [_holder(f"repo:{rel_file}:fn", "OTHER")]

        with mock.patch.object(scbm, "fetch_holders", side_effect=fake_fetch):
            conflicts, errors = scbm.collect_conflicts(
                "repo",
                ["src/ok.rs", "src/broken.rs", "src/also-ok.rs"],
                "local",
            )
        # Two OK files → two conflicts.
        self.assertEqual(len(conflicts), 2)
        self.assertEqual(
            sorted(c["file"] for c in conflicts),
            ["src/also-ok.rs", "src/ok.rs"],
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("src/broken.rs", errors[0])
        self.assertIn("simulated 500", errors[0])


class RenderTextTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(
            scbm.render_text([]),
            "No conflicting symbol claims.",
        )

    def test_table_alignment(self) -> None:
        conflicts = [
            {
                "symbol": "fn_a",
                "file": "src/x.rs",
                "machine_id": "uuid-1",
                "ttl_seconds": 100,
            },
            {
                "symbol": "very_long_symbol_name",
                "file": "src/y.rs",
                "machine_id": "uuid-2",
                "ttl_seconds": 200,
            },
        ]
        out = scbm.render_text(conflicts)
        lines = out.splitlines()
        # Header + separator + 2 rows = 4 lines.
        self.assertEqual(len(lines), 4)
        self.assertIn("Symbol", lines[0])
        self.assertIn("File", lines[0])
        self.assertIn("Held by", lines[0])
        self.assertIn("TTL", lines[0])
        # All rows are the same width (left-justified).
        widths = {len(line) for line in lines}
        self.assertEqual(len(widths), 1)


class RenderJsonTests(unittest.TestCase):
    def test_includes_both_conflicts_and_errors(self) -> None:
        out = scbm.render_json(
            [
                {
                    "symbol": "f",
                    "file": "x.rs",
                    "machine_id": "u",
                    "ttl_seconds": 1,
                    "resource_key": "r:x.rs:f",
                }
            ],
            ["x.rs: oops"],
        )
        parsed = json.loads(out)
        self.assertEqual(len(parsed["conflicts"]), 1)
        self.assertEqual(parsed["errors"], ["x.rs: oops"])


class MachineJsonTests(unittest.TestCase):
    def test_reads_device_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "machine.json"
            p.write_text(
                json.dumps({"device_id": "abc-uuid", "hostname": "h"}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"QONTINUI_MACHINE_JSON_PATH": str(p)}
            ):
                self.assertEqual(scbm._read_local_machine_id(), "abc-uuid")

    def test_falls_back_to_machine_id(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "machine.json"
            p.write_text(
                json.dumps({"machine_id": "legacy-uuid"}),
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ, {"QONTINUI_MACHINE_JSON_PATH": str(p)}
            ):
                self.assertEqual(scbm._read_local_machine_id(), "legacy-uuid")

    def test_missing_file_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {"QONTINUI_MACHINE_JSON_PATH": str(Path(td) / "nope.json")},
            ):
                self.assertIsNone(scbm._read_local_machine_id())

    def test_malformed_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "machine.json"
            p.write_text("not json", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"QONTINUI_MACHINE_JSON_PATH": str(p)}
            ):
                self.assertIsNone(scbm._read_local_machine_id())


class MainCliTests(unittest.TestCase):
    def test_explicit_repo_and_file_skip_git(self) -> None:
        """``--repo`` + ``--file`` lets the script run outside a git tree.

        The hook calls this with explicit args, so we don't need a worktree.
        """
        with tempfile.TemporaryDirectory() as td:
            # No git repo here.
            machine_json = Path(td) / "machine.json"
            machine_json.write_text(
                json.dumps({"device_id": "local-uuid"}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "QONTINUI_MACHINE_JSON_PATH": str(machine_json),
                    "COORD_HTTP_URL": "http://invalid.invalid:1",
                },
            ):
                with mock.patch.object(
                    scbm,
                    "fetch_holders",
                    return_value=[_holder("r:foo.rs:bar", "OTHER")],
                ):
                    rc = scbm.main(
                        [
                            "--repo",
                            "r",
                            "--file",
                            "foo.rs",
                            "--json",
                            "--cwd",
                            td,
                        ]
                    )
        # Non-zero because a conflict was found.
        self.assertEqual(rc, 1)

    def test_no_conflicts_exit_zero(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            machine_json = Path(td) / "machine.json"
            machine_json.write_text(
                json.dumps({"device_id": "local-uuid"}), encoding="utf-8"
            )
            with mock.patch.dict(
                os.environ, {"QONTINUI_MACHINE_JSON_PATH": str(machine_json)}
            ):
                with mock.patch.object(scbm, "fetch_holders", return_value=[]):
                    rc = scbm.main(
                        [
                            "--repo",
                            "r",
                            "--file",
                            "foo.rs",
                            "--cwd",
                            td,
                        ]
                    )
        self.assertEqual(rc, 0)

    def test_outside_git_with_no_explicit_files_errors(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rc = scbm.main(["--cwd", td])
        # Exit 2 — not in a git worktree, no --file given.
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
