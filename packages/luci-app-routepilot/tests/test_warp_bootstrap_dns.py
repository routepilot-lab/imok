"""Exercise bootstrap DNS selection without accessing any external network."""

from contextlib import nullcontext, redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
from threading import Barrier
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import warp_bootstrap_dns as bootstrap


def reply(query, address="104.16.1.2", *, flags=0x8180, matching_id=True):
    ident = struct.unpack("!H", query[:2])[0]
    if not matching_id:
        ident ^= 1
    count = int(address is not None)
    result = struct.pack("!6H", ident, flags, 1, count, 0, 0) + query[12:]
    if count:
        result += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 60, 4) + socket.inet_aton(address)
    return result


class WarpBootstrapDNSTests(unittest.TestCase):
    def test_manual_probe_launch_failure_releases_lock_and_finishes_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            result = run / "warp-dns-tests.json"
            with (
                patch.object(bootstrap, "RUN", run),
                patch.object(bootstrap, "TEST_FILE", result),
                patch.object(bootstrap, "command", return_value=SimpleNamespace(stdout="rp-warp")),
                patch("mplane.NS", "rp-warp"),
                patch("runtime.read_config", return_value={"main": {}}),
                patch.object(bootstrap, "local_addresses", return_value=set()),
                patch.object(
                    bootstrap.subprocess, "Popen", side_effect=OSError("cannot spawn")
                ) as launch,
            ):
                for attempt in (1, 2):
                    with self.assertRaises(OSError):
                        bootstrap.start_manual_test()
                    report = json.loads(result.read_text())
                    self.assertEqual(report["state"], "error")
                    self.assertGreater(report["finished_at"], 0)
                    self.assertEqual(launch.call_count, attempt)

    def test_manual_lan_probe_compares_transports_on_same_deadline(self):
        address = "192.168.0.1"
        deadline = time.monotonic() + 10
        barrier = Barrier(2)

        def probe(execute, addresses, until, *, attempts, interface, tcp):
            self.assertEqual(
                (addresses, until, attempts, interface), ([address], deadline, 3, "eth1")
            )
            barrier.wait(timeout=2)
            if tcp:
                raise subprocess.TimeoutExpired("probe", 1)
            return {address: {"ms": 6.22, "reason": None}}

        with patch.object(bootstrap, "namespace_probe", side_effect=probe):
            tested = bootstrap.manual_probe(
                self.fail, [address], deadline, "eth1", lan=True, tcp=True
            )
        self.assertEqual(
            tested[address],
            {
                "ms": None,
                "reason": "timeout",
                "udp": {"ms": 6.22, "reason": None},
                "tcp": {"ms": None, "reason": "timeout"},
            },
        )

    def test_manual_warp_probe_keeps_existing_udp_only_result(self):
        result = {"1.1.1.1": {"ms": 2, "reason": None}}
        with patch.object(bootstrap, "namespace_probe", return_value=result) as probe:
            self.assertEqual(bootstrap.manual_probe(self.fail, ["1.1.1.1"], 5, "rp-client"), result)
        probe.assert_called_once_with(
            self.fail, ["1.1.1.1"], 5, attempts=3, interface="rp-client", tcp=False
        )

    def test_only_provisioned_peers_reachable_through_active_wan_are_candidates(self):
        def link(name, device, dns, up=True):
            return {"interface": name, "l3_device": device, "up": up, "dns-server": dns}

        wan = "pppoe-fibre"
        links = [
            link("lan", "br-lan", ["192.168.1.1"]),
            link("old", wan, ["1.1.1.1"], up=False),
            link("other", wan, ["9.9.9.9"]),
            link(
                "fibre",
                wan,
                [
                    "1.1.1.1",
                    "192.168.1.1",
                    "127.0.0.1",
                    "169.254.1.2",
                    "224.0.0.1",
                    "10.254.239.1",
                    "::1",
                    bootstrap.FALLBACK,
                    "100.64.0.2",
                    "100.64.0.2",
                    "10.20.0.2",
                    "10.20.0.3",
                ],
            ),
        ]
        checked = []

        def command(args, *, timeout, check):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1)
            if args[0] == "ubus":
                self.assertEqual(args, ["ubus", "call", "network.interface", "dump"])
                return SimpleNamespace(stdout=json.dumps({"interface": links}))
            if args[0] == "uci":
                self.assertNotIn("network.lan", args[-1])
                self.assertNotIn("network.old", args[-1])
                values = {"network.other.peerdns": "0", "network.fibre.dns": "1.1.1.1"}
                return SimpleNamespace(stdout=values.get(args[-1], ""))
            self.assertEqual(args[:5], ["ip", "-j", "-4", "route", "get"])
            self.assertEqual(args[6:], ["from", "10.254.239.2", "iif", "rp-host"])
            checked.append(args[5])
            device = "br-lan" if args[5] == "192.168.1.1" else wan
            return SimpleNamespace(stdout=json.dumps([{"dev": device}]))

        with patch.object(bootstrap, "command", side_effect=command):
            self.assertEqual(bootstrap.wan_peers(wan), ["100.64.0.2", "10.20.0.2"])
        self.assertEqual(checked, ["192.168.1.1", "100.64.0.2", "10.20.0.2"])

    def test_lan_peer_lookup_uses_host_routing_and_can_label_fallback_as_peer(self):
        def command(args, **kwargs):
            if args[0] == "ubus":
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "interface": [
                                {
                                    "interface": "wan",
                                    "up": True,
                                    "l3_device": "eth1",
                                    "dns-server": [bootstrap.FALLBACK, "192.168.0.1"],
                                },
                            ]
                        }
                    )
                )
            if args[0] == "uci":
                return SimpleNamespace(stdout="")
            self.assertEqual(len(args), 6)
            return SimpleNamespace(stdout='[{"dev":"eth1"}]')

        with patch.object(bootstrap, "command", side_effect=command):
            self.assertEqual(
                bootstrap.wan_peers("eth1", host=True), [bootstrap.FALLBACK, "192.168.0.1"]
            )

    def test_no_wan_dns_or_unavailable_probe_uses_original_fallback(self):
        fallback = "nameserver 223.5.5.5\n"
        with (
            patch.object(
                bootstrap, "command", return_value=SimpleNamespace(stdout='{"interface":[]}')
            ),
            patch.object(bootstrap, "main") as probe,
        ):
            self.assertEqual(bootstrap.bootstrap_resolver("wan0", probe), fallback)
            probe.assert_not_called()
        with patch.object(bootstrap, "wan_peers", return_value=["100.64.0.2"]):
            for failure in (subprocess.TimeoutExpired("ip netns exec", 2), OSError("unavailable")):
                with (
                    self.subTest(failure=type(failure)),
                    patch.object(bootstrap, "main", side_effect=failure) as probe,
                ):
                    self.assertEqual(bootstrap.bootstrap_resolver("wan0", probe), fallback)
            with patch.object(
                bootstrap, "main", return_value=SimpleNamespace(returncode=0, stdout="1.1.1.1\n")
            ) as probe:
                self.assertEqual(bootstrap.bootstrap_resolver("wan0", probe), fallback)
        with patch.object(
            bootstrap, "command", return_value=SimpleNamespace(stdout="invalid JSON")
        ):
            self.assertEqual(bootstrap.bootstrap_resolver("wan0", self.fail), fallback)

    def test_new_auto_mode_preserves_successful_wan_bytes(self):
        with (
            patch.object(bootstrap, "local_addresses", return_value=set()),
            patch.object(bootstrap, "wan_peers", return_value=["100.64.0.2"]),
            patch.object(
                bootstrap, "namespace_probe", return_value={"100.64.0.2": {"ms": 9}}
            ) as probe,
            patch.object(bootstrap, "HISTORY") as history,
        ):
            history.read_text.side_effect = FileNotFoundError
            with patch.object(bootstrap, "atomic"):
                value, details = bootstrap.select_resolver("wan0", self.fail, {})
        self.assertEqual(
            value, "nameserver 100.64.0.2\nnameserver 223.5.5.5\noptions timeout:1 attempts:2\n"
        )
        self.assertEqual(details["source"], "wan")
        probe.assert_called_once()

    def test_new_auto_mode_uses_fastest_candidate_when_wan_is_unavailable(self):
        with (
            patch.object(bootstrap, "local_addresses", return_value=set()),
            patch.object(bootstrap, "wan_peers", return_value=[]),
            patch.object(
                bootstrap,
                "namespace_probe",
                return_value={"1.1.1.1": {"ms": 10}, "8.8.8.8": {"ms": 5}},
            ),
            patch.object(bootstrap, "HISTORY") as history,
            patch.object(bootstrap, "atomic") as save,
        ):
            history.read_text.return_value = '{"address":"9.9.9.9"}'
            value, details = bootstrap.select_resolver("wan0", self.fail, {})
        self.assertTrue(value.startswith("nameserver 8.8.8.8\nnameserver 9.9.9.9\n"))
        self.assertEqual(details["source"], "candidate")
        self.assertEqual(json.loads(save.call_args.args[1]), {"address": "8.8.8.8"})

    def test_save_preflight_rejects_the_router_and_reserved_addresses(self):
        for bad in ("192.168.50.1", "10.254.238.1", "127.0.0.1", "1.1.1.1:53"):
            raw = dict(warp_dns_mode="pinned", warp_dns_pinned=bad, warp_dns_custom=[])
            with (
                self.subTest(address=bad),
                patch("request_input.claim_request", return_value=nullcontext(raw)),
                patch.object(bootstrap, "local_addresses", return_value={"192.168.50.1"}),
                self.assertRaises(bootstrap.ControlError),
            ):
                bootstrap.validate_preferences("request:checksum")

    def test_auto_wan_discovery_and_probe_share_the_first_second(self):
        clock = [10.0]
        observed = []

        def local_addresses(*, timeout):
            clock[0] += timeout
            return set()

        def discover(_, until):
            clock[0] = until
            return ["100.64.0.2"]

        def probe(_, addresses, until):
            observed.append((addresses, until))
            if addresses == ["100.64.0.2"]:
                # A result finishing after its deadline cannot beat the fallback.
                clock[0] = until + 0.001
                return {"100.64.0.2": {"ms": 100}}
            return {"8.8.8.8": {"ms": 5}}

        with (
            patch.object(bootstrap.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(bootstrap, "local_addresses", side_effect=local_addresses),
            patch.object(bootstrap, "wan_peers", side_effect=discover),
            patch.object(bootstrap, "namespace_probe", side_effect=probe),
            patch.object(bootstrap, "HISTORY") as history,
            patch.object(bootstrap, "atomic"),
        ):
            history.read_text.side_effect = FileNotFoundError
            _, details = bootstrap.select_resolver("wan", self.fail, {})
        self.assertEqual(observed[0], (["100.64.0.2"], 11.0))
        self.assertEqual(observed[1][1], 13.0)
        self.assertEqual(details["address"], "8.8.8.8")

    def test_pinned_and_full_custom_fallback_share_three_second_budget(self):
        for mode in ("pinned", "custom"):
            clock = [10.0]
            deadlines = []

            def probe(execute, addresses, deadline):
                deadlines.append(deadline)
                if addresses:
                    clock[0] = deadline
                return {}

            with (
                self.subTest(mode=mode),
                patch.object(bootstrap.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(bootstrap, "local_addresses", return_value=set()),
                patch.object(bootstrap, "wan_peers", return_value=["100.64.0.2"]),
                patch.object(bootstrap, "namespace_probe", side_effect=probe),
                patch.object(bootstrap, "HISTORY") as history,
            ):
                history.read_text.return_value = '{"address":"9.9.9.9"}'
                value, details = bootstrap.select_resolver(
                    "wan",
                    self.fail,
                    dict(
                        warp_dns_mode=mode,
                        warp_dns_pinned="8.8.8.8",
                        warp_dns_custom=["8.8.8.8", "1.1.1.1", "223.5.5.5", "119.29.29.29"],
                    ),
                )
            self.assertTrue(details["fallback"])
            self.assertEqual(clock[0], 13.0)
            self.assertTrue(all(deadline <= 13.0 for deadline in deadlines))
            self.assertEqual(
                value,
                "nameserver 9.9.9.9\nnameserver 1.1.1.1\nnameserver 223.5.5.5\n"
                + bootstrap.OPTIONS,
            )

    def probe_via_loopback(self, peers, *, send_reply, expected_queries):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
            server.bind(("127.0.0.1", 0))
            server.settimeout(1)
            endpoint = server.getsockname()
            real_probe = bootstrap.usable_resolver

            def serve():
                for i in range(expected_queries):
                    query, client = server.recvfrom(1024)
                    server.sendto(send_reply(query, i), client)

            def namespace(args, *, timeout, check):
                self.assertEqual(args[1], "-B")
                self.assertEqual(args[3:], peers)
                self.assertEqual(timeout, 2)
                self.assertFalse(check)
                output = io.StringIO()
                with redirect_stdout(output):
                    code = bootstrap.main(args[3:])
                return SimpleNamespace(returncode=code, stdout=output.getvalue())

            def local_resolver(address):
                return real_probe(endpoint, timeout=0.25)

            with (
                ThreadPoolExecutor(max_workers=1) as pool,
                patch.object(bootstrap, "wan_peers", return_value=peers),
                patch.object(bootstrap, "usable_resolver", side_effect=local_resolver),
            ):
                worker = pool.submit(serve)
                result = bootstrap.bootstrap_resolver("wan0", namespace)
                worker.result(timeout=2)
                return result

    def test_working_wan_dns_is_first_and_fallback_is_preserved(self):
        result = self.probe_via_loopback(
            ["100.64.0.2"], send_reply=lambda query, i: reply(query), expected_queries=2
        )
        self.assertEqual(
            result, "nameserver 100.64.0.2\nnameserver 223.5.5.5\noptions timeout:1 attempts:2\n"
        )

    def test_silent_wan_dns_automatically_uses_public_fallback(self):
        result = self.probe_via_loopback(["100.64.0.2"], send_reply=self.fail, expected_queries=0)
        self.assertEqual(result, "nameserver 223.5.5.5\n")

    def test_invalid_answers_use_public_fallback(self):
        for invalid in (
            lambda query: reply(query, matching_id=False),
            lambda query: reply(query, address=None, flags=0x8183),
            lambda query: reply(query, address="192.168.0.1"),
            lambda query: reply(query, address="224.0.0.1"),
            lambda query: reply(query, flags=0x8380),
            lambda query: b"invalid",
        ):
            with self.subTest(invalid=invalid):
                result = self.probe_via_loopback(
                    ["100.64.0.2"],
                    send_reply=lambda query, i: reply(query) if i == 0 else invalid(query),
                    expected_queries=2,
                )
                self.assertEqual(result, "nameserver 223.5.5.5\n")

    def test_first_healthy_wan_peer_wins(self):
        with (
            patch.object(bootstrap, "usable_resolver", side_effect=[False, True]) as probe,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(bootstrap.main(["100.64.0.2", "100.64.0.3"]), 0)
        self.assertEqual(output.getvalue(), "100.64.0.3\n")
        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            [("100.64.0.2", 53), ("100.64.0.3", 53)],
        )


if __name__ == "__main__":
    unittest.main()
