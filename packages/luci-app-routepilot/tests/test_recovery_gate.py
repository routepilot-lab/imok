import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_endpoints as et
import test_config_backup as bt
import test_request_binding as requests
import endpoint_control as e
import config_backup as b


@contextlib.contextmanager
def case(cls=et.Endpoints):
    t = cls()
    t.setUp()
    try:
        yield t
    finally:
        t.doCleanups()


def transaction(t, phase="apply"):
    return dict(
        revision="one",
        previous="162.159.197.2:8443",
        target="162.159.197.2:443",
        baseline="8.8.8.8",
        vnet=t.main["vnet_usa"],
        phase=phase,
    )


class RecoveryGate(unittest.TestCase):
    def test_critical_history_rejects_all_new_modes_without_overwriting_evidence(self):
        for state in ("applying", "verifying", "rolling_back", "unknown"):
            for mode in ("preserve", "manual"):
                with self.subTest(state=state, mode=mode), case() as t:
                    t.config(rev="previous")
                    e.APPLIED.write_text(json.dumps(dict(state=state, revision="old")))
                    before = e.CONFIG.read_bytes()
                    history = e.APPLIED.read_bytes()
                    token = requests.put(
                        e.INPUT, "a" * 32, {"mode": mode, "address": "162.159.197.2:443"}
                    )
                    with self.assertRaises(e.RecoveryBlocked) as ex:
                        e.save(token)
                    self.assertEqual(ex.exception.code, e.info()["recovery_block"]["code"])
                    self.assertEqual(e.CONFIG.read_bytes(), before)
                    self.assertEqual(e.APPLIED.read_bytes(), history)
                    self.assertEqual(t.calls, [])
                    self.assertTrue(e.RECOVERY.exists())

    def test_failed_rollback_confirmation_must_be_boolean_true(self):
        for value in (False, "false", "true", 1, None):
            with self.subTest(value=value), case() as _t:
                e.APPLIED.write_text(json.dumps(dict(state="failed", rollback_confirmed=value)))
                token = requests.put(e.INPUT, "a" * 32, {"mode": "preserve"})
                with self.assertRaises(e.RecoveryBlocked):
                    e.save(token)
                self.assertFalse(e.CONFIG.exists())

    def test_verified_terminal_records_and_empty_initial_record_allow_save(self):
        for record in (
            {},
            {"state": "applied"},
            {"state": "preserved"},
            {"state": "rejected"},
            {"state": "failed", "rollback_confirmed": True},
        ):
            with self.subTest(record=record), case() as _t:
                e.APPLIED.write_text(json.dumps(record))
                token = requests.put(e.INPUT, "a" * 32, {"mode": "preserve"})
                self.assertTrue(e.save(token)["saved"])
                self.assertFalse(e.RECOVERY.exists())

    def test_pending_revision_cannot_bypass_tick_gate(self):
        with case() as t:
            t.config(rev="new")
            e.APPLIED.write_text('{"state":"unknown","revision":"old"}')
            before = e.APPLIED.read_bytes()
            with self.assertRaises(e.RecoveryBlocked):
                e.Manager().tick(t.svc, t.main)
            self.assertEqual(e.APPLIED.read_bytes(), before)
            self.assertFalse(e.TRANSACTION.exists())
            self.assertEqual(t.calls, [])

    def test_import_rejects_old_unknown_history_before_any_uci_write(self):
        with case(bt.ConfigBackup) as t:
            e.APPLIED.write_text('{"state":"unknown","revision":"old"}')
            token = requests.put(b.INPUT, "a" * 32, t.doc)
            before = b.UCI.read_bytes()
            with patch.object(b, "command") as cmd, self.assertRaises(e.RecoveryBlocked):
                b.import_settings(token)
            cmd.assert_not_called()
            self.assertEqual(b.UCI.read_bytes(), before)
            self.assertFalse(e.CONFIG.exists())

    def test_valid_interrupted_transaction_still_rolls_back(self):
        with case() as t:
            e.TRANSACTION.write_text(json.dumps(transaction(t)))
            e.APPLIED.write_text('{"revision":"one","state":"verifying"}')
            self.assertEqual(e.pause_reason(t.main), "")
            self.assertTrue(e.classify_recovery(t.main)["resume_existing"])
            self.assertTrue(e.Manager().tick(t.svc, t.main))
            self.assertFalse(e.TRANSACTION.exists())
            self.assertTrue(e.read(e.APPLIED)["rollback_confirmed"])

    def test_active_transaction_rejects_new_requests_without_terminal_pause(self):
        with case() as t:
            e.TRANSACTION.write_text(json.dumps(transaction(t)))
            token = requests.put(e.INPUT, "a" * 32, {"mode": "preserve"})
            with self.assertRaises(e.RecoveryBlocked):
                e.save(token)
            block = e.info()["recovery_block"]
            self.assertTrue(block["new_request_block"])
            self.assertFalse(block["terminal"])
            self.assertFalse(block["startup_pause"])
            self.assertFalse(e.RECOVERY.exists())

    def test_critical_marker_survives_deleted_and_corrupted_history_and_new_manager(self):
        with case() as t:
            e.APPLIED.write_text('{"revision":"old","state":"unknown"}')
            self.assertTrue(e.pause_reason(t.main))
            before = e.RECOVERY.read_bytes()
            for change in ("bad", "{}", '{"state":"applied"}'):
                e.APPLIED.write_text(change)
                self.assertTrue(e.pause_reason(t.main))
                self.assertTrue(e.classify_recovery()["startup_pause"])
                self.assertEqual(e.RECOVERY.read_bytes(), before)
                with self.assertRaises(e.RecoveryBlocked):
                    e.Manager().tick(t.svc, t.main)
            e.APPLIED.unlink()
            self.assertTrue(e.pause_reason(t.main))

    def test_unobserved_ordinary_history_corruption_does_not_stop_healthy_line(self):
        for value in ("bad", "[]", '{"state":[]}', '{"revision":"old"}'):
            with self.subTest(value=value), case() as t:
                e.APPLIED.write_text(value)
                self.assertFalse(e.classify_recovery()["startup_pause"])
                self.assertTrue(e.classify_recovery()["new_request_block"])
                self.assertFalse(e.Manager().tick(t.svc, t.main))
                t.svc.suspend_capture.assert_not_called()
                self.assertFalse(e.RECOVERY.exists())
                token = requests.put(e.INPUT, "a" * 32, {"mode": "preserve"})
                with self.assertRaises(e.RecoveryBlocked):
                    e.save(token)
                self.assertEqual(e.APPLIED.read_text(), value)

    def test_corrupt_latch_is_fail_closed_and_not_replaced(self):
        with case() as t:
            e.RECOVERY.write_text("bad")
            self.assertTrue(e.pause_reason(t.main))
            self.assertEqual(e.RECOVERY.read_text(), "bad")
            self.assertEqual(e.info()["recovery_block"]["code"], "RECOVERY_RECORD_INVALID")

    def test_mismatched_vnet_persists_block_even_after_selecting_original_vnet(self):
        with case() as t:
            r = transaction(t)
            r["vnet"] = "00000000-0000-0000-0000-000000000002"
            e.TRANSACTION.write_text(json.dumps(r))
            self.assertTrue(e.pause_reason(t.main))
            r["vnet"] = t.main["vnet_usa"]
            e.TRANSACTION.write_text(json.dumps(r))
            self.assertTrue(e.pause_reason(t.main))

    def test_verified_rollback_only_clears_matching_transaction_block(self):
        with case() as t:
            r = transaction(t)
            e.TRANSACTION.write_text(json.dumps(r))
            e.durable(
                e.RECOVERY,
                dict(version=1, code="TRANSACTION_UNKNOWN", source="transaction", revision="one"),
            )
            e.Manager().rollback(t.svc, r)
            self.assertFalse(e.RECOVERY.exists())
            self.assertFalse(e.TRANSACTION.exists())
            self.assertTrue(e.read(e.APPLIED)["exit_verified"])

    def test_other_case_block_cannot_be_cleared_by_rollback(self):
        with case() as t:
            r = transaction(t)
            e.TRANSACTION.write_text(json.dumps(r))
            e.durable(
                e.RECOVERY,
                dict(version=1, code="LEGACY_RECOVERY_UNKNOWN", source="history", revision="old"),
            )
            before = e.RECOVERY.read_bytes()
            with self.assertRaises(ValueError):
                e.Manager().rollback(t.svc, r)
            self.assertEqual(e.RECOVERY.read_bytes(), before)

    def test_conflicting_unknown_history_cannot_hide_behind_recoverable_transaction(self):
        with case() as t:
            e.TRANSACTION.write_text(json.dumps(transaction(t)))
            e.APPLIED.write_text('{"revision":"one","state":"unknown"}')
            self.assertTrue(e.pause_reason(t.main))
            self.assertEqual(t.calls, [])

    def test_readonly_info_does_not_create_persistent_block(self):
        with case() as _t:
            e.APPLIED.write_text('{"state":"unknown"}')
            self.assertTrue(e.info()["recovery_block"]["startup_pause"])
            self.assertFalse(e.RECOVERY.exists())

    def test_persistence_failure_never_allows_client_start(self):
        with case() as t:
            e.APPLIED.write_text('{"state":"unknown"}')
            with patch.object(e, "durable", side_effect=OSError("storage")):
                self.assertTrue(e.pause_reason(t.main))

    def test_restore_preferences_cannot_clear_persistent_block(self):
        with case() as t:
            t.config()
            e.APPLIED.write_text('{"state":"unknown"}')
            e.pause_reason(t.main)
            before = e.RECOVERY.read_bytes()
            e.APPLIED.unlink()
            e.stage_restored_preferences()
            self.assertEqual(e.RECOVERY.read_bytes(), before)
            self.assertTrue(e.classify_recovery()["startup_pause"])

    def test_lock_contention_has_no_config_history_or_latch_writes(self):
        with case() as _t:
            e.APPLIED.write_text('{"state":"unknown"}')
            before = e.APPLIED.read_bytes()
            token = requests.put(e.INPUT, "a" * 32, {"mode": "preserve"})
            with e.locked(), self.assertRaises(BlockingIOError):
                e.save(token)
            self.assertFalse(e.CONFIG.exists())
            self.assertFalse(e.RECOVERY.exists())
            self.assertEqual(e.APPLIED.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
