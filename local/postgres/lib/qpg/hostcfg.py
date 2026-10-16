"""Rendering and checking the host files the installer manages (D4, D15, D16).

``install.sh`` drives packages, the cluster and systemd; everything it WRITES
is rendered here, from the installed config, so the same code produces the
files and judges them in ``--check``. Every managed file starts with
:data:`MARKER`, which is how ``--uninstall`` recognises its own files.

The D4 isolation rule, as ``check_hba`` enforces it (re-vet 2026-10-16):

* coord connects with tokio-postgres ``NoTls``, so the APPLICATION ROLE may log
  in without TLS — but only to its database and only from a tailnet address,
  where WireGuard encrypts the connection;
* every other TCP client must use ``hostssl``, also only from the tailnet;
* everything else is ``reject``; ``trust`` appears nowhere;
* the server listens on its tailnet address and the unix socket only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import re
from dataclasses import dataclass

from .config import DEFAULT_PATH, PLACEHOLDER, Config, ConfigError

MARKER = "# managed-by: qontinui-stack/local/postgres/install.sh"
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(HERE)), "templates")


@dataclass
class Managed:
    path: str  # absolute, as on the host
    content: str
    mode: int


def _cidrs(cfg: Config) -> list[str]:
    raw = cfg.get("cluster", "tailnet_cidrs", fallback="100.64.0.0/10, fd7a:115c:a1e0::/48") or ""
    cidrs = [c.strip() for c in raw.split(",") if c.strip()]
    for c in cidrs:
        ipaddress.ip_network(c, strict=False)
    return cidrs


def _value(cfg: Config, section: str, key: str, allow_placeholders: bool, default: str | None = None) -> str:
    value = cfg.get(section, key, fallback=default)
    if value is None or not value.strip():
        raise ConfigError(f"[{section}] {key} is required")
    value = value.strip()
    if PLACEHOLDER in value and not allow_placeholders:
        raise ConfigError(f"[{section}] {key} still holds the {PLACEHOLDER} placeholder")
    return value


def cluster(cfg: Config) -> tuple[str, str]:
    version = cfg.get("cluster", "version", fallback="16") or "16"
    return version, cfg.get("cluster", "name", fallback="qontinui") or "qontinui"


def _template(name: str) -> str:
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


def render(cfg: Config, allow_placeholders: bool = False) -> list[Managed]:
    v = lambda s, k, d=None: _value(cfg, s, k, allow_placeholders, d)  # noqa: E731
    version, name = cluster(cfg)
    pgb = cfg.getbool("pgbackrest", "enabled", False)
    stanza = cfg.get("pgbackrest", "stanza", fallback="qontinui") or "qontinui"
    values = {
        "marker": MARKER,
        "version": version,
        "cluster": name,
        "tailnet_address": v("cluster", "tailnet_address"),
        "port": v("cluster", "port", "5432"),
        "database": v("cluster", "database", "qontinui_db"),
        "app_role": cfg.app_role,
        "data_directory": v("cluster", "data_directory"),
        "ssl_cert_file": v("cluster", "ssl_cert_file"),
        "ssl_key_file": v("cluster", "ssl_key_file"),
        "archive_mode": "on" if pgb else "off",
        "archive_command": f"pgbackrest --stanza={stanza} archive-push %p" if pgb else "",
        "stanza": stanza,
        # Units always read the INSTALLED config, whichever file was rendered from.
        "config_path": DEFAULT_PATH,
        "partitions_oncalendar": cfg.get("partitions", "on_calendar", fallback="hourly") or "hourly",
        "full_oncalendar": cfg.get("pgbackrest", "full_on_calendar", fallback="Sun *-*-* 02:00:00"),
        "diff_oncalendar": cfg.get("pgbackrest", "diff_on_calendar", fallback="Mon..Sat *-*-* 02:00:00"),
    }
    cidrs = _cidrs(cfg)
    app_lines = "\n".join(
        f"host    {values['database']}  {values['app_role']}  {c}  scram-sha-256" for c in cidrs)
    ssl_lines = "\n".join(f"hostssl all  all  {c}  scram-sha-256" for c in cidrs)
    repl_users = (cfg.get("cluster", "replication_users") or "").strip()
    repl_lines = "\n".join(
        f"hostssl replication  {repl_users}  {c}  scram-sha-256" for c in cidrs) if repl_users else \
        "# (no physical replication users configured: [cluster] replication_users)"
    values.update(app_lines=app_lines, ssl_lines=ssl_lines, repl_lines=repl_lines)

    conf_dir = f"/etc/postgresql/{version}/{name}"
    files = [
        Managed(f"{conf_dir}/conf.d/50-qontinui.conf", _template("postgresql.conf.tmpl").format(**values), 0o644),
        Managed(f"{conf_dir}/pg_hba.conf", _template("pg_hba.conf.tmpl").format(**values), 0o640),
        Managed("/etc/postgresql-common/createcluster.d/50-qontinui.conf",
                _template("createcluster.conf.tmpl").format(**values), 0o644),
    ]
    units = ["follower.service"]
    if pgb:
        units += ["backup-full.service", "backup-full.timer", "backup-diff.service", "backup-diff.timer"]
        files.append(Managed("/etc/pgbackrest/pgbackrest.conf",
                             render_pgbackrest(cfg, values, allow_placeholders), 0o640))
    for unit in units:
        files.append(Managed(f"/etc/systemd/system/qontinui-pg-{unit}",
                             _template(f"systemd/qontinui-pg-{unit}").format(**values), 0o644))
    return files


def render_pgbackrest(cfg: Config, values: dict, allow_placeholders: bool) -> str:
    v = lambda k, d=None: _value(cfg, "pgbackrest", k, allow_placeholders, d)  # noqa: E731
    lines = [MARKER, "# D15: pgBackRest. The cipher passphrase is NOT here: it lives in",
             "# /etc/pgbackrest/conf.d/qontinui-cipher.conf, generated on this host (OD4).", "[global]"]
    repo1 = (cfg.get("pgbackrest", "repo1_path") or "").strip()
    if repo1:
        lines += [f"repo1-path={repo1}", "repo1-retention-full-type=time",
                  f"repo1-retention-full={v('retention_full_days', '35')}", "repo1-bundle=y", "repo1-block=y"]
    lines += [f"repo2-type={v('repo2_type')}", f"repo2-path={v('repo2_path')}"]
    if cfg.p.has_section("pgbackrest.repo2"):
        for key, value in cfg.p.items("pgbackrest.repo2"):
            if PLACEHOLDER in value and not allow_placeholders:
                raise ConfigError(f"[pgbackrest.repo2] {key} still holds the placeholder")
            lines.append(f"repo2-{key}={value}")
    lines += [
        "repo2-cipher-type=aes-256-cbc",
        "repo2-retention-full-type=time",
        f"repo2-retention-full={v('retention_full_days', '35')}",
        "repo2-bundle=y",
        "repo2-block=y",
        "archive-async=y",
        f"archive-push-queue-max={v('archive_push_queue_max', '20GiB')}",
        "spool-path=/var/spool/pgbackrest",
        f"process-max={v('process_max', '4')}",
        "compress-type=zst",
        "start-fast=y",
        "log-level-file=detail",
        "",
        f"[{values['stanza']}]",
        f"pg1-path={values['data_directory']}",
        f"pg1-port={values['port']}",
        "pg1-socket-path=/var/run/postgresql",
        "",
    ]
    return "\n".join(lines)


def sha256_tree(path: str) -> str:
    h = hashlib.sha256()
    for root, dirs, files in sorted(os.walk(path)):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for f in sorted(files):
            if f.endswith(".pyc"):
                continue
            full = os.path.join(root, f)
            h.update(os.path.relpath(full, path).encode())
            with open(full, "rb") as fh:
                h.update(hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


# --------------------------------------------------------------------- checks
def _in_tailnet(address: str, cidrs: list[str]) -> bool:
    try:
        net = ipaddress.ip_network(address, strict=False)
    except ValueError:
        return False
    for c in cidrs:
        tn = ipaddress.ip_network(c, strict=False)
        if net.version == tn.version and net.subnet_of(tn):
            return True
    return False


@dataclass
class HbaRule:
    type: str
    database: str
    user: str
    address: str
    method: str
    raw: str


def parse_hba(text: str) -> list[HbaRule]:
    rules = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if parts[0] == "local":
            rules.append(HbaRule("local", parts[1], parts[2], "", parts[3] if len(parts) > 3 else "", raw))
            continue
        if len(parts) >= 6 and re.match(r"^\d+\.\d+\.\d+\.\d+$", parts[3]):  # address + netmask form
            address = f"{parts[3]}/{ipaddress.ip_network(f'0.0.0.0/{parts[4]}').prefixlen}"
            method = parts[5]
        else:
            address, method = parts[3], parts[4] if len(parts) > 4 else ""
        rules.append(HbaRule(parts[0], parts[1], parts[2], address, method, raw))
    return rules


def check_hba_rules(rules: list[HbaRule], cfg: Config) -> list[tuple[bool, str]]:
    cidrs = _cidrs(cfg)
    database = cfg.get("cluster", "database", fallback="qontinui_db") or "qontinui_db"
    app = cfg.app_role
    out: list[tuple[bool, str]] = []
    app_nontls = 0
    tls = 0
    for r in rules:
        if r.method == "trust":
            out.append((False, f"pg_hba: trust is forbidden: {r.raw.strip()}"))
            continue
        if r.type == "local" or r.method == "reject":
            continue
        if r.type in ("host", "hostnossl"):
            ok = r.database == database and r.user == app and _in_tailnet(r.address, cidrs)
            out.append((ok, f"pg_hba: non-TLS admitted only for {app} on {database} from the tailnet: {r.raw.strip()}"))
            app_nontls += ok
        elif r.type == "hostssl":
            ok = _in_tailnet(r.address, cidrs) and r.method in ("scram-sha-256", "cert")
            out.append((ok, f"pg_hba: TLS client from the tailnet with scram/cert: {r.raw.strip()}"))
            tls += ok
        else:
            out.append((False, f"pg_hba: connection type {r.type} is not allowed: {r.raw.strip()}"))
    out.append((app_nontls > 0,
                f"pg_hba: the application role's non-TLS tailnet login exists (coord is NoTls) [{app_nontls}]"))
    out.append((tls > 0, f"pg_hba: every other client is hostssl [{tls} hostssl rule(s)]"))
    return out


def check_listen(listen: str, ssl: str, cfg: Config) -> list[tuple[bool, str]]:
    cidrs = _cidrs(cfg)
    addrs = [a.strip() for a in listen.split(",") if a.strip()]
    ok = bool(addrs) and all(_in_tailnet(a, cidrs) for a in addrs)
    return [
        (ok, f"listen_addresses holds tailnet addresses only (plus the unix socket) [{listen}]"),
        (ssl.lower() in ("on", "true"), f"ssl = on [{ssl}]"),
    ]


def conf_value(text: str, key: str) -> str | None:
    found = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(rf"^{re.escape(key)}\s*=\s*(.*)$", line)
        if m:
            found = m.group(1).strip().strip("'")
    return found


def check_files(cfg: Config, root: str, allow_placeholders: bool = False) -> list[tuple[bool, str]]:
    out: list[tuple[bool, str]] = []
    rendered = render(cfg, allow_placeholders)
    for m in rendered:
        path = os.path.join(root, m.path.lstrip("/")) if root else m.path
        try:
            with open(path, encoding="utf-8") as fh:
                current = fh.read()
        except OSError as exc:
            out.append((False, f"{m.path}: missing ({exc.strerror})"))
            continue
        out.append((current == m.content, f"{m.path}: matches the rendered content"))
        mode = os.stat(path).st_mode & 0o777
        out.append(((mode & ~m.mode) == 0, f"{m.path}: mode {oct(mode)} no wider than {oct(m.mode)}"))
    version, name = cluster(cfg)
    hba_path = os.path.join(root, f"etc/postgresql/{version}/{name}/pg_hba.conf") if root else \
        f"/etc/postgresql/{version}/{name}/pg_hba.conf"
    conf_path = os.path.join(root, f"etc/postgresql/{version}/{name}/conf.d/50-qontinui.conf") if root else \
        f"/etc/postgresql/{version}/{name}/conf.d/50-qontinui.conf"
    try:
        with open(hba_path, encoding="utf-8") as fh:
            out += check_hba_rules(parse_hba(fh.read()), cfg)
        with open(conf_path, encoding="utf-8") as fh:
            conf = fh.read()
        out += check_listen(conf_value(conf, "listen_addresses") or "", conf_value(conf, "ssl") or "", cfg)
        out.append((conf_value(conf, "wal_level") == "logical", "wal_level = logical"))
        out.append((conf_value(conf, "password_encryption") == "scram-sha-256", "password_encryption = scram-sha-256"))
        out.append(("jsonlog" in (conf_value(conf, "log_destination") or ""),
                    "log_destination includes jsonlog (the follower reads apply errors there)"))
        out.append((conf_value(conf, "max_sync_workers_per_subscription") == "1",
                    "max_sync_workers_per_subscription = 1 (D3)"))
    except OSError as exc:
        out.append((False, f"cannot read the cluster config: {exc}"))
    if cfg.getbool("pgbackrest", "enabled", False):
        path = os.path.join(root, "etc/pgbackrest/pgbackrest.conf") if root else "/etc/pgbackrest/pgbackrest.conf"
        try:
            with open(path, encoding="utf-8") as fh:
                pgb = fh.read()
            queue = cfg.get("pgbackrest", "archive_push_queue_max", fallback="20GiB") or "20GiB"
            out.append((f"archive-push-queue-max={queue}" in pgb, f"pgBackRest archive-push-queue-max={queue} (D4)"))
            out.append(("repo2-cipher-type=aes-256-cbc" in pgb, "pgBackRest repo2 is encrypted (aes-256-cbc)"))
            has_repo1 = "repo1-path=" in pgb
            want_repo1 = bool((cfg.get("pgbackrest", "repo1_path") or "").strip())
            label = "configured" if want_repo1 else "absent (one-disk interim host, D4)"
            out.append((has_repo1 == want_repo1, f"pgBackRest repo1 {label}"))
        except OSError as exc:
            out.append((False, f"cannot read pgbackrest.conf: {exc}"))
        cipher = os.path.join(root, "etc/pgbackrest/conf.d/qontinui-cipher.conf") if root else \
            "/etc/pgbackrest/conf.d/qontinui-cipher.conf"
        if os.path.exists(cipher):
            mode = os.stat(cipher).st_mode & 0o777
            out.append(((mode & 0o007) == 0, f"cipher passphrase file is not world-readable [{oct(mode)}]"))
        else:
            out.append((False, "cipher passphrase file exists (generated by install.sh)"))
    cfg_path = cfg.path
    if os.path.exists(cfg_path):
        mode = os.stat(cfg_path).st_mode & 0o777
        out.append(((mode & 0o007) == 0, f"{cfg_path} is not world-readable (it names DSNs) [{oct(mode)}]"))
    return out
