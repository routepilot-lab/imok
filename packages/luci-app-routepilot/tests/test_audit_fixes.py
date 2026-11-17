# Regression tests for the 2026-11-16 deep audit fixes.
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import egress_guard as guard
import masque_config
import mplane
import native
import service as routing_service
import supervisor
import warp_bootstrap_dns as bootstrap


class Stage1DataPlaneAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.rules = Path(self.temp.name)
        (self.rules / "china.txt").write_text("1.0.1.0/24\n")
        (self.rules / "china_domains.txt").write_text("apple.com\nbaidu.com\n")
        (self.rules / "whitelist.txt").write_text("custom-white.cn\n")
        self.cfg = {
            "main": {"exit": "seoul", "vnet_seoul": "11111111-1111-4111-8111-111111111111"},
            "policies": [
                {
                    "id": "macmini",
                    "enabled": "1",
                    "ip": "10.77.50.10",
                    "mac": "02:00:00:00:00:10",
                    "mode": "global",
                }
            ],
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_ipv6_router_input_dns_is_rejected_even_when_ipv6_forwarding_is_direct(self):
        self.cfg["main"]["ipv6_mode"] = "direct"
        out = masque_config.compile_config(self.cfg, self.rules)
        dns_guard = out["nft"].split("chain dns_guard")[1]
        self.assertIn(
            'iifname "br-lan" ether saddr 02:00:00:00:00:10 meta nfproto ipv6 meta l4proto { tcp, udp } th dport 53 reject',
            dns_guard,
        )

    def test_whole_lan_does_not_bypass_shared_cloudflare_cdn(self):
        self.cfg["main"].update(
            scope="lan", lan_mode="global", lan_networks=[{"interface": "br-lan", "network": "10.77.50.0/24"}]
        )
        out = masque_config.compile_config(self.cfg, self.rules)
        self.assertIn("162.159.197.0/24", out["nft"])
        self.assertNotIn("188.114.96.0/20", out["nft"])

    def test_policy_rule_conflict_leaves_both_priorities_untouched(self):
        show = ["ip", "-4", "rule", "show"]
        row = f"{masque_config.PRIORITY}: from all lookup 100"
        with patch.object(routing_service, "command", return_value=SimpleNamespace(stdout=row)) as run:
            with self.assertRaisesRegex(ValueError, "优先级被占用"):
                routing_service.ensure_policy_rule()
        run.assert_called_once_with(show)

    def test_unreachable_default_is_not_a_live_tunnel_route(self):
        svc = routing_service.Service()
        svc.last_fw4_check = float("inf")
        rule = " ".join(routing_service.POLICY_RULE)
        with patch.object(
            routing_service,
            "command",
            side_effect=[
                SimpleNamespace(returncode=0),
                SimpleNamespace(stdout=f"{masque_config.PRIORITY}: {rule}"),
                SimpleNamespace(stdout="unreachable default metric 4294967295"),
            ],
        ):
            with self.assertRaisesRegex(RuntimeError, "专用路由已丢失"):
                svc.check_capture()

    def test_policy_rule_migration_verifies_new_rule_before_removing_old(self):
        show = ["ip", "-4", "rule", "show"]
        selector = routing_service.POLICY_SELECTOR
        rule = " ".join(routing_service.POLICY_RULE)
        old = f"{masque_config.LEGACY_PRIORITY}: {rule}"
        new = f"{masque_config.PRIORITY}: {rule}"
        with patch.object(
            routing_service, "command", side_effect=[
                SimpleNamespace(stdout=old), None, SimpleNamespace(stdout=f"{new}\n{old}"), None,
            ]
        ) as run:
            routing_service.ensure_policy_rule()
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            show, ["ip", "-4", "rule", "add", "pref", str(masque_config.PRIORITY), *selector],
            show, ["ip", "-4", "rule", "del", "pref", str(masque_config.LEGACY_PRIORITY), *selector],
        ])

    def test_global_mode_routes_china_domains_through_tunnel_dns_unless_custom_direct(self):
        out = masque_config.compile_config(self.cfg, self.rules)
        policy = out["dns"]["policies"][0]
        policy["dns_foreign"] = "1.1.1.1"
        policy["dns_domestic"] = "223.5.5.5"
        policy["custom_direct_domains"] = frozenset({"intranet.example.cn"})
        policy["china_domains"] = frozenset(policy["china_domains"])
        self.assertEqual(native.dns_route("www.apple.com", policy), ("1.1.1.1", masque_config.MARK, "proxy"))
        self.assertEqual(native.dns_route("intranet.example.cn", policy), ("223.5.5.5", 0, "domestic"))

    def test_whitelist_mode_uses_mode_domains_for_domestic_dns(self):
        self.cfg["policies"][0]["mode"] = "whitelist"
        out = masque_config.compile_config(self.cfg, self.rules)
        policy = out["dns"]["policies"][0]
        policy["dns_foreign"] = "1.1.1.1"
        policy["dns_domestic"] = "223.5.5.5"
        policy["mode_domains"] = frozenset(policy["mode_domains"])
        policy["china_domains"] = frozenset()
        self.assertEqual(native.dns_route("app.custom-white.cn", policy), ("223.5.5.5", 0, "domestic"))

    def test_qtype_65_https_does_not_collide_with_qtype_97_and_ad_flag_normalizes(self):
        qname_upper = b"\x07ExAmPlE\x03CoM\x00"
        qname_lower = b"\x07example\x03com\x00"
        q_https = struct.pack("!6H", 1, 0x0100, 1, 0, 0, 0) + qname_upper + struct.pack("!HH", 65, 1)
        q_97 = struct.pack("!6H", 2, 0x0100, 1, 0, 0, 0) + qname_lower + struct.pack("!HH", 97, 1)
        self.assertNotEqual(native.cache_key(q_https), native.cache_key(q_97))
        q_ad_upper = struct.pack("!6H", 3, 0x0120, 1, 0, 0, 0) + qname_upper + struct.pack("!HH", 1, 1)
        q_ad_lower = struct.pack("!6H", 4, 0x0120, 1, 0, 0, 0) + qname_lower + struct.pack("!HH", 1, 1)
        self.assertIsNotNone(native.ordinary_query(q_ad_upper))
        self.assertEqual(native.cache_key(q_ad_upper), native.cache_key(q_ad_lower))

    def test_tunnel_resolv_strips_private_wan_peers_but_keeps_warp_loopback(self):
        self.assertEqual(
            mplane.tunnel_resolv("nameserver 127.0.2.2\n"),
            "nameserver 127.0.2.2\n",
        )
        self.assertEqual(
            mplane.tunnel_resolv("nameserver 192.168.1.1\nnameserver 223.5.5.5\noptions timeout:1\n"),
            "nameserver 223.5.5.5\noptions timeout:1\n",
        )
        self.assertEqual(
            mplane.tunnel_resolv("nameserver 100.64.0.2\n"),
            "nameserver 1.1.1.1\n",
        )

    def test_fw4_remove_verify_checks_matching_backend_prefix(self):
        calls = []

        def fake_cmd(args, **kw):
            calls.append(args)
            if args[:4] == ["nft", "-a", "list", "chain"]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=' ip saddr 10.254.239.2 accept comment "routepilot-control-out" # handle 9\n',
                )
            return SimpleNamespace(returncode=0, stdout="")

        with patch.object(mplane, "command", side_effect=fake_cmd):
            self.assertFalse(mplane.fw4_remove("masque", verify=True))

    def test_partial_egress_guard_keeps_valid_mac_closed_when_sibling_mac_invalid(self):
        cfg = {
            "main": {"enabled": "1", "scope": "devices"},
            "policies": [
                {"id": "good", "enabled": "1", "mode": "global", "mac": "02:11:22:33:44:55", "egress_guard": "1"},
                {"id": "bad", "enabled": "1", "mode": "global", "mac": "not-a-mac", "egress_guard": "1"},
            ],
        }
        manager = guard.Guard()
        with (
            patch.object(guard, "present", return_value=False),
            patch.object(guard, "transact") as txn,
            patch.object(guard, "record"),
        ):
            with self.assertRaises(ValueError):
                manager.configure(cfg)
        self.assertEqual(manager.selectors, ("ether saddr 02:11:22:33:44:55",))
        self.assertIn("02:11:22:33:44:55", txn.call_args.args[0])

    def test_bootstrap_probe_rejects_forged_non_cloudflare_global_ip(self):
        def query_reply(q):
            return (
                struct.pack("!6H", struct.unpack("!H", q[:2])[0], 0x8180, 1, 1, 0, 0)
                + q[12:]
                + b"\xc0\x0c"
                + struct.pack("!HHIH", 1, 1, 60, 4)
                + bytes([31, 13, 72, 1])  # Non-Cloudflare forged edge IP
            )
        fake_sock = Mock()
        fake_sock.__enter__ = Mock(return_value=fake_sock)
        fake_sock.__exit__ = Mock(return_value=False)
        sent = []
        fake_sock.send.side_effect = lambda data: sent.append(data)
        fake_sock.recv.side_effect = lambda _: query_reply(sent[-1])
        with patch.object(bootstrap.socket, "socket", return_value=fake_sock):
            self.assertEqual(bootstrap.resolver_result(("8.8.8.8", 53)), (None, "unexpected_answer"))

    def test_all_direct_policies_rejected_without_applying_empty_dns_runtime(self):
        from contextlib import contextmanager

        @contextmanager
        def snap():
            yield self.rules

        self.cfg["main"]["enabled"] = "1"
        self.cfg["policies"][0]["mode"] = "direct"
        with patch.object(routing_service, "uci_value", return_value=""):
            with self.assertRaisesRegex(ValueError, "启用至少一条代理策略"):
                routing_service.preflight(self.cfg)
        svc = Mock()
        with (
            patch.object(supervisor, "preflight", return_value={}),
            patch.object(supervisor, "RULES", self.rules),
            patch.object(supervisor, "rule_snapshot", side_effect=snap),
            patch.object(supervisor, "compile_config", return_value={"dns": {"policies": []}}),
        ):
            with self.assertRaisesRegex(ValueError, "启用至少一条代理策略"):
                supervisor.apply_running_configuration(self.cfg, svc, None)
        svc.apply.assert_not_called()


