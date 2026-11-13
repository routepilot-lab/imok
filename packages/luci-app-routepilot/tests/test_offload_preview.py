"""Check offline rule shape without sending an nft transaction to the host."""

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import mplane


class OffloadPreviewTests(unittest.TestCase):
    def test_candidate_changes_only_unprotected_device_forward_rules(self):
        devices = [
            dict(
                scope="device",
                lan_interface="br-lan",
                mac=f"02:00:00:00:00:{number}",
                ip=f"192.168.50.{number}",
                mode="bypass_cn",
                **flags,
            )
            for number, flags in (
                (10, {"egress_guard": False}),
                (11, {"egress_guard": True}),
                (12, {}),
            )
        ]
        for proxy in ("rp-host", "rp-backup"):
            with self.subTest(proxy=proxy), patch.object(mplane, "command") as command:
                kwargs = dict(
                    devices=devices, wan="wan-test", proxy=proxy, control=proxy == "rp-host"
                )
                before = mplane.fw4_rule_text(**kwargs).splitlines()
                after = mplane.fw4_rule_text(**kwargs, preview_offload=True).splitlines()
                command.assert_not_called()
            differences = [(a, b) for a, b in zip(before, after) if a != b]
            self.assertEqual(len(before), len(after))
            self.assertEqual(len(differences), 2)
            (old_out, out), (old_return, reply) = differences
            self.assertIn('oifname { "' + proxy + '", "wan-test" }', old_out)
            self.assertIn('192.168.50.10 oifname "' + proxy + '"', out)
            self.assertIn("forward ip daddr 192.168.50.10", old_return)
            self.assertIn('iifname "' + proxy + '" ip daddr 192.168.50.10', reply)
            self.assertNotIn("wan-test", out)
            self.assertTrue(any("192.168.50.11 ct state" in line for line in after))
            self.assertTrue(any("192.168.50.12 ct state" in line for line in after))
