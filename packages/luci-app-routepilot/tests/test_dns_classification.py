"""Exercise DNS table replacement and classification without touching host nftables."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
from dns_classification import ClassificationBusy, ClassificationWriter
from domain_sets import classification_fingerprints, migration_commands
from service import Service

ITEM = ("rpd_lan", "192.0.2.1")


class ClassificationTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.write = Mock()
        self.writer = ClassificationWriter(
            "old", lambda groups, *, timeout: self.write(groups), clock=lambda: self.now
        )

    def test_first_reply_is_synchronous_and_nearby_replies_skip_writes(self):
        def send():
            self.write.assert_called_once_with({"rpd_lan": ["192.0.2.1"]})

        self.writer.deliver([ITEM], send)
        self.writer.deliver([ITEM], send)

    def test_refreshes_batch_and_an_old_queue_is_invalidated(self):
        other = ("rpp_lan", "192.0.2.2")
        self.writer.deliver([ITEM, other], Mock())
        self.now += 1900
        self.writer.deliver([ITEM], Mock())
        self.writer.deliver([other], Mock())
        self.writer.flush()
        self.assertEqual(self.write.call_count, 2)
        self.write.assert_called_with({"rpd_lan": ["192.0.2.1"], "rpp_lan": ["192.0.2.2"]})
        self.now += 1900
        self.writer.deliver([ITEM], Mock())
        self.writer.pause("old")
        self.writer.resume("old", "new")
        self.writer.flush()
        self.assertEqual(self.write.call_count, 2)
        self.writer.deliver([ITEM], Mock())
        self.assertEqual(self.write.call_count, 3)

    def test_query_waiting_for_rebuild_is_classified_before_its_reply(self):
        ready = threading.Event()
        sent = threading.Event()
        self.writer.pause("old")

        def query():
            ready.set()
            self.writer.deliver([ITEM], sent.set)

        worker = threading.Thread(target=query)
        worker.start()
        self.assertTrue(ready.wait(1))
        self.assertFalse(sent.is_set())
        self.write.assert_not_called()
        self.writer.resume("old", "new")
        worker.join(1)
        self.assertTrue(sent.is_set())
        self.write.assert_called_once()

    def test_pause_timeout_or_write_failure_never_sends_unclassified_reply(self):
        send = Mock()
        self.writer.pause("old")
        with patch("dns_classification.REPLY_WAIT", 0), self.assertRaises(TimeoutError):
            self.writer.deliver([ITEM], send)
        self.writer.resume("old", "new")
        self.write.side_effect = RuntimeError("nft failed")
        with self.assertRaises(RuntimeError):
            self.writer.deliver([ITEM], send)
        send.assert_not_called()
        self.write.side_effect = None
        self.writer.deliver([ITEM], send)
        send.assert_called_once()

    def test_burst_batches_new_addresses_without_delaying_unmatched_replies(self):
        started = threading.Event()
        release = threading.Event()
        matched_count = 50
        unmatched_count = 40
        barrier = threading.Barrier(matched_count)

        def write(_):
            started.set()
            time.sleep(0.04)
            self.assertTrue(release.wait(1))

        def matched(index):
            barrier.wait(timeout=1)
            return self.writer.deliver([("rpd_lan", f"192.0.2.{index + 1}")], lambda: "ok")

        self.write.side_effect = write
        with ThreadPoolExecutor(max_workers=matched_count + unmatched_count) as pool:
            matching = [pool.submit(matched, index) for index in range(matched_count)]
            try:
                self.assertTrue(started.wait(1))
                bypass = [
                    pool.submit(self.writer.deliver, [], lambda: "ok")
                    for _ in range(unmatched_count)
                ]
                self.assertEqual(
                    [job.result(timeout=0.2) for job in bypass], ["ok"] * unmatched_count
                )
                self.assertFalse(release.is_set())
            finally:
                release.set()
            self.assertEqual([job.result(timeout=1) for job in matching], ["ok"] * matched_count)
        self.assertLess(self.write.call_count, 6)

    def test_synchronous_writer_uses_remaining_budget_and_reports_timeout(self):
        def write(groups, *, timeout):
            self.assertGreater(timeout, 0)
            self.assertLess(timeout, 1)
            raise subprocess.TimeoutExpired("nft", timeout)

        send = Mock()
        writer = ClassificationWriter("old", write)
        with self.assertRaises(ClassificationBusy):
            writer.deliver([ITEM], send)
        send.assert_not_called()

    def test_live_install_requires_both_control_acknowledgements(self):
        service = Service()
        service.table_epoch = "old"
        service.runtime_id = "unchanged"
        service.egress_guard = Mock()
        compiled = {"nft": "new table", "dns": {}}
        events = []
        with (
            patch("service.classification_control", side_effect=lambda *a, **kw: events.append(kw)),
            patch("service.command", side_effect=lambda *a, **kw: events.append(kw)),
        ):
            service.install_capture_table(compiled, exists=False, live_dns=True)
        self.assertEqual(events, [{}, {"data": "new table"}, {"resume": service.table_epoch}])
        self.assertEqual(compiled["dns"]["table_epoch"], service.table_epoch)
        with (
            patch("service.classification_control", side_effect=ValueError("unavailable")),
            patch("service.command") as command,
            self.assertRaises(ValueError),
        ):
            service.install_capture_table(compiled, exists=False, live_dns=True)
        command.assert_not_called()


class MigrationTests(unittest.TestCase):
    def fingerprints(
        self, *, ip="10.0.0.2", mode="gfw", direct=("a.example",), proxy=(), rules=("b.example",)
    ):
        return classification_fingerprints(
            dict(
                devices=[dict(id="lan", ip=ip, mac="", lan_interface="br-lan", dns_port=12100)],
                dns=dict(
                    policies=[
                        dict(
                            port=12100,
                            mode=mode,
                            direct_set="rpd_lan",
                            proxy_set="rpp_lan",
                            mode_set="rpg_lan",
                            mode_domains=rules,
                            custom_direct_domains=direct,
                            custom_proxy_domains=proxy,
                        )
                    ]
                ),
            )
        )

    def test_only_equivalent_sets_migrate_with_remaining_time(self):
        text = "\n".join(
            f"set {name} {{ type ipv4_addr; elements = {{ 192.0.2.1 expires 4m12s, 192.0.2.2 expires 5ms }} }}"
            for name in ("rpd_lan", "rpp_lan", "rpg_lan")
        )
        old = self.fingerprints()
        for new, names in (
            (old, ("rpd_lan", "rpp_lan", "rpg_lan")),
            (self.fingerprints(direct=(), proxy=("a.example",)), ("rpg_lan",)),
            (self.fingerprints(rules=("c.example",)), ("rpd_lan", "rpp_lan")),
            (self.fingerprints(mode="whitelist"), ("rpd_lan", "rpp_lan")),
            (self.fingerprints(ip="10.0.0.3"), ()),
        ):
            result = migration_commands(text, old, new, 1)
            expected = "".join(
                f"add element inet routepilot {name} {{ 192.0.2.1 timeout 251s }}\n"
                for name in names
            )
            self.assertEqual(result, expected)
        self.assertEqual(migration_commands(text, {}, old, 1), "")
