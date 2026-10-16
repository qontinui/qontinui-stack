#!/usr/bin/env bash
# install.sh — install, check or remove the local Postgres subscriber host
# (plan 2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication,
# Phases 1d/1e: D4, D15, D16, plus the units of D6, D7, D9, D11).
#
# WHAT IT INSTALLS (Debian/Ubuntu, as root)
#   * PGDG packages: postgresql-16, postgresql-16-pgvector (0.8.x, at least
#     RDS's 0.8.1, so vector text parses), postgresql-client-16, pgbackrest,
#     python3, curl. Native packages under systemd, not containers: a
#     production database must not share the lifecycle of a CI host's Docker
#     daemon (D16). Debian's default "main" cluster is suppressed.
#   * One cluster, 16/<name>, data directory on the LUKS-encrypted volume the
#     operator prepared (OD1). Postgres runs as the `postgres` system user —
#     its own account, holding no agent's files. The installer REFUSES a data
#     directory that is not on a dm-crypt device unless --allow-unencrypted.
#     (LUKS protects a lost or recycled disk, not the data from whoever runs
#     the hypervisor; OD1 states that.)
#   * conf.d/50-qontinui.conf: listen on the tailnet address and the unix
#     socket only; ssl on; wal_level logical; jsonlog (the follower reads it);
#     max_sync_workers_per_subscription = 1 (D3); archiving off until Phase 4.
#   * pg_hba.conf per D4 (re-vet 2026-10-16): the application role's NON-TLS
#     login only from the tailnet (coord connects NoTls; WireGuard encrypts
#     it), hostssl from the tailnet for every other client, reject for the
#     rest, no trust anywhere.
#   * pgBackRest (only with [pgbackrest] enabled = true, Phase 4): repo2
#     off-site and encrypted, time retention 35 days, archive-async with
#     archive-push-queue-max = 20GiB, NO repo1 on a one-disk host. The repo2
#     cipher passphrase is generated here and shown once on the console (OD4).
#   * /opt/qontinui-pg (this directory's lib/, templates/ and qontinui-pg) and
#     /usr/local/bin/qontinui-pg.
#   * systemd units qontinui-pg-{follower,seqsync,watchdog,partitions}
#     (+ backup-full/backup-diff with pgBackRest). INSTALLED, NOT ENABLED:
#     enabling them is a Phase 3 step (--enable-services).
#
# WHAT IT NEVER DOES
#   Connect to the publisher (production), create a publication or a
#   subscription, create a role on RDS, move a secret, or delete data.
#   --uninstall removes what install wrote and prints, but does not run, the
#   pg_dropcluster command.
#
# USAGE
#   install.sh [--config FILE] [--dry-run] [--root DIR] [--allow-unencrypted]
#              [--enable-services]
#   install.sh --check [--config FILE] [--root DIR]
#   install.sh --uninstall [--config FILE] [--dry-run] [--root DIR]
#
#   --config FILE   the qontinui-pg.conf to install (default: the installed
#                   /etc/qontinui-pg/qontinui-pg.conf)
#   --dry-run       print every action; change nothing; no root needed
#   --root DIR      write every file under DIR instead of / and skip packages,
#                   the cluster and systemd (a staging tree; CI uses it)
#   --allow-placeholders  accept REPLACE_ME values (only with --root/--dry-run)
#
# Every managed file starts with the marker line
#   # managed-by: qontinui-stack/local/postgres/install.sh
# which is how --uninstall recognises its own files.
#
# Exit codes: 0 ok; 1 --check found a failure; 2 usage/precondition refused.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER='# managed-by: qontinui-stack/local/postgres/install.sh'
MODE=install
CONFIG=""
DRY=0
ROOT=""
ALLOW_UNENCRYPTED=0
ENABLE_SERVICES=0
ALLOW_PLACEHOLDERS=0

die() { echo "install.sh: $*" >&2; exit 2; }
say() { echo "==> $*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check ;;
    --uninstall) MODE=uninstall ;;
    --config) CONFIG="${2:?--config needs a file}"; shift ;;
    --dry-run) DRY=1 ;;
    --root) ROOT="${2:?--root needs a directory}"; shift ;;
    --allow-unencrypted) ALLOW_UNENCRYPTED=1 ;;
    --enable-services) ENABLE_SERVICES=1 ;;
    --allow-placeholders) ALLOW_PLACEHOLDERS=1 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

