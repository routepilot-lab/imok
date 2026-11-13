# Synthetic unit-test addresses; no live router, tenant or user configuration.
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
from masque_config import compile_config
from native import block_aaaa_for


class IPv6ChoiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.rules = Path(self.folder.name)
        self.cfg = {
            "main": {"exit": "usa", "vnet_usa": "11111111-1111-4111-8111-111111111111"},
            "policies": [
                {
                    "id": "one",
                    "enabled": "1",
                    "ip": "10.77.50.10",
                    "mac": "02:00:00:00:00:01",
                    "mode": "global",
                }
            ],
        }

    def tearDown(self):
        self.folder.cleanup()

    def compile(self):
        return compile_config(self.cfg, self.rules)

    def test_legacy_unchecked_aaaa_still_defaults_to_block(self):
        self.cfg["main"]["block_aaaa"] = "0"
        result = self.compile()
        self.assertIn("meta nfproto ipv6 reject", result["nft"])
        self.assertTrue(result["dns"]["policies"][0]["block_aaaa"])
        self.assertTrue(result["dns"]["block_aaaa"])

    def test_default_direct_omits_guard_and_explicitly_allows_aaaa(self):
        self.cfg["main"]["ipv6_mode"] = "direct"
        result = self.compile()
        self.assertNotIn("meta nfproto ipv6 reject", result["nft"])
        self.assertFalse(result["dns"]["policies"][0]["block_aaaa"])
        guard = result["nft"].split("chain ipv6_guard")[1].split("chain dns_guard")[0]
        self.assertNotIn(" accept", guard.replace("policy accept;", ""))

    def test_same_lan_mixed_mac_modes_are_independent(self):
        self.cfg["policies"].append(
            dict(
                self.cfg["policies"][0],
                id="two",
                ip="10.77.50.11",
                mac="02:00:00:00:00:02",
                ipv6_mode="direct",
            )
        )
        result = self.compile()
        self.assertIn("ether saddr 02:00:00:00:00:01 meta nfproto ipv6 reject", result["nft"])
        self.assertNotIn("ether saddr 02:00:00:00:00:02 meta nfproto ipv6 reject", result["nft"])
        self.assertEqual([p["block_aaaa"] for p in result["dns"]["policies"]], [True, False])

    def test_device_can_override_default_direct_with_block(self):
        self.cfg["main"]["ipv6_mode"] = "direct"
        self.cfg["policies"][0]["ipv6_mode"] = "block"
        self.assertTrue(self.compile()["dns"]["policies"][0]["block_aaaa"])

    def test_whole_lan_group_ignores_saved_device_override(self):
        self.cfg["main"].update(
            scope="lan", ipv6_mode="direct", lan_network="10.77.50.0/24", lan_mode="global"
        )
        self.cfg["policies"][0]["ipv6_mode"] = "invalid-unused-device-value"
        self.assertFalse(self.compile()["dns"]["policies"][0]["block_aaaa"])

    def test_multiple_lans_follow_group_choice(self):
        self.cfg["main"].update(
            scope="lan",
            lan_interfaces=["br-lan", "br-guest"],
            lan_networks=[
                {"interface": "br-lan", "network": "10.77.50.0/24"},
                {"interface": "br-guest", "network": "192.168.9.0/24"},
            ],
            lan_mode="global",
        )
        for mode, count, blocked in [("block", 2, True), ("direct", 0, False)]:
            self.cfg["main"]["ipv6_mode"] = mode
            result = self.compile()
            self.assertEqual(result["nft"].count("meta nfproto ipv6 reject"), count)
            self.assertEqual(
                [p["block_aaaa"] for p in result["dns"]["policies"]], [blocked, blocked]
            )

    def test_invalid_main_and_enabled_device_enums_are_rejected(self):
        for value in ["proxy", "", None, ["block"]]:
            self.cfg["main"]["ipv6_mode"] = value
            with self.assertRaises(ValueError):
                self.compile()
        self.cfg["main"].pop("ipv6_mode")
        for mode in ["global", "direct"]:
            self.cfg["policies"][0].update(mode=mode, ipv6_mode="invalid")
            with self.assertRaises(ValueError):
                self.compile()

    def test_unmanaged_device_does_not_gain_ipv6_or_dns_rules(self):
        self.cfg["policies"][0].update(mode="direct", ipv6_mode="block")
        result = self.compile()
        self.assertNotIn("meta nfproto ipv6 reject", result["nft"])
        self.assertEqual(result["dns"]["policies"], [])
        self.assertEqual(result["devices"][0]["ipv6_mode"], "unmanaged")

    def test_policy_dns_false_overrides_safe_global_default(self):
        self.assertFalse(block_aaaa_for({"block_aaaa": False}, {"block_aaaa": True}))
        self.assertTrue(block_aaaa_for({}, {}))
        for value in ["0", "1", 0, 1, None, []]:
            with self.assertRaises(ValueError):
                block_aaaa_for({"block_aaaa": value}, {})

    def test_compilation_preserves_input(self):
        before = json.loads(json.dumps(self.cfg))
        self.compile()
        self.assertEqual(self.cfg, before)
