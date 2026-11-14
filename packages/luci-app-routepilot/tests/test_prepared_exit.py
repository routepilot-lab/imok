import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))

import prepared_exit as p
from public_errors import ControlError


class PreparedExitTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.run = Path(folder.name)
        self.main = {
            "enabled": "0",
            "preparing": "1",
            "exit": "9e6e5e56-a5df-46bd-8272-1fd0b3496113",
            "apply_nonce": "test",
        }
        self.selected = self.main["exit"]
        self.tunnel = "Disconnected"
        self.plane = Mock(protocol="MASQUE")
        self.plane.connect.side_effect = self.connect
        self.service = SimpleNamespace(plane=self.plane, backend="masque", compiled=None)
        (self.run / "status.json").write_text(
            json.dumps({"state": "preparing", "time": time.time()})
        )
        for key, value in [
            ("RUN", self.run),
            ("cli", Mock(side_effect=self.cli)),
            ("read_config", lambda **_: {"main": self.main}),
            ("probe_exit", Mock(return_value=("8.8.8.8", "US"))),
        ]:
            patcher = patch.object(p, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def connect(self, exit_id, *, deadline, checkpoint, keep_selection):
        if keep_selection:
            self.assertEqual(exit_id, self.selected)
        self.assertGreater(deadline, time.monotonic())
        checkpoint()
        self.selected = exit_id
        self.tunnel = "Connected"

    def cli(self, action, *_args, **_kwargs):
        if action == "vnet":
            if _args != ("--no-paginate",):
                self.selected = _args[0]
            rows = {self.selected, self.main["exit"]} - {None}
            stdout = f"Currently selected: {self.selected}\nVirtual Networks:\n" + "\n".join(
                f"ID: {row}\nName: test\nDefault: false" for row in rows
            )
        elif action == "status":
            stdout = "Status update: " + self.tunnel
        elif action == "disconnect":
            self.tunnel = "Disconnected"
            stdout = ""
        else:
            raise AssertionError(action)
        return SimpleNamespace(stdout=stdout, returncode=0)

    def result(self):
        return json.loads((self.run / p.RESULT).read_text())

    def test_probes_and_restores_only_the_test_connection(self):
        reply = p.submit()
        with self.assertRaises(ControlError):
            p.submit()
        self.assertTrue(p.run_pending(self.main, self.service))
        self.assertEqual(self.result()["request_id"], reply["request_id"])
        self.assertEqual(self.result()["state"], "done")
        self.assertEqual(self.result()["nonce"], self.main["apply_nonce"])
        self.assertEqual(self.result()["exit_id"], self.main["exit"])
        self.assertEqual(self.result()["country"], "US")
        self.assertEqual(self.tunnel, "Disconnected")
        self.assertEqual(self.main["enabled"], "0")
        self.plane.forward.assert_not_called()
        self.assertFalse(p.run_pending(self.main, self.service))

    def test_never_changes_a_different_exit_or_disconnects_existing_tunnel(self):
        self.tunnel = "Connected"
        self.selected = "9e6e5e56-a5df-46bd-8272-1fd0b3496114"
        p.submit()
        p.run_pending(self.main, self.service)
        self.assertEqual(self.result()["state"], "failed")
        self.assertIn("未切换出口", self.result()["message"])
        self.plane.connect.assert_not_called()
        p.probe_exit.assert_not_called()
        self.assertEqual(self.tunnel, "Connected")

    def test_can_probe_saved_exit_and_restore_previous_disconnected_selection(self):
        other = "9e6e5e56-a5df-46bd-8272-1fd0b3496114"
        self.selected = other
        p.submit()
        p.run_pending(self.main, self.service)
        self.assertEqual(self.result()["state"], "done")
        self.assertEqual(self.selected, other)
        self.assertEqual(self.tunnel, "Disconnected")

    def test_configuration_change_or_missing_preparation_blocks_the_test(self):
        p.submit()
        self.main["apply_nonce"] = "changed"
        p.run_pending(self.main, self.service)
        self.assertEqual(self.result()["state"], "failed")
        self.plane.connect.assert_not_called()
        self.main["enabled"] = "1"
        with self.assertRaises(ControlError):
            p.submit()

    def test_connect_timeout_reports_stage_and_does_not_probe(self):
        self.plane.connect.side_effect = TimeoutError("private diagnostic")
        p.submit()
        p.run_pending(self.main, self.service)
        self.assertEqual(self.result()["state"], "failed")
        self.assertIn("建立 WARP 测试隧道超时", self.result()["message"])
        self.assertNotIn("private diagnostic", self.result()["message"])
        p.probe_exit.assert_not_called()
        p.cli.assert_any_call("disconnect", timeout=4)

    def test_probe_failure_reports_stage_and_restores_original_selection(self):
        original = "9e6e5e56-a5df-46bd-8272-1fd0b3496114"
        self.selected = original
        p.probe_exit.side_effect = RuntimeError("private diagnostic")
        p.submit()
        p.run_pending(self.main, self.service)
        self.assertEqual(self.result()["state"], "failed")
        self.assertIn("读取出口检测站点", self.result()["message"])
        self.assertNotIn("private diagnostic", self.result()["message"])
        p.probe_exit.assert_called_once()
        self.assertEqual(self.selected, original)
        self.assertEqual(self.tunnel, "Disconnected")

    def test_cleanup_error_reports_unconfirmed_disconnect_even_after_successful_probe(self):
        def fail_disconnect(action, *args, **kwargs):
            if action == "disconnect":
                raise RuntimeError("private diagnostic")
            return self.cli(action, *args, **kwargs)

        p.cli.side_effect = fail_disconnect
        p.submit()
        p.run_pending(self.main, self.service)
        self.assertEqual(self.result()["state"], "failed")
        self.assertIn("无法确认 WARP 测试隧道已断开", self.result()["message"])
        self.assertNotIn("private diagnostic", self.result()["message"])
        self.assertNotIn("ip", self.result())