if [ "$ALLOW_PLACEHOLDERS" = 1 ] && [ -z "$ROOT" ] && [ "$DRY" = 0 ]; then
  die "--allow-placeholders is for --root or --dry-run only"
fi
if [ -n "$ROOT" ]; then
  mkdir -p "$ROOT"
  ROOT="$(cd "$ROOT" && pwd)"
fi

INSTALLED_CONFIG="$ROOT/etc/qontinui-pg/qontinui-pg.conf"
if [ -z "$CONFIG" ]; then
  CONFIG="$INSTALLED_CONFIG"
fi
[ -r "$CONFIG" ] || die "config not readable: $CONFIG (start from $HERE/qontinui-pg.conf.example)"

PH=()
[ "$ALLOW_PLACEHOLDERS" = 1 ] && PH=(--allow-placeholders)

# Run a mutating command, or print it under --dry-run.
run() {
  if [ "$DRY" = 1 ]; then
    printf '+ (dry-run) %q' "$1"; shift; printf ' %q' "$@"; echo
  else
    "$@"
  fi
}

qpg() { python3 "$HERE/qontinui-pg" -c "$CONFIG" "$@"; }

conf_get() {  # conf_get SECTION KEY DEFAULT
  python3 - "$CONFIG" "$1" "$2" "$3" <<'PY'
import configparser, sys
p = configparser.ConfigParser(interpolation=None)
p.read(sys.argv[1], encoding="utf-8")
print(p.get(sys.argv[2], sys.argv[3], fallback=sys.argv[4]).strip())
PY
}

PG_VERSION="$(conf_get cluster version 16)"
CLUSTER="$(conf_get cluster name qontinui)"
DATA_DIR="$(conf_get cluster data_directory '')"
PORT="$(conf_get cluster port 5432)"
PGB_ENABLED="$(conf_get pgbackrest enabled false)"
UNIT_NAMES=(follower.service seqsync.timer watchdog.timer partitions.timer)
case "$PGB_ENABLED" in true|yes|on|1) UNIT_NAMES+=(backup-full.timer backup-diff.timer) ;; esac

