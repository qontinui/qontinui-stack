"""``qontinui-pg`` — the local/postgres operations CLI.

Exit codes: 0 ok; 1 a check failed / a difference or alert was found;
2 usage or configuration error; 3 the follower is halted; 4 timed out.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys

from . import config as config_mod
from . import hostcfg
from .config import ConfigError
from .follower import Follower, Halt
from .notify import Notifier
from .pg import PgError


def _print_checks(results: list[tuple[bool, str]]) -> int:
    failed = 0
    for ok, text in results:
        print(f"{'OK  ' if ok else 'FAIL'} {text}")
        failed += not ok
    print(f"{len(results) - failed} ok, {failed} failed")
    return 1 if failed else 0


def cmd_follower(args, cfg) -> int:
    f = Follower(cfg, Notifier(cfg, force_dry_run=args.dry_run_notify))
    if args.action == "install":
        f.install()
        print("follower schema and stall triggers installed")
        return 0
    if args.action == "seed":
        print(json.dumps(f.seed()))
        return 0
    if args.action == "run":
        return f.run(once=args.once, until_idle=args.until_idle, timeout=args.timeout)
    if args.action == "status":
        print(json.dumps(f.state(), indent=2))
        return 0
    if args.action == "resume":
        f.resume()
        return 0
    if args.action == "check":
        return _print_checks(f.check())
    if args.action == "coord-adopt":
        f.coord_adopt(args.version)
        return 0
    if args.action == "coord-refresh":
        f.coord_refresh()
        print("REFRESH PUBLICATION WITH (copy_data = false) done")
        return 0
    if args.action == "coord-gate-check":
        gate = f.coord_gate()
        print(json.dumps(gate, indent=2))
        return 0 if gate["ok"] else 1
    raise ConfigError(f"unknown follower action {args.action}")


def cmd_host(args, cfg) -> int:
    if args.action == "render":
        for m in hostcfg.render(cfg, allow_placeholders=args.allow_placeholders):
            dest = os.path.join(args.root, m.path.lstrip("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".qpg-tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(m.content)
            os.chmod(tmp, m.mode)
            os.replace(tmp, dest)
            print(dest)
        return 0
    if args.action == "list":
        for m in hostcfg.render(cfg, allow_placeholders=True):
            print(m.path)
        return 0
    if args.action == "check-hba-live":
        admin = cfg.sub_admin("qontinui-pg-check")
        errors = admin.rows("SELECT line_number::text, error FROM pg_hba_file_rules WHERE error IS NOT NULL")
        rows = admin.rows(
            "SELECT type, array_to_string(database, ','), array_to_string(user_name, ','), "
            "coalesce(address, ''), coalesce(netmask, ''), coalesce(auth_method, '') FROM pg_hba_file_rules "
            "WHERE error IS NULL ORDER BY line_number")
        rules = []
        for t, db, user, addr, mask, method in rows:
            if addr and mask and "/" not in addr:
                addr = str(ipaddress.ip_network(f"{addr}/{mask}", strict=False))
            rules.append(hostcfg.HbaRule(t, db, user, addr, method, f"{t} {db} {user} {addr} {method}"))
        results = [(not errors, f"pg_hba_file_rules has no errors {errors}")]
        results += hostcfg.check_hba_rules(rules, cfg)
        listen = admin.scalar("SHOW listen_addresses") or ""
        ssl = admin.scalar("SHOW ssl") or ""
        results += hostcfg.check_listen(listen, ssl, cfg)
        return _print_checks(results)
    # check
    return _print_checks(hostcfg.check_files(cfg, args.root, allow_placeholders=args.allow_placeholders))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qontinui-pg", description=__doc__.split("\n", 1)[0])
    p.add_argument("-c", "--config", help=f"config file (default ${{QONTINUI_PG_CONFIG}} or {config_mod.DEFAULT_PATH})")
    sub = p.add_subparsers(dest="group", required=True)

    f = sub.add_parser("follower", help="the schema follower (D7, A2-A4)")
    f.add_argument("action", choices=["install", "seed", "run", "status", "resume", "check",
                                      "coord-adopt", "coord-refresh", "coord-gate-check"])
    f.add_argument("--once", action="store_true", help="run one cycle")
    f.add_argument("--until-idle", action="store_true", help="loop until settled, halted or --timeout")
    f.add_argument("--timeout", type=float, default=0.0)
    f.add_argument("--version", help="coord version (coord-adopt)")
    f.add_argument("--dry-run-notify", action="store_true", help="log notices, never post them")
    f.set_defaults(fn=cmd_follower)

    h = sub.add_parser("host", help="installer support: render/check managed host files")
    h.add_argument("action", choices=["render", "check", "list", "check-hba-live"])
    h.add_argument("--root", default="", help="filesystem root prefix (render/check into a staging tree)")
    h.add_argument("--allow-placeholders", action="store_true")
    h.set_defaults(fn=cmd_host)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = None if getattr(args, "needs_config", True) is False else config_mod.load(args.config)
        return args.fn(args, cfg)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Halt as exc:
        print(f"halted: {exc}", file=sys.stderr)
        return 3
    except PgError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
