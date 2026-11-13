# Synthetic unit-test addresses; no live router, tenant or user configuration.
import contextlib
import json
import sys
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_endpoints as fixtures
import test_request_binding as requests
import endpoint_control as e
import supervisor as m


@contextlib.contextmanager
def case():
    t = fixtures.Endpoints()
    t.setUp()
    try:
        yield t
    finally:
        t.doCleanups()


def tx(t):
    return dict(
        revision="one",
        previous="162.159.197.2:8443",
        target="162.159.197.2:443",
        baseline="8.8.8.8",
        vnet=t.main["vnet_usa"],
        phase="apply",
    )


class Durability(unittest.TestCase):
    def test_both_intent_records_directory_synced_before_withdrawal_or_cli(self):
        with case() as t:
            t.config()
            events = []
            durable = e.durable
            sync = e.sync_directory
            cli = t.cli

            def save(path, value):
                events.append(("write", path.name, value.get("state")))
                return durable(path, value)

            def fsync(path):
                events.append(("dirsync", str(path)))
                return sync(path)

            def command(*a, **kw):
                events.append(("cli", a))
                return cli(*a, **kw)

            t.svc.suspend_capture.side_effect = lambda: events.append(("withdraw",)) or True
            with (
                patch.object(e, "durable", side_effect=save),
                patch.object(e, "sync_directory", side_effect=fsync),
                patch("mplane.cli", side_effect=command),
            ):
                self.assertTrue(e.Manager().tick(t.svc, t.main))
            first = next(i for i, event in enumerate(events) if event[0] in ("cli", "withdraw"))
            prefix = events[:first]
            self.assertIn(("write", e.TRANSACTION.name, "applying"), prefix)
            self.assertIn(("write", e.APPLIED.name, "applying"), prefix)
            self.assertGreaterEqual(sum(x[0] == "dirsync" for x in prefix), 2)

    def test_rollback_phase_is_durable_before_network_command(self):
        with case() as t:
            events = []
            durable = e.durable
            cli = t.cli

            def save(path, value):
                events.append(("durable", path.name, value.get("state")))
                return durable(path, value)

            def command(*a, **kw):
                events.append(("cli", a))
                return cli(*a, **kw)

            with (
                patch.object(e, "durable", side_effect=save),
                patch("mplane.cli", side_effect=command),
            ):
                e.Manager().rollback(t.svc, tx(t))
            first = next(i for i, x in enumerate(events) if x[0] == "cli")
            self.assertIn(("durable", e.TRANSACTION.name, "rolling_back"), events[:first])
            self.assertIn(("durable", e.APPLIED.name, "rolling_back"), events[:first])

    def test_success_proof_precedes_transaction_removal_and_directory_sync(self):
        with case() as t:
            r = tx(t)
            e.TRANSACTION.write_text(json.dumps(r))
            events = []
            durable = e.durable
            remove = e.remove_durable

            def save(path, value):
                events.append(("proof", path.name, value.get("state")))
                return durable(path, value)

            def delete(path):
                events.append(("remove", path.name))
                return remove(path)

            with (
                patch.object(e, "durable", side_effect=save),
                patch.object(e, "remove_durable", side_effect=delete),
            ):
                e.Manager().rollback(t.svc, r)
            proof = events.index(("proof", e.APPLIED.name, "failed"))
            removal = events.index(("remove", e.TRANSACTION.name))
            self.assertLess(proof, removal)
            self.assertFalse(e.TRANSACTION.exists())

    def test_failure_before_intent_sync_never_changes_endpoint_and_pauses_memory(self):
        with case() as t:
            t.config()
            manager = e.Manager()
            with (
                patch.object(e, "durable", side_effect=OSError("disk")),
                patch.object(e, "atomic", side_effect=OSError("ram")),
            ):
                with self.assertRaises(ValueError):
                    manager.tick(t.svc, t.main)
                for _ in range(5):
                    self.assertTrue(manager.pause_reason(t.main))
                    with self.assertRaises(ValueError):
                        manager.tick(t.svc, t.main)
            self.assertEqual(t.calls, [])
            self.assertIsNotNone(manager.process_block)

    def test_failed_recovery_and_failed_unknown_writes_stop_five_supervisor_cycles(self):
        with case() as t:
            e.TRANSACTION.write_text(json.dumps(tx(t)))
            svc = t.svc
            svc.stop = Mock(return_value={"direct": True, "legacy": ""})
            svc.plane.start = Mock()
            svc.plane.protocol_preference = "auto"
            svc.plane.probe.side_effect = RuntimeError("connection")
            durable = e.durable
            count = [0]

            def save(path, value):
                if path == e.RECOVERY or value.get("phase") == "unknown":
                    raise OSError("disk")
                return durable(path, value)

            def sleep(_):
                count[0] += 1
                if count[0] >= 5:
                    m.STOP = True

            cfg = {"main": dict(t.main, enabled="1", backend="masque"), "policies": []}
            svc.egress_guard = Mock(state="off", selectors=())
            svc.confirm_egress_ready = Mock()
            with (
                patch.object(e, "durable", side_effect=save),
                patch.object(m, "RUN", e.RUN),
                patch.object(m.sys, "argv", ["routepilot", "run"]),
                patch.object(m, "Service", return_value=svc),
                patch.object(m, "boot_config", return_value={"main": {}, "policies": []}),
                patch.object(m, "STOP", False),
                patch.object(m, "read_config", return_value=cfg),
                patch.object(m, "selected_lans", return_value=["br-lan"]),
                patch.object(
                    m,
                    "lan_interfaces",
                    return_value=[{"name": "br-lan", "up": True, "network": "10.77.50.0/24"}],
                ),
                patch.object(m.signal, "signal"),
                patch.object(m.time, "sleep", side_effect=sleep),
            ):
                m.run()
            self.assertEqual(count[0], 5)
            self.assertEqual(svc.plane.start.call_count, 1)
            self.assertEqual(t.calls.count(("disconnect",)), 1)
            self.assertTrue(e.PROCESS_BLOCK.exists())
            self.assertTrue(e.classify_recovery(t.main)["startup_pause"])

    def test_ram_evidence_blocks_a_new_controller_process(self):
        with case() as t:
            r = tx(t)
            e.TRANSACTION.write_text(json.dumps(r))
            manager = e.Manager()
            t.svc.plane.probe.side_effect = RuntimeError("connection")
            durable = e.durable

            def save(path, value):
                if path == e.RECOVERY or value.get("phase") == "unknown":
                    raise OSError("disk")
                return durable(path, value)

            with patch.object(e, "durable", side_effect=save), self.assertRaises(ValueError):
                manager.rollback(t.svc, r)
            self.assertIsNone(e.Manager().process_block)
            self.assertTrue(e.classify_recovery(t.main)["startup_pause"])

    def test_info_uses_current_exit_context_without_persisting_a_block(self):
        with case() as t:
            r = tx(t)
            r["vnet"] = "00000000-0000-0000-0000-000000000002"
            e.TRANSACTION.write_text(json.dumps(r))
            self.assertEqual(e.info()["recovery_block"]["code"], "TRANSACTION_VNET_MISMATCH")
            self.assertFalse(e.RECOVERY.exists())

    def test_missing_info_context_is_unknown_not_active_or_invalid_transaction(self):
        with case() as t:
            e.TRANSACTION.write_text(json.dumps(tx(t)))
            with patch.object(e, "read_config", side_effect=RuntimeError("uci")):
                self.assertEqual(e.info()["recovery_block"]["code"], "CONTEXT_UNAVAILABLE")
            self.assertFalse(e.RECOVERY.exists())

    def test_startup_migration_lock_contention_leaves_new_input_and_config_untouched(self):
        with case() as t:
            t.config(rev="old")
            e.APPLIED.unlink()
            before = e.CONFIG.read_bytes()
            with e.locked():
                self.assertFalse(e.stage_restored_preferences())
            self.assertEqual(e.CONFIG.read_bytes(), before)
            self.assertTrue(e.stage_restored_preferences())
            self.assertTrue(e.settings()["staged"])

    def test_busy_startup_migration_is_retried_before_admitting_an_old_request(self):
        with case() as t:
            t.config(rev="old")
            e.APPLIED.unlink()
            with e.locked():
                self.assertFalse(e.stage_restored_preferences())
            self.assertFalse(e.Manager().tick(t.svc, t.main))
            self.assertTrue(e.settings()["staged"])
            self.assertEqual(t.calls, [])

    def test_save_cannot_interleave_inside_migration_and_retry_preserves_new_request(self):
        with case() as t:
            t.config(rev="old")
            e.APPLIED.unlink()
            token = requests.put(e.INPUT, "a" * 32, {"mode": "preserve", "hours": 6})
            atomic = e.atomic
            errors = []
            attempted = [False]

            def interleave(path, text, *args):
                if path == e.CONFIG and not attempted[0]:
                    attempted[0] = True
                    try:
                        e.save(token)
                    except Exception as error:
                        errors.append(type(error).__name__)
                return atomic(path, text, *args)

            with patch.object(e, "atomic", side_effect=interleave):
                self.assertTrue(e.stage_restored_preferences())
            self.assertEqual(errors, ["BlockingIOError"])
            self.assertTrue(e.save(token)["saved"])
            self.assertEqual(e.settings()["hours"], 6)
            self.assertEqual(e.settings()["revision"], "a" * 32)

    def test_keep_list_replay_retains_unlatched_history_and_active_transaction(self):
        keep = Path(__file__).resolve().parents[1] / "root/lib/upgrade/keep.d/routepilot"
        names = keep.read_text().splitlines()
        for kind in ("legacy", "transaction"):
            with self.subTest(kind=kind), case() as t:
                source = t.p / "firmware-source"
                dest = t.p / "firmware-restored"
                folder = "etc/routepilot/"

                def put(name, value):
                    path = source / (folder + name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(value))

                put(
                    "endpoint-settings.json",
                    {"mode": "manual", "address": "162.159.197.2:443", "revision": "one"},
                )
                if kind == "legacy":
                    put("endpoint-applied.json", {"state": "unknown", "revision": "old"})
                else:
                    put("endpoint-transaction.json", tx(t))
                for name in names:
                    src = source / name.lstrip("/")
                    target = dest / name.lstrip("/")
                    if src.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, target)
                with (
                    patch.object(e, "CONFIG", dest / (folder + "endpoint-settings.json")),
                    patch.object(e, "APPLIED", dest / (folder + "endpoint-applied.json")),
                    patch.object(e, "TRANSACTION", dest / (folder + "endpoint-transaction.json")),
                    patch.object(e, "RECOVERY", dest / (folder + "endpoint-recovery-block.json")),
                    patch.object(e, "PROCESS_BLOCK", dest / "var/run/process-block.json"),
                ):
                    block = e.classify_recovery(t.main)
                    self.assertTrue(block["new_request_block"])
                    self.assertEqual(block["startup_pause"], kind == "legacy")
                    self.assertEqual(block["resume_existing"], kind == "transaction")

    def test_auxiliary_warning_cleanup_failure_does_not_rollback_verified_endpoint(self):
        with case() as t:
            t.config()
            unlink = Path.unlink

            def fail_warning(path, *args, **kwargs):
                if path == e.WARNING:
                    raise OSError("warning cleanup")
                return unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True, side_effect=fail_warning):
                self.assertTrue(e.Manager().tick(t.svc, t.main))
            self.assertNotIn(("tunnel", "endpoint", "set", "162.159.197.2:8443"), t.calls)
            self.assertTrue(e.read(e.APPLIED)["exit_verified"])
            self.assertFalse(e.TRANSACTION.exists())


if __name__ == "__main__":
    unittest.main()
