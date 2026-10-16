"""install.sh in its staging (--root) and --dry-run modes; --check's D4 and A4 assertions.

No database and no root are needed: --root renders every managed file under a
temporary directory and skips packages, the cluster and systemd, and --check
judges that tree. Each tampering below must make --check fail.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("QPG_SRC", os.path.dirname(HERE))
INSTALL = os.path.join(SRC, "install.sh")
EXAMPLE = os.path.join(SRC, "qontinui-pg.conf.example")

VALUES = {
    ("cluster", "tailnet_address"): "100.64.0.10",
    ("cluster", "ssl_cert_file"): "/etc/qontinui-pg/tls/server.crt",
    ("cluster", "ssl_key_file"): "/etc/qontinui-pg/tls/server.key",
    ("publisher", "dsn"): "host=publisher.invalid port=5432 dbname=qontinui_db user=qontinui_replication",
    ("pgbackrest", "enabled"): "true",
    ("pgbackrest", "repo2_type"): "posix",
    ("pgbackrest", "repo2_path"): "/srv/repo2",
}


def make_config(path: str, overrides: dict | None = None) -> None:
    values = dict(VALUES)
    values.update(overrides or {})
    section = None
    out = []
    with open(EXAMPLE, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"^\[(.+)\]\s*$", line)
            if m:
                section = m.group(1)
            k = re.match(r"^([A-Za-z0-9_]+)\s*=", line)
            if k and (section, k.group(1)) in values:
                line = f"{k.group(1)} = {values[(section, k.group(1))]}\n"
            out.append(line)
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(out)
    os.chmod(path, 0o640)


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", INSTALL, *args], capture_output=True, text=True, timeout=120)


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qpg-install-")
        self.root = os.path.join(self.tmp, "root")
        self.conf = os.path.join(self.tmp, "qontinui-pg.conf")
        make_config(self.conf)
        res = sh("--root", self.root, "--config", self.conf)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.hba = os.path.join(self.root, "etc/postgresql/16/qontinui/pg_hba.conf")
        self.pgconf = os.path.join(self.root, "etc/postgresql/16/qontinui/conf.d/50-qontinui.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def check(self) -> subprocess.CompletedProcess:
        return sh("--check", "--root", self.root)

    def edit(self, path: str, fn) -> None:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(fn(text))

    def assert_check_fails(self, needle: str):
        res = self.check()
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        fails = [ln for ln in res.stdout.splitlines() if ln.startswith("FAIL")]
        self.assertTrue(any(needle in ln for ln in fails), f"no FAIL line mentions {needle!r}:\n{res.stdout}")

    def test_01_staging_install_checks_green(self):
        res = self.check()
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("SKIP live checks", res.stdout)
        with open(self.hba, encoding="utf-8") as fh:
            hba = fh.read()
        self.assertIn("host    qontinui_db  qontinui_user  100.64.0.0/10  scram-sha-256", hba)
        self.assertIn("hostssl all  all  100.64.0.0/10  scram-sha-256", hba)
        units = os.listdir(os.path.join(self.root, "etc/systemd/system"))
        self.assertIn("qontinui-pg-follower.service", units)
        self.assertIn("qontinui-pg-backup-full.timer", units)
        cipher = os.path.join(self.root, "etc/pgbackrest/conf.d/qontinui-cipher.conf")
        self.assertEqual(os.stat(cipher).st_mode & 0o777, 0o640)
        self.assertTrue(os.path.islink(os.path.join(self.root, "usr/local/bin/qontinui-pg")))

    def test_02_non_tls_for_another_role_fails(self):
        self.edit(self.hba, lambda t: t.replace(
            "# Every other client", "host all all 100.64.0.0/10 scram-sha-256\n# Every other client"))
        self.assert_check_fails("non-TLS admitted only for qontinui_user")

    def test_03_non_tls_from_outside_the_tailnet_fails(self):
        self.edit(self.hba, lambda t: t.replace(
            "# Every other client", "host qontinui_db qontinui_user 0.0.0.0/0 scram-sha-256\n# Every other client"))
        self.assert_check_fails("non-TLS admitted only for qontinui_user")

    def test_04_missing_app_non_tls_line_fails(self):
        # coord connects NoTls: a hostssl-only file would refuse it at cutover.
        self.edit(self.hba, lambda t: re.sub(r"^host    qontinui_db .*\n", "", t, flags=re.M))
        self.assert_check_fails("non-TLS tailnet login exists")

    def test_05_tls_from_anywhere_fails(self):
        self.edit(self.hba, lambda t: t + "hostssl all all 0.0.0.0/0 scram-sha-256\n")
        self.assert_check_fails("TLS client from the tailnet")

    def test_06_trust_fails(self):
        self.edit(self.hba, lambda t: t.replace("local   all  all  scram-sha-256", "local   all  all  trust"))
        self.assert_check_fails("trust is forbidden")

    def test_07_listen_everywhere_fails(self):
        self.edit(self.pgconf, lambda t: t.replace("listen_addresses = '100.64.0.10'", "listen_addresses = '*'"))
        self.assert_check_fails("listen_addresses")

    def test_08_queue_max_removed_fails(self):
        path = os.path.join(self.root, "etc/pgbackrest/pgbackrest.conf")
        self.edit(path, lambda t: t.replace("archive-push-queue-max=20GiB\n", ""))
        self.assert_check_fails("archive-push-queue-max")

    def test_10_placeholders_are_refused(self):
        root = os.path.join(self.tmp, "root2")
        res = sh("--root", root, "--config", EXAMPLE)
        self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
        self.assertIn("REPLACE_ME", res.stderr)

    def test_11_uninstall_removes_only_its_files(self):
        res = sh("--uninstall", "--root", self.root)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(os.path.exists(self.hba))
        self.assertFalse(os.path.exists(os.path.join(self.root, "opt/qontinui-pg")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc/qontinui-pg/qontinui-pg.conf")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "etc/pgbackrest/conf.d/qontinui-cipher.conf")))
        self.assertIn("pg_dropcluster", res.stdout)

    def test_12_dry_run_changes_nothing(self):
        if not shutil.which("apt-get") or os.path.exists("/etc/qontinui-pg"):
            self.skipTest("needs a Debian host with no local/postgres install (the tools container)")
        res = sh("--dry-run", "--config", self.conf)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("dry run: nothing was changed", res.stdout)
        self.assertIn("(dry-run) pg_createcluster", res.stdout)
        for path in ("/etc/qontinui-pg", "/opt/qontinui-pg", "/etc/postgresql/16/qontinui",
                     "/etc/systemd/system/qontinui-pg-follower.service", "/usr/local/bin/qontinui-pg"):
            self.assertFalse(os.path.exists(path), f"--dry-run created {path}")


if __name__ == "__main__":
    unittest.main()
