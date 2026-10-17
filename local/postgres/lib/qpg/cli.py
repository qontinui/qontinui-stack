"""``qontinui-pg`` — the local/postgres operations CLI.

Exit codes: 0 ok; 1 a check failed / a difference or alert was found;
2 usage or configuration error; 3 the follower is halted; 4 timed out.
"""

from __future__ import annotations

import argparse
import configparser
import ipaddress
import json
import os
import sys

from . import config as config_mod
from . import credential, hostcfg, partitions, roles, secdiff
from .config import ConfigError
from .follower import Follower, Halt, Locked
from .notify import Notifier
from .pg import PgError
from .seqsync import SeqSync, SeqSyncError
from .watchdog import Watchdog


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
        if not args.expect:
            raise ConfigError("follower seed needs --expect <the web revision the schema dump was taken at>")
        print(json.dumps(f.seed(args.expect)))
        return 0
    if args.action == "run":
        return f.run(once=args.once, until_idle=args.until_idle, timeout=args.timeout)
    if args.action == "status":
        print(json.dumps({**f.state(), "copy_columns_owed": f.owed()}, indent=2))
        return 0
    if args.action == "resume":
        f.resume()
        return 0
    if args.action == "copy-columns":
        if not args.table:
            raise ConfigError("follower copy-columns needs --table <schema>.<table> [--column <c>]...")
        print(json.dumps(f.copy_columns(args.table, args.column, args.allow_unmatched)))
        return 0
    if args.action == "resolve":
        if not args.case:
            raise ConfigError(f"follower resolve needs --case ({', '.join(sorted(f.RESOLVABLE))})")
        f.resolve(args.case, args.target)
        print(f"resolved {args.case} {args.target or ''}; resumed")
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


def cmd_seqsync(args, cfg) -> int:
    s = SeqSync(cfg, reverse=args.reverse)
    try:
        results = s.from_data(args.dry_run) if args.from_data else s.run(final=args.final, dry_run=args.dry_run)
    except SeqSyncError as exc:
        print(f"seqsync FAILED: {exc}", file=sys.stderr)
        return 1
    counts: dict[str, int] = {}
    for r in results:
        counts[r.action] = counts.get(r.action, 0) + 1
    print(json.dumps({"sequences": len(results), **counts}))
    return 0


def cmd_watchdog(args, cfg) -> int:
    checks = Watchdog(cfg, Notifier(cfg, force_dry_run=args.dry_run_notify)).run()
    for c in checks:
        print(f"{c.status:7} {c.name}: {c.message}")
    return 0 if all(c.status == "OK" for c in checks) else 1


def cmd_credential(args, _cfg) -> int:
    verifier = credential.new_credential(args.role, args.passfile, args.host, args.port, args.database, args.rotate)
    print(verifier)
    return 0


def cmd_roles(args, cfg) -> int:
    if args.action == "apply":
        verifier = None
        if args.verifier_file:
            with open(args.verifier_file, encoding="utf-8") as fh:
                verifier = fh.read().strip()
        print(json.dumps(roles.apply(cfg, args.revision_file, verifier, args.allow_missing), indent=2))
        return 0
    report = roles.check_grantees(cfg)
    print(json.dumps(report, indent=2))
    return 1 if report["missing_locally"] else 0


def cmd_secdiff(args, cfg) -> int:
    report = secdiff.run(cfg)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["empty"] else 1


def cmd_partitions(args, cfg) -> int:
    print(partitions.run(cfg))
    return 0


def cmd_partition_parity(args, cfg) -> int:
    report = partitions.parity(cfg, args.parent or None)
    print(json.dumps(report, indent=2))
    return 0 if report["equal"] else 1


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
    f.add_argument("action", choices=["install", "seed", "run", "status", "resume", "resolve", "copy-columns", "check",
                                      "coord-adopt", "coord-refresh", "coord-gate-check"])
    f.add_argument("--once", action="store_true", help="run one cycle")
    f.add_argument("--until-idle", action="store_true", help="loop until settled, halted or --timeout")
    f.add_argument("--timeout", type=float, default=0.0)
    f.add_argument("--version", help="coord version (coord-adopt)")
    f.add_argument("--expect", help="seed: the web revision the loaded schema dump was taken at")
    f.add_argument("--case", help="resolve: the halt case (rename | missing-value)")
    f.add_argument("--target", help="resolve: the target the halt names (table, or table.column)")
    f.add_argument("--table", help="copy-columns: <schema>.<table>")
    f.add_argument("--column", action="append", help="copy-columns: a column (repeatable; default: all non-key)")
    f.add_argument("--allow-unmatched", type=int, default=0,
                   help="copy-columns: publisher rows that may be missing locally (newer than the subscriber)")
    f.add_argument("--dry-run-notify", action="store_true", help="log notices, never post them")
    f.set_defaults(fn=cmd_follower)

    s = sub.add_parser("seqsync", help="sequence sync (D6)")
    s.add_argument("action", choices=["run"])
    s.add_argument("--final", action="store_true", help="cutover margin floor")
    s.add_argument("--reverse", action="store_true", help="local -> [seqsync] reverse_dsn (D8)")
    s.add_argument("--from-data", action="store_true", help="set column-owned sequences from local data (restore)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_seqsync)

    w = sub.add_parser("watchdog", help="the D11 watchdog")
    w.add_argument("action", choices=["run"])
    w.add_argument("--dry-run-notify", action="store_true", help="log notices, never post them")
    w.set_defaults(fn=cmd_watchdog)

    c = sub.add_parser("credential", help="generate a password locally, print only its SCRAM verifier (D2)")
    c.add_argument("action", choices=["new"])
    c.add_argument("--role", required=True)
    c.add_argument("--passfile", required=True)
    c.add_argument("--host", default="*")
    c.add_argument("--port", default="*")
    c.add_argument("--database", default="*")
    c.add_argument("--rotate", action="store_true")
    c.set_defaults(fn=cmd_credential, needs_config=False)

    r = sub.add_parser("roles", help="B1 role step")
    r.add_argument("action", choices=["apply", "check-grantees"])
    r.add_argument("--revision-file", help="coordrls_01's revision file at production's head")
    r.add_argument("--verifier-file", help="file holding the app role's SCRAM verifier (role creation)")
    r.add_argument("--allow-missing", action="store_true")
    r.set_defaults(fn=cmd_roles)

    d = sub.add_parser("secdiff", help="B3 security catalog diff (publisher vs subscriber)")
    d.set_defaults(fn=cmd_secdiff)

    t = sub.add_parser("partitions", help="D9 partition maintenance")
    t.add_argument("action", choices=["run"])
    t.set_defaults(fn=cmd_partitions)

    pp = sub.add_parser("partition-parity",
                        help="Phase 3 / D9: child partition NAMES per published partitioned table, both sides")
    pp.add_argument("--parent", action="append", default=[],
                    help="<schema>.<table>; default: every partitioned root the publication carries")
    pp.set_defaults(fn=cmd_partition_parity)

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
    except (ConfigError, configparser.Error) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except Halt as exc:
        print(f"halted: {exc}", file=sys.stderr)
        return 3
    except Locked as exc:
        print(f"locked: {exc}", file=sys.stderr)
        return 5
    except (PgError, roles.RoleError, credential.CredentialError, partitions.PartitionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
