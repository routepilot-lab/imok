"""Exercise the real DNS listeners over loopback with a controlled upstream."""

from contextlib import contextmanager, ExitStack
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import dns_diagnostics
import dns_runtime
import native


def query(ident):
    return struct.pack("!H", ident) + dns_diagnostics.make_query(f"probe-{ident}.example.com")[2:]


def serve(port, folder, latency):
    real_socket = socket.socket
    real_runtime = dns_runtime.DNSRuntime

    class LoopbackSocket(real_socket):
        def bind(self, address):
            if address == ("0.0.0.0", 12100):
                address = ("127.0.0.1", port)
            return super().bind(address)

    def runtime(spec):
        return real_runtime(spec, folder / "control.sock", folder / "state.json")

    def upstream(packet, policy, tcp=False):
        time.sleep(latency)
        ident, _, _, end = native.question(packet)
        return (
            packet[:2]
            + struct.pack("!5H", 0x8180, 1, 1, 0, 0)
            + packet[12:end]
            + b"\xc0\x0c"
            + struct.pack("!HHIH", 1, 1, 60, 4)
            + bytes((192, 0, 2, ident % 256))
        )

    spec = {"policies": [{"port": 12100, "mark": 0}]}
    with (
        patch.object(native.socket, "socket", LoopbackSocket),
        patch.object(dns_runtime, "DNSRuntime", runtime),
        patch.object(native, "upstream", upstream),
    ):
        native.run_dns(spec)


@contextmanager
def listener(latency):
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        with socket.socket() as guard:
            guard.bind(("127.0.0.1", 0))
            port = guard.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-B", __file__, "--serve", str(port), tmp, str(latency)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 4
            while not (folder / "state.json").exists():
                if proc.poll() is not None:
                    raise RuntimeError(proc.stderr.read().decode())
                if time.monotonic() > deadline:
                    raise TimeoutError("DNS fixture did not start")
                time.sleep(0.01)
            yield ("127.0.0.1", port)
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()


def exchange_burst(address, packets, timeout=2.5):
    replies = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
        client.connect(address)
        for packet in packets.values():
            client.send(packet)
        deadline = time.monotonic() + timeout
        while len(replies) < len(packets) and time.monotonic() < deadline:
            client.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                response = client.recv(2048)
            except socket.timeout:
                break
            ident, flags = struct.unpack_from("!HH", response)
            assert native.question(response, True)[:3] == native.question(packets[ident])[:3]
            replies[ident] = flags & 15
    return replies


def burst(address, total, timeout=2.5):
    return exchange_burst(address, {ident: query(ident) for ident in range(total)}, timeout)