need_root() {
  if [ -z "$ROOT" ] && [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then
    die "$MODE needs root (or --dry-run, or --root DIR for a staging tree)"
  fi
}

# --------------------------------------------------------------------- check
do_check() {
  local rc=0
  say "managed files and the D4 rule ($([ -n "$ROOT" ] && echo "staging tree $ROOT" || echo "this host"))"
  qpg host check --root "$ROOT" "${PH[@]}" || rc=1
  if [ -n "$ROOT" ]; then
    echo "SKIP live checks (cluster, systemd, pg_hba_file_rules, follower): --root staging tree"
    return $rc
  fi
  say "live checks"
  if ! command -v pg_lsclusters >/dev/null 2>&1; then
    echo "FAIL postgresql-common is not installed"; return 1
  fi
  if pg_lsclusters --no-header "$PG_VERSION" "$CLUSTER" 2>/dev/null | grep -q online; then
    echo "OK   cluster $PG_VERSION/$CLUSTER online"
  else
    echo "FAIL cluster $PG_VERSION/$CLUSTER is not online"; rc=1
  fi
  if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
    local owner mode
    owner="$(stat -c %U "$DATA_DIR")"; mode="$(stat -c %a "$DATA_DIR")"
    if [ "$owner" = postgres ] && [ "$mode" = 700 ]; then echo "OK   $DATA_DIR owner postgres mode 700"
    else echo "FAIL $DATA_DIR owner $owner mode $mode (want postgres 700)"; rc=1; fi
    if on_crypt_device "$DATA_DIR"; then echo "OK   $DATA_DIR is on a dm-crypt (LUKS) device"
    else echo "FAIL $DATA_DIR is not on a dm-crypt device (OD1 isolation rule)"; rc=1; fi
  else
    echo "FAIL data directory $DATA_DIR missing"; rc=1
  fi
  local ver
  ver="$(dpkg-query -W -f='${Version}' "postgresql-$PG_VERSION-pgvector" 2>/dev/null || true)"
  case "$ver" in 0.8.*|0.9.*|0.1[0-9].*) echo "OK   pgvector $ver (>= RDS 0.8.1)" ;;
    *) echo "FAIL pgvector package version '$ver' (need 0.8.x or newer)"; rc=1 ;; esac
  if runuser -u postgres -- python3 /opt/qontinui-pg/qontinui-pg -c "$INSTALLED_CONFIG" host check-hba-live; then :; else rc=1; fi
  if runuser -u postgres -- psql -X -qtAc "SELECT 1 FROM pg_namespace WHERE nspname = 'follower'" \
       -d "$(conf_get cluster database qontinui_db)" -p "$PORT" 2>/dev/null | grep -q 1; then
    runuser -u postgres -- python3 /opt/qontinui-pg/qontinui-pg -c "$INSTALLED_CONFIG" follower check || rc=1
  else
    echo "INFO follower schema not installed yet (Phase 3: qontinui-pg follower install)"
  fi
  local want have
  want="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from qpg.hostcfg import sha256_tree; print(sha256_tree(sys.argv[2]))' "$HERE/lib" "$HERE/lib")"
  have="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from qpg.hostcfg import sha256_tree; print(sha256_tree(sys.argv[2]))' "$HERE/lib" /opt/qontinui-pg/lib 2>/dev/null || echo none)"
  if [ "$want" = "$have" ]; then echo "OK   /opt/qontinui-pg/lib matches this checkout"
  else echo "FAIL /opt/qontinui-pg/lib differs from this checkout (re-run install.sh)"; rc=1; fi
  for u in "${UNIT_NAMES[@]}"; do
    echo "INFO qontinui-pg-$u: $(systemctl is-enabled "qontinui-pg-$u" 2>/dev/null || echo not-enabled)"
  done
  return $rc
}

on_crypt_device() {
  local src
  src="$(findmnt -no SOURCE -T "$1" 2>/dev/null | head -1)" || return 1
  [ -n "$src" ] || return 1
  lsblk -nso TYPE "$src" 2>/dev/null | grep -qx crypt
}

# ------------------------------------------------------------------- install
install_code() {
  local dest="$ROOT/opt/qontinui-pg"
  run mkdir -p "$dest"
  run rm -rf "$dest/lib" "$dest/templates"
  run cp -r "$HERE/lib" "$HERE/templates" "$dest/"
  run install -m 0755 "$HERE/qontinui-pg" "$dest/qontinui-pg"
  run find "$dest" -name __pycache__ -prune -exec rm -rf {} +
  run mkdir -p "$ROOT/usr/local/bin"
  run ln -sfn /opt/qontinui-pg/qontinui-pg "$ROOT/usr/local/bin/qontinui-pg"
}

install_config() {
  run mkdir -p "$ROOT/etc/qontinui-pg" "$ROOT/var/lib/qontinui-pg"
  if [ "$CONFIG" != "$INSTALLED_CONFIG" ]; then
    if [ -e "$INSTALLED_CONFIG" ] && ! cmp -s "$CONFIG" "$INSTALLED_CONFIG"; then
      say "replacing $INSTALLED_CONFIG with $CONFIG (previous kept as .previous)"
      run cp -p "$INSTALLED_CONFIG" "$INSTALLED_CONFIG.previous"
    fi
    run install -m 0640 "$CONFIG" "$INSTALLED_CONFIG"
  fi
  if [ -z "$ROOT" ]; then
    run chown root:postgres "$ROOT/etc/qontinui-pg" "$INSTALLED_CONFIG"
    run chmod 0750 "$ROOT/etc/qontinui-pg"
    run chown postgres:postgres "$ROOT/var/lib/qontinui-pg"
    run chmod 0700 "$ROOT/var/lib/qontinui-pg"
  fi
}

