import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import egress_guard as guard
import control
import service as routing_service
import masque_config
import native
from connection_events import record


def configuration(scope="devices"):
    return {
        "main": {
            "enabled": "1",
            "scope": scope,
            "lan_interfaces": ["br-lan"],
            "lan_network": "10.1.0.0/24",
            "lan_mode": "global",
            "exit": "11111111-1111-4111-8111-111111111111",
        },
        "policies": [
            {
                "id": "computer",
                "enabled": "1",
                "mode": "global",
                "mac": "02:11:22:33:44:55",
                "ip": "10.1.0.2",
                "ipv6_mode": "direct",
                "egress_guard": "1",
            },
            {
                "id": "direct",
                "enabled": "1",
                "mode": "direct",
                "mac": "02:11:22:33:44:66",
                "ip": "10.1.0.3",
            },
        ],
    }


class EgressGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run = Path(temp.name)
        target = patch.object(guard, "RUN", self.run)
        target.start()
        self.addCleanup(target.stop)

    def test_history_survives_supervisor_takeover_and_does_not_log_lease_renewals(self):
        cfg = configuration()
        link = SimpleNamespace(
            is_symlink=lambda: True, resolve=lambda: Path("/etc/init.d/routepilot-egress-guard")
        )
        flowtables = SimpleNamespace(stdout='{"nftables":[]}')
        with (
            patch.object(guard, "present", side_effect=[False, True, True, False]),
            patch.object(guard, "transact"),
            patch.object(guard, "BOOT_LINK", link),
            patch.object(guard, "command", return_value=flowtables),
        ):
            guard.Guard().configure(cfg)  # Early boot process.
            manager = guard.Guard()
            manager.configure(cfg)
            record(self.run, "events", {"state": "running"})
            manager.allow()
            manager.allow()
            manager.block("reconnect")
            manager.allow()
            cfg["policies"][0]["egress_guard"] = "0"
            manager.configure(cfg)
            guard.Guard().configure(cfg)
        history = json.loads((self.run / "connection-events.json").read_text())
        self.assertEqual(
            [(event["state"], event["reason"]) for event in history["protection"]],
            [
                ("blocking", "startup"),
                ("ready", "verified"),
                ("blocking", "reconnect"),
                ("ready", "verified"),
                ("off", "opt_out"),
            ],
        )
        self.assertEqual(history["events"][0]["state"], "running")
        self.assertTrue(
            all(
                set(event) == {"state", "reason", "scope", "time"}
                for event in history["protection"]
            )
        )

    def test_failed_gate_install_records_unverified_without_raw_error(self):
        with (
            patch.object(guard, "present", return_value=False),
            patch.object(guard, "transact", side_effect=RuntimeError("private stderr")),
        ):
            with self.assertRaises(RuntimeError):
                guard.Guard().configure(configuration())
        raw = (self.run / "connection-events.json").read_text()
        events = json.loads(raw)["protection"]
        self.assertEqual(
            [(event["state"], event["reason"], event["scope"]) for event in events],
            [("unverified", "firewall_error", "devices")],
        )
        self.assertNotIn("private stderr", raw)

    def test_committed_boot_config_does_not_require_links_leases_or_rpcd(self):
        data = """package routepilot
config main 'main'
        option egress_guard '1'
        option enabled '1'
        option scope 'devices'
config policy 'protected'
        option enabled '1'
        option mode 'global'
        option mac '02:11:22:33:44:55'
        option egress_guard '1'
config policy 'direct'
        option enabled '1'
        option mode 'direct'
        option mac '02:11:22:33:44:66'
        option egress_guard '1'
config policy 'disabled'
        option enabled '0'
        option mode 'global'
        option mac '02:11:22:33:44:77'
        option egress_guard '1'
config policy 'legacy'
        option enabled '1'
        option mode 'global'
        option mac '02:11:22:33:44:88'
        option egress_guard 'inherit'
config policy 'ordinary'
        option enabled '1'
        option mode 'global'
        option mac '02:11:22:33:44:99'
"""
        with patch.object(guard, "command", return_value=SimpleNamespace(stdout=data)) as cmd:
            cfg = guard.boot_config()
        self.assertEqual(guard.scope(cfg), ("ether saddr 02:11:22:33:44:55",))
        self.assertEqual(cfg["main"]["enabled"], "1")
        self.assertEqual(cmd.call_args.args[0][0], "uci")
        cfg["policies"][0]["egress_guard"] = "0"
        self.assertEqual(guard.scope(cfg), ())

    def test_protection_uses_mac_even_before_dhcp_and_excludes_direct_mode(self):
        cfg = configuration()
        cfg["policies"][0].pop("ip")
        cfg["policies"][1]["egress_guard"] = "1"
        self.assertEqual(guard.scope(cfg), ("ether saddr 02:11:22:33:44:55",))
        cfg["main"]["enabled"] = "0"
        self.assertEqual(guard.scope(cfg), ("ether saddr 02:11:22:33:44:55",))

    def test_only_an_enabled_proxy_policy_can_guard_dns_and_forwarding(self):
        cfg = configuration()
        cfg["main"].update(egress_guard="1", ipv6_mode="direct")
        cfg["policies"][1]["egress_guard"] = "1"
        cfg["policies"].extend(
            [
                dict(
                    id="disabled",
                    enabled="0",
                    mode="global",
                    mac="02:11:22:33:44:77",
                    ip="10.1.0.4",
                    egress_guard="1",
                ),
                dict(
                    id="legacy",
                    enabled="1",
                    mode="global",
                    mac="02:11:22:33:44:88",
                    ip="10.1.0.5",
                    egress_guard="inherit",
                ),
                dict(
                    id="ordinary",
                    enabled="1",
                    mode="global",
                    mac="02:11:22:33:44:99",
                    ip="10.1.0.6",
                ),
            ]
        )
        expected_mac = cfg["policies"][0]["mac"]
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(guard.scope(cfg), ("ether saddr " + expected_mac,))
            compiled = masque_config.compile_config(cfg, Path(temp))
            by_mac = {policy["mac"]: policy for policy in compiled["dns"]["policies"]}
            self.assertEqual(set(by_mac), {expected_mac, "02:11:22:33:44:88", "02:11:22:33:44:99"})
            for mac, policy in by_mac.items():
                self.assertEqual(policy["guarded_global"], mac == expected_mac)
                self.assertEqual(policy["block_aaaa"], mac == expected_mac)
            direct_marks = [line for line in compiled["nft"].splitlines() if "0x5b000000" in line]
            self.assertTrue(direct_marks)
            self.assertTrue(all(expected_mac in line for line in direct_marks))

            cfg["main"]["scope"] = "lan"
            self.assertEqual(guard.scope(cfg), ())
            lan = masque_config.compile_config(cfg, Path(temp))
            self.assertFalse(lan["dns"]["policies"][0]["guarded_global"])
            self.assertFalse(lan["dns"]["policies"][0]["block_aaaa"])
            self.assertNotIn("0x5b000000", lan["nft"])

    def test_gate_starts_closed_and_never_releases_before_validated_dns(self):
        cfg = configuration()
        service = routing_service.Service()
        service.runtime_id = "fresh"
        link = SimpleNamespace(
            is_symlink=lambda: True, resolve=lambda: Path("/etc/init.d/routepilot-egress-guard")
        )
        now = time.time()
        dns = {"state": "running", "runtime_id": "fresh", "time": now}
        flowtables = SimpleNamespace(stdout=json.dumps({"nftables": []}))
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(routing_service, "RUN", Path(temp)),
            patch.object(guard, "present", return_value=False),
            patch.object(guard, "transact") as txn,
            patch.object(guard, "BOOT_LINK", link),
            patch.object(guard, "command", return_value=flowtables),
        ):
            service.egress_guard.configure(cfg)
            self.assertIn("type mark; flags timeout", txn.call_args.args[0])
            self.assertNotIn("add element", txn.call_args.args[0])
            state = Path(temp) / "dns-state.json"
            self.assertFalse(service.confirm_egress_ready())
            for snapshot in (
                dict(dns, runtime_id="wrong"),
                dict(dns, time=now - 30),
                dict(dns, time=now + 30),
            ):
                with self.subTest(snapshot=snapshot):
                    state.write_text(json.dumps(snapshot))
                    self.assertFalse(service.confirm_egress_ready())
                    self.assertEqual(service.egress_guard.state, "blocking")
            txn.assert_called_once()
            state.write_text(json.dumps(dns))
            self.assertTrue(service.confirm_egress_ready())
            self.assertIn("add element", txn.call_args.args[0])
            state.unlink()
            self.assertFalse(service.confirm_egress_ready())
            self.assertEqual(
                txn.call_args.args[0], "flush set inet routepilot_egress_guard ready\n"
            )
            state.write_text(json.dumps(dns))
            self.assertTrue(service.confirm_egress_ready())
            with patch.object(service.dns, "stop"), patch.object(routing_service, "command") as fw:
                service.withdraw()
                fw.assert_not_called()
            self.assertEqual(
                txn.call_args.args[0], "flush set inet routepilot_egress_guard ready\n"
            )

    def test_flow_offload_closes_gate_and_is_never_reported_as_protected(self):
        manager = guard.Guard()
        manager.selectors = guard.scope(configuration())
        manager.state = "ready"
        link = SimpleNamespace(
            is_symlink=lambda: True, resolve=lambda: Path("/etc/init.d/routepilot-egress-guard")
        )
        flowtables = SimpleNamespace(stdout=json.dumps({"nftables": [{"flowtable": {}}]}))
        with (
            patch.object(guard, "BOOT_LINK", link),
            patch.object(guard, "transact") as txn,
            patch.object(guard, "command", return_value=flowtables),
        ):
            with self.assertRaisesRegex(ValueError, "流量卸载"):
                manager.allow()
        self.assertEqual(manager.state, "unverified")
        self.assertEqual(routing_service.guard_fallback(manager, True), "unknown")
        txn.assert_called_once_with("flush set inet routepilot_egress_guard ready\n")

    def test_disabling_checkbox_removes_guard_after_supervisor_restart(self):
        cfg = configuration()
        cfg["policies"][0]["egress_guard"] = "0"
        manager = guard.Guard()
        with (
            patch.object(guard, "present", return_value=True),
            patch.object(guard, "transact") as txn,
        ):
            manager.configure(cfg)
        txn.assert_called_once_with("delete table inet routepilot_egress_guard\n")

    def test_master_off_removes_previous_guard_without_forgetting_preferences(self):
        cfg = configuration()
        cfg["main"]["enabled"] = "0"
        manager = guard.Guard()
        with (
            patch.object(guard, "present", side_effect=[True, False]),
            patch.object(guard, "transact") as txn,
        ):
            manager.configure(cfg)
            self.assertEqual(manager.state, "off")
            self.assertEqual(manager.selectors, ())
            txn.assert_called_once_with("delete table inet routepilot_egress_guard\n")
            self.assertEqual(cfg["policies"][0]["egress_guard"], "1")
            cfg["main"]["enabled"] = "1"
            manager.configure(cfg)
            self.assertEqual(manager.state, "blocking")
            self.assertEqual(manager.selectors, guard.scope(cfg))
            self.assertNotIn("add element", txn.call_args.args[0])

    def test_master_off_recovers_a_guard_left_blocking_by_offload_conflict(self):
        cfg = configuration()
        manager = guard.Guard()
        boot_link = SimpleNamespace(
            is_symlink=lambda: True, resolve=lambda: Path("/etc/init.d/routepilot-egress-guard")
        )
        with (
            patch.object(guard, "present", side_effect=[False, True]),
            patch.object(guard, "BOOT_LINK", boot_link),
            patch.object(guard, "offload_active", return_value=True) as offload,
            patch.object(guard, "transact") as txn,
        ):
            manager.configure(cfg)
            with self.assertRaisesRegex(ValueError, "流量卸载"):
                manager.require_supported()
            self.assertEqual(manager.state, "unverified")
            cfg["main"]["enabled"] = "0"
            manager.configure(cfg)
            self.assertEqual(manager.state, "off")
            self.assertEqual(cfg["policies"][0]["egress_guard"], "1")
            offload.assert_called_once()
            self.assertEqual(txn.call_args.args[0], "delete table inet routepilot_egress_guard\n")

    def test_preflight_rejects_configured_software_or_hardware_offload_without_writes(self):
        for option in ("flow_offloading", "flow_offloading_hw"):
            with self.subTest(option=option):
                output = f"firewall.defaults=defaults\nfirewall.defaults.{option}='1'\n"
                with patch.object(
                    guard, "command", return_value=SimpleNamespace(stdout=output)
                ) as cmd:
                    with self.assertRaisesRegex(guard.ControlError, "本次未保存"):
                        guard.validate_activation()
                cmd.assert_called_once_with(
                    ["uci", "-q", "show", "firewall.@defaults[0]"], timeout=5
                )

    def test_preflight_checks_live_flowtables_and_fails_safely_if_unreadable(self):
        defaults = SimpleNamespace(
            stdout="firewall.@defaults[0]=defaults\nfirewall.@defaults[0].flow_offloading='0'\n"
        )
        for nft_result, error in (
            ({"nftables": [{"flowtable": {}}]}, "检测到防火墙流量卸载"),
            ({}, "无法确认防火墙流量卸载状态"),
            ({"nftables": []}, None),
        ):
            with self.subTest(nft=nft_result):
                with patch.object(
                    guard,
                    "command",
                    side_effect=[defaults, SimpleNamespace(stdout=json.dumps(nft_result))],
                ) as cmd:
                    if error:
                        with self.assertRaisesRegex(guard.ControlError, error):
                            guard.validate_activation()
                    else:
                        guard.validate_activation()
                self.assertEqual(cmd.call_args.args[0], ["nft", "-j", "list", "flowtables"])

    def test_power_on_checks_saved_protection_before_any_uci_write(self):
        conflict = guard.ControlError("flow offload conflict")
        with (
            patch.object(control, "boot_config", return_value=configuration()),
            patch.object(control, "validate_activation", side_effect=conflict) as check,
            patch.object(control, "command") as cmd,
            patch("builtins.print"),
        ):
            with self.assertRaises(guard.ControlError):
                control.control("enable")
            cmd.assert_not_called()
            check.assert_called_once()
            control.control("disable")
            check.assert_called_once()
            self.assertIn(
                ["uci", "set", "routepilot.main.enabled=0"],
                [call.args[0] for call in cmd.call_args_list],
            )

    def test_power_on_checks_device_subnet_before_writing(self):
        cfg = configuration()
        cfg["policies"][0]["ip"] = "192.168.44.10"
        interfaces = [{"name": "br-lan", "up": True, "network": "10.1.0.0/24"}]
        with (
            patch.object(control, "boot_config", return_value=cfg),
            patch.object(control, "validate_activation"),
            patch.object(control, "read_config", return_value=cfg),
            patch.object(control, "lan_interfaces", return_value=interfaces),
            patch.object(control, "command") as cmd,
            patch("builtins.print"),
        ):
            with self.assertRaisesRegex(guard.ControlError, "IPv4 不属于所选 LAN 网段"):
                control.control("enable")
            cmd.assert_not_called()
            cfg["policies"][0]["ip"] = "10.1.0.20"
            control.control("enable")
            cmd.assert_any_call(["uci", "set", "routepilot.main.enabled=1"])
            cfg["policies"][0]["ip"] = "192.168.44.10"
            cfg["main"]["scope"] = "lan"
            control.control("enable")
            control.control("disable")
            cmd.assert_any_call(["uci", "set", "routepilot.main.enabled=0"])

    def test_manual_shutdown_expires_only_imok_dns_redirects(self):
        spec = self.run / "dns.json"
        spec.write_text(json.dumps({"policies": [{"ip": "10.1.0.2", "port": 12101}]}))
        service = routing_service.Service()
        with (
            patch.object(routing_service, "RUN", self.run),
            patch.object(service, "withdraw", return_value=True),
            patch.object(service.plane, "stop"),
            patch.object(routing_service, "WorkerPlane"),
            patch.object(routing_service, "finish_interrupted_update"),
            patch.object(routing_service, "command") as cmd,
        ):
            self.assertTrue(service.stop()["direct"])
        self.assertFalse(spec.exists())
        self.assertEqual(
            [call.args[0] for call in cmd.call_args_list],
            [
                [
                    "conntrack",
                    "-D",
                    "-p",
                    protocol,
                    "-s",
                    "10.1.0.2/32",
                    "--dst-nat",
                    "--dport",
                    "53",
                    "--reply-port-src",
                    "12101",
                ]
                for protocol in ("udp", "tcp")
            ],
        )

    def test_compiler_marks_intentional_direct_and_uses_tunnel_dns_in_global_mode(self):
        cfg = configuration()
        cfg["main"]["custom_direct_ips"] = ["198.51.100.8"]
        with tempfile.TemporaryDirectory() as temp:
            compiled = masque_config.compile_config(cfg, Path(temp))
            cfg["policies"][0]["egress_guard"] = "0"
            old = masque_config.compile_config(cfg, Path(temp))
        self.assertIn("ip daddr { 198.51.100.8/32 } counter meta mark set", compiled["nft"])
        self.assertIn("0x5b000000", compiled["nft"])
        self.assertNotIn("0x5b000000", old["nft"])
        policy = compiled["dns"]["policies"][0]
        self.assertTrue(policy["block_aaaa"])
        self.assertEqual(
            native.dns_route(
                "www.example.com",
                dict(policy, dns_foreign="1.1.1.1", china_domains=["example.com"]),
            )[1:],
            (guard.PROXY_MARK, "proxy"),
        )