class Stage2And3AuditTests(unittest.TestCase):
    def test_client_revoke_stops_enabled_subscription_and_preserves_review_error(self):
        import client_access as ca
        import time

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            private_dir = base / "clients"
            run_dir = base / "run"
            vol_dir = run_dir / "client-jobs"
            private_dir.mkdir(mode=0o700)
            vol_dir.mkdir(parents=True, mode=0o700)
            with (
                patch.object(ca, "PRIVATE", private_dir),
                patch.object(ca, "RUN", run_dir),
                patch.object(ca, "VOLATILE", vol_dir),
                patch.object(ca, "configured_team", return_value="myteam"),
            ):
                created = ca.create({"name": "Phone"})["client"]
                cid = created["id"]
                op_id = "1" * 32
                doc = ca.load()
                row = ca.find(doc, cid)
                row.update(
                    state="queued",
                    operation="revoke",
                    operation_id=op_id,
                    operation_at=time.time(),
                    device_id="11111111-1111-4111-8111-111111111111",
                    device_token="tok",
                    subscription=dict(
                        state="published",
                        token="A" * 43,
                        revision="b" * 64,
                        synced_at=100,
                    ),
                )
                ca.save(doc)
                job_file = vol_dir / (op_id + ".json")
                job_file.write_text(json.dumps({"id": cid, "action": "revoke"}))
                job_file.chmod(0o600)
                with (
                    patch.object(ca.provider, "request", return_value={"deleted": True}),
                    patch.object(
                        ca.subscriptions, "stop", side_effect=ca.ClientError("无 API 令牌")
                    ) as stop_sub,
                ):
                    ca.job(cid, op_id)
                stop_sub.assert_called_once()
                revoked = ca.find(ca.load(), cid)
                self.assertEqual(revoked["state"], "revoked")
                self.assertIn("无 API 令牌", revoked["subscription"]["error"])

                op2 = "2" * 32
                doc = ca.load()
                row2 = ca.find(doc, cid)
                row2.update(
                    state="queued",
                    operation="stop_subscription",
                    operation_id=op2,
                    operation_at=time.time(),
                    error="需人工核对客户端状态",
                )
                ca.save(doc)
                job_file2 = vol_dir / (op2 + ".json")
                job_file2.write_text(
                    json.dumps({"id": cid, "action": "stop_subscription", "resume_state": "review"})
                )
                job_file2.chmod(0o600)
                with patch.object(ca.subscriptions, "stop"):
                    ca.job(cid, op2)
                stopped = ca.find(ca.load(), cid)
                self.assertEqual(stopped["state"], "review")
                self.assertEqual(stopped["error"], "需人工核对客户端状态")

    def test_client_subscription_fetch_body_falls_back_to_warp_tunnel_on_wan_error(self):
        import client_subscriptions as cs
        import time

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "curl":
                return SimpleNamespace(returncode=35, stdout="", stderr="reset")
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_bytes(b"subscription-ok")
            hdr = Path(cmd[cmd.index("-D") + 1])
            hdr.write_text("HTTP/2 200\r\ncache-control: no-store\r\n\r\n")
            return SimpleNamespace(returncode=0, stdout="200", stderr="")

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            (run_dir / "status.json").write_text(
                json.dumps({"state": "running", "backend": "masque", "time": time.time()})
            )
            import runtime as rt

            url = "https://imok-clients-001122334455.sub.example.com/s/" + ("A" * 43)
            with (
                patch.object(rt, "RUN", run_dir),
                patch.object(cs.subprocess, "run", side_effect=fake_run),
            ):
                self.assertTrue(cs.fetch_body(url, "subscription-ok"))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1][:4], ["ip", "netns", "exec", "routepilot"])

    def test_udp_buffers_and_endpoint_quality_cover_zero_trust_masque_ranges(self):
        import ipaddress
        import endpoint_quality as eq
        import udp_buffers as ub

        self.assertTrue(
            any(
                ipaddress.IPv4Address("162.159.198.2") in cidr
                for cidr in ub.INGRESS
            )
        )
        self.assertEqual(eq.target_sni("162.159.197.2"), "zt-masque.cloudflareclient.com")
        self.assertEqual(eq.target_sni("162.159.198.5"), "zt-masque.cloudflareclient.com")
        self.assertEqual(eq.target_sni("162.159.192.1"), "consumer-masque.cloudflareclient.com")
        with tempfile.TemporaryDirectory() as temp:
            ep = Path(temp) / "endpoint-settings.json"
            ep.write_text(json.dumps({"mode": "manual", "address": "188.114.97.3:443"}))
            real_path = ub.Path
            with patch.object(
                ub,
                "Path",
                side_effect=lambda p: ep if str(p) == "/etc/routepilot/endpoint-settings.json" else real_path(p),
            ):
                self.assertEqual(ub.configured_ingress_ip(), ipaddress.IPv4Address("188.114.97.3"))

    def test_normalize_domains_strips_inline_comments_and_v2ray_attributes(self):
        self.assertEqual(
            masque_config.normalize_domains(
                "\n".join(
                    [
                        "domain:example.com:@cn # inline comment",
                        "full:sub.test.org:@!cn",
                        "keyword:google",
                        "include:category-ads",
                    ]
                )
            ),
            ("example.com\nfull:sub.test.org\n", 2),
        )

    def test_runtime_prune_backups_keeps_newest_and_removes_failed_remnants(self):
        import runtime_maintenance as rm

        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            folder = base / "runtime-backups"
            folder.mkdir(mode=0o700)
            ids = ["1" * 32, "2" * 32, "3" * 32]
            for idx, bid in enumerate(ids, start=1):
                b = folder / bid
                (b / "runtime").mkdir(parents=True)
                meta = b / "meta.json"
                meta.write_text(json.dumps({"id": bid, "version": "1.0", "created_at": 1000 + idx}))
                meta.chmod(0o600)
            stale_failed = base / ".runtime-failed-999"
            stale_failed.mkdir()
            with (
                patch.object(rm, "BASE", base),
                patch.object(rm, "recovery_required", return_value=False),
            ):
                rm.prune_backups(keep=2)
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ["2" * 32, "3" * 32])
            self.assertFalse(stale_failed.exists())

    def test_worker_backend_validation_raises_control_error_and_reaper_cleans_all_inputs(self):
        from public_errors import ControlError
        import request_input as ri
        import worker_backend as wb

        secret_uri = "vless://11111111-1111-4111-8111-111111111111@edge.example.com:443?type=ws&security=none#secret"
        with self.assertRaises(ControlError) as ctx:
            wb.parse_input({"source": "uri", "uri": secret_uri})
        self.assertNotIn("11111111", str(ctx.exception))

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp)
            req_dir = run_dir / "requests"
            req_dir.mkdir(mode=0o700)
            for prefix in ("speed-compare-input", "warp-dns-input"):
                f = req_dir / f"{prefix}-{'a' * 32}.json"
                f.write_text("{}")
                f.chmod(0o600)
            ri.reap_requests(run_dir, now=10**12)
            self.assertEqual(list(req_dir.iterdir()), [])

            bad = run_dir / "world-writable.json"
            raw = json.dumps({"request_id": "b" * 32, "payload": {}})
            bad.write_text(raw)
            bad.chmod(0o666)
            import hashlib

            token = "b" * 32 + ":" + hashlib.sha256(raw.encode()).hexdigest()
            with self.assertRaises(ControlError):
                ri.read_request(bad, token, 1024)

    def test_traffic_observer_save_fsyncs_parent_directory(self):
        import traffic_observer as to

        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp) / "usage" / "state.json"
            fsynced = []
            real_fsync = to.os.fsync
            with (
                patch.object(to, "STORE", store),
                patch.object(to.os, "fsync", side_effect=lambda fd: (fsynced.append(fd), real_fsync(fd))),
            ):
                self.assertTrue(to.save(to.Ledger(wall=1793401200).data))
            self.assertGreaterEqual(len(fsynced), 2)

    def test_server_wizard_config_blocks_private_and_metadata_destinations(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "server_install_audit",
            Path(__file__).resolve().parents[1] / "server/install.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as temp:
            src_x = Path(temp) / "x"
            src_c = Path(temp) / "c"
            src_x.write_bytes(b"xray")
            src_c.write_bytes(b"cf")
            out = Path(temp) / "out"
            mod.prepare(out, "node.example.com", "T" * 32, src_x, src_c, "/opt/routepilot-server")
            cfg = json.loads((out / "config.json").read_text())
            rules = cfg["routing"]["rules"]
            self.assertEqual(rules[0]["outboundTag"], "block")
            self.assertIn("169.254.0.0/16", rules[0]["ip"])
            self.assertIn("127.0.0.0/8", rules[0]["ip"])


if __name__ == "__main__":
    unittest.main()
