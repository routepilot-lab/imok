import copy
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import traffic_observer as t

WALL = 1793401200


class LedgerTest(unittest.TestCase):
    def sample(self, obj, n, wall=WALL, mono=100, key="boot:epoch", **kw):
        return obj.sample(key, {k: n for k in t.KEYS}, wall, mono, **kw)

    def test_restart_does_not_double_count_persisted_baseline(self):
        a = t.Ledger(wall=WALL)
        self.sample(a, 0)
        self.sample(a, 100, WALL + 5, 105)
        b = t.Ledger(json.loads(json.dumps(a.data)))
        self.sample(b, 130, WALL + 10, 110)
        self.assertEqual(b.data["total"]["proxy_down"], 130)
        self.assertEqual(sum(x["bytes"]["proxy_down"] for x in b.data["days"].values()), 130)

    def test_epoch_or_counter_reset_never_invents_delta(self):
        a = t.Ledger(wall=WALL)
        self.sample(a, 500)
        self.sample(a, 550, WALL + 5, 105)
        self.sample(a, 4, WALL + 10, 110)
        self.sample(a, 900, WALL + 15, 115, key="newboot:newtable")
        self.sample(a, 910, WALL + 20, 120, key="newboot:newtable")
        self.assertEqual(a.data["total"]["direct_up"], 60)
        self.assertEqual(a.data["gaps"], 2)

    def test_midnight_recovered_interval_is_not_assigned_to_wrong_day(self):
        w = __import__("datetime").datetime(2026, 10, 30, 23, 59, 58).timestamp()
        a = t.Ledger(wall=w)
        self.sample(a, 0, w, 100)
        self.sample(a, 20, w + 5, 105)
        self.assertEqual(a.data["total"]["proxy_up"], 20)
        self.assertEqual(a.data["unallocated"]["proxy_up"], 20)
        self.assertEqual(sum(x["bytes"]["proxy_up"] for x in a.data["days"].values()), 0)

    def test_unsynced_clock_and_clock_jump_only_update_lifetime(self):
        a = t.Ledger(wall=WALL)
        self.sample(a, 0, synced=False)
        self.sample(a, 20, WALL + 5, 105, synced=False)
        self.assertEqual(a.data["days"], {})
        self.sample(a, 40, WALL + 10, 110, synced=True)
        self.sample(a, 60, WALL + 3600, 115, synced=True)
        self.assertEqual(a.data["total"]["direct_down"], 60)
        self.assertEqual(a.data["unallocated"]["direct_down"], 60)

    def test_unknown_history_is_absent_not_zero_and_days_are_bounded(self):
        a = t.Ledger(wall=WALL)
        self.sample(a, 0)
        self.sample(a, 20, WALL + 5, 105)
        self.assertEqual(len(a.data["days"]), 1)
        for i in range(1, 120):
            self.sample(a, 20 + i, WALL + i * 86400, 105 + i * 86400)
        self.assertEqual(len(a.data["days"]), 90)
        self.assertGreater(a.data["total"]["proxy_up"], 100)

    def test_corrupt_history_rejected_not_reset(self):
        d = t.Ledger(wall=WALL).data
        for change in [
            {"total": {"proxy_up": 3}},
            {"total": dict.fromkeys(t.KEYS, -1)},
            {"schema": 99},
            {"days": {"2026-10-30": {"bytes": t.zero(), "partial": "false"}}},
        ]:
            with self.assertRaises(ValueError):
                t.Ledger({**copy.deepcopy(d), **change})


