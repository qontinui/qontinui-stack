"""Tests for ``plan-graph.py``.

Runnable via either:

    python -m pytest qontinui-stack/scripts/tests/test_plan_graph.py
    python -m unittest qontinui-stack.scripts.tests.test_plan_graph

Each test allocates its own temp directory of fixture plans so the assertions
are hermetic and never depend on the operator's real ``D:/qontinui-root/plans``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Load plan-graph.py as a module (hyphenated filename → spec_from_file)
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_SCRIPT_PATH = _SCRIPTS_DIR / "plan-graph.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("plan_graph", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_graph"] = mod
    spec.loader.exec_module(mod)
    return mod


pg = _load_module()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_plan(
    dir_path: Path,
    stem: str,
    status: str = "DRAFT",
    deps: list[str] | None = None,
    *,
    malformed: bool = False,
) -> Path:
    """Write a minimal plan file with a status blockquote."""
    path = dir_path / f"{stem}.md"
    if malformed:
        # No status blockquote at all.
        path.write_text(
            textwrap.dedent(
                f"""\
                # Plan: {stem}

                This plan deliberately has no status blockquote.
                """
            ),
            encoding="utf-8",
        )
        return path

    summary = f"Test fixture for {stem}."
    deps_clause = ""
    if deps:
        deps_clause = " Depends-On: " + ", ".join(deps) + "."

    block = (
        f"> **Status: {status} 2026-05-21.** {summary}{deps_clause}\n"
    )
    body = textwrap.dedent(
        f"""\
        # Plan: {stem}

        {block}
        ## Body

        Fixture content.
        """
    )
    path.write_text(body, encoding="utf-8")
    return path


def _run_cli(
    plans_dir: Path,
    archive_dir: Path,
    *extra_args: str,
) -> tuple[int, str, str]:
    env = os.environ.copy()
    env["QONTINUI_PLANS_DIR"] = str(plans_dir)
    env["QONTINUI_PLANS_ARCHIVE_DIR"] = str(archive_dir)
    cmd = [sys.executable, str(_SCRIPT_PATH), *extra_args]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Empty / trivial graph tests
# ---------------------------------------------------------------------------


class TestEmptyGraph(unittest.TestCase):
    def test_empty_workspace_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            graph = pg.build_graph(d, d)
            self.assertEqual(graph.nodes, {})
            self.assertEqual(graph.edges, {})
            self.assertEqual(graph.missing, set())
            self.assertEqual(graph.cycles, [])

    def test_empty_workspace_cli_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            rc, stdout, _ = _run_cli(d, d, "--format", "json")
            self.assertEqual(rc, 0)
            payload = json.loads(stdout)
            self.assertEqual(payload["nodes"], [])
            self.assertEqual(payload["edges"], [])
            self.assertEqual(payload["missing"], [])
            self.assertEqual(payload["cycles"], [])

    def test_empty_workspace_cli_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            rc, stdout, _ = _run_cli(d, d, "--format", "text")
            self.assertEqual(rc, 0)
            self.assertIn("(no plans matched)", stdout)


# ---------------------------------------------------------------------------
# Single-plan graph
# ---------------------------------------------------------------------------


class TestSingleNode(unittest.TestCase):
    def test_single_no_deps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "p-alone", "VETTED")
            graph = pg.build_graph(d, d)
            self.assertIn("p-alone", graph.nodes)
            self.assertEqual(graph.nodes["p-alone"].status, "VETTED")
            self.assertEqual(graph.edges["p-alone"], set())
            self.assertEqual(graph.missing, set())

    def test_single_text_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "p-alone", "VETTED")
            rc, stdout, _ = _run_cli(d, d, "--format", "text")
            self.assertEqual(rc, 0)
            self.assertIn("p-alone", stdout)
            self.assertIn("[VETTED]", stdout)
            self.assertIn("(no deps)", stdout)


# ---------------------------------------------------------------------------
# Linear chain a -> b -> c
# ---------------------------------------------------------------------------


class TestLinearChain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        _write_plan(self.d, "a", "VETTED", deps=["b"])
        _write_plan(self.d, "b", "VETTED", deps=["c"])
        _write_plan(self.d, "c", "DRAFT")

    def test_graph_structure(self) -> None:
        graph = pg.build_graph(self.d, self.d)
        self.assertEqual(graph.edges["a"], {"b"})
        self.assertEqual(graph.edges["b"], {"c"})
        self.assertEqual(graph.edges["c"], set())
        self.assertEqual(graph.cycles, [])

    def test_text_renders_three_levels(self) -> None:
        rc, stdout, _ = _run_cli(self.d, self.d, "--format", "text")
        self.assertEqual(rc, 0)
        # All three nodes should appear, with a as the root.
        self.assertIn("a  [VETTED]", stdout)
        self.assertIn("b  [VETTED]", stdout)
        self.assertIn("c  [DRAFT]", stdout)
        # a should appear ABOVE b and c in the render.
        a_idx = stdout.index("a  [VETTED]")
        b_idx = stdout.index("b  [VETTED]")
        c_idx = stdout.index("c  [DRAFT]")
        self.assertLess(a_idx, b_idx)
        self.assertLess(b_idx, c_idx)

    def test_root_filter_to_b(self) -> None:
        rc, stdout, _ = _run_cli(
            self.d, self.d, "--root", "b", "--format", "json"
        )
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        stems = {n["stem"] for n in payload["nodes"]}
        # Subgraph from b = {b, c}. Should NOT include a.
        self.assertEqual(stems, {"b", "c"})


# ---------------------------------------------------------------------------
# Diamond a -> b -> d, a -> c -> d
# ---------------------------------------------------------------------------


class TestDiamond(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        _write_plan(self.d, "a", "VETTED", deps=["b", "c"])
        _write_plan(self.d, "b", "VETTED", deps=["d"])
        _write_plan(self.d, "c", "VETTED", deps=["d"])
        _write_plan(self.d, "d", "DRAFT")

    def test_graph_has_diamond_edges(self) -> None:
        graph = pg.build_graph(self.d, self.d)
        self.assertEqual(graph.edges["a"], {"b", "c"})
        self.assertEqual(graph.edges["b"], {"d"})
        self.assertEqual(graph.edges["c"], {"d"})
        self.assertEqual(graph.edges["d"], set())

    def test_d_node_appears_once(self) -> None:
        # The JSON node-list must have exactly one entry for d (not two).
        rc, stdout, _ = _run_cli(self.d, self.d, "--format", "json")
        self.assertEqual(rc, 0)
        payload = json.loads(stdout)
        d_nodes = [n for n in payload["nodes"] if n["stem"] == "d"]
        self.assertEqual(len(d_nodes), 1)
        # And there must be exactly 2 edges into d.
        edges_to_d = [e for e in payload["edges"] if e["to"] == "d"]
        self.assertEqual(len(edges_to_d), 2)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


class TestCycle(unittest.TestCase):
    def test_two_node_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "DRAFT", deps=["b"])
            _write_plan(d, "b", "DRAFT", deps=["a"])
            graph = pg.build_graph(d, d)
            self.assertEqual(len(graph.cycles), 1)
            cycle = graph.cycles[0]
            # Cycle is [..., start] where start equals cycle[0].
            self.assertEqual(cycle[0], cycle[-1])
            # Both members appear in the cycle.
            self.assertEqual(set(cycle[:-1]), {"a", "b"})

    def test_cycle_flagged_in_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "DRAFT", deps=["b"])
            _write_plan(d, "b", "DRAFT", deps=["a"])
            rc, stdout, _ = _run_cli(d, d, "--format", "json")
            self.assertEqual(rc, 0)
            payload = json.loads(stdout)
            self.assertEqual(len(payload["cycles"]), 1)

    def test_cycle_does_not_crash_text_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "DRAFT", deps=["b"])
            _write_plan(d, "b", "DRAFT", deps=["a"])
            rc, stdout, _ = _run_cli(d, d, "--format", "text")
            self.assertEqual(rc, 0)
            self.assertIn("cycle", stdout.lower())


# ---------------------------------------------------------------------------
# Missing dep
# ---------------------------------------------------------------------------


class TestMissing(unittest.TestCase):
    def test_missing_dep_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "VETTED", deps=["does-not-exist"])
            graph = pg.build_graph(d, d)
            self.assertIn("does-not-exist", graph.missing)
            self.assertEqual(
                graph.nodes["does-not-exist"].status, pg.MISSING_STATUS
            )

    def test_missing_in_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "VETTED", deps=["does-not-exist"])
            rc, stdout, _ = _run_cli(d, d, "--format", "json")
            self.assertEqual(rc, 0)
            payload = json.loads(stdout)
            self.assertIn("does-not-exist", payload["missing"])

    def test_missing_root_returns_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "VETTED")
            rc, stdout, _ = _run_cli(
                d, d, "--root", "ghost", "--format", "json"
            )
            self.assertEqual(rc, 0)
            payload = json.loads(stdout)
            stems = {n["stem"] for n in payload["nodes"]}
            self.assertEqual(stems, {"ghost"})
            self.assertIn("ghost", payload["missing"])


# ---------------------------------------------------------------------------
# include-shipped flag
# ---------------------------------------------------------------------------


class TestIncludeShipped(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)
        _write_plan(self.d, "shipped-loner", "SHIPPED")
        _write_plan(self.d, "draft-loner", "DRAFT")

    def test_default_hides_shipped_leaf(self) -> None:
        rc, stdout, _ = _run_cli(self.d, self.d, "--format", "text")
        self.assertEqual(rc, 0)
        self.assertIn("draft-loner", stdout)
        self.assertNotIn("shipped-loner", stdout)

    def test_include_shipped_shows_both(self) -> None:
        rc, stdout, _ = _run_cli(
            self.d, self.d, "--include-shipped", "--format", "text"
        )
        self.assertEqual(rc, 0)
        self.assertIn("draft-loner", stdout)
        self.assertIn("shipped-loner", stdout)

    def test_shipped_dep_of_anchor_still_visible(self) -> None:
        # When SHIPPED is on the path between an anchor and a leaf, it
        # should appear in the default render.
        _write_plan(self.d, "alpha", "VETTED", deps=["shipped-dep"])
        _write_plan(self.d, "shipped-dep", "SHIPPED")
        rc, stdout, _ = _run_cli(self.d, self.d, "--format", "text")
        self.assertEqual(rc, 0)
        self.assertIn("alpha", stdout)
        self.assertIn("shipped-dep", stdout)


# ---------------------------------------------------------------------------
# Format coverage
# ---------------------------------------------------------------------------


class TestFormatJSON(unittest.TestCase):
    def test_json_roundtrips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "VETTED", deps=["b"])
            _write_plan(d, "b", "SHIPPED")
            rc, stdout, _ = _run_cli(d, d, "--format", "json")
            self.assertEqual(rc, 0)
            # Round-trip through json.loads + dumps must equal original.
            payload = json.loads(stdout)
            self.assertEqual(payload, json.loads(json.dumps(payload)))
            # Schema sanity:
            self.assertIn("nodes", payload)
            self.assertIn("edges", payload)
            self.assertIn("missing", payload)
            self.assertIn("cycles", payload)


class TestFormatMermaid(unittest.TestCase):
    def test_mermaid_renders_header_and_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "VETTED", deps=["b"])
            _write_plan(d, "b", "DRAFT")
            rc, stdout, _ = _run_cli(d, d, "--format", "mermaid")
            self.assertEqual(rc, 0)
            self.assertIn("graph TD;", stdout)
            self.assertIn("-->", stdout)
            # Node labels are quoted-bracket form with <br/> between stem
            # and status.
            self.assertIn("VETTED", stdout)
            self.assertIn("DRAFT", stdout)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases(unittest.TestCase):
    def test_malformed_status_block_renders_as_question_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            # malformed=True writes a plan with no status blockquote at all
            # → DRAFT per resolve-plan-deps convention. To get a literal
            # "?", we need a block that's present but lacks a lifecycle.
            path = d / "malformed.md"
            path.write_text(
                "# Plan\n\n> **Status:** No lifecycle word here.\n",
                encoding="utf-8",
            )
            graph = pg.build_graph(d, d)
            self.assertIn("malformed", graph.nodes)
            self.assertEqual(graph.nodes["malformed"].status, pg.UNKNOWN_STATUS)

    def test_path_with_spaces_in_temp_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp) / "dir with spaces"
            sub.mkdir()
            _write_plan(sub, "a", "VETTED")
            graph = pg.build_graph(sub, sub)
            self.assertIn("a", graph.nodes)
            # Location path uses forward-slashes regardless of OS.
            self.assertIn("/", graph.nodes["a"].location or "")

    def test_no_status_block_treated_as_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "p", malformed=True)
            graph = pg.build_graph(d, d)
            self.assertEqual(graph.nodes["p"].status, "DRAFT")

    def test_archive_dir_precedence(self) -> None:
        # If a stem exists in BOTH plans/ and archive/, plans/ wins.
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            plans = Path(t1)
            archive = Path(t2)
            _write_plan(plans, "same", "DRAFT")
            _write_plan(archive, "same", "SHIPPED")
            graph = pg.build_graph(plans, archive)
            self.assertEqual(graph.nodes["same"].status, "DRAFT")


# ---------------------------------------------------------------------------
# Subgraph: --root with predecessors-going-upward not in scope
# ---------------------------------------------------------------------------


class TestRootSubgraph(unittest.TestCase):
    def test_root_only_includes_descendants(self) -> None:
        # a -> b -> c. --root b should include b and c only.
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_plan(d, "a", "VETTED", deps=["b"])
            _write_plan(d, "b", "VETTED", deps=["c"])
            _write_plan(d, "c", "DRAFT")
            rc, stdout, _ = _run_cli(
                d, d, "--root", "b", "--format", "json"
            )
            self.assertEqual(rc, 0)
            payload = json.loads(stdout)
            stems = {n["stem"] for n in payload["nodes"]}
            self.assertEqual(stems, {"b", "c"})
            self.assertNotIn("a", stems)


if __name__ == "__main__":
    unittest.main()
