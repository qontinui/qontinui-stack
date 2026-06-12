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


class _FakeResponse:
    """Minimal urlopen context-manager response."""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _req_headers_lower(req) -> dict:
    """urllib capitalizes header keys; normalize for assertions."""
    return {k.lower(): v for k, v in req.headers.items()}


_PROXY_MCP = {
    "mcpServers": {
        "coord-mcp": {
            "type": "http",
            "url": "http://127.0.0.1:9876/coord-mcp",
            "headers": {"X-Coord-Mcp-Proxy-Key": "nonce-abc123"},
        }
    }
}

_BEARER_MCP = {
    "mcpServers": {
        "coord-mcp": {
            "type": "http",
            "url": "https://coord.qontinui.io/mcp",
            "headers": {"Authorization": "Bearer agent-jwt-xyz"},
        }
    }
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


class ResolveClaimsAuthTests(unittest.TestCase):
    """``.mcp.json`` dual-shape classification (Phase 3 read-auth)."""

    def setUp(self) -> None:
        # Hermetic: a developer machine may export QONTINUI_MCP_JSON.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("QONTINUI_MCP_JSON", None)

    def _write_mcp(self, td: str, payload) -> Path:
        p = Path(td) / ".mcp.json"
        body = payload if isinstance(payload, str) else json.dumps(payload)
        p.write_text(body, encoding="utf-8")
        return p

    def test_proxy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_mcp(td, _PROXY_MCP)
            auth = scbm.resolve_claims_auth(Path(td))
        self.assertEqual(auth.mode, "proxy")
        self.assertEqual(auth.proxy_base, "http://127.0.0.1:9876/coord-mcp")
        self.assertEqual(
            auth.headers, {"X-Coord-Mcp-Proxy-Key": "nonce-abc123"}
        )

    def test_bearer_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_mcp(td, _BEARER_MCP)
            auth = scbm.resolve_claims_auth(Path(td))
        self.assertEqual(auth.mode, "bearer")
        self.assertIsNone(auth.proxy_base)
        self.assertEqual(auth.headers, {"Authorization": "Bearer agent-jwt-xyz"})

    def test_missing_file_is_anonymous(self) -> None:
        # Explicit override pointing at a nonexistent file (hermetic — a
        # cwd walk-up could find a real workspace .mcp.json above $TMP).
        with tempfile.TemporaryDirectory() as td:
            auth = scbm.resolve_claims_auth(
                Path(td), str(Path(td) / "absent.mcp.json")
            )
        self.assertEqual(auth.mode, "anonymous")
        self.assertEqual(auth.headers, {})

    def test_malformed_json_is_anonymous(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_mcp(td, "{not json")
            auth = scbm.resolve_claims_auth(Path(td))
        self.assertEqual(auth.mode, "anonymous")

    def test_unrecognized_shape_is_anonymous(self) -> None:
        # coord-mcp entry present but with neither nonce nor bearer.
        with tempfile.TemporaryDirectory() as td:
            self._write_mcp(
                td,
                {
                    "mcpServers": {
                        "coord-mcp": {
                            "type": "http",
                            "url": "https://coord.qontinui.io/mcp",
                            "headers": {},
                        }
                    }
                },
            )
            auth = scbm.resolve_claims_auth(Path(td))
        self.assertEqual(auth.mode, "anonymous")

    def test_bearer_on_loopback_is_anonymous(self) -> None:
        # Loopback URL without the proxy nonce never becomes bearer mode.
        with tempfile.TemporaryDirectory() as td:
            self._write_mcp(
                td,
                {
                    "mcpServers": {
                        "coord-mcp": {
                            "type": "http",
                            "url": "http://127.0.0.1:9876/coord-mcp",
                            "headers": {"Authorization": "Bearer x"},
                        }
                    }
                },
            )
            auth = scbm.resolve_claims_auth(Path(td))
        self.assertEqual(auth.mode, "anonymous")

    def test_walks_up_from_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            self._write_mcp(td, _PROXY_MCP)
            sub = Path(td) / "a" / "b"
            sub.mkdir(parents=True)
            auth = scbm.resolve_claims_auth(sub)
        self.assertEqual(auth.mode, "proxy")

    def test_env_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            # cwd has a proxy-shape file; env points at a bearer-shape one.
            self._write_mcp(td, _PROXY_MCP)
            other = Path(td) / "elsewhere"
            other.mkdir()
            env_file = other / "custom-mcp.json"
            env_file.write_text(json.dumps(_BEARER_MCP), encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"QONTINUI_MCP_JSON": str(env_file)}
            ):
                auth = scbm.resolve_claims_auth(Path(td))
        self.assertEqual(auth.mode, "bearer")

    def test_explicit_arg_beats_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            arg_file = Path(td) / "arg-mcp.json"
            arg_file.write_text(json.dumps(_PROXY_MCP), encoding="utf-8")
            with mock.patch.dict(
                os.environ, {"QONTINUI_MCP_JSON": str(Path(td) / "nope.json")}
            ):
                auth = scbm.resolve_claims_auth(Path(td), str(arg_file))
        self.assertEqual(auth.mode, "proxy")


class FetchHoldersAuthTests(unittest.TestCase):
    """Wire-level behavior of ``fetch_holders`` per auth mode (mocked urlopen)."""

    _PAYLOAD = {"holders": [_holder("r:f.rs:sym", "OTHER")]}

    def test_proxy_mode_hits_loopback_with_nonce(self) -> None:
        auth = scbm.ClaimsAuth(
            mode="proxy",
            proxy_base="http://127.0.0.1:9876/coord-mcp",
            headers={"X-Coord-Mcp-Proxy-Key": "nonce-abc123"},
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(self._PAYLOAD)
        ) as m:
            holders = scbm.fetch_holders("r", "f.rs", auth=auth)
        self.assertEqual(len(holders), 1)
        self.assertEqual(m.call_count, 1)
        req = m.call_args[0][0]
        self.assertTrue(
            req.full_url.startswith(
                "http://127.0.0.1:9876/coord-mcp/claims/list?"
            ),
            req.full_url,
        )
        self.assertIn("kind=symbol", req.full_url)
        self.assertEqual(
            _req_headers_lower(req).get("x-coord-mcp-proxy-key"),
            "nonce-abc123",
        )
        self.assertEqual(m.call_args[1].get("timeout"), 2.5)

    def test_bearer_mode_hits_coord_with_authorization(self) -> None:
        auth = scbm.ClaimsAuth(
            mode="bearer", headers={"Authorization": "Bearer agent-jwt-xyz"}
        )
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(self._PAYLOAD)
        ) as m:
            holders = scbm.fetch_holders(
                "r", "f.rs", coord_url="https://coord.example", auth=auth
            )
        self.assertEqual(len(holders), 1)
        self.assertEqual(m.call_count, 1)
        req = m.call_args[0][0]
        self.assertTrue(
            req.full_url.startswith("https://coord.example/coord/claims/list?"),
            req.full_url,
        )
        self.assertEqual(
            _req_headers_lower(req).get("authorization"), "Bearer agent-jwt-xyz"
        )
        self.assertEqual(m.call_args[1].get("timeout"), 2.5)

    def test_no_auth_is_anonymous_direct(self) -> None:
        with mock.patch(
            "urllib.request.urlopen", return_value=_FakeResponse(self._PAYLOAD)
        ) as m:
            scbm.fetch_holders("r", "f.rs", coord_url="https://coord.example")
        self.assertEqual(m.call_count, 1)
        req = m.call_args[0][0]
        self.assertTrue(
            req.full_url.startswith("https://coord.example/coord/claims/list?")
        )
        hdrs = _req_headers_lower(req)
        self.assertNotIn("authorization", hdrs)
        self.assertNotIn("x-coord-mcp-proxy-key", hdrs)

    def test_loopback_refused_falls_open_to_anonymous(self) -> None:
        auth = scbm.ClaimsAuth(
            mode="proxy",
            proxy_base="http://127.0.0.1:9876/coord-mcp",
            headers={"X-Coord-Mcp-Proxy-Key": "nonce-abc123"},
        )
        calls: list = []

        def fake_urlopen(req, timeout=None):
            calls.append(req)
            if req.full_url.startswith("http://127.0.0.1:"):
                raise scbm.urllib.error.URLError("connection refused")
            return _FakeResponse(self._PAYLOAD)

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            holders = scbm.fetch_holders(
                "r", "f.rs", coord_url="https://coord.example", auth=auth
            )
        self.assertEqual(len(holders), 1)
        self.assertEqual(len(calls), 2)
        # Fallback is anonymous direct-coord — no proxy nonce leaked.
        fallback = calls[1]
        self.assertTrue(
            fallback.full_url.startswith("https://coord.example/coord/claims/list?")
        )
        self.assertNotIn("x-coord-mcp-proxy-key", _req_headers_lower(fallback))

    def test_both_attempts_failing_raises(self) -> None:
        auth = scbm.ClaimsAuth(
            mode="proxy",
            proxy_base="http://127.0.0.1:9876/coord-mcp",
            headers={"X-Coord-Mcp-Proxy-Key": "n"},
        )
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=scbm.urllib.error.URLError("down"),
        ) as m:
            with self.assertRaises(RuntimeError):
                scbm.fetch_holders(
                    "r", "f.rs", coord_url="https://coord.example", auth=auth
                )
        self.assertEqual(m.call_count, 2)


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
