import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_endpoints as et
import test_request_binding as requests
import endpoint_control as e


class OperatorRecovery(unittest.TestCase):
    def setUp(self):
        self.t = et.Endpoints()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.t.config()
        self.t.main["enabled"] = "1"
        self.t.main["backend"] = "masque"
        patcher = patch.object(e, "boot_id", return_value="test-boot")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t.svc.plane.start = Mock()
        self.t.svc.stop = Mock(return_value={"direct": True})
        self.record = dict(
            revision="one",
            previous="162.159.197.2:8443",
            target="162.159.197.2:443",
            baseline="8.8.8.8",
            vnet=self.t.main["vnet_usa"],
            phase="unknown",
            state="unknown",
        )
        e.TRANSACTION.write_text(json.dumps(self.record))
        e.APPLIED.write_text(json.dumps(self.record))
        e.observe_recovery(self.t.main)

    def queue(self):
        offer = e.recovery_offer(self.t.main)
        self.assertTrue(offer["available"])
        token = requests.put(
            e.RUN / "endpoint-recover-input",
            "a" * 32,
            {"confirm": "RESTORE", "revision": offer["revision"], "context": offer["context"]},
        )
        return e.save_recovery(token)

    def test_explicit_verified_recovery_archives_and_survives_new_manager(self):
        self.queue()
        self.assertTrue(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertFalse(e.RECOVERY.exists())
        self.assertFalse(e.TRANSACTION.exists())
        self.assertEqual(len(list((e.DIRECTORY / "recovery-audit").glob("*.json"))), 1)
        self.assertFalse(e.Manager().pause_reason(self.t.main))
        self.assertTrue(e.read(e.APPLIED)["operator_recovered"])
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))

    def test_failure_remains_latched_and_no_automatic_retry(self):
        self.queue()
        self.t.svc.plane.probe.return_value = "1.1.1.1"
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertTrue(e.classify_recovery()["startup_pause"])
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertTrue(e.RECOVERY.exists())

    def test_context_change_rejects_before_any_client_start(self):
        self.queue()
        self.t.main["warp_protocol"] = "WireGuard"
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.t.svc.plane.start.assert_not_called()
        self.assertEqual(self.t.calls, [])

    def test_queue_requires_typed_confirmation_and_original_evidence(self):
        token = requests.put(
            e.RUN / "endpoint-recover-input",
            "a" * 32,
            {"confirm": "yes", "revision": "one", "context": "x"},
        )
        with self.assertRaises(ValueError):
            e.save_recovery(token)
        e.TRANSACTION.write_text("bad")
        self.assertFalse(e.recovery_offer(self.t.main)["available"])
        self.assertTrue(e.classify_recovery()["startup_pause"])

    def test_other_vnet_and_other_override_are_not_recoverable(self):
        self.t.main["vnet_usa"] = "00000000-0000-0000-0000-000000000002"
        self.assertFalse(e.recovery_offer(self.t.main)["available"])
        self.t.main["vnet_usa"] = self.record["vnet"]
        (e.ROOT / "settings.json").write_text('{"override_warp_endpoint":"1.1.1.1:443"}')
        self.assertFalse(e.recovery_offer(self.t.main)["available"])

    def test_request_cannot_replay_after_reboot_or_expiry(self):
        self.queue()
        path = e.DIRECTORY / "endpoint-recover-request.json"
        r = e.read(path)
        r["boot"] = "other"
        path.write_text(json.dumps(r))
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.t.svc.plane.start.assert_not_called()
        self.assertTrue(e.RECOVERY.exists())

    def test_archive_failure_never_starts_client_or_unlocks(self):
        self.queue()
        with patch.object(e, "durable", side_effect=OSError("storage")):
            self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.t.svc.plane.start.assert_not_called()
        self.assertTrue(e.RECOVERY.exists())

    def test_verified_history_write_failure_and_unlock_failure_remain_blocked(self):
        self.queue()
        normal = e.durable

        def fail(path, value):
            if path == e.APPLIED and value.get("operator_recovered"):
                raise OSError("synthetic final proof storage failure")
            return normal(path, value)

        with patch.object(e, "durable", side_effect=fail):
            self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertTrue(e.Manager().pause_reason(self.t.main))
        self.assertTrue(e.TRANSACTION.exists())

    def test_expired_request_does_not_execute_or_change_original_evidence(self):
        self.queue()
        path = e.DIRECTORY / "endpoint-recover-request.json"
        r = e.read(path)
        r["time"] = 1
        path.write_text(json.dumps(r))
        before = e.TRANSACTION.read_bytes()
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertEqual(e.TRANSACTION.read_bytes(), before)
        self.t.svc.plane.start.assert_not_called()

    def test_conflicting_history_or_preferences_never_offers_unlock(self):
        e.APPLIED.write_text('{"revision":"other","state":"unknown"}')
        self.assertFalse(e.recovery_offer(self.t.main)["available"])

    def test_power_loss_after_journal_delete_preserves_explicit_recovery_path(self):
        record = {
            **self.record,
            "state": "failed",
            "phase": "rolled_back",
            "connected": True,
            "exit_verified": True,
            "rollback_confirmed": True,
        }
        e.APPLIED.write_text(json.dumps(record))
        e.TRANSACTION.unlink()
        self.assertTrue(e.recovery_offer(self.t.main)["available"])
        self.queue()
        self.assertTrue(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertFalse(e.RECOVERY.exists())

    def test_scope_change_without_nonce_is_detected_during_verification(self):
        self.queue()

        def changed():
            self.t.main["scope"] = "lan"
            return "8.8.8.8"

        self.t.svc.plane.probe.side_effect = changed
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertTrue(e.RECOVERY.exists())

    def test_incomplete_terminal_proof_cannot_replace_missing_transaction(self):
        e.APPLIED.write_text(
            '{"state":"failed","revision":"one","connected":true,"exit_verified":true,"rollback_confirmed":true}'
        )
        e.TRANSACTION.unlink()
        self.assertFalse(e.recovery_offer(self.t.main)["available"])

    def test_applied_proof_without_journal_is_not_reverted_to_previous(self):
        e.APPLIED.write_text(
            json.dumps(
                {
                    **self.record,
                    "state": "applied",
                    "phase": "done",
                    "connected": True,
                    "exit_verified": True,
                }
            )
        )
        e.TRANSACTION.unlink()
        self.assertFalse(e.recovery_offer(self.t.main)["available"])

    def test_expired_queue_can_be_replaced_before_supervisor_runs(self):
        self.queue()
        path = e.DIRECTORY / "endpoint-recover-request.json"
        r = e.read(path)
        r["boot"] = "old-boot"
        path.write_text(json.dumps(r))
        out = self.queue()
        self.assertTrue(out["saved"])
        self.assertEqual(e.read(path)["boot"], "test-boot")

    def test_busy_operation_lock_does_not_start_or_stop_client(self):
        self.queue()
        with e.locked():
            self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.t.svc.plane.start.assert_not_called()
        self.t.svc.stop.assert_not_called()
        self.assertTrue((e.DIRECTORY / "endpoint-recover-request.json").exists())

    def test_mid_verification_config_change_does_not_unlock(self):
        self.queue()

        def changed():
            self.t.main["apply_nonce"] = "changed"
            return "8.8.8.8"

        self.t.svc.plane.probe.side_effect = changed
        self.assertFalse(e.Manager().recover_requested(self.t.svc, self.t.main))
        self.assertTrue(e.RECOVERY.exists())


class PendingAndSchedule(unittest.TestCase):
    def setUp(self):
        self.t = et.Endpoints()
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        self.t.config()

    def test_pending_waits_for_original_line_then_is_verified(self):
        self.t.main.pop("expected_usa")
        self.t.svc.compiled = None
        manager = e.Manager()
        self.assertFalse(manager.tick(self.t.svc, self.t.main))
        self.assertEqual(e.read(e.APPLIED), {})
        self.assertIn("等待", e.read(e.WARNING)["message"])
        self.t.svc.compiled = {"ready": True}
        with (
            patch(
                "mplane.cli",
                side_effect=lambda *a, **k: type(
                    "R", (), {"stdout": "Status update: Connected", "returncode": 0}
                )(),
            ),
            patch.object(e, "set_override"),
        ):
            self.assertTrue(manager.tick(self.t.svc, self.t.main))
        self.assertEqual(e.read(e.APPLIED)["state"], "applied")
        self.assertTrue(e.read(e.APPLIED)["exit_verified"])

    def test_schedule_only_preserves_all_preferences_and_history(self):
        e.APPLIED.write_text('{"state":"unknown","revision":"other"}')
        history = e.APPLIED.read_bytes()
        before = e.settings()
        token = requests.put(e.RUN / "endpoint-schedule-input", "a" * 32, {"hours": 6})
        out = e.save_schedule(token)
        after = e.settings()
        self.assertEqual(after, {**before, "hours": 6})
        self.assertEqual(e.APPLIED.read_bytes(), history)
        self.assertEqual(self.t.calls, [])
        self.assertEqual(out["request_id"], "a" * 32)

    def test_staged_preference_stays_staged_and_invalid_interval_does_not_write(self):
        cfg = e.settings()
        cfg["staged"] = True
        e.CONFIG.write_text(json.dumps(cfg))
        token = requests.put(e.RUN / "endpoint-schedule-input", "a" * 32, {"hours": 12})
        e.save_schedule(token)
        self.assertTrue(e.settings()["staged"])
        before = e.CONFIG.read_bytes()
        token = requests.put(
            e.RUN / "endpoint-schedule-input", "a" * 32, {"hours": 2, "mode": "auto"}
        )
        with self.assertRaises(ValueError):
            e.save_schedule(token)
        self.assertEqual(e.CONFIG.read_bytes(), before)


class SupervisorPending(unittest.TestCase):
    def test_closed_saved_request_waits_through_real_supervisor_startup(self):
        import supervisor as m
        import mplane

        t = et.Endpoints()
        t.setUp()
        self.addCleanup(t.doCleanups)
        t.main.update(enabled="0", backend="masque", scope="devices")
        t.main.pop("expected_usa")
        t.config()
        svc = t.svc
        connected = [False]
        history = []
        svc.compiled = None
        svc.recovery_started = None
        svc.runtime_id = "fixture"
        svc.update_job = Mock()
        svc.update_job.poll.return_value = None
        svc.plane.start = Mock()
        svc.plane.protocol_preference = "auto"
        svc.plane.protocol = "MASQUE"
        svc.plane.connect = Mock(side_effect=lambda *_: connected.__setitem__(0, True))
        svc.health = Mock()

        def stop(**kw):
            svc.compiled = None
            connected[0] = False
            return {"direct": True, "legacy": ""}

        def apply(cfg, compiled):
            svc.plane.connect(compiled["vnet"])
            svc.compiled = compiled

        def withdraw():
            svc.compiled = None
            return True

        svc.stop = Mock(side_effect=stop)
        svc.apply = Mock(side_effect=apply)
        svc.suspend_capture = Mock(side_effect=withdraw)

        def cli(*a, **k):
            if a == ("status",):
                return type(
                    "R",
                    (),
                    {
                        "stdout": "Status update: Connected"
                        if connected[0]
                        else "Status update: Disconnected",
                        "returncode": 0,
                    },
                )()
            return t.cli(*a, **k)

        def sleep(_):
            history.append(e.read(e.APPLIED))
            t.main["enabled"] = "1"
            if len(history) >= 3:
                m.STOP = True

        compiled = {
            "dns": {},
            "devices": [],
            "exit": "usa",
            "scope": "devices",
            "vnet": t.main["vnet_usa"],
        }
        svc.egress_guard = Mock(state="off", selectors=())
        svc.confirm_egress_ready = Mock()
        with (
            patch.object(m, "RUN", e.RUN),
            patch.object(m.sys, "argv", ["routepilot", "run"]),
            patch.object(m, "STOP", False),
            patch.object(m, "Service", return_value=svc),
            patch.object(m, "boot_config", return_value={"main": {}, "policies": []}),
            patch.object(
                m, "read_config", side_effect=lambda: {"main": dict(t.main), "policies": []}
            ),
            patch.object(m, "lan_interfaces", return_value=[]),
            patch.object(m, "selected_lans", return_value=[]),
            patch.object(m, "preflight", return_value={}),
            patch.object(m, "rule_snapshot", side_effect=lambda: contextlib.nullcontext(t.p)),
            patch.object(m, "routing_digest", return_value="fixture"),
            patch.object(m, "compile_config", side_effect=lambda *a: dict(compiled, dns={})),
            patch.object(m.signal, "signal"),
            patch.object(m.time, "sleep", side_effect=sleep),
            patch.object(mplane, "cli", side_effect=cli),
        ):
            m.run()
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0], {})
        self.assertEqual(history[1], {})
        self.assertEqual(history[2]["state"], "applied")
        self.assertTrue(history[2]["exit_verified"])
        self.assertGreaterEqual(svc.apply.call_count, 2)