class DNSQueueTests(unittest.TestCase):
    def test_idle_tcp_connections_do_not_starve_udp(self):
        with listener(0.001) as address, ExitStack() as sockets:
            for ident in range(16):
                client = sockets.enter_context(socket.create_connection(address, timeout=2))
                packet = query(ident)
                client.sendall(struct.pack("!H", len(packet)) + packet)
                (size,) = struct.unpack("!H", native._recv_exact(client, 2))
                response = native._recv_exact(client, size)
                self.assertEqual(struct.unpack_from("!H", response)[0], ident)
            self.assertEqual(burst(address, 1, timeout=0.6), {0: 0})

    def test_ordinary_burst_completes_with_slow_upstream(self):
        with listener(0.35) as address:
            self.assertEqual(burst(address, 96), dict.fromkeys(range(96), 0))

    def test_full_udp_queue_returns_servfail_instead_of_silence(self):
        with listener(0.2) as address:
            total = native.UDP_MAX_PENDING + 52
            results = burst(address, total)
            self.assertEqual(set(results), set(range(total)), "all valid requests get a response")
            self.assertEqual(set(results.values()), {0, 2})

    def test_stale_udp_queue_entries_fail_without_reaching_upstream(self):
        with listener(0.75) as address:
            # The entire burst fits the admission limit. The last wave waits
            # past the queue deadline and must fail instead of starting work.
            results = burst(address, native.UDP_MAX_PENDING, timeout=5)
            self.assertEqual(set(results), set(range(native.UDP_MAX_PENDING)))
            self.assertEqual(set(results.values()), {0, 2})

    def test_repeated_udp_questions_share_upstream_work_and_keep_client_ids(self):
        # Independently enqueuing every copy would exhaust the UDP queue.
        packets = {ident: struct.pack("!H", ident) + query(ident % 12)[2:] for ident in range(288)}
        with listener(0.3) as address:
            self.assertEqual(exchange_burst(address, packets), dict.fromkeys(packets, 0))

    def test_identical_edns_options_do_not_share_an_inflight_request(self):
        packet = query(1)
        cookie = struct.pack("!HH", 10, 8) + b"testcook"
        opt = b"\x00" + struct.pack("!HHIH", 41, 1232, 0, len(cookie)) + cookie
        question_with_opt = b"\x00\x01" + packet[12:] + opt
        with listener(0.3) as address, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            client.connect(address)
            for ident in (1, 2):
                client.send(struct.pack("!H", ident) + packet[2:10] + question_with_opt)
            replies = {}
            for _ in range(2):
                response = client.recv(2048)
                replies[struct.unpack_from("!H", response)[0]] = response[-1]
            self.assertEqual(replies, {1: 1, 2: 2})

    def test_blocked_aaaa_gets_a_local_reply_while_upstream_is_busy(self):
        packets = {ident: query(ident) for ident in range(native.UDP_MAX_PENDING)}
        ident = len(packets)
        aaaa = query(ident)[:-4] + struct.pack("!HH", 28, 1)
        packets[ident] = aaaa
        with listener(1.0) as address:
            replies = exchange_burst(address, packets, timeout=0.7)
        self.assertEqual(
            replies.get(ident), 0, "local AAAA handling must not wait for a UDP worker"
        )

    def test_tcp_reuses_connections_by_route_and_reopens_a_stale_socket(self):
        class Connection:
            def __init__(self):
                self.readable = bytearray()
                self.closed = False
                self.stale = False
                self.mark = None

            def settimeout(self, value):
                pass

            def setsockopt(self, _, __, mark):
                self.mark = mark

            def connect(self, address):
                self.address = address

            def sendall(self, frame):
                if self.stale:
                    return
                packet = frame[2:]
                response = packet[:2] + struct.pack("!5H", 0x8180, 1, 0, 0, 0) + packet[12:]
                self.readable += struct.pack("!H", len(response)) + response

            def recv(self, size):
                part = bytes(self.readable[:size])
                del self.readable[:size]
                return part

            def close(self):
                self.closed = True

        pool = native.UpstreamTCP()
        sockets = []

        def open_socket(*_):
            connection = Connection()
            sockets.append(connection)
            return connection

        def lookup(ident, mark):
            packet = query(ident)
            response, reused = pool.exchange(
                packet, native.question(packet)[:3], "192.0.2.53", mark
            )
            self.assertEqual(native.question(response, True)[0], ident)
            return reused

        with patch.object(native.socket, "socket", side_effect=open_socket):
            self.assertFalse(lookup(1, 0))
            self.assertTrue(lookup(2, 0))
            self.assertFalse(lookup(3, native_mark := 0x5A000000))
            self.assertEqual([conn.mark for conn in sockets], [0, native_mark])
            sockets[0].stale = True
            self.assertFalse(lookup(4, 0))
            self.assertTrue(sockets[0].closed)
            self.assertTrue(lookup(5, native_mark))
            self.assertEqual(len(sockets), 3)
        pool.close()
        self.assertTrue(all(conn.closed for conn in sockets))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]), Path(sys.argv[3]), float(sys.argv[4]))
    else:
        unittest.main()
