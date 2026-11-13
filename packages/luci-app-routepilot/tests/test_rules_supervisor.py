"""Exercise the real supervisor loop with local fakes; no router or network calls."""

import contextlib
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import supervisor as m
import service as routing_service
import egress_guard as guard
import endpoint_control as e
import rules_schedule as rs
import worker_backend as w
from warp_cli import UnknownTunnelStatus


class SupervisorRulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / "run"
        self.rules = self.root / "rules"
        self.run.mkdir()
        self.rules.mkdir()
        (self.rules / "china.txt").write_text("1.0.1.0/24\n")
        self.cfg = {
            "main": {
                "enabled": "1",
                "backend": "masque",
                "scope": "devices",
                "warp_protocol": "auto",
                "apply_nonce": "n",
                "update_hours": "168",
            },
            "policies": [],
        }
        self.compiled = {
            "dns": {},
            "devices": [],
            "exit": "usa",
            "scope": "devices",
            "vnet": "fixture",
        }
        self.svc = SimpleNamespace(
            backend="masque",
            update_job=None,
            recovery_started=None,
            worker_failed_nonce=None,
            plane=SimpleNamespace(
                owned=False, protocol_preference="auto", protocol="MASQUE", start=Mock()
            ),
            compiled=None,
            exit_ip="1.0.1.1",
            runtime_id="fixture",
            health=Mock(),
            egress_guard=Mock(state="off", selectors=()),
            confirm_egress_ready=Mock(),
        )
        self.svc.stop = Mock(return_value={"direct": True, "legacy": ""})
        self.svc.apply = Mock(side_effect=lambda cfg, c: setattr(self.svc, "compiled", c))
        self.events = []

    def run_loop(
        self,
        iterations=3,
        on_tick=None,
        *,
        preflight=None,
        compiler=None,
        lans=(),
        reader=None,
        interfaces=None,
    ):
        manager = SimpleNamespace(
            poll=Mock(),
            recover_requested=Mock(return_value=False),
            pause_reason=Mock(return_value=""),
            tick=Mock(return_value=False),
        )
        count = [0]

        def sleep(_):
            count[0] += 1
            if on_tick is not None:
                on_tick(count[0])
            if count[0] >= iterations:
                m.STOP = True

        with contextlib.ExitStack() as stack:
            for obj, key, value in [
                (m, "RUN", self.run),
                (routing_service, "RUN", self.run),
                (m, "RULES", self.rules),
                (m.sys, "argv", ["routepilot", "run"]),
                (m, "STOP", False),
                (m, "Service", Mock(return_value=self.svc)),
                (m, "read_config", reader or Mock(return_value=self.cfg)),
                (m, "boot_config", Mock(return_value=self.cfg)),
                (m, "lan_interfaces", interfaces or Mock(return_value=list(lans))),
                (m, "selected_lans", Mock(return_value=[lan["name"] for lan in lans])),
                (m, "preflight", preflight or Mock(return_value={})),
                (
                    m,
                    "compile_config",
                    compiler or Mock(side_effect=lambda *a: dict(self.compiled, dns={})),
                ),
                (m, "rule_snapshot", Mock(side_effect=lambda: contextlib.nullcontext(self.rules))),
                (
                    m,
                    "output",
                    Mock(
                        side_effect=lambda state, message, *a, **kw: self.events.append(
                            dict(state=state, message=message, **kw)
                        )
                    ),
                ),
                (m.signal, "signal", Mock()),
                (m.time, "sleep", Mock(side_effect=sleep)),
                (e, "Manager", Mock(return_value=manager)),
                (e, "stage_restored_preferences", Mock()),
            ]:
                stack.enter_context(patch.object(obj, key, value))
            m.run()

    def use_worker(self):
        self.cfg["main"]["backend"] = self.svc.backend = "worker"
        self.svc.plane = SimpleNamespace(start=Mock(), running=True, node=None)
        self.svc.suspend_capture = Mock(return_value=True)
        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "fixture")
        schedule.finish(self.cfg["main"], True)

    def test_running_service_checks_connectivity_without_luci_and_survives_spawn_failure(self):
        check = Mock()
        check.tick.side_effect = OSError("synthetic child launch failure")
        with patch.object(m, "AutomaticCheck", return_value=check):
            self.run_loop(2)
        self.assertEqual(check.tick.call_count, 2)
        self.assertEqual(
            [event["state"] for event in self.events], ["running", "running", "stopped"]
        )

    def test_automatic_connectivity_waits_while_protection_blocks(self):
        check = Mock()
        self.svc.confirm_egress_ready.return_value = False
        with patch.object(m, "AutomaticCheck", return_value=check):
            self.run_loop(2)
        check.tick.assert_not_called()

    def test_worker_retries_config_read_timeout_without_reusing_stale_nonce(self):
        self.use_worker()
        error = subprocess.TimeoutExpired(["ubus"], 30)
        reader = Mock(side_effect=[self.cfg, error, error, self.cfg])
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(4, reader=reader)
        mark.assert_not_called()
        self.assertEqual(self.svc.suspend_capture.call_count, 2)
        self.assertEqual(
            self.svc.stop.call_count, 2, "only startup and shutdown should stop the core"
        )
        self.assertEqual(self.svc.apply.call_count, 2)
        self.assertEqual(
            [e["state"] for e in self.events], ["running", "error", "error", "running", "stopped"]
        )
        self.assertEqual([e["attempted_nonce"] for e in self.events[1:3]], ["", ""])
        self.assertTrue(all("无法读取 imOK 配置" in e["message"] for e in self.events[1:3]))

    def test_worker_retries_lan_and_offload_prechecks_without_latching(self):
        self.use_worker()
        lans = [dict(name="br-lan", up=True, network="10.77.50.0/24")]
        interfaces = Mock(side_effect=[lans, [], lans])
        self.svc.egress_guard.require_supported.side_effect = [
            None,
            ValueError("流量卸载冲突"),
            None,
            None,
        ]
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(4, lans=lans, interfaces=interfaces)
        mark.assert_not_called()
        self.assertEqual(self.svc.suspend_capture.call_count, 2)
        self.assertEqual(self.svc.stop.call_count, 2)
        self.assertEqual(self.svc.apply.call_count, 2)
        self.assertEqual(
            [e["state"] for e in self.events], ["running", "error", "error", "running", "stopped"]
        )
        self.assertIn("流量卸载冲突", self.events[1]["message"])
        self.assertIn("所选 LAN 接口缺失", self.events[2]["message"])

    def test_worker_line_failure_latches_only_its_attempt(self):
        self.use_worker()
        self.svc.health.side_effect = [w.WorkerLineFailure("备用线路连续两次联网检查失败"), None]

        def stop(**_):
            self.svc.compiled = None
            return {"direct": True, "legacy": ""}

        self.svc.stop.side_effect = stop
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry") as retry:
            self.run_loop(
                3, lambda tick: self.cfg["main"].update(apply_nonce="new") if tick == 2 else None
            )
        mark.assert_called_once_with("n")
        self.svc.suspend_capture.assert_not_called()
        self.assertEqual(self.svc.plane.start.call_count, 2)
        self.assertEqual([call.args[0] for call in retry.call_args_list], ["n", "new"])
        self.assertEqual(
            [e["state"] for e in self.events], ["error", "error", "running", "stopped"]
        )
        self.assertIn("重新检测", self.events[1]["message"])

    def test_worker_rejects_preflight_without_latching(self):
        self.use_worker()
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(1, preflight=Mock(side_effect=ValueError("策略校验失败")))
        mark.assert_not_called()
        self.svc.suspend_capture.assert_not_called()
        self.assertEqual(self.svc.stop.call_count, 3)
        self.assertEqual(self.events[0]["state"], "error")
        self.assertEqual(self.events[0]["fallback"], "direct")

    def test_worker_retries_start_failure_without_latching(self):
        self.use_worker()
        self.svc.plane.start.side_effect = [w.WorkerStartupFailure("备用核心启动失败"), None]
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(2)
        mark.assert_not_called()
        self.svc.suspend_capture.assert_not_called()
        self.assertEqual(self.svc.stop.call_count, 3)
        self.assertEqual(self.svc.plane.start.call_count, 2)
        self.assertEqual([event["state"] for event in self.events], ["error", "running", "stopped"])
        self.assertEqual(self.events[0]["fallback"], "direct")

    def test_worker_start_failure_latches_after_one_retry(self):
        self.use_worker()
        self.svc.plane.start.side_effect = w.WorkerStartupFailure("备用核心启动失败")
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(3)
        mark.assert_called_once_with("n")
        self.assertEqual(self.svc.plane.start.call_count, 2)
        self.assertEqual(
            [event["state"] for event in self.events], ["error", "error", "error", "stopped"]
        )

    def test_worker_nft_failure_keeps_core_after_verified_withdrawal(self):
        self.use_worker()
        self.svc.apply.side_effect = [
            routing_service.CaptureUnavailable("nft 写入失败", withdrawn=True),
            None,
        ]

        def resume(_):
            self.svc.apply.side_effect = lambda cfg, compiled: setattr(
                self.svc, "compiled", compiled
            )

        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(2, resume)
        mark.assert_not_called()
        self.svc.suspend_capture.assert_not_called()
        self.assertEqual(self.svc.stop.call_count, 2)
        self.assertEqual([event["state"] for event in self.events], ["error", "running", "stopped"])

    def test_worker_unconfirmed_capture_cleanup_stops_core(self):
        self.use_worker()
        self.svc.apply.side_effect = routing_service.CaptureCleanupUnconfirmed()
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(1)
        mark.assert_not_called()
        self.assertEqual(self.svc.stop.call_count, 3)
        self.assertIn("清理未确认", self.events[0]["message"])

    def test_worker_exit_probe_failure_stops_and_latches(self):
        self.use_worker()
        self.svc.apply.side_effect = w.WorkerLineFailure("备用线路未能通过出口检测")
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(1)
        mark.assert_called_once_with("n")
        self.svc.suspend_capture.assert_not_called()
        self.assertEqual(self.svc.stop.call_count, 3)
        self.assertEqual(self.events[0]["state"], "error")
        self.assertEqual(self.events[0]["fallback"], "direct")

    def test_worker_core_exit_stops_and_latches(self):
        self.use_worker()
        self.svc.health.side_effect = w.WorkerLineFailure("备用核心已退出")
        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(1)
        mark.assert_called_once_with("n")
        self.svc.suspend_capture.assert_not_called()
        self.assertEqual(self.svc.stop.call_count, 3)
        self.assertEqual(self.events[0]["state"], "error")
        self.assertEqual(self.events[0]["fallback"], "direct")

    def test_worker_exit_during_capture_is_latched_before_another_start(self):
        self.use_worker()

        def exit_after_apply(tick):
            if tick == 1:
                self.svc.plane.running = False

        with patch.object(w, "mark_failed") as mark, patch.object(w, "check_retry"):
            self.run_loop(2, exit_after_apply)

        mark.assert_called_once_with("n")
        self.svc.plane.start.assert_called_once()
        self.assertEqual([row["state"] for row in self.events], ["running", "error", "stopped"])

    def test_main_tunnel_pending_withdraws_capture_without_stopping_client(self):
        self.svc.suspend_capture = Mock(return_value=True)
        self.svc.health.side_effect = m.TunnelPending("WARP 暂时中断")
        self.run_loop(1)
        self.svc.suspend_capture.assert_called_once_with()
        self.assertEqual(self.svc.stop.call_count, 2)
        self.assertEqual(self.events[0]["state"], "error")
        self.assertEqual(self.events[0]["fallback"], "direct")
        self.assertTrue(self.events[0]["recovering"])

    def test_unknown_status_withdraws_then_restores_capture_without_rebuilding_plane(self):
        self.svc.unknown_since = time.monotonic() - 301
        self.svc.suspend_capture = Mock(return_value=True)
        self.svc.health.side_effect = [UnknownTunnelStatus(), None]
        self.run_loop(2)
        self.svc.suspend_capture.assert_called_once_with()
        self.assertEqual(self.svc.stop.call_count, 2, "only startup and shutdown stop the plane")
        self.assertEqual([event["state"] for event in self.events], ["error", "running", "stopped"])
        for event in self.events[:2]:
            self.assertEqual(event["tunnel_status"], "unknown")
            self.assertIn("客户端状态无法识别", event["message"])
            self.assertNotIn("已连接", event["message"])

    def test_missing_or_conflicting_lease_only_defers_its_own_device(self):
        import copy
        import mplane

        self.cfg["main"].update(exit="seoul", vnet_seoul="11111111-1111-4111-8111-111111111111")
        tablet = dict(
            id="tablet",
            name="Test tablet",
            enabled="1",
            mode="bypass_cn",
            egress_guard="1",
            ip="10.77.50.100",
            mac="02:00:00:00:00:01",
        )
        desktop = dict(
            id="desktop", enabled="1", mode="global", ip="10.77.50.101", mac="02:00:00:00:00:02"
        )
        self.cfg["policies"] = [tablet, desktop]
        original = copy.deepcopy(self.cfg["policies"])
        leasefile = self.root / "dhcp.leases"
        desktop_lease = "0 02:00:00:00:00:02 10.77.50.101 desktop *\n"
        tablet_lease = "0 02:00:00:00:00:01 10.77.50.100 tablet *\n"
        conflicting_lease = "0 02:00:00:00:00:03 10.77.50.100 other *\n"
        (self.run / "active-owner").touch()
        self.svc.withdraw = Mock(return_value=True)
        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "fixture")
        schedule.finish(self.cfg["main"], True)

        def tick(n):
            if n == 1:
                leasefile.write_text(desktop_lease)
            elif n == 2:
                leasefile.write_text(desktop_lease + conflicting_lease)
            elif n == 3:
                leasefile.write_text(desktop_lease + tablet_lease)
            elif n == 4:
                leasefile.write_text(desktop_lease)

        with (
            patch.object(routing_service, "DHCP_LEASES", leasefile),
            patch.object(routing_service, "uci_value", return_value=""),
        ):
            self.run_loop(
                6,
                tick,
                preflight=m.preflight,
                compiler=m.compile_config,
                lans=[dict(name="br-lan", up=True, network="10.77.50.0/24")],
            )
        states = [e["state"] for e in self.events]
        self.assertEqual(states, ["waiting_devices"] + ["running"] * 5 + ["stopped"])
        self.assertEqual(self.events[0]["pending_devices"]["desktop"]["reason"], "lease_missing")
        self.assertEqual(self.events[1]["pending_devices"]["tablet"]["reason"], "lease_missing")
        self.assertEqual(self.events[2]["pending_devices"]["tablet"]["reason"], "lease_mismatch")
        self.assertEqual(self.events[3]["pending_devices"], {})
        self.assertEqual(self.events[4]["pending_devices"]["tablet"]["reason"], "lease_missing")
        self.assertEqual(self.svc.stop.call_count, 2, "lease changes must not stop the tunnel")
        self.svc.withdraw.assert_called_once()
        first, both, last = [call.args[1] for call in self.svc.apply.call_args_list]
        self.assertEqual([d["id"] for d in first["devices"]], ["desktop"])
        self.assertEqual([d["id"] for d in both["devices"]], ["tablet", "desktop"])
        self.assertEqual([d["id"] for d in last["devices"]], ["desktop"])
        self.assertNotIn(tablet["mac"], first["nft"])
        self.assertIn(tablet["mac"], both["nft"])
        for snapshot in (first, both, last):
            device = next(d for d in snapshot["devices"] if d["id"] == "desktop")
            self.assertEqual(device["dns_port"], 12101)

        def firewall_command(args, **kwargs):
            return SimpleNamespace(stdout='[{"dev":"eth1"}]' if args[0] == "ip" else "")

        with patch.object(mplane, "command", side_effect=firewall_command) as command:
            mplane.fw4_install(first["devices"])
        self.assertIn("th dport 12101 ct status dnat accept", command.call_args.kwargs["data"])
        self.assertEqual(self.cfg["policies"], original)
        self.assertEqual(guard.scope(self.cfg), ("ether saddr " + tablet["mac"],))

    def test_dns_confirmation_only_blocks_protected_device_without_withdrawing_other_rows(self):
        self.cfg["main"]["exit"] = "11111111-1111-4111-8111-111111111111"
        protected = dict(
            id="tablet",
            enabled="1",
            mode="global",
            egress_guard="1",
            ip="10.77.50.100",
            mac="02:00:00:00:00:01",
        )
        ordinary = dict(
            id="desktop", enabled="1", mode="global", ip="10.77.50.101", mac="02:00:00:00:00:02"
        )
        self.cfg["policies"] = [protected, ordinary]
        self.svc.egress_guard = guard.Guard()
        self.svc.confirm_egress_ready = m.Service.confirm_egress_ready.__get__(self.svc)
        self.svc.withdraw = Mock(return_value=True)
        state = self.run / "dns-state.json"
        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "fixture")
        schedule.finish(self.cfg["main"], True)

        def tick(n):
            if n == 4:
                state.unlink()
            elif n <= 3:
                state.write_text(
                    json.dumps(
                        dict(
                            state="running",
                            runtime_id="wrong" if n == 1 else self.svc.runtime_id,
                            time=time.time() - 30 if n == 2 else time.time(),
                        )
                    )
                )

        link = SimpleNamespace(
            is_symlink=lambda: True, resolve=lambda: Path("/etc/init.d/routepilot-egress-guard")
        )
        with (
            patch.object(guard, "RUN", self.run),
            patch.object(guard, "BOOT_LINK", link),
            patch.object(guard, "present", side_effect=[False, True, True, True, True, True]),
            patch.object(guard, "offload_active", return_value=False),
            patch.object(guard, "transact") as nft,
        ):
            self.run_loop(
                5,
                tick,
                compiler=m.compile_config,
                lans=[dict(name="br-lan", up=True, network="10.77.50.0/24")],
            )

        running = [event for event in self.events if event["state"] == "running"]
        self.assertEqual(
            [event["egress_guard"] for event in running],
            ["blocking", "blocking", "blocking", "ready", "blocking"],
        )
        self.assertEqual(self.svc.stop.call_count, 2)
        self.svc.withdraw.assert_not_called()
        self.svc.apply.assert_called_once()
        self.assertEqual([row["id"] for row in self.svc.compiled["devices"]], ["tablet", "desktop"])
        install, allow, block = [call.args[0] for call in nft.call_args_list]
        self.assertIn("ether saddr " + protected["mac"], install)
        self.assertNotIn("ether saddr " + ordinary["mac"], install)
        self.assertNotIn("add element", install)
        self.assertIn("add element", allow)
        self.assertEqual(block, "flush set inet routepilot_egress_guard ready\n")

    def test_schedule_write_failure_never_tears_down_healthy_tunnel(self):
        with (
            patch.object(rs, "atomic", side_effect=PermissionError("read-only disk")),
            patch.object(m.subprocess, "Popen") as spawn,
        ):
            self.run_loop()
        spawn.assert_not_called()
        self.assertEqual(
            self.svc.stop.call_count, 2, "only startup/final shutdown may stop service"
        )
        running = [e for e in self.events if e["state"] == "running"]
        self.assertEqual(len(running), 3)
        self.assertTrue(all("更新计划无法持久保存" in e["message"] for e in running))

    def test_master_off_withdraws_capture_before_guard_and_keeps_preferences(self):
        order = []
        protected = dict(
            id="computer",
            enabled="1",
            mode="global",
            egress_guard="1",
            ip="10.77.50.100",
            mac="02:11:22:33:44:55",
        )
        self.cfg["policies"] = [protected]
        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "fixture")
        schedule.finish(self.cfg["main"], True)

        def configure(cfg):
            enabled = cfg["main"]["enabled"] == "1"
            order.append(("guard", enabled))
            self.svc.egress_guard.state = "ready" if enabled else "off"

        def stop(**kwargs):
            order.append(("stop", self.cfg["main"]["enabled"] == "1"))
            self.svc.compiled = None
            return {"direct": True, "legacy": ""}

        self.svc.egress_guard.configure.side_effect = configure
        self.svc.stop.side_effect = stop
        self.run_loop(2, lambda n: self.cfg["main"].update(enabled="0", apply_nonce="off"))
        self.assertEqual(
            order,
            [
                ("guard", True),
                ("stop", True),
                ("guard", True),
                ("stop", False),
                ("guard", False),
                ("stop", False),
            ],
        )
        disabled = next(e for e in self.events if e["state"] == "disabled")
        self.assertEqual(disabled["egress_guard"], "off")
        self.assertEqual(disabled["applied_nonce"], "off")
        self.assertEqual(self.cfg["policies"][0]["egress_guard"], "1")

    def test_failed_background_worker_schedules_retry_without_stopping_tunnel(self):
        job = SimpleNamespace(poll=lambda: 1, returncode=1)
        with patch.object(m.subprocess, "Popen", return_value=job) as spawn:
            self.run_loop()
        spawn.assert_called_once()
        schedule = rs.Schedule(self.rules, self.run).plan(self.cfg["main"])
        self.assertEqual(schedule["state"], "retry")
        self.assertTrue(schedule["retry_pending"])
        self.assertEqual(self.svc.stop.call_count, 2)
        self.assertEqual(len([e for e in self.events if e["state"] == "running"]), 3)

    def test_persisted_success_prevents_startup_redownload(self):
        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "ok")
        schedule.finish(self.cfg["main"], True)
        with patch.object(m.subprocess, "Popen") as spawn:
            self.run_loop()
        spawn.assert_not_called()
        self.assertEqual(len([e for e in self.events if e["state"] == "running"]), 3)

    def test_interrupted_blocked_worker_is_not_automatically_retried(self):
        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "drop")
        (self.run / "update.json").write_text(json.dumps(dict(request_id="drop", state="blocked")))
        with patch.object(m.subprocess, "Popen") as spawn:
            self.run_loop()
        spawn.assert_not_called()
        plan = rs.Schedule(self.rules, self.run).plan(self.cfg["main"])
        self.assertEqual(plan["state"], "awaiting_confirmation")
        self.assertEqual(plan["next_due_at"], 0)

    def test_running_status_reports_compiled_rules_revision(self):
        from rules_catalog import directory_revision

        schedule = rs.Schedule(self.rules, self.run)
        schedule.start(self.cfg["main"], "ok")
        schedule.finish(self.cfg["main"], True)
        self.run_loop(1)
        running = next(e for e in self.events if e["state"] == "running")
        self.assertEqual(running["rules_revision"], directory_revision(self.rules))

    def test_resolved_candidate_is_not_overwritten_by_late_worker_completion(self):
        # The child released its operation lock; an administrator accepted the
        # candidate before the supervisor collected that child's exit status.
        def completed_after_accept():
            rs.Schedule(self.rules, self.run).resolve()
            (self.run / "update.json").write_text(
                json.dumps(dict(request_id="accepted-candidate", state="success"))
            )
            return 1

        job = SimpleNamespace(poll=completed_after_accept, returncode=1)
        with patch.object(m.subprocess, "Popen", return_value=job):
            self.run_loop(2)
        plan = rs.Schedule(self.rules, self.run).plan(self.cfg["main"])
        self.assertEqual(
            plan["state"],
            "scheduled",
            "late failed worker must not undo an explicit candidate resolution",
        )
        self.assertFalse(plan["retry_pending"])

    def assert_preparing_failure_feedback(self, fail_resolve):
        request_id = "manual-preparing-failed"
        self.cfg["main"].update(enabled="0", preparing="1", update_nonce=request_id)
        self.svc.plane.connect = Mock(
            side_effect=None if fail_resolve else RuntimeError("fixture connection unavailable")
        )
        resolver = (
            Mock(side_effect=ValueError("fixture exit unavailable"))
            if fail_resolve
            else Mock(return_value="fixture")
        )
        with patch.object(m, "resolve_exit", resolver), patch.object(m, "update_rules") as update:
            self.run_loop()
        update.assert_not_called()
        resolver.assert_called_once()
        if not fail_resolve:
            self.svc.plane.connect.assert_called_once_with("fixture")
        self.assertTrue(
            (self.run / "update.json").exists(),
            "manual failure must publish request-correlated feedback",
        )
        feedback = json.loads((self.run / "update.json").read_text())
        self.assertEqual(feedback["state"], "failed")
        self.assertEqual(feedback["request_id"], request_id)
        self.assertGreater(feedback["finished_at"], 0)
        persisted = rs.Schedule(self.rules, self.run)
        self.assertGreater(
            persisted.data["last_attempt_at"],
            0,
            "a failed pre-download attempt still counts as an attempt",
        )
        self.assertEqual(persisted.data["last_nonce"], request_id)
        self.assertFalse(persisted.data.get("running"))
        self.assertEqual(persisted.plan(self.cfg["main"])["state"], "paused")

    def test_preparing_manual_update_exit_resolution_failure_reports_request(self):
        self.assert_preparing_failure_feedback(True)

    def test_preparing_manual_update_connection_failure_reports_request(self):
        self.assert_preparing_failure_feedback(False)

    def test_close_then_reopen_reconciles_cancelled_worker_instead_of_staying_running(self):
        process = SimpleNamespace(poll=lambda: None, returncode=None)

        def stop(**kwargs):
            self.svc.update_job = None
            self.svc.compiled = None
            return {"direct": True, "legacy": ""}

        self.svc.stop = Mock(side_effect=stop)

        def steer(tick):
            if tick == 1:
                self.cfg["main"]["enabled"] = "0"
            if tick == 2:
                self.cfg["main"]["enabled"] = "1"

        with patch.object(m.subprocess, "Popen", return_value=process) as spawn:
            self.run_loop(3, on_tick=steer)
        spawn.assert_called_once()
        persisted = rs.Schedule(self.rules, self.run)
        self.assertFalse(
            persisted.data.get("running"),
            "cancelled child must be reconciled when service comes back",
        )
        self.assertEqual(persisted.plan(self.cfg["main"])["state"], "retry")
        states = [event["state"] for event in self.events]
        self.assertEqual(states[:3], ["running", "disabled", "running"])
