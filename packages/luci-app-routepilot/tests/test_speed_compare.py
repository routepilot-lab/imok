"""Comparison lifecycle against synthetic connections and a temporary runtime directory."""

from contextlib import nullcontext
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import speed_compare as comparison
from public_errors import ControlError
from warp_control import ProtocolMismatch


class SpeedComparisonTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        run = Path(self.sandbox.name)
        paths = {
            "REQUEST": run / "request",
            "CANCEL": run / "cancel",
            "RESULT": run / "result",
            "RUN": run,
            "locks": nullcontext,
        }
        self.real_locks = comparison.locks
        patcher = patch.multiple(comparison, **paths)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("TRANSACTION", "PROCESS_BLOCK"):
            patcher = patch(f"endpoint_control.{key}", run / key)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.saved = str(uuid.uuid4())
        self.cfg = dict(main=dict(exit=self.saved, apply_nonce="one", enabled="1"), policies=[])
        self.targets = [str(uuid.uuid4()) for _ in range(6)]
        self.service = SimpleNamespace(
            compiled={"vnet": self.saved},
            update_job=None,
            suspend_capture=Mock(return_value=True),
            plane=Mock(),
            stop=Mock(return_value={"direct": True}),
        )

    def submit(self, kind="exit", managed=False):
        with (
            patch.object(comparison, "read_config", return_value=self.cfg),
            patch(
                "request_input.claim_request",
                return_value=nullcontext({"kind": kind, "targets": self.targets}),
            ),
            patch.object(
                comparison.speed_test,
                "status",
                return_value=dict(backend="masque", applied_nonce="one", protocol_managed=managed),
            ),
            patch("mplane.cli") as cli,
        ):
            result = comparison.start("mock-request")
        cli.assert_not_called()
        return result

    def test_request_only_records_intent_and_enforces_batch_budget(self):
        self.assertEqual(self.submit()["request_id"], "mock-request")
        request = comparison.load(comparison.REQUEST)
        self.assertEqual(request["targets"], self.targets)
        self.assertLessEqual(request["bytes_per_target"] * len(self.targets), 1_000_000_000)
        self.assertEqual(request["nonce"], "one")
        self.assertEqual(request["digest"], comparison.config_key(self.cfg))
        self.assertNotIn("digest", comparison.load(comparison.RESULT))

    def test_failed_probe_skips_target_and_each_success_uses_its_own_proof(self):
        self.targets = self.targets[:2]
        job = self.submit()["id"]
        events = []
        self.service.suspend_capture.side_effect = lambda **_: events.append("blocked") or True

        def measured(plane, target, deadline, checkpoint):
            self.assertEqual(events[0], "blocked")
            if target == self.targets[0]:
                raise ControlError("strict probe failed")
            return "9.9.9.9"

        with (
            patch.object(comparison, "read_config", return_value=self.cfg),
            patch.object(comparison, "measure_target", side_effect=measured),
            patch.object(comparison, "confirm_target"),
            patch.object(
                comparison.speed_test,
                "measure_path",
                return_value=dict(state="done", bytes=200, exit_ip="9.9.9.9"),
            ) as measure,
        ):
            result = comparison.execute(
                self.service, self.cfg, lambda: False, lambda *args: events.append(args)
            )
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(
            [(r["target"], r["state"]) for r in result["rows"]],
            [(self.targets[0], "skipped"), (self.targets[1], "done")],
        )
        self.assertEqual(measure.call_args.args[:3], ("masque", "proxy", "9.9.9.9"))
        self.assertTrue(all(row["id"] == job for row in result["rows"]))
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "restoring")
        comparison.finish(result, restored=True)
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "done")

    def test_target_failures_report_the_failed_check_without_raw_client_output(self):
        plane = self.service.plane
        plane.connect.return_value = comparison.TunnelStatus.CONNECTED
        deadline = time.monotonic() + 5
        checks = (
            (plane.connect, ProtocolMismatch("synthetic client output"), "客户端未采用所选协议"),
            (plane.probe, TimeoutError("synthetic client output"), "严格出口探测超时"),
        )
        with patch.object(comparison, "confirm_target"):
            for operation, error, expected in checks:
                with self.subTest(expected=expected):
                    operation.side_effect = error
                    try:
                        row = comparison.trial_row(plane, self.saved, deadline, Mock(), 200)
                    finally:
                        operation.side_effect = None
                    self.assertEqual(row, dict(state="skipped", bytes=0, message=expected))

    def test_change_stops_next_target_and_marks_preserved_results(self):
        self.submit()
        updated = dict(self.cfg, main={**self.cfg["main"], "apply_nonce": "changed"})
        with (
            patch.object(
                comparison, "read_config", side_effect=[self.cfg, self.cfg, self.cfg, updated]
            ),
            patch.object(comparison, "measure_target", return_value="9.9.9.9") as probe,
            patch.object(comparison, "confirm_target"),
            patch.object(
                comparison.speed_test, "measure_path", return_value=dict(state="done", bytes=200)
            ),
        ):
            result = comparison.execute(self.service, self.cfg, lambda: False, Mock())
        probe.assert_called_once()
        self.assertEqual(result["outcome"], "config_changed")
        comparison.finish(result, restored=True)
        self.assertEqual(
            comparison.load(comparison.RESULT)["history"][self.targets[0]]["batch_state"],
            "config_changed",
        )

    def test_cancel_and_restart_consume_intent_without_switching(self):
        self.submit()
        comparison.cancel()
        comparison.execute(self.service, self.cfg, lambda: False, Mock())
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "cancelled")
        self.assertFalse(comparison.REQUEST.exists())
        self.service.suspend_capture.assert_not_called()
        self.submit()
        comparison.discard_interrupted()
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "interrupted")
        self.assertFalse(comparison.REQUEST.exists())

    def test_timeout_requires_restoration_and_failed_restoration_is_reported(self):
        self.submit()
        with (
            patch.object(comparison, "read_config", return_value=self.cfg),
            patch.object(comparison, "measure_target", side_effect=TimeoutError),
            patch.object(comparison.time, "monotonic", side_effect=[10, 10, 10, 10, 11, 400]),
        ):
            result = comparison.execute(self.service, self.cfg, lambda: False, Mock())
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "restoring")
        comparison.finish(result, restored=False)
        self.assertFalse(comparison.load(comparison.RESULT)["restored"])

    def test_stale_request_expires_without_capture_changes(self):
        self.submit()
        with patch.object(comparison.time, "time", return_value=time.time() + 31):
            comparison.expire_queued()
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "timeout")
        self.service.suspend_capture.assert_not_called()

    def test_busy_shared_lock_preserves_queued_request_until_next_pass(self):
        job = self.submit()["id"]
        lock = Path(self.sandbox.name) / "speed.lock"
        with (
            patch.object(comparison, "locks", self.real_locks),
            patch.object(comparison.speed_test, "LOCK", lock),
            lock.open("a") as submitting,
        ):
            comparison.fcntl.flock(submitting, comparison.fcntl.LOCK_EX | comparison.fcntl.LOCK_NB)
            self.assertIsNone(comparison.execute(self.service, self.cfg, lambda: False, Mock()))
        self.assertEqual(comparison.load(comparison.REQUEST)["id"], job)
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "queued")
        comparison.cancel()
        comparison.execute(self.service, self.cfg, lambda: False, Mock())
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "cancelled")
        self.service.suspend_capture.assert_not_called()

    def test_protocol_request_disables_managed_settings(self):
        self.targets = list(comparison.PROTOCOLS)
        with self.assertRaisesRegex(ControlError, "受管理"):
            self.submit("protocol", managed=True)
        self.assertFalse(comparison.REQUEST.exists())

    def test_protocol_readback_failure_invalidates_but_keeps_measured_bytes(self):
        args = dict(
            protocol="MASQUE",
            organization="",
            dns={},
            vnet=self.saved,
            deadline=time.monotonic() + 5,
            budget=200,
        )
        with (
            patch("mplane.Plane"),
            patch("mplane.cli", side_effect=RuntimeError("synthetic settings failure")),
            patch.object(
                comparison,
                "trial_row",
                return_value=dict(state="done", bytes=120, exit_ip="8.8.8.8"),
            ),
        ):
            row = comparison.protocol_trial(args)
        self.assertEqual(row["state"], "invalid")
        self.assertEqual(row["bytes"], 120)
        self.assertEqual(row["exit_ip"], "8.8.8.8")
        self.assertEqual(row["message"], "测速结束时协议未确认")

    def test_protocol_trial_runs_only_under_block_and_restores_via_supervisor(self):
        self.targets = list(comparison.PROTOCOLS)
        job = self.submit("protocol")["id"]
        events = []
        self.service.suspend_capture.side_effect = lambda **_: events.append("block") or True
        self.service.stop.side_effect = lambda **_: events.append("cleanup") or {"direct": True}

        def run_trial(main, protocol, deadline, checkpoint, budget):
            self.assertEqual(events[0], "block")
            events.append(protocol)
            if protocol == "MASQUE":
                raise ControlError("probe failed")
            return dict(state="done", bytes=100, exit_ip="8.8.8.8")

        with (
            patch.object(comparison, "read_config", return_value=self.cfg),
            patch("mplane.cli"),
            patch("warp_control.parse_settings", return_value={"managed": False}),
            patch.object(comparison, "run_protocol_trial", side_effect=run_trial),
            patch.object(comparison, "restore_protocol_preferences") as restore_settings,
        ):
            result = comparison.execute(self.service, self.cfg, lambda: False, Mock())
        self.assertEqual(events, ["block", "MASQUE", "WireGuard", "cleanup"])
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(
            [(row["target"], row["state"]) for row in result["rows"]],
            [("MASQUE", "skipped"), ("WireGuard", "done")],
        )
        self.assertTrue(all(row["id"] == job for row in result["rows"]))
        self.assertEqual(comparison.load(comparison.RESULT)["state"], "restoring")
        self.service.stop.assert_called_once_with(restore=False)
        restore_settings.assert_called_once_with(self.cfg["main"])
