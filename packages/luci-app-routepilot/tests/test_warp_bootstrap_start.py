"""Exercise the real plane startup using private files and stubbed system tools."""

from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import mplane
from service import routing_digest
import warp_control


class BootstrapStartTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = base / "warp"
        self.run = base / "run"
        self.namespace_config = base / "netns" / "routepilot" / "resolv.conf"
        self.chroot_config = self.root / "etc/resolv.conf"
        for name in (
            "usr/bin/warp-svc",
            "dev/null",
            "dev/zero",
            "dev/random",
            "dev/urandom",
            "dev/net/tun",
        ):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

        self.replace(mplane, "ROOT", self.root)
        self.replace(mplane, "RUN", self.run)
        self.replace(mplane, "NETNS_CONFIG", self.namespace_config)
        self.plane = mplane.Plane()
        self.addCleanup(mplane.release_plane, self.plane)

    def replace(self, obj, name, value):
        override = patch.object(obj, name, value)
        override.start()
        self.addCleanup(override.stop)

    def assert_startup_writes(self, expected, *, preferences=None):
        self.plane.dns_preferences = preferences
        choice = {"address": "10.20.30.40", "source": "pinned", "fallback": False}

        def command(argv, **kwargs):
            if argv == ["ip", "-j", "-4", "route", "get", "1.1.1.1"]:
                return SimpleNamespace(returncode=0, stdout='[{"dev":"wan-test"}]')
            if argv == ["ip", "-j", "-4", "route", "show", "table", "main"]:
                return SimpleNamespace(returncode=0, stdout="[]")
            if argv in (
                ["ip", "link", "show", mplane.HOST],
                ["nft", "list", "table", "inet", "routepilot_control"],
            ):
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout="")

        def choose(wan, run_in_namespace):
            self.assertEqual(wan, "wan-test")
            self.assertIs(run_in_namespace, mplane.net)
            self.assertTrue(
                any(
                    call.args[0] == ["nft", "-f", "-"]
                    and 'oifname "wan-test"' in call.kwargs["data"]
                    for call in commands.call_args_list
                )
            )
            firewall.assert_called_once_with([])
            self.assertFalse(self.namespace_config.exists())
            self.assertFalse(self.chroot_config.exists())
            return expected

        def launch(argv, **kwargs):
            self.assertEqual(
                argv,
                ["ip", "netns", "exec", mplane.NS, "chroot", str(self.root), "/usr/bin/warp-svc"],
            )
            self.assertEqual(self.namespace_config.read_text(), expected)
            self.assertEqual(self.chroot_config.read_text(), expected)
            return SimpleNamespace(pid=123)

        with (
            patch.object(mplane, "command", side_effect=command) as commands,
            patch.object(mplane, "fw4_install") as firewall,
            patch.object(mplane, "bootstrap_resolver", side_effect=choose) as resolver,
            patch.object(mplane, "select_resolver", return_value=(expected, choice)) as select,
            patch.object(mplane.subprocess, "Popen", side_effect=launch) as daemon,
            patch.object(mplane.time, "sleep"),
            patch.object(warp_control, "configure_protocol"),
        ):
            self.plane.start()
        if preferences is None:
            resolver.assert_called_once_with("wan-test", mplane.net)
            select.assert_not_called()
        else:
            resolver.assert_not_called()
            select.assert_called_once_with("wan-test", mplane.net, preferences)
            self.assertEqual(self.plane.warp_dns, choice)
        daemon.assert_called_once()
        self.assertEqual(self.plane.client_pid, 123)

    def test_new_plane_writes_primary_and_backup_before_warp_service(self):
        self.assert_startup_writes("nameserver 10.20.30.40\nnameserver 223.5.5.5\n")

    def test_new_plane_writes_backup_before_warp_service_when_primary_failed(self):
        self.assert_startup_writes("nameserver 223.5.5.5\n")

    def test_saved_dns_preferences_are_used_on_new_plane_without_changing_routing_digest(self):
        preferences = {"warp_dns_mode": "pinned", "warp_dns_pinned": "10.20.30.40"}
        baseline = {"main": {"apply_nonce": "old"}, "policies": []}
        edited = {"main": {"apply_nonce": "new", **preferences}, "policies": []}
        self.assertEqual(routing_digest(baseline, self.root), routing_digest(edited, self.root))
        self.assert_startup_writes(
            "nameserver 10.20.30.40\nnameserver 223.5.5.5\n", preferences=preferences
        )

    def test_reusing_owned_namespace_keeps_existing_resolvers(self):
        for path in (self.namespace_config, self.chroot_config):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("nameserver 10.0.0.2\n")
        self.plane.owned = True
        self.plane.dns_preferences = {"warp_dns_mode": "pinned", "warp_dns_pinned": "1.1.1.1"}
        with (
            patch.object(
                mplane, "command", return_value=SimpleNamespace(stdout="routepilot\n")
            ) as commands,
            patch.object(mplane, "bootstrap_resolver") as resolver,
            patch.object(mplane, "select_resolver") as select,
            patch.object(mplane.subprocess, "Popen") as daemon,
        ):
            self.plane.start()
        commands.assert_called_once_with(["ip", "netns", "list"])
        resolver.assert_not_called()
        select.assert_not_called()
        daemon.assert_not_called()
        self.assertEqual(self.namespace_config.read_text(), "nameserver 10.0.0.2\n")
        self.assertEqual(self.chroot_config.read_text(), "nameserver 10.0.0.2\n")

    def test_connected_plane_still_copies_client_resolver_to_namespace(self):
        vnet = "11111111-1111-4111-8111-111111111111"
        replies = {
            "vnet": "Currently selected: " + vnet,
            "status": "Status update: Connected",
            "settings": "WARP tunnel protocol: MASQUE",
        }
        self.chroot_config.parent.mkdir(parents=True, exist_ok=True)
        self.chroot_config.write_text("nameserver 127.0.2.2\n")
        with (
            patch.object(
                mplane,
                "cli",
                side_effect=lambda action, *_args, **_kwargs: SimpleNamespace(
                    stdout=replies[action]
                ),
            ),
            patch.object(mplane, "net", return_value=SimpleNamespace(stdout="CloudflareWARP")),
            patch.object(mplane, "bootstrap_resolver") as resolver,
            patch.object(self.plane, "optimize"),
        ):
            self.plane.connect(vnet)
        resolver.assert_not_called()
        self.assertEqual(self.namespace_config.read_text(), "nameserver 127.0.2.2\n")


if __name__ == "__main__":
    unittest.main()
