"""Verify path identity and download limits with synthetic curl replies."""

from pathlib import Path
import os
from tempfile import TemporaryFile
from tempfile import TemporaryDirectory
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import speed_test as speed


class SpeedMeasurementTests(unittest.TestCase):
    def test_run_observed_leaving_running_stays_stale_after_recovery(self):
        running = dict(backend="masque", protocol="MASQUE", exit_ip="8.8.8.8")
        record = dict(id="test", rows=[], history=[])

        def measure(backend, path, exit_ip, deadline, *, cancelled):
            self.assertEqual(path, "proxy")
            self.assertTrue(cancelled())
            self.assertTrue(cancelled())
            return dict(path=path, state="cancelled", bytes=400)

        with (
            TemporaryFile() as lock,
            patch.object(speed, "read_result", return_value=record),
            patch.object(speed, "publish") as publish,
            patch.object(
                speed, "status", side_effect=[running, running, speed.ControlError(), running]
            ) as read_status,
            patch.object(speed, "STATUS_CHECK_SECONDS", 0),
            patch.object(speed, "measure_path", side_effect=measure) as measure_path,
        ):
            speed.worker(os.dup(lock.fileno()), "test")

        measure_path.assert_called_once()
        self.assertEqual(read_status.call_count, 3)
        result = publish.call_args.args[0]
        self.assertEqual((result["state"], result["bytes"]), ("stale", 400))
        self.assertEqual(result["history"][-1]["state"], "stale")

    def test_download_budget_is_enforced_and_reported(self):
        def request(args, *, timeout, check):
            self.assertIn("if!CloudflareWARP", args)
            self.assertEqual(args[-1], speed.TRACE)
            if "%{time_appconnect}" in args:
                return SimpleNamespace(returncode=0, stdout="0.05")
            return SimpleNamespace(returncode=0, stdout="ip=8.8.8.8\n")

        with (
            patch.object(speed, "net", side_effect=request),
            patch.object(
                speed,
                "stream_download",
                return_value=(
                    200_000_000,
                    6,
                    "budget",
                    speed.download_rates([(0.1, 200_000_000)], 6),
                ),
            ) as download,
            patch.object(
                speed,
                "cpu_times",
                side_effect=[
                    {"cpu": (100, 75), "cpu0": (50, 40), "cpu1": (50, 35)},
                    {"cpu": (300, 125), "cpu0": (150, 50), "cpu1": (150, 75)},
                ],
            ),
        ):
            row = speed.measure_path("masque", "proxy", "8.8.8.8", time.monotonic() + 10)
        download.assert_called_once()
        self.assertEqual(download.call_args.args[3], 200_000_000)
        self.assertEqual(row["bytes"], 200_000_000)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["stop_reason"], "budget")
        self.assertEqual(row["https_connect_median_ms"], 50)
        self.assertEqual(row["cpu_busy_percent"], 75.0)
        self.assertEqual(row["cpu_busiest_core"], "cpu0")
        self.assertEqual(row["cpu_busiest_core_percent"], 90.0)

    def test_stream_uses_one_curl_and_stable_rate_excludes_slow_start(self):
        samples = [
            (second + 0.5, size)
            for second, size in enumerate([100_000, 300_000, 650_000] + [1_000_000] * 5)
        ]
        rates = speed.download_rates(samples, 8)
        self.assertAlmostEqual(rates["stable_mbps"], 8, delta=0.8)
        self.assertAlmostEqual(rates["peak_mbps"], 8, delta=0.8)
        self.assertLess(rates["average_mbps"], rates["stable_mbps"])
        import subprocess
        import sys

        original = subprocess.Popen
        with patch.object(
            speed.subprocess,
            "Popen",
            side_effect=lambda argv, **kwargs: original(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('HTTP/2 200\\r\\n\\r\\n' + 'a' * 100); sys.stderr.write('200 0.2 0.7')",
                ],
                **kwargs,
            ),
        ) as curl:
            total, _, stop, _ = speed.stream_download(
                speed.command, [], time.monotonic() + 10, 100, lambda: False
            )
        curl.assert_called_once()
        self.assertEqual(curl.call_args.args[0][-1], speed.DOWNLOAD + "100")
        self.assertEqual((total, stop), (100, "budget"))

    def test_cpu_counters_skip_guest_columns_and_handle_missing_proc(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "stat"
            path.write_text("cpu  10 1 2 70 10 1 2 4 8 1\ncpu0 10 1 2 70 10 1 2 4 8 1\nintr 123\n")
            with patch.object(speed, "CPU_STAT", path):
                self.assertEqual(speed.cpu_times(), {"cpu": (100, 80), "cpu0": (100, 80)})
                path.unlink()
                self.assertEqual(speed.cpu_times(), {})
        self.assertEqual(speed.cpu_usage({"cpu": (100, 80)}, {"cpu": (100, 80)}), {})

    def test_wrong_initial_exit_skips_measurement(self):
        with patch.object(
            speed, "net", return_value=SimpleNamespace(returncode=0, stdout="ip=9.9.9.9\n")
        ) as curl:
            row = speed.measure_path("masque", "proxy", "8.8.8.8", time.monotonic() + 10)
        curl.assert_called_once()
        self.assertEqual((row["state"], row["bytes"]), ("invalid", 0))

    def test_exit_change_invalidates_completed_download(self):
        with (
            patch.object(speed, "trace_ip", side_effect=["8.8.8.8", "9.9.9.9"]),
            patch.object(
                speed,
                "request",
                side_effect=[
                    *[SimpleNamespace(returncode=0, stdout="0.05") for _ in range(5)],
                ],
            ),
            patch.object(speed, "stream_download", return_value=(100, 0.5, "budget", {})),
        ):
            row = speed.measure_path("masque", "proxy", "8.8.8.8", time.monotonic() + 10, 100)
        self.assertEqual((row["state"], row["bytes"]), ("invalid", 100))

    def test_current_identity_covers_runtime_and_saved_line(self):
        running = dict.fromkeys(speed.IDENTITY, "before")
        self.assertTrue(speed.same_line(running, running))
        for field in speed.IDENTITY:
            self.assertFalse(speed.same_line(running, {**running, field: "after"}))
