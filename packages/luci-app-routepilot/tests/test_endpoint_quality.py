"""Exercise handshake ranking and candidate constraints without network access."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import endpoint_quality as quality
from public_errors import ControlError


class EndpointQualityTests(unittest.TestCase):
    def test_only_existing_ipv4_candidates_are_admitted(self):
        value = dict(entries=["1.1.1.1:443", "1.1.1.1:443", "[2606:4700:4700::1111]:443"])
        with patch.object(quality, "optional", return_value=(value, False)):
            self.assertEqual(quality.candidates(), ["1.1.1.1:443"])
            with self.assertRaises(ControlError):
                quality.in_namespace(["8.8.8.8:443"])

    def test_rank_prefers_reliable_then_fast_handshakes(self):
        times = {
            "1.1.1.1": iter([10, 15, None]),
            "8.8.8.8": iter([23, 28, 25]),
            "9.9.9.9": iter([30, 28, 31]),
        }
        result = quality.measure([ip + ":443" for ip in times], lambda ip, _: next(times[ip]))
        self.assertEqual(result["recommended"], "8.8.8.8:443")
        self.assertEqual(result["rows"][0]["failed"], 1)
        self.assertEqual(result["rows"][0]["median_ms"], 12.5)

    def test_no_quic_results_are_fabricated_on_non_http3_curl(self):
        targets = ["1.1.1.1:443"]
        with (
            patch.object(quality, "candidates", return_value=targets),
            patch.object(
                quality, "command", return_value=SimpleNamespace(stdout="Features: HTTP2")
            ),
            patch.object(quality, "tcp_handshake", return_value=None) as tcp,
            patch.object(quality, "quic_handshake") as quic,
        ):
            result = quality.in_namespace(targets)
        self.assertEqual(result["transport"], "tls_tcp")
        self.assertIsNone(result["recommended"])
        self.assertEqual(tcp.call_count, 3)
        quic.assert_not_called()
