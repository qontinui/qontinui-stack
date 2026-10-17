"""Source guards for ``aws/modules/replication-tunnel-wireguard``.

Runnable via either::

    python -m pytest qontinui-stack/scripts/tests/test_replication_tunnel_wireguard.py
    python -m unittest qontinui-stack.scripts.tests.test_replication_tunnel_wireguard

The module is the D2(e) WireGuard tunnel of plan
2026-10-15-coord-and-web-data-move-off-aws-by-logical-replication. Its security
properties live in two files CI otherwise only checks for syntax: the first-boot
script ``user-data.sh.tftpl`` and ``main.tf``. These tests read both as text
(no terraform, no credentials, no AWS) and pin the properties a review found
missing once already:

- the host comes up FAIL-CLOSED: ``ip_forward`` persisted as 0, a static base
  nftables ruleset whose forward chain drops everything, loaded by
  nftables.service before wg-quick@wg0 (which requires it), and forwarding
  turned on only after the firewall script's ``nft -f`` succeeded;
- the security group admits only UDP ``wg_port``;
- every resource and data source is counted on ``enabled``, so the disabled
  module in aws/staging creates nothing.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

_MODULE_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "aws"
    / "modules"
    / "replication-tunnel-wireguard"
)
_USER_DATA = _MODULE_DIR / "user-data.sh.tftpl"
_MAIN_TF = _MODULE_DIR / "main.tf"


def _heredoc(text: str, path: str) -> str:
    """Body of the ``cat > <path> <<'EOF'`` / ``<<EOF`` heredoc writing ``path``."""
    m = re.search(
        r"^cat > " + re.escape(path) + r" <<'?EOF'?\n(.*?)^EOF$",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if m is None:
        raise AssertionError(f"user_data writes no heredoc to {path}")
    return m.group(1)


def _blocks(text: str, kind: str) -> dict[str, str]:
    """Top-level ``<kind> "<type>" "<name>" { ... }`` blocks, keyed type.name."""
    out = {}
    for m in re.finditer(
        r'^' + kind + r' "([\w]+)" "([\w]+)" \{\n(.*?)^\}$',
        text,
        re.MULTILINE | re.DOTALL,
    ):
        out[f"{m.group(1)}.{m.group(2)}"] = m.group(3)
    return out


def _nested(body: str, name: str) -> list[str]:
    """Bodies of the nested ``<name> { ... }`` blocks at two-space indent."""
    return re.findall(
        r"^  " + name + r" \{\n(.*?)^  \}$", body, re.MULTILINE | re.DOTALL
    )


def _attr(body: str, name: str) -> str | None:
    m = re.search(r"^\s*" + name + r"\s*=\s*(.+?)\s*(#.*)?$", body, re.MULTILINE)
    return m.group(1) if m else None


class UserDataFailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ud = _USER_DATA.read_text()

    def test_sysctl_persists_ip_forward_zero(self) -> None:
        body = _heredoc(self.ud, "/etc/sysctl.d/90-qontinui-wg.conf")
        settings = re.findall(r"^\s*net\.ipv4\.ip_forward\s*=\s*(\S+)", body, re.M)
        self.assertEqual(settings, ["0"])
        # No sysctl config line anywhere in user_data persists forwarding on.
        self.assertNotRegex(self.ud, r"(?m)^\s*net\.ipv4\.ip_forward\s*=\s*1\b")

    def test_firewall_enables_forwarding_only_after_nft(self) -> None:
        script = _heredoc(self.ud, "/usr/local/sbin/qontinui-wg-firewall")
        lines = script.splitlines()
        self.assertEqual(lines[0], "#!/bin/bash")
        self.assertEqual(lines[1], "set -euo pipefail")
        nft = [i for i, ln in enumerate(lines) if re.match(r"^nft -f\b", ln)]
        fwd = [
            i
            for i, ln in enumerate(lines)
            if re.search(r"\bsysctl\b.*-w\s+net\.ipv4\.ip_forward=1\b", ln)
        ]
        self.assertEqual(len(nft), 1, "exactly one nft -f in the firewall script")
        self.assertEqual(len(fwd), 1, "exactly one ip_forward=1 in the firewall script")
        # The nft heredoc's terminator, so "after" means after the ruleset loaded.
        end = lines.index("NFT", nft[0])
        self.assertGreater(fwd[0], end)
        # Nothing outside the firewall script turns forwarding on.
        self.assertEqual(len(re.findall(r"ip_forward=1", self.ud)), 1)

    def test_wg_quick_requires_and_follows_nftables(self) -> None:
        dropin = _heredoc(
            self.ud,
            "/etc/systemd/system/wg-quick@wg0.service.d/10-qontinui-needs-nftables.conf",
        )
        self.assertRegex(dropin, r"(?m)^\[Unit\]$")
        self.assertRegex(dropin, r"(?m)^Requires=nftables\.service$")
        self.assertRegex(dropin, r"(?m)^After=nftables\.service$")

    def test_nftables_ordered_before_wg_and_enabled(self) -> None:
        dropin = _heredoc(
            self.ud,
            "/etc/systemd/system/nftables.service.d/10-qontinui-before-wg.conf",
        )
        self.assertRegex(dropin, r"(?m)^Before=wg-quick@wg0\.service$")
        self.assertRegex(self.ud, r"(?m)^systemctl enable --now nftables\.service$")
        # The base ruleset is loaded before wg0 is first brought up.
        self.assertLess(
            self.ud.index("systemctl enable --now nftables.service"),
            self.ud.index("systemctl enable --now wg-quick@wg0.service"),
        )

    def test_base_ruleset_forward_chain_drops(self) -> None:
        base = _heredoc(self.ud, "/etc/nftables.conf")
        chain = re.search(r"chain forward \{\n(.*?)\n  \}", base, re.DOTALL)
        self.assertIsNotNone(chain, "base ruleset has no forward chain")
        self.assertRegex(chain.group(1), r"hook forward priority filter; policy drop;")
        self.assertNotIn("accept", chain.group(1))
        # Same table the firewall script swaps, so the swap replaces it.
        script = _heredoc(self.ud, "/usr/local/sbin/qontinui-wg-firewall")
        self.assertIn("table inet qontinui_wg {", base)
        self.assertIn("delete table inet qontinui_wg", script)


class MainTfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        text = _MAIN_TF.read_text()
        cls.resources = _blocks(text, "resource")
        cls.data = _blocks(text, "data")

    def test_found_the_blocks(self) -> None:
        # Guards the parser: an empty parse would make the tests below vacuous.
        self.assertIn("aws_instance.tunnel", self.resources)
        self.assertIn("aws_security_group.tunnel", self.resources)
        self.assertIn("aws_ami.debian", self.data)

    def test_every_block_is_counted_on_enabled(self) -> None:
        for name, body in {**self.resources, **self.data}.items():
            count = _attr(body, "count")
            self.assertIsNotNone(count, f"{name} has no count")
            self.assertRegex(
                count,
                r"^(local\.enabled_count|var\.enabled\b.*\? 1 : 0)$",
                f"{name} count is not gated on enabled: {count}",
            )

    def test_sg_ingress_is_udp_wg_port_only(self) -> None:
        sg = self.resources["aws_security_group.tunnel"]
        ingress = _nested(sg, "ingress")
        self.assertEqual(len(ingress), 1, "exactly one inline ingress rule")
        rule = ingress[0]
        self.assertEqual(_attr(rule, "protocol"), '"udp"')
        self.assertEqual(_attr(rule, "from_port"), "var.wg_port")
        self.assertEqual(_attr(rule, "to_port"), "var.wg_port")
        # No separate rule resource opens the tunnel group either.
        for name, body in self.resources.items():
            if name.startswith("aws_vpc_security_group_ingress_rule."):
                self.fail(f"{name} adds ingress outside the inline rule")
            if name.startswith("aws_security_group_rule."):
                self.assertNotRegex(
                    body,
                    r"(?m)^\s*security_group_id\s*=\s*aws_security_group\.tunnel",
                    f"{name} adds a rule to the tunnel group",
                )

    def test_wg_port_default_is_51820(self) -> None:
        text = _MAIN_TF.read_text()
        m = re.search(r'variable "wg_port" \{\n(.*?)^\}$', text, re.M | re.S)
        self.assertIsNotNone(m)
        self.assertEqual(_attr(m.group(1), "default"), "51820")

    def test_no_key_pair_or_instance_profile(self) -> None:
        inst = self.resources["aws_instance.tunnel"]
        self.assertIsNone(_attr(inst, "key_name"))
        self.assertIsNone(_attr(inst, "iam_instance_profile"))
        self.assertEqual(_attr(inst, "http_tokens"), '"required"')


if __name__ == "__main__":
    unittest.main()