class ScopeTest(unittest.TestCase):
    def row(self, **kw):
        return dict(
            ip="192.168.22.0/24", lan_interface="br-test", scope="lan", mode="bypass_cn", **kw
        )

    def test_counter_only_rules_exclude_tunnel_control_and_private(self):
        rows = t.scope({"devices": [self.row()]})
        text = t.rules(rows, "wan0", "a" * 32)
        self.assertNotIn("mark set", text)
        self.assertNotIn(" dnat ", text)
        self.assertNotIn(" drop", text)
        self.assertEqual(text.count("counter name "), 4)
        self.assertIn('iifname "br-test" ip saddr 192.168.22.0/24', text)
        self.assertNotIn('iifname "rp-host" ip saddr 192.168.22.0/24', text)
        self.assertNotIn("ether daddr", text)

    def test_overlap_bad_interface_and_unmanaged_policy_rejected(self):
        for rows in [
            [self.row(), self.row()],
            [dict(self.row(), lan_interface='wan"; drop')],
            [dict(self.row(), ip="8.8.8.0/24")],
            [dict(self.row(), mode="direct")],
        ]:
            with self.assertRaises(ValueError):
                t.scope({"devices": rows})

    def test_device_download_uses_compiled_ip_not_ether_destination(self):
        rows = t.scope(
            {
                "devices": [
                    dict(self.row(), ip="192.168.22.4", scope="device", mac="02:11:22:33:44:55")
                ]
            }
        )
        text = t.rules(rows, "wan0", "b" * 32)
        self.assertEqual(text.count("ether saddr"), 2)
        self.assertNotIn("ether daddr", text)
        self.assertIn("ip daddr 192.168.22.4/32", text)

    def test_unknown_table_owner_is_never_deleted(self):
        fake = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": json.dumps(
                    {"nftables": [{"table": {"name": t.TABLE, "comment": "someone else"}}]}
                ),
            },
        )()
        with patch.object(t, "call", return_value=fake) as call:
            with self.assertRaises(ValueError):
                t.remove_owned()
            self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class IntegrationBoundaries(unittest.TestCase):
    def test_failed_table_installs_reuse_checkpoint_until_accounting_resumes(self):
        def counts(n):
            return dict.fromkeys(t.KEYS, n)

        ledger = t.Ledger(wall=WALL - 5)
        ledger.sample("boot:old", counts(20), WALL - 5, 95)
        times = [100, 105, 110, 409, 410]
        tick, sleeps, current = 0, 0, "old"
        actions = []

        def sleep(_):
            nonlocal tick, sleeps
            sleeps += 1
            if sleeps % 10 == 0:
                if tick == len(times) - 1:
                    t.request_stop()
                else:
                    tick += 1

        def table():
            if current == "old":
                return current, counts(30)
            if current == "new":
                return current, counts({2: 0, 3: 20, 4: 30}[tick])
            return None

        def install(_args, _text):
            nonlocal current
            actions.append(("install", tick))
            if tick < 2:
                return SimpleNamespace(returncode=1)
            current = "new"
            return SimpleNamespace(returncode=0)

        def remove():
            nonlocal current
            current = None

        clock = SimpleNamespace(
            time=lambda: WALL + times[tick] - 100, monotonic=lambda: times[tick], sleep=sleep
        )
        with tempfile.TemporaryDirectory() as temp, ExitStack() as stack:
            root = Path(temp)
            (root / "status.json").write_text("{}")
            boot_id = root / "boot-id"
            boot_id.write_text("boot")
            store = root / "usage.json"
            store.write_text(json.dumps(ledger.data))
            stack.enter_context(patch.object(t, "STOP", False))
            stack.enter_context(patch.object(t.signal, "signal"))
            stack.enter_context(patch.object(t, "Path", return_value=boot_id))
            stack.enter_context(patch.multiple(t, RUN=root, STORE=store, time=clock))
            for name, value in [
                ("refresh_timezone", True),
                ("clock_synced", True),
                ("environment", ([], "wan0", "signature")),
                ("rules", ""),
                ("atomic", None),
                ("offload", "unknown"),
                ("client_version", None),
            ]:
                stack.enter_context(patch.object(t, name, return_value=value))
            for name, callback in [
                ("table", table),
                ("call", install),
                ("remove_owned", remove),
                ("save", lambda data: actions.append(("save", tick, data["total"]["proxy_up"]))),
            ]:
                stack.enter_context(patch.object(t, name, side_effect=callback))
            t.run()

        self.assertEqual(
            actions,
            [
                ("save", 0, 10),
                ("install", 0),
                ("install", 1),
                ("install", 2),
                ("save", 2, 10),
                ("save", 4, 40),
                ("save", 4, 40),
            ],
        )

    def test_first_save_creates_directory_and_keeps_private_mode(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "absent" / "usage" / "state.json"
            with patch.object(t, "STORE", p):
                t.save(t.Ledger(wall=WALL).data)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(p.read_text())["schema"], 1)

    def test_auxiliary_timeout_and_invalid_json_do_not_escape(self):
        import subprocess

        for error in [subprocess.TimeoutExpired("warp-cli", 3), ValueError("bad json")]:
            with patch.object(t, "call", side_effect=error) as command:
                self.assertEqual(t.offload(), "unknown")
                self.assertIsNone(t.client_version())
                self.assertTrue(all("delete" not in args[0] for args, kw in command.call_args_list))

    def test_timezone_change_marks_interval_unallocated(self):
        a = t.Ledger(wall=WALL)
        with patch.object(t, "zone", return_value="A +0800"):
            a.sample("key", t.zero(), WALL, 100)
        with patch.object(t, "zone", return_value="B +0900"):
            a.sample("key", dict.fromkeys(t.KEYS, 20), WALL + 5, 105)
        self.assertEqual(a.data["unallocated"]["proxy_down"], 20)

    def test_shutdown_persists_known_totals_even_if_counter_read_fails(self):
        a = t.Ledger(wall=WALL)
        a.sample("boot:epoch", t.zero(), WALL, 100)
        a.sample("boot:epoch", dict.fromkeys(t.KEYS, 64), WALL + 5, 105)
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "usage.json"
            with (
                patch.object(t, "STORE", target),
                patch.object(t, "table", side_effect=ValueError("nft unavailable")),
            ):
                t.flush_on_stop(a, "boot", "epoch", True)
            b = t.Ledger(json.loads(target.read_text()))
            self.assertEqual(b.data["total"]["direct_up"], 64)
            self.assertIsNone(b.data["anchor"])
            self.assertEqual(b.data["gaps"], 1)

    def test_openwrt_without_python_tzset_uses_libc(self):
        fake = type("Libc", (), {})()
        from unittest.mock import Mock

        fake.tzset = Mock()
        with (
            patch.object(t.Path, "read_text", return_value="CST-8\n"),
            patch.object(t.time, "tzset", None),
            patch.object(t.ctypes, "CDLL", return_value=fake),
            patch.dict(t.os.environ),
        ):
            self.assertTrue(t.refresh_timezone())
            fake.tzset.assert_called_once_with()
