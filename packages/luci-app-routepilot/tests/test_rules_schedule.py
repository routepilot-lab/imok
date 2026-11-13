import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
from rules_schedule import Schedule


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rules = self.root / "rules"
        self.run = self.root / "run"
        self.run.mkdir()
        self.main = dict(enabled="1", update_hours="168", update_nonce="")
        self.s = Schedule(self.rules, self.run)

    def test_persisted_success_does_not_update_again_after_restart(self):
        self.assertTrue(self.s.due(self.main, 1000))
        self.s.start(self.main, "a", 1000)
        self.s.finish(self.main, True, now=1020)
        restarted = Schedule(self.rules, self.run)
        self.assertFalse(restarted.due(self.main, 1030))
        self.assertEqual(restarted.plan(self.main, 1030)["next_due_at"], 1020 + 168 * 3600)

    def test_failed_update_has_three_bounded_retries_then_regular_interval(self):
        for index, delay in enumerate((300, 1800, 7200, 168 * 3600)):
            now = 1000 + index * 10000
            self.s.start(self.main, str(index), now)
            self.s.finish(self.main, False, now=now + 1)
            self.assertEqual(self.s.plan(self.main, now + 1)["next_due_at"], now + delay)
            self.assertFalse(self.s.due(self.main, now + delay - 1))
            self.assertTrue(self.s.due(self.main, now + delay))
        self.assertTrue(self.s.plan(self.main, 40000)["retries_exhausted"])

    def test_saved_interval_and_manual_request_do_not_wait_for_old_schedule(self):
        self.s.start(self.main, "a", 1000)
        self.s.finish(self.main, True, now=1010)
        self.main["update_hours"] = "24"
        self.assertEqual(self.s.plan(self.main, 1020)["next_due_at"], 1010 + 86400)
        self.main["update_nonce"] = "manual-new"
        self.assertTrue(self.s.due(self.main, 1020))
        self.s.start(self.main, "manual-new", 1020)
        self.s.finish(self.main, True, now=1030)
        self.assertFalse(Schedule(self.rules, self.run).due(self.main, 1040))

    def test_crashed_worker_reconciles_to_retry_not_permanent_running(self):
        self.s.start(self.main, "interrupted", 1000)
        restarted = Schedule(self.rules, self.run)
        restarted.reconcile(self.main, 1010)
        self.assertEqual(restarted.plan(self.main, 1011)["state"], "retry")
        self.assertFalse(restarted.data["running"])

    def test_success_before_supervisor_crash_is_reconciled(self):
        self.s.start(self.main, "ok", 1000)
        (self.run / "update.json").write_text(json.dumps(dict(request_id="ok", state="success")))
        restarted = Schedule(self.rules, self.run)
        restarted.reconcile(self.main, 1010)
        self.assertEqual(restarted.data["failures"], 0)
        self.assertFalse(restarted.due(self.main, 1011))

    def test_blocked_candidate_does_not_automatically_retry(self):
        self.s.start(self.main, "small", 1000)
        self.s.finish(self.main, False, blocked=True, now=1010)
        self.assertFalse(self.s.due(self.main, 1000000))
        self.assertEqual(self.s.plan(self.main, 1020)["state"], "awaiting_confirmation")
        self.main["enabled"] = "0"
        self.assertEqual(self.s.plan(self.main, 1020)["state"], "paused")
        self.main["update_nonce"] = "explicit"
        self.assertTrue(self.s.due(self.main, 1020))

    def test_legacy_success_metadata_prevents_restart_redownload(self):
        self.rules.mkdir()
        (self.rules / "updated.json").write_text('{"time":1000}')
        self.assertFalse(self.s.due(self.main, 1010))
        self.assertEqual(self.s.plan(self.main, 1010)["last_success_at"], 1000)