render_files() {
  if [ "$DRY" = 1 ]; then
    say "files that would be written:"
    qpg host list | sed 's/^/    /'
    return
  fi
  qpg host render --root "${ROOT:-/}" "${PH[@]}" >/dev/null
  if [ -z "$ROOT" ]; then
    chown root:postgres "/etc/postgresql/$PG_VERSION/$CLUSTER/pg_hba.conf"
    [ -e /etc/pgbackrest/pgbackrest.conf ] && chown root:postgres /etc/pgbackrest/pgbackrest.conf
  fi
}

install_packages() {
  if [ -n "$ROOT" ]; then echo "SKIP packages (--root)"; return; fi
  command -v apt-get >/dev/null 2>&1 || die "apt-get not found: this installer targets Debian/Ubuntu"
  # The createcluster.d drop-in must exist before postgresql-16 is installed,
  # or the package creates Debian's default "main" cluster.
  run mkdir -p /etc/postgresql-common/createcluster.d
  if [ "$DRY" = 0 ]; then
    printf '%s\n# Installing postgresql-%s must not create the default "main" cluster.\ncreate_main_cluster = false\n' \
      "$MARKER" "$PG_VERSION" > /etc/postgresql-common/createcluster.d/50-qontinui.conf
  fi
  if ! ls /etc/apt/sources.list.d/pgdg.* >/dev/null 2>&1; then
    run apt-get update -qq
    run apt-get install -y -qq postgresql-common ca-certificates
    run /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
  fi
  run apt-get update -qq
  run apt-get install -y -qq "postgresql-$PG_VERSION" "postgresql-$PG_VERSION-pgvector" \
    "postgresql-client-$PG_VERSION" pgbackrest python3 curl
}

create_cluster() {
  if [ -n "$ROOT" ]; then echo "SKIP cluster creation (--root)"; return; fi
  [ -n "$DATA_DIR" ] || die "[cluster] data_directory is empty"
  local parent; parent="$(dirname "$DATA_DIR")"
  if [ "$DRY" = 0 ]; then
    mkdir -p "$parent"
    if ! on_crypt_device "$parent"; then
      if [ "$ALLOW_UNENCRYPTED" = 1 ]; then
        echo "WARNING: $parent is not on a dm-crypt device; continuing under --allow-unencrypted" >&2
      else
        die "$parent is not on a dm-crypt (LUKS) device. Prepare the encrypted volume first (OD1), or pass --allow-unencrypted for a non-production host."
      fi
    fi
  fi
  if pg_lsclusters --no-header 2>/dev/null | awk '{print $1"/"$2}' | grep -qx "$PG_VERSION/$CLUSTER"; then
    echo "cluster $PG_VERSION/$CLUSTER exists"
    return
  fi
  run mkdir -p "$parent"
  run chown postgres:postgres "$parent"
  run chmod 0700 "$parent"
  run pg_createcluster "$PG_VERSION" "$CLUSTER" -d "$DATA_DIR" -p "$PORT" --start-conf=auto -- \
    --data-checksums --auth-local=peer --auth-host=scram-sha-256
}

pgbackrest_cipher() {
  case "$PGB_ENABLED" in true|yes|on|1) ;; *) return ;; esac
  local f="$ROOT/etc/pgbackrest/conf.d/qontinui-cipher.conf"
  if [ -e "$f" ]; then echo "pgBackRest cipher passphrase present (not shown)"; return; fi
  run mkdir -p "$ROOT/etc/pgbackrest/conf.d"
  if [ "$DRY" = 1 ]; then echo "+ (dry-run) generate the repo2 cipher passphrase into $f (0640 root:postgres)"; return; fi
  local pass
  pass="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
  ( umask 027; printf '%s\n[global]\nrepo2-cipher-pass=%s\n' "$MARKER" "$pass" > "$f" )
  [ -z "$ROOT" ] && chown root:postgres "$f"
  chmod 0640 "$f"
  # OD4: a repository whose passphrase exists in one place is lost with that
  # place. Show it ONCE, on the controlling terminal only (never stdout, never
  # a log), for the operator to record offline.
  if [ -z "$ROOT" ] && { exec 3>/dev/tty; } 2>/dev/null; then
    printf '\n*** pgBackRest repo2 cipher passphrase (record it OFFLINE now; it is not shown again):\n%s\n\n' "$pass" >&3
    exec 3>&-
  else
    echo "No terminal: read the passphrase once as root from $f and record it offline (OD4)."
  fi
}

