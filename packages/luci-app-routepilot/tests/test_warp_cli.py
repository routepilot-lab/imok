"""WARP CLI parsing and unknown-state recovery with a simulated lease clock."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import egress_guard
import mplane
import service
import warp_cli

VNET = "11111111-1111-4111-8111-111111111111"
EXIT_IP = "8.8.8.8"


class CliParserTests(unittest.TestCase):
    def test_observed_status_line(self):
        fixture = Path(__file__).parent / "fixtures/warp_cli/2026.8.2115.0/status-connected.txt"
        self.assertIs(warp_cli.parse_status(fixture.read_text()), warp_cli.TunnelStatus.CONNECTED)
        self.assertIn(fixture.parent.name, warp_cli.VERIFIED_VERSIONS)

    def test_status_requires_one_unambiguous_line(self):
        for raw in (
            "Connected",
            "Status update: Connected (old)",
            "Status update: Connected\nStatus update: Disconnected",
            "Status update: New format",
        ):
            with self.subTest(raw=raw):
                self.assertIs(warp_cli.parse_status(raw), warp_cli.TunnelStatus.UNKNOWN)
        self.assertIs(
            warp_cli.parse_status("Status update: Disconnected\nReason: test\n"),
            warp_cli.TunnelStatus.DISCONNECTED,
        )
        self.assertIs(
            warp_cli.read_status(Mock(return_value=SimpleNamespace(stdout="", returncode=1))),
            warp_cli.TunnelStatus.UNAVAILABLE,
        )

    def test_settings_directory_and_version_are_structured(self):
        self.assertEqual(
            warp_cli.parse_settings("(network policy) WARP tunnel protocol: WireGuard"),
            dict(
                protocol="WireGuard",
                managed=True,
                source="organization",
                local_switch_allowed=False,
                supported=["MASQUE", "WireGuard"],
            ),
        )
        self.assertIsNone(warp_cli.parse_settings("WARP tunnel protocol: future"))
        directory = warp_cli.parse_directory(
            f"Currently selected: {VNET}\n\nVirtual Networks:\n"
            f"ID: {VNET}\nName: Fixture network\nComment: Ignored\nDefault: true\n"
        )
        self.assertEqual(directory.selected, VNET)
        self.assertEqual(
            directory.networks, [dict(id=VNET, name="Fixture network", is_default=True)]
        )
        self.assertEqual(warp_cli.parse_version("warp-cli 2026.8.2115.0\n"), "2026.8.2115.0")
        self.assertIsNone(warp_cli.parse_version("warp-cli 2026.8.2115.0extra"))

    def test_unknown_status_cannot_switch_exit_or_connect(self):
        other = "22222222-2222-4222-8222-222222222222"
        replies = [
            SimpleNamespace(
                stdout=f"Currently selected: {other}\nVirtual Networks:\n"
                f"ID: {VNET}\nName: Fixture network\nDefault: false\n"
            ),
            SimpleNamespace(stdout="Status update: New format"),
        ]
        with patch.object(mplane, "cli", side_effect=replies) as cli:
            with self.assertRaises(warp_cli.UnknownTunnelStatus):
                mplane.Plane().connect(VNET)
        self.assertEqual(
            cli.call_args_list,
            [call("vnet", "--no-paginate", check=False), call("status", check=False)],
        )


class UnknownStatusTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.run_path = Path(self.folder.name)
        self.svc = service.Service()
        self.svc.compiled = {"vnet": VNET}
        self.svc.runtime_id = "fixture"
        self.svc.plane.probe = Mock(return_value=EXIT_IP)
        self.svc.verified_exit = (self.svc.plane, VNET, EXIT_IP)
        self.cli = Mock(return_value=SimpleNamespace(stdout="Status update: Future state"))
        for mock in (
            patch.object(service, "RUN", self.run_path),
            patch.object(service, "cli", self.cli),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    def test_uses_previous_success_for_same_plane_and_network(self):
        self.svc.tunnel_ready()
        self.svc.plane.probe.assert_called_once_with(strict=True, timeout=4)
        self.assertIsNotNone(self.svc.unknown_since)
        self.cli.assert_called_once_with("status", check=False)
        self.svc.compiled["vnet"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaises(warp_cli.UnknownTunnelStatus):
            self.svc.tunnel_ready()
        self.assertEqual(self.svc.plane.probe.call_count, 1)

    def test_configured_expected_exit_takes_precedence(self):
        self.svc.expected_exit = "1.1.1.1"
        with self.assertRaises(warp_cli.UnknownTunnelStatus):
            self.svc.tunnel_ready()

    def protect(self):
        guard = self.svc.egress_guard
        guard.selectors = ("fixture",)
        guard.state = "ready"
        guard.renewed_at = 10
        self.clock = 14
        for mock in (
            patch.object(time, "monotonic", side_effect=lambda: self.clock),
            patch.object(guard, "require_supported"),
            patch.object(guard, "_transact"),
            patch.object(guard, "_record"),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        (self.run_path / "dns-state.json").write_text(
            json.dumps(dict(state="running", runtime_id="fixture", time=time.time()))
        )
        return guard

    def test_slow_probe_rechecks_every_cycle_and_renews_before_lease_expires(self):
        guard = self.protect()

        def probe(**options):
            self.assertEqual(options, {"strict": True, "timeout": 4})
            self.clock += 3.9
            return EXIT_IP

        self.svc.plane.probe.side_effect = probe
        for _ in range(2):
            previous = guard.renewed_at
            self.svc.tunnel_ready()
            self.assertTrue(self.svc.confirm_egress_ready())
            self.assertLess(guard.renewed_at - previous, egress_guard.LEASE_SECONDS)
            self.clock += 4
        self.assertEqual(self.svc.plane.probe.call_count, 2)
        self.assertTrue(all(args.args == ("status",) for args in self.cli.call_args_list))
        self.assertEqual(guard.state, "ready")

    def test_probe_timeout_blocks_before_any_new_lease(self):
        guard = self.protect()
        self.svc.plane.probe.side_effect = subprocess.TimeoutExpired("curl", 4)
        self.svc.tunnel_ready()
        with self.assertRaises(warp_cli.UnknownTunnelStatus):
            self.svc.confirm_egress_ready()
        guard._transact.assert_called_once_with("flush set inet routepilot_egress_guard ready\n")
        self.assertEqual(guard.state, "blocking")
        self.svc.plane.probe.assert_called_once_with(strict=True, timeout=4)

    def test_spent_renewal_budget_blocks_without_extending_the_old_lease(self):
        guard = self.protect()
        self.clock = 21
        self.svc.tunnel_ready()
        with self.assertRaises(warp_cli.UnknownTunnelStatus):
            self.svc.confirm_egress_ready()
        self.svc.plane.probe.assert_not_called()
        self.assertEqual(guard.state, "blocking")
