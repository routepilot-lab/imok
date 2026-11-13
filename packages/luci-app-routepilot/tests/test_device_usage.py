import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import patch


USAGE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "root/usr/libexec/routepilot-device-usage")
)


class DeviceUsage(unittest.TestCase):
    def test_device_totals_use_the_firmware_directions_and_keep_unknown_values(self):
        raw = {
            "clients": {
                "first": {
                    "mac": "02:00:00:00:00:01",
                    "name": "第一台\n",
                    "total_tx": "1024",
                    "total_rx": 2048,
                    "ip": "192.0.2.12",
                },
                "second": {
                    "mac": "02:00:00:00:00:02",
                    "name": "第二台",
                    "total_tx": "unknown",
                    "total_rx": "0",
                },
            }
        }
        self.assertEqual(
            USAGE["devices"](raw),
            [
                {
                    "mac": "02:00:00:00:00:01",
                    "name": "第一台",
                    "upload": 1024,
                    "download": 2048,
                    "total": 3072,
                },
                {
                    "mac": "02:00:00:00:00:02",
                    "name": "第二台",
                    "upload": None,
                    "download": 0,
                    "total": None,
                },
            ],
        )

    def test_snapshot_only_calls_readonly_firmware_list(self):
        for code, raw, expected in (
            (0, {"clients": {}}, "ready"),
            (4, {}, "unsupported"),
            (0, {}, "error"),
        ):
            with (
                self.subTest(code=code, expected=expected),
                patch(
                    "subprocess.run",
                    return_value=SimpleNamespace(returncode=code, stdout=json.dumps(raw).encode()),
                ) as run,
            ):
                self.assertEqual(USAGE["snapshot"](), {"state": expected, "devices": []})
                run.assert_called_once()
                self.assertEqual(
                    run.call_args.args[0], ["ubus", "-t", "3", "call", "gl-clients", "list", "{}"]
                )
