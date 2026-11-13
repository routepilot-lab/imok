from concurrent.futures import Future, ThreadPoolExecutor
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import native
import dns_runtime as dr
import dns_diagnostics as dd


def query(name="example.com"):
    return dd.make_query(name)


def answer(q, ttl=86400, flags=0x8180):
    return (
        q[:2]
        + struct.pack("!5H", flags, 1, 1, 0, 0)
        + q[12:]
        + b"\xc0\x0c"
        + struct.pack("!HHIH", 1, 1, ttl, 4)
        + b"\xcb\x00\x71\x07"
    )


def spec():
    return dict(
        runtime_id="runtime",
        generation="generation",
        dns_domestic="223.5.5.5",
        dns_foreign="1.1.1.1",
        transport="udp",
        cache=True,
        policies=[
            dict(
                port=12100,
                mark=0x5A000000,
                mode="gfw",
                mode_domains=["example.com"],
                mode_set="rpg_x",
                direct_set="rpd_x",
                proxy_set="rpp_x",
                china_domains=["baidu.com"],
            )
        ],
    )


class DNSBatchTests(unittest.TestCase):
    def test_binary_dns_labels_are_canonical_without_suffix_injection(self):
        def packet(labels):
            return (
                struct.pack("!6H", 7, 256, 1, 0, 0, 0)
                + b"".join(bytes([len(x)]) + x for x in labels)
                + b"\0"
                + struct.pack("!2H", 1, 1)
            )

        q = packet([b"Photo Studio", b"\xff", b"EXAMPLE", b"com"])
        ident, name, kind, finish = native.question(q)
        self.assertEqual((ident, kind, finish), (7, 1, len(q)))
        self.assertEqual(name, r"photo\032studio.\255.example.com")
        self.assertTrue(native.suffix_match({name}, {"example.com"}))
        disguised = packet([b"www.example.com"])
        escaped = native.question(disguised)[1]
        self.assertEqual(escaped, r"www\046example\046com")
        self.assertFalse(native.suffix_match({escaped}, {"example.com"}))
        backslash = packet([b"www\\046example", b"com"])
        self.assertIn(r"\092046", native.question(backslash)[1])

    def test_dns_name_limit_uses_wire_octets_not_escaped_display_length(self):
        def wire(last):
            return (
                b"".join(bytes([len(x)]) + x for x in [b"\xff" * 63] * 3 + [b"\xff" * last]) + b"\0"
            )

        name, end = native.read_name(wire(61), 0)
        self.assertEqual(end, 255)
        self.assertGreater(len(name), 253)
        with self.assertRaises(native.DNSFormatError):
            native.read_name(wire(62), 0)
        for invalid in [b"\x40" + b"x" * 64 + b"\0", b"\xc0\x00", b"\x03ab"]:
            with self.assertRaises(native.DNSFormatError):
                native.read_name(invalid, 0)

    def test_special_label_forward_cache_and_failure_preserve_original_question(self):
        raw = b"Photo Studio"
        q = (
            struct.pack("!6H", 27, 256, 1, 0, 0, 0)
            + bytes([len(raw)])
            + raw
            + b"\x07example\x03com\0"
            + struct.pack("!2H", 1, 1)
        )
        policy = dr.DNSRuntime(spec()).policies[0]
        with patch("native.socket.socket") as sock, patch("native.populate_sets") as classify:
            conn = sock.return_value.__enter__.return_value
            conn.recv.return_value = answer(q, 60)
            first = native.upstream(q, policy)
            second = native.upstream(q, policy)
            self.assertEqual(sock.call_count, 1)
            self.assertEqual(conn.send.call_args.args[0], q)
            self.assertEqual(first[12 : len(q)], q[12:])
            self.assertEqual(second[12 : len(q)], q[12:])
            self.assertIn(r"photo\032studio.example.com", classify.call_args.args[1])
        failed = native.failure(q)
        self.assertEqual(struct.unpack("!H", failed[2:4])[0] & 15, 2)
        self.assertEqual(failed[12:], q[12:])

    def test_ttl_rrset_opt_dnssec_and_negative_soa_are_preserved(self):
        q = query()
        _a = answer(q)

        # Two records from one RRset with unequal original TTLs.
        def rr(ttl):
            return b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, ttl, 4) + b"\xcb\x00\x71\x07"

        packet = q[:2] + struct.pack("!5H", 0x81A0, 1, 2, 0, 1) + q[12:] + rr(86400) + rr(60)
        opt = b"\0" + struct.pack("!HHIH", 41, 1232, 0x00008000, 0)
        limited = native.limit_response_ttl(packet + opt)
        self.assertEqual([ttl for _, ttl in native.ttl_fields(limited)], [60, 60])
        self.assertEqual(limited[-len(opt) :], opt)
        self.assertEqual(limited[2:4], packet[2:4], "AD/header flags unchanged")
        self.assertEqual(
            [ttl for _, ttl in native.ttl_fields(native.age_response(limited, 61))], [0, 0]
        )
        # SOA minimum and RRSIG original TTL are opaque RDATA and must not change.
        for kind, data in [
            (6, b"\0\0" + struct.pack("!5I", 1, 2, 3, 4, 86400)),
            (46, struct.pack("!HBBIIIH", 1, 8, 2, 86400, 99, 1, 7) + b"\0signature"),
        ]:
            packet = (
                q[:2]
                + struct.pack("!5H", 0x8183 if kind == 6 else 0x81A0, 1, 0, 1, 0)
                + q[12:]
                + b"\xc0\x0c"
                + struct.pack("!HHIH", kind, 1, 86400, len(data))
                + data
            )
            limited = native.limit_response_ttl(packet)
            self.assertEqual(native.response_ttl(limited), 300)
            self.assertTrue(limited.endswith(data))

    def test_long_ttl_first_response_and_hit_fit_dynamic_rule_lifetime(self):
        runtime = dr.DNSRuntime(spec())
        p = runtime.policies[0]
        q = query()
        with patch("native.socket.socket") as sock, patch("native.subprocess.run") as nft:
            sock.return_value.__enter__.return_value.recv.return_value = answer(q)
            nft.return_value.returncode = 0
            first = native.upstream(q, p)
            self.assertLessEqual(native.response_ttl(first), 300)
            self.assertGreater(native.response_ttl(first), 0)
            cache_entry = next(iter(p["_cache"].values()))
            self.assertLessEqual(cache_entry[0] - cache_entry[2], 300)
            second = native.upstream(q, p)
            self.assertLessEqual(native.response_ttl(second), native.response_ttl(first))
            self.assertEqual(sock.call_count, 1, "cache hit must not query upstream")
            for call in nft.call_args_list:
                script = call.kwargs["input"]
                self.assertLess(script.index("delete element"), script.rindex("add element"))
                self.assertIn("timeout 1h", script)

    def test_zero_ttl_not_cached_and_classification_failure_not_delivered(self):
        runtime = dr.DNSRuntime(spec())
        p = runtime.policies[0]
        q = query()
        with patch("native.socket.socket") as sock, patch("native.populate_sets"):
            sock.return_value.__enter__.return_value.recv.return_value = answer(q, 0)
            self.assertEqual(native.response_ttl(native.upstream(q, p)), 0)
            self.assertEqual(p["_cache"], {})
        with (
            patch("native.socket.socket") as sock,
            patch("native.populate_sets", side_effect=RuntimeError("nft failed")),
        ):
            sock.return_value.__enter__.return_value.recv.return_value = answer(q)
            with self.assertRaises(RuntimeError):
                native.upstream(q, p)
            self.assertEqual(p["_cache"], {})

    def test_no_ttl_renewal_when_classification_outlasts_original_ttl(self):
        runtime = dr.DNSRuntime(spec())
        policy = runtime.policies[0]
        q = query()
        clock = [100.0]

        def classify(*args):
            clock[0] += 5

        with (
            patch("native.time.monotonic", side_effect=lambda: clock[0]),
            patch("native.populate_sets", side_effect=classify),
            patch("native.socket.socket") as sock,
        ):
            sock.return_value.__enter__.return_value.recv.return_value = answer(q, 2)
            result = native.upstream(q, policy)
        self.assertEqual(native.response_ttl(result), 0)
        self.assertEqual(policy["_cache"], {})

    def test_normalized_keys_preserve_edns_options_and_do_bit(self):
        def edns(q, size, do=0, option=b""):
            return (
                q[:10]
                + b"\x00\x01"
                + q[12:]
                + b"\0"
                + struct.pack("!HHIH", 41, size, do, len(option))
                + option
            )

        first = edns(query("ExAmPlE.com"), 512)
        larger = edns(query("example.com"), 1232)
        self.assertEqual(native.cache_key(first), native.cache_key(larger))
        self.assertNotEqual(native.cache_key(first), native.cache_key(edns(query(), 512, 0x8000)))
        cookie = struct.pack("!HH", 10, 8) + b"testcook"
        ecs = struct.pack("!HHHBBB", 8, 5, 1, 8, 0, 192)
        self.assertNotEqual(
            native.cache_key(first), native.cache_key(edns(query(), 512, option=cookie))
        )
        self.assertNotEqual(
            native.cache_key(edns(query(), 512, option=ecs)),
            native.cache_key(edns(query(), 512, option=ecs[:-1] + b"\xc6")),
        )
        packet = answer(query(), 60)
        packet = packet[:6] + struct.pack("!H", 40) + packet[8:-16] + packet[-16:] * 40
        runtime = dr.DNSRuntime(spec())
        policy = runtime.policies[0]
        policy["_classification_at_send"] = True
        with patch.object(
            native, "fetch_response", return_value=(packet, time.monotonic(), (), ())
        ) as fetch:
            full = native.upstream(first, policy)
            cached = native.upstream(larger, policy)
        fetch.assert_called_once()
        self.assertGreater(len(full), 512)
        self.assertTrue(native.client_response(first, full)[2] & 2)
        result = native.client_response(larger, cached)
        self.assertFalse(result[2] & 2)
        self.assertEqual(result[:2], larger[:2])
        self.assertEqual(result[12 : len(larger) - 11], larger[12:-11])

    def test_expired_cache_falls_back_if_refresh_workers_are_busy(self):
        runtime = dr.DNSRuntime(dict(spec(), table_epoch="current"))
        policy = runtime.policies[0]
        q = query()
        packet = answer(q, 60, flags=0x81A0)
        now = time.monotonic()
        policy["_cache"][native.cache_key(q)] = (now - 1, packet, now - 61, False)
        with (
            ThreadPoolExecutor(1) as pool,
            patch.object(native, "fetch_response") as fetch,
        ):
            slots = threading.BoundedSemaphore(1)
            slots.acquire()
            policy.update(_refresh_pool=pool, _refresh_slots=slots, _refreshes={})
            reply = native.upstream(q, policy)
        fetch.assert_not_called()
        self.assertIsInstance(reply, native.CacheResponse)
        self.assertEqual(reply.cache_epoch, 0)
        self.assertEqual(native.response_ttl(reply), native.STALE_TTL)
        self.assertFalse(reply[3] & 0x20)
        self.assertEqual(policy["_stats"]["served_stale"], 1)

    def test_udp_response_respects_edns_payload_with_options(self):
        q = query("ExAmPlE.com")
        cookie = struct.pack("!HH", 10, 8) + b"testcook"
        q_with_opt = (
            q[:10]
            + b"\x00\x01"
            + q[12:]
            + b"\0"
            + struct.pack("!HHIH", 41, 512, 0x8000, len(cookie))
            + cookie
        )
        response = answer(q)
        response = response[:6] + struct.pack("!H", 40) + response[8:-16] + response[-16:] * 40
        self.assertEqual(native.cache_key(q_with_opt), q_with_opt[2:])
        self.assertEqual(native.client_response(q_with_opt, response, tcp=True), response)
        packet = native.client_response(q_with_opt, response)
        self.assertLessEqual(len(packet), 512)
        self.assertTrue(packet[2] & 2)
        self.assertEqual(packet[12 : len(q)], q[12:])
        self.assertEqual(struct.unpack_from("!H", packet, 10)[0], 1)
        self.assertEqual(packet[len(q) :][0], 0)
        self.assertEqual(struct.unpack_from("!H", packet, len(q) + 1)[0], 41)

    def test_expired_waiter_requeries_after_clear_during_refresh(self):
        runtime = dr.DNSRuntime(dict(spec(), table_epoch="current"))
        policy = runtime.policies[0]
        q = query()
        key = native.cache_key(q)
        now = time.monotonic()
        previous = answer(q, 60)
        replacement = previous[:-1] + b"\x08"
        policy["_cache"][key] = (now - 1, previous, now - 61, False)

        def completed_refresh(*args, **kwargs):
            runtime.clear()
            future = Future()
            future.set_result((previous, time.monotonic()))
            return future

        with (
            patch.object(runtime, "publish"),
            patch.object(native, "refresh_response", side_effect=completed_refresh),
            patch.object(
                native, "fetch_response", return_value=(replacement, time.monotonic(), (), ())
            ) as fetch,
        ):
            policy["_refresh_pool"] = object()
            reply = native.upstream(q, policy)
        fetch.assert_called_once_with(q, policy, False)
        self.assertEqual(reply[-1:], b"\x08")
        self.assertEqual(policy["_cache"][key][1], replacement)

    def test_nxdomain_refresh_replaces_a_stale_positive_answer(self):
        runtime = dr.DNSRuntime(dict(spec(), table_epoch="current"))
        policy = runtime.policies[0]
        q = query()
        key = native.cache_key(q)
        now = time.monotonic()
        policy["_cache"][key] = (now - 1, answer(q, 60), now - 61, False)
        nxdomain = q[:2] + struct.pack("!5H", 0x8183, 1, 0, 0, 0) + q[12:]
        with (
            ThreadPoolExecutor(1) as pool,
            patch.object(
                native, "fetch_response", return_value=(nxdomain, time.monotonic(), (), ())
            ),
        ):
            policy.update(
                _refresh_pool=pool, _refresh_slots=threading.BoundedSemaphore(1), _refreshes={}
            )
            self.assertEqual(native.upstream(q, policy), nxdomain)
        self.assertNotIn(key, policy["_cache"])
        self.assertNotIn("served_stale", policy["_stats"])

    def test_prefetch_waits_for_a_repeat_hit_and_counts_only_the_started_job(self):
        policy = dr.DNSRuntime(dict(spec(), table_epoch="current")).policies[0]
        q = query()
        packet = answer(q, 60)
        now = time.monotonic()
        native.remember_response(policy, native.cache_key(q), 0, packet, now - 55)
        release = threading.Event()

        def fetch(*_):
            if not release.wait(2):
                raise TimeoutError("Refresh fixture did not release")
            return packet, time.monotonic(), (), ()

        with (
            ThreadPoolExecutor(1) as pool,
            patch.object(native, "fetch_response", side_effect=fetch) as upstream_fetch,
        ):
            policy.update(
                _refresh_pool=pool, _refresh_slots=threading.BoundedSemaphore(1), _refreshes={}
            )
            try:
                native.upstream(q, policy)
                self.assertEqual(policy["_refreshes"], {})
                native.upstream(q, policy)
                native.upstream(q, policy)
                self.assertEqual(len(policy["_refreshes"]), 1)
                self.assertEqual(policy["_stats"]["prefetched"], 1)
            finally:
                release.set()
        upstream_fetch.assert_called_once()

    def test_clear_prevents_old_refresh_refill(self):
        q = query()
        key = native.cache_key(q)
        for rebuild in (False, True):
            with (
                self.subTest(rebuild=rebuild),
                ThreadPoolExecutor(1) as pool,
                patch.object(native, "write_sets") as write,
            ):
                runtime = dr.DNSRuntime(dict(spec(), table_epoch="before"))
                self.check_clear_during_refresh(runtime, pool, q, key, rebuild, write)

    def check_clear_during_refresh(self, runtime, pool, q, key, rebuild, write):
        policy = runtime.policies[0]
        policy.update(
            _refresh_pool=pool, _refresh_slots=threading.BoundedSemaphore(1), _refreshes={}
        )
        entered, released = threading.Event(), threading.Event()

        def fetch(*_):
            entered.set()
            if not released.wait(2):
                raise TimeoutError("Refresh fixture did not release")
            return answer(q, 60), time.monotonic(), ("example.com",), ("203.0.113.7",)

        try:
            with (
                patch.object(runtime, "publish"),
                patch.object(native, "fetch_response", side_effect=fetch) as request,
            ):
                background = native.refresh_response(q, policy, False, key, 0)
                self.assertTrue(entered.wait(2))
                runtime.clear()
                if rebuild:
                    runtime.classifier.pause("before")
                    runtime.classifier.resume("before", "after")
                released.set()
                background.result(timeout=2)
                runtime.classifier.flush()
                self.assertEqual(policy["_cache"], {})
                self.assertEqual(policy["_cache_wire_bytes"], 0)
                if rebuild:
                    write.assert_not_called()
                else:
                    write.assert_called_once_with({"rpg_x": ["203.0.113.7"]}, timeout=0.65)
                native.upstream(q, policy)
                self.assertEqual(request.call_count, 2)
                self.assertIn(key, policy["_cache"])
        finally:
            released.set()

    def test_website_registry_matches_requested_fixed_targets(self):
        import diagnostics

        self.assertEqual(
            set(diagnostics.TARGETS), {"google", "youtube", "github", "taobao", "baidu"}
        )
        self.assertEqual(diagnostics.TARGETS["taobao"][1:3], ("https://taobao.com/", "direct"))
        self.assertEqual(diagnostics.TARGETS["github"][2], "proxy")

    def test_clear_epoch_prevents_inflight_query_refill(self):
        runtime = dr.DNSRuntime(spec())
        p = runtime.policies[0]
        q = query()
        entered = threading.Event()
        release = threading.Event()
        result = []

        def classify(*args):
            entered.set()
            release.wait(2)

        with (
            patch("native.socket.socket") as sock,
            patch("native.populate_sets", side_effect=classify),
            patch.object(runtime, "publish"),
        ):
            sock.return_value.__enter__.return_value.recv.return_value = answer(q)
            thread = threading.Thread(target=lambda: result.append(native.upstream(q, p)))
            thread.start()
            self.assertTrue(entered.wait(1))
            runtime.clear()
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertEqual(p["_cache"], {})

    def test_private_control_real_unix_round_trip_clear_and_identity(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("dns_runtime.root_peer", return_value=True),
        ):
            root = Path(folder)
            runtime = dr.DNSRuntime(spec(), root / "dns-control.sock", root / "dns-state.json")
            status = dict(
                state="running",
                time=time.time(),
                runtime_id="runtime",
                generation="generation",
                applied_nonce="new-noop-save",
            )
            (root / "status.json").write_text(json.dumps(status))
            runtime.start()
            try:
                p = runtime.policies[0]
                p["_cache"][b"x"] = (time.monotonic() + 10, b"", time.monotonic())
                result = dr.request_clear(root)
                self.assertTrue(result["ok"])
                self.assertEqual(result["removed"], 1)
                self.assertEqual(p["_cache"], {})
                self.assertEqual((root / "dns-control.sock").stat().st_mode & 0o777, 0o600)
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                    conn.settimeout(2)
                    conn.connect(str(root / "dns-control.sock"))
                    conn.sendall(
                        b'{"action":"clear","runtime_id":"old","generation":"generation","process_instance":"bad"}\n'
                    )
                    self.assertFalse(dr.read_message(conn)["ok"])
            finally:
                runtime.stop()
            self.assertFalse((root / "dns-control.sock").exists())

    def test_control_peer_denial_and_publish_failure_do_not_stop_dns_runtime(self):
        with (
            patch.object(socket, "SO_PEERCRED", 17, create=True),
            patch("dns_runtime.struct.unpack", return_value=(1, 501, 20)),
        ):
            self.assertFalse(
                dr.root_peer(type("Fake", (), {"getsockopt": lambda *args: b"x" * 12})())
            )
        runtime = dr.DNSRuntime(spec())
        with patch("dns_runtime.atomic", side_effect=OSError("disk")):
            runtime.publish(force=True)
        self.assertEqual(len(runtime.policies), 1)

    def test_probe_real_local_udp_and_tcp_and_bad_id(self):
        for proto in ("udp", "tcp"):
            with socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM if proto == "udp" else socket.SOCK_STREAM
            ) as server:
                server.bind(("127.0.0.1", 0))
                port = server.getsockname()[1]
                if proto == "tcp":
                    server.listen(1)

                def serve():
                    if proto == "udp":
                        q, peer = server.recvfrom(2048)
                        server.sendto(answer(q, 60), peer)
                    else:
                        conn, _ = server.accept()
                        with conn:
                            size = struct.unpack("!H", conn.recv(2))[0]
                            q = dd.recv_exact(conn, size, time.monotonic() + 1)
                            packet = answer(q, 60)
                            conn.sendall(struct.pack("!H", len(packet)) + packet)

                thread = threading.Thread(target=serve)
                thread.start()
                policy = {**spec(), **spec()["policies"][0], "port": port}
                result = dd.probe(policy, 0, ("proxy", "example.com"), proto)
                thread.join(2)
                self.assertEqual(result["state"], "ok")
                self.assertEqual(result["addresses"], ["203.0.113.7"])
        policy = {**spec(), **spec()["policies"][0]}
        with patch("dns_diagnostics.socket.socket") as sock:
            q = query()
            sock.return_value.__enter__.return_value.recv.return_value = answer(
                b"\x00\x01" + q[2:], 60
            )
            with patch("dns_diagnostics.make_query", return_value=b"\x00\x00" + q[2:]):
                self.assertEqual(
                    dd.probe(policy, 0, ("proxy", "example.com"), "udp")["state"], "failed"
                )

    def context(self, root):
        now = time.time()
        status = dict(
            state="running",
            time=now,
            runtime_id="runtime",
            generation="generation",
            applied_nonce="n1",
            exit="test",
            exit_ip="192.0.2.1",
        )
        runtime = dr.DNSRuntime(spec(), root / "dns-control.sock", root / "dns-state.json")
        state = runtime.snapshot()
        for name, obj in [("status.json", status), ("dns-state.json", state), ("dns.json", spec())]:
            (root / name).write_text(json.dumps(obj))
        return status, state

    def test_async_start_locks_before_spawn_and_publishes_current_request(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.context(root)
            with patch("dns_diagnostics.subprocess.Popen") as spawn:
                result = dd.start(root)
                self.assertEqual(result["state"], "submitted")
                kwargs = spawn.call_args.kwargs
                self.assertTrue(kwargs["start_new_session"])
                self.assertEqual(len(kwargs["pass_fds"]), 1)
                self.assertEqual(
                    json.loads((root / "dns-tests.json").read_text())["request_id"],
                    result["request_id"],
                )
                with (root / "dns-tests.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.assertEqual(dd.start(root)["state"], "running")
                self.assertEqual(spawn.call_count, 1)
            status = json.loads((root / "status.json").read_text())
            status["state"] = "disabled"
            (root / "status.json").write_text(json.dumps(status))
            self.assertEqual(dd.start(root)["state"], "unavailable")

    def test_worker_marks_generation_or_process_change_stale_and_never_tests_new_context(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            status, state = self.context(root)
            with patch("dns_diagnostics.subprocess.Popen"):
                job = dd.start(root)

            def probe(*args):
                changed = {**status, "generation": "new"}
                dr.atomic(root / "status.json", json.dumps(changed))
                return dict(state="ok")

            with (
                (root / "dns-tests.lock").open("a") as lock,
                patch("dns_diagnostics.probe", side_effect=probe),
            ):
                fcntl.flock(lock, fcntl.LOCK_EX)
                dd.worker(os.dup(lock.fileno()), job["request_id"], root)
            record = json.loads((root / "dns-tests.json").read_text())
            self.assertTrue(record["stale"])
            self.assertFalse(
                dd.context_same(status, state, status, {**state, "process_instance": "new"})
            )
            # A changed context before worker startup must issue zero queries.
            with (
                patch("dns_diagnostics.probe") as probe,
                (root / "dns-tests.lock").open("a") as lock,
            ):
                dd.worker(os.dup(lock.fileno()), job["request_id"], root)
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