systemd_apply() {
  if [ -n "$ROOT" ]; then echo "SKIP systemd (--root)"; return; fi
  run systemctl daemon-reload
  run systemctl enable --now "postgresql@$PG_VERSION-$CLUSTER.service"
  # pg_hba/conf.d changes: a reload picks up pg_hba; listen_addresses, ssl
  # and wal_level need a restart, done only when the running values differ.
  run systemctl reload "postgresql@$PG_VERSION-$CLUSTER.service"
  if [ "$DRY" = 0 ]; then
    local pending
    pending="$(runuser -u postgres -- psql -X -qtA -p "$PORT" -c \
      "SELECT count(*) FROM pg_settings WHERE pending_restart" 2>/dev/null || echo 0)"
    if [ "${pending:-0}" != 0 ]; then
      local subs
      subs="$(runuser -u postgres -- psql -X -qtA -p "$PORT" -d "$(conf_get cluster database qontinui_db)" -c \
        "SELECT count(*) FROM pg_subscription" 2>/dev/null || echo UNKNOWN)"
      if [ "$subs" != 0 ]; then
        # A restart stops apply; an unbounded stop invalidates the publisher's
        # slot (D11). Never restart a subscriber unattended.
        die "settings need a cluster restart, but pg_subscription holds ${subs} subscription(s) (or could not be read). Restart it yourself in a planned window: systemctl restart postgresql@$PG_VERSION-$CLUSTER"
      fi
      say "settings pending restart: restarting the cluster (it holds no subscription)"
      systemctl restart "postgresql@$PG_VERSION-$CLUSTER.service"
    fi
  fi
  if [ "$ENABLE_SERVICES" = 1 ]; then
    for u in "${UNIT_NAMES[@]}"; do run systemctl enable --now "qontinui-pg-$u"; done
  else
    echo "units installed, NOT enabled (Phase 3: install.sh --enable-services)"
  fi
}

do_install() {
  need_root
  say "packages (PGDG postgresql-$PG_VERSION, pgvector, pgBackRest)"
  install_packages
  say "configuration and code"
  install_config
  install_code
  say "cluster $PG_VERSION/$CLUSTER"
  create_cluster
  say "managed files"
  render_files
  pgbackrest_cipher
  say "systemd"
  systemd_apply
  if [ "$DRY" = 1 ]; then say "dry run: nothing was changed"; return 0; fi
  say "check"
  do_check
}

# ----------------------------------------------------------------- uninstall
do_uninstall() {
  need_root
  if [ -z "$ROOT" ]; then
    for u in follower.service seqsync.timer watchdog.timer partitions.timer backup-full.timer backup-diff.timer; do
      if systemctl list-unit-files "qontinui-pg-$u" >/dev/null 2>&1; then
        run systemctl disable --now "qontinui-pg-$u" 2>/dev/null || true
      fi
    done
  fi
  local f
  while IFS= read -r f; do
    f="$ROOT$f"
    if [ -f "$f" ] && head -1 "$f" | grep -qxF "$MARKER"; then
      run rm -f "$f"
    elif [ -f "$f" ]; then
      echo "keeping $f: not written by this installer (no marker line)"
    fi
  done < <(qpg host list)
  f="$ROOT/etc/pgbackrest/conf.d/qontinui-cipher.conf"
  [ -e "$f" ] && echo "keeping $f: the repo2 passphrase (deleting it makes repo2 unreadable)"
  run rm -rf "$ROOT/opt/qontinui-pg"
  run rm -f "$ROOT/usr/local/bin/qontinui-pg"
  [ -z "$ROOT" ] && run systemctl daemon-reload
  echo "kept: $INSTALLED_CONFIG, /var/lib/qontinui-pg (the passfile), and the cluster's data."
  echo "To destroy the cluster (operator decision, never run here): pg_dropcluster --stop $PG_VERSION $CLUSTER"
}

case "$MODE" in
  check) do_check ;;
  install) do_install ;;
  uninstall) do_uninstall ;;
esac
