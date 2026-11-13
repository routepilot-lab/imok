import argparse
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "server_install", Path(__file__).with_name("install.py")
)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.xray = self.root / "xray-source"
        self.cf = self.root / "cf-source"
        self.xray.write_bytes(b"not executable test fixture xray")
        self.cf.write_bytes(b"not executable test fixture cloudflared")
        self.args = argparse.Namespace(
            xray=str(self.xray),
            xray_sha256=installer.digest(self.xray),
            cloudflared=str(self.cf),
            cloudflared_sha256=installer.digest(self.cf),
            hostname="node.example.com",
            dry_run=str(self.root / "output"),
        )
        self.token = "TEST_SECRET_TOKEN_01234567890123456789"

    def test_dry_run_does_not_execute_or_change_host_and_does_not_print_token(self):
        output = io.StringIO()
        with (
            patch.object(
                installer.subprocess, "run", side_effect=AssertionError("must not execute")
            ),
            contextlib.redirect_stdout(output),
        ):
            uri = installer.install(self.args, secret=lambda _: self.token)
        target = self.root / "output"
        self.assertNotIn(self.token, output.getvalue())
        self.assertTrue(uri.startswith("vless://"))
        self.assertIn("node.example.com:443", uri)
        config = json.loads((target / "config.json").read_text())
        inbound = config["inbounds"][0]
        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["port"], 18082)
        self.assertEqual(inbound["streamSettings"]["network"], "ws")
        self.assertEqual((target / "tunnel-token").read_text().strip(), self.token)
        self.assertEqual((target / "tunnel-token").stat().st_mode & 0o777, 0o600)
        self.assertEqual((target / "client-link.txt").stat().st_mode & 0o777, 0o600)
        units = [p.read_text() for p in target.glob("*.service")]
        self.assertEqual(len(units), 2)
        self.assertTrue(all("DynamicUser=yes" in unit for unit in units))
        self.assertNotIn(self.token, "\n".join(units))
        self.assertIn("--token-file %d/tunnel-token", "\n".join(units))
        self.assertEqual(
            json.loads((target / "install-state.json").read_text())["owner"], "routepilot-server"
        )
        self.assertTrue(inbound["streamSettings"]["wsSettings"]["path"].startswith("/rp-"))

    def test_checksum_failure_leaves_no_output(self):
        self.args.xray_sha256 = "0" * 64
        with self.assertRaises(ValueError):
            installer.install(self.args, secret=lambda _: self.token)
        self.assertFalse((self.root / "output").exists())

    def test_existing_directory_is_never_overwritten(self):
        target = self.root / "output"
        target.mkdir()
        sentinel = target / "existing-node"
        sentinel.write_text("preserve")
        with self.assertRaises(ValueError):
            installer.install(self.args, secret=lambda _: self.token)
        self.assertEqual(sentinel.read_text(), "preserve")

    def test_domain_and_token_cannot_inject_config_or_command(self):
        for hostname in (
            "a;systemctl.example.com",
            "https://node.example.com",
            "../node",
            "127.0.0.1",
            "node.example.com\nrm -rf /",
        ):
            self.args.hostname = hostname
            with self.assertRaises(ValueError):
                installer.install(self.args, secret=lambda _: self.token)
        self.args.hostname = "node.example.com"
        with self.assertRaises(ValueError):
            installer.install(self.args, secret=lambda _: "token\nExecStart=evil")
        self.assertFalse((self.root / "output").exists())

    def test_service_scope_and_no_firewall_or_old_service_commands(self):
        units = installer.units("/opt/routepilot-server")
        self.assertEqual(
            set(units), {"routepilot-server.service", "routepilot-cloudflared.service"}
        )
        content = "\n".join(units.values())
        self.assertNotIn("iptables", content)
        self.assertNotIn("nft", content)
        self.assertNotIn("xray.service", content)
        self.assertNotIn(
            "cloudflared.service\n", content.replace("routepilot-cloudflared.service", "")
        )
        self.assertIn("LoadCredential=config.json:/opt/routepilot-server/config.json", content)


if __name__ == "__main__":
    unittest.main()
