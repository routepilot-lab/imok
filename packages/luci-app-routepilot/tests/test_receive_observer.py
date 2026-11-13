from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import receive_observer as obs

HEADER = "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode ref pointer drops\n"


def line(inode=77, remote="02C59FA2:1F9F", queue="00001000", drops=5):
    return f"0: 00000000:0000 {remote} 01 00000000:{queue} 00:00000000 00000000 0 0 {inode} 2 00000000 {drops}\n"


def row(drops, queue=100):
    return {"drops": drops, "queue": queue}


OWNED = {"77": {"fd": 10, "category": "known_ingress"}}


class ReceiverTests(unittest.TestCase):
    def test_ipv4_ipv6_mapped_udp_and_filter(self):
        import ipaddress

        self.assertEqual(str(obs.udp_address("02C59FA2:1F9F")[0]), "162.159.197.2")
        v6 = "2001:4860:4860::8888"
        packed = ipaddress.ip_address(v6).packed
        raw = "".join(packed[i : i + 4][::-1].hex() for i in range(0, 16, 4))
        self.assertEqual(str(obs.udp_address(raw + ":0035")[0]), v6)
        mapped = ipaddress.ip_address("::ffff:162.159.197.2").packed
        raw = "".join(mapped[i : i + 4][::-1].hex() for i in range(0, 16, 4))
        self.assertEqual(str(obs.udp_address(raw + ":1F9F")[0]), "162.159.197.2")
        data, ok = obs.parse_udp([HEADER, line(), line(88)], {"77"})
        self.assertTrue(ok)
        self.assertEqual(set(data), {"77"})
        self.assertEqual(data["77"]["queue"], 4096)

    def test_malformed_or_capped_tables_mark_partial_not_silent_zero(self):
        data, ok = obs.parse_udp([HEADER, line(), "bad\n"])
        self.assertFalse(ok)
        self.assertEqual(data["77"]["drops"], 5)
        with patch.object(obs, "MAX_ROWS", 1):
            data, ok = obs.parse_udp([HEADER, line(), line(88)])
        self.assertFalse(ok)
        self.assertNotIn("88", data)
        with self.assertRaises(ValueError):
            obs.parse_udp(["changed header"])

    def test_exact_process_namespace_and_deleted_binary_rejected(self):
        fields = ["S"] + ["0"] * 18 + ["77"]
        with (
            patch.object(obs.os, "readlink", return_value=str(obs.ROOT / "usr/bin/warp-svc")),
            patch.object(obs.Path, "stat", return_value=SimpleNamespace(st_dev=1, st_ino=2)),
            patch.object(
                obs.Path, "read_text", return_value="42 (odd ) process) " + " ".join(fields)
            ),
        ):
            self.assertEqual(obs.process_key(42), (42, 77, 1, 2))
        with (
            patch.object(
                obs.os, "readlink", return_value=str(obs.ROOT / "usr/bin/warp-svc") + " (deleted)"
            ),
            self.assertRaises(ValueError),
        ):
            obs.process_key(42)
        with (
            patch.object(obs.os, "readlink", return_value=str(obs.ROOT / "usr/bin/warp-svc")),
            patch.object(
                obs.Path,
                "stat",
                side_effect=[
                    SimpleNamespace(st_dev=1, st_ino=2),
                    SimpleNamespace(st_dev=1, st_ino=3),
                ],
            ),
            self.assertRaises(ValueError),
        ):
            obs.process_key(42)

    def test_first_sample_is_baseline_counter_resets_and_new_runtime_start_over(self):
        t = obs.Tracker((1, 2, "run", "generation"), 100)
        t.sample({"77": row(500)}, OWNED, True, 100, 1)
        self.assertEqual(t.publish(100, "observing", True)["observed_drops"], 0)
        t.sample({"77": row(509)}, OWNED, True, 100.1, 1.1)
        self.assertEqual(t.publish(100.1, "observing", True)["observed_drops"], 9)
        t.sample({"77": row(2)}, OWNED, True, 100.2, 1.2)
        out = t.publish(100.2, "observing", True)
        self.assertEqual(out["observed_drops"], 9)
        self.assertEqual(out["counter_resets"], 1)
        self.assertEqual(out["coverage"], "partial")
        new = obs.Tracker((1, 2, "newrun", "generation"), 101)
        new.sample({"77": row(9000)}, OWNED, True, 101, 2)
        self.assertEqual(new.publish(101, "observing", True)["observed_drops"], 0)
        self.assertNotEqual(new.session_id, t.session_id)

    def test_missing_samples_and_gaps_rebaseline_without_inventing_zero_loss(self):
        t = obs.Tracker((1, 2, "run", "gen"), 100)
        t.sample({"77": row(5)}, OWNED, True, 100, 1)
        t.sample({}, OWNED, False, 100.1, 1.1)
        t.sample({"77": row(20)}, OWNED, True, 100.2, 1.2)
        t.sample({"77": row(40)}, OWNED, True, 101, 2)
        o = t.publish(101, "observing", True)
        self.assertEqual(o["observed_drops"], 0)
        self.assertEqual(o["coverage"], "partial")
        self.assertEqual(o["gaps"], 1)
        self.assertEqual(o["missing_samples"], 1)

    def test_closed_and_new_sockets_do_not_splice_counters_or_grow_history(self):
        t = obs.Tracker((1, 2, "run", "gen"), 0)
        for n in range(100):
            key = str(n)
            t.sample(
                {key: row(1000 + n)},
                {key: {"fd": 1, "category": "other_public_udp"}},
                True,
                n,
                n * 0.1,
            )
            t.publish(n, "observing", True)
        self.assertEqual(len(t.sockets), 1)
        self.assertEqual(len(t.windows), 60)
        self.assertEqual(t.total_drops, 0)

    def test_only_owned_public_connected_udp_selected_and_limits_reported(self):
        rows, ok = obs.parse_udp([HEADER, line(77), line(88, remote="0100007F:0035")])
        with (
            patch.object(obs.Path, "iterdir", return_value=[Path("/fd/1"), Path("/fd/2")]),
            patch.object(obs.os, "readlink", side_effect=["socket:[77]", "socket:[88]"]),
        ):
            owned, ok = obs.owned_sockets(42, rows)
        self.assertEqual(set(owned), {"77"})
        self.assertTrue(ok)
        with (
            patch.object(obs, "MAX_FDS", 1),
            patch.object(obs.Path, "iterdir", return_value=[Path("/fd/1"), Path("/fd/2")]),
            patch.object(obs.os, "readlink", return_value="socket:[77]"),
        ):
            owned, ok = obs.owned_sockets(42, rows)
        self.assertFalse(ok)

    def test_report_omits_addresses_and_unavailable_is_not_zero(self):
        t = obs.Tracker((1, 2, "run", "gen"), 0)
        t.sample({"77": row(5)}, OWNED, True, 1, 1)
        o = t.publish(1, "observing", True)
        self.assertNotIn("ip", str(o))
        self.assertNotIn("port", str(o))
        self.assertIsNone(obs.unavailable("unavailable", 1)["observed_drops"])

    def test_observer_never_opens_or_changes_sockets_or_invokes_commands(self):
        import ast

        tree = ast.parse(Path(obs.__file__).read_text())
        calls = {ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertFalse(
            calls & {"socket.socket", "tune", "command", "subprocess.run", "os.system", "os.kill"}
        )
        self.assertFalse(any("setsockopt" in n for n in calls))

    def test_observer_failure_only_publishes_unknown_does_not_modify_status(self):
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            status = folder / "status.json"
            status.write_text(
                json.dumps({"state": "running", "time": 100, "runtime_id": "r", "generation": "g"})
            )
            old = status.read_bytes()

            def stop(_):
                obs.STOP = True

            obs.STOP = False
            with (
                patch.object(obs, "RUN", folder),
                patch.object(obs.signal, "signal"),
                patch.object(obs.os, "nice"),
                patch.object(obs.time, "time", return_value=100),
                patch.object(obs.time, "monotonic", return_value=1),
                patch.object(obs.time, "sleep", side_effect=stop),
                patch.object(obs, "discover", return_value=(42, 77, 1, 2)),
                patch.object(obs, "read_udp", side_effect=OSError("read failed")),
                patch.object(obs, "atomic") as atomic,
            ):
                obs.run()
                first = json.loads(atomic.call_args_list[0].args[1])
                self.assertEqual(first["state"], "unavailable")
                self.assertIsNone(first["observed_drops"])
                self.assertEqual(status.read_bytes(), old)
                self.assertTrue(
                    all(call.args[0] == folder / "receive.json" for call in atomic.call_args_list)
                )
            obs.STOP = False

    def test_cached_identity_avoids_process_scan_and_revalidates_reuse(self):
        key = (42, 77, 1, 2)
        with (
            patch.object(obs, "process_key", return_value=key),
            patch.object(obs, "discover") as discover,
        ):
            self.assertEqual(obs.find_client(key), key)
            discover.assert_not_called()
        with (
            patch.object(obs, "process_key", return_value=(42, 78, 1, 2)),
            patch.object(obs, "discover", return_value=None) as discover,
        ):
            self.assertIsNone(obs.find_client(key))
            discover.assert_called_once()

    def test_empty_observed_set_with_missing_rows_remains_unknown(self):
        t = obs.Tracker((1, 2, "run", "gen"), 0)
        t.sample({"77": row(5)}, OWNED, True, 1, 1)
        t.sample({}, OWNED, False, 1.1, 1.1)
        o = t.publish(1.1, "observing", False)
        self.assertEqual(o["state"], "unavailable")
        self.assertIsNone(o["queue_now"])

    def test_non_object_status_is_unavailable_and_does_not_crash_worker(self):
        import tempfile
        import json

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "status.json").write_text("[]")

            def stop(_):
                obs.STOP = True

            obs.STOP = False
            with (
                patch.object(obs, "RUN", folder),
                patch.object(obs.signal, "signal"),
                patch.object(obs.os, "nice"),
                patch.object(obs.time, "time", return_value=100),
                patch.object(obs.time, "monotonic", return_value=1),
                patch.object(obs.time, "sleep", side_effect=stop),
                patch.object(obs, "atomic") as atomic,
            ):
                obs.run()
                self.assertEqual(
                    json.loads(atomic.call_args_list[0].args[1])["state"], "unavailable"
                )
            obs.STOP = False
