# Synthetic unit-test addresses; no live router, tenant or user configuration.
from input_fixture import stage
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import warp_control as w


class WarpTest(unittest.TestCase):
    def test_mdm_source_distinguishes_owned_file_without_leaking_contents(self):
        import mplane

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / "state.json"
            w.configure_protocol(root, "MASQUE", "test-team")
            settings = SimpleNamespace(stdout="(mdm) WARP tunnel protocol: MASQUE")
            with (
                patch.object(mplane, "ROOT", root),
                patch.object(mplane, "cli", return_value=settings),
                patch.object(w, "STATE", state),
                patch.object(w, "read_config", return_value={"main": {"warp_protocol": "MASQUE"}}),
            ):
                result = w.refresh()
                self.assertEqual(result["source"], "routepilot")
                self.assertNotIn("test-team", json.dumps(result))
                (root / "var/lib/cloudflare-warp/routepilot-protocol-owner.json").unlink()
                self.assertEqual(w.refresh()["source"], "external_mdm")

    def test_only_effective_protocol_is_projected(self):
        out = w.parse_settings(
            "(network policy) WARP tunnel protocol: MASQUE\nsecret credential: do-not-project\nOrganization: personal-name"
        )
        self.assertEqual(out["protocol"], "MASQUE")
        self.assertTrue(out["managed"])
        self.assertNotIn("secret", json.dumps(out))
        self.assertNotIn("personal", json.dumps(out))
        self.assertEqual(
            w.parse_settings("(local policy) WARP tunnel protocol: WireGuard")["source"],
            "local_policy",
        )
        with self.assertRaises(ValueError):
            w.parse_settings("WARP tunnel protocol: unknown")

    def test_auto_and_managed_mismatch_never_issue_set_or_exit_commands(self):
        calls = []

        def cli(*args):
            calls.append(args)
            return SimpleNamespace(stdout="(network policy) WARP tunnel protocol: MASQUE")

        self.assertEqual(w.ensure_protocol("auto", cli)["protocol"], "MASQUE")
        self.assertEqual(calls, [("settings",)])
        calls.clear()
        with self.assertRaises(ValueError):
            w.ensure_protocol("WireGuard", cli)
        self.assertEqual(calls, [("settings",)])

    def test_consumer_mismatch_never_persists_cli_protocol_override(self):
        calls = []

        def cli(*args):
            calls.append(args)
            return SimpleNamespace(stdout="(local policy) WARP tunnel protocol: MASQUE")

        with self.assertRaises(ValueError):
            w.ensure_protocol("WireGuard", cli)
        self.assertEqual(calls, [("settings",)])

    def test_mdm_auto_leaves_existing_external_file_byte_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mdm = root / "var/lib/cloudflare-warp/mdm.xml"
            mdm.parent.mkdir(parents=True)
            mdm.write_text("external management")
            w.configure_protocol(root, "auto")
            self.assertEqual(mdm.read_text(), "external management")
            with self.assertRaises(ValueError):
                w.configure_protocol(root, "WireGuard")
            self.assertEqual(mdm.read_text(), "external management")

    def test_mdm_only_owns_its_file_and_can_return_to_organization(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            mdm = root / "var/lib/cloudflare-warp/mdm.xml"
            identity = mdm.parent / "reg.json"
            identity.parent.mkdir(parents=True)
            identity.write_text("private-registration")
            w.configure_protocol(root, "WireGuard", "test-team")
            self.assertIn("<string>wireguard</string>", mdm.read_text())
            self.assertEqual(mdm.stat().st_mode & 0o777, 0o600)
            w.configure_protocol(root, "MASQUE", "test-team")
            self.assertIn("<string>masque</string>", mdm.read_text())
            w.configure_protocol(root, "auto")
            self.assertFalse(mdm.exists())
            self.assertEqual(identity.read_text(), "private-registration")

    def test_external_edit_or_symlink_is_never_destroyed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            w.configure_protocol(root, "WireGuard")
            mdm = root / "var/lib/cloudflare-warp/mdm.xml"
            mdm.write_text("external edit")
            w.configure_protocol(root, "auto")
            self.assertEqual(mdm.read_text(), "external edit")
            with self.assertRaises(ValueError):
                w.configure_protocol(root, "MASQUE")

    def test_journal_before_write_can_recover_old_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            w.configure_protocol(root, "WireGuard")
            real = w.atomic

            def fail(path, text, *args):
                if path.name == "mdm.xml":
                    raise OSError("simulated interruption")
                return real(path, text, *args)

            with patch.object(w, "atomic", side_effect=fail), self.assertRaises(OSError):
                w.configure_protocol(root, "MASQUE")
            w.configure_protocol(root, "MASQUE")
            self.assertIn(
                "<string>masque</string>", (root / "var/lib/cloudflare-warp/mdm.xml").read_text()
            )

    def test_invalid_protocol_or_org_is_rejected(self):
        for bad in ["wireguard", "wg", "auto;connect", ""]:
            with self.assertRaises(ValueError):
                w.preference(bad)
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(ValueError):
            w.configure_protocol(Path(folder), "WireGuard", "<xml>")

    def test_readonly_observation_binds_runtime_and_does_not_echo_trace(self):
        import mplane
        import worker_backend

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            now = w.time.time()
            status = dict(
                state="running",
                time=now,
                runtime_id="r",
                generation="g",
                applied_nonce="n",
                exit="a",
                backend="masque",
            )
            (root / "status.json").write_text(json.dumps(status))
            with (
                patch.object(w, "RUN", root),
                patch.object(w, "OBSERVATION", root / "obs"),
                patch.object(
                    mplane,
                    "net",
                    return_value=SimpleNamespace(
                        stdout="ip=8.8.8.8\nloc=US\nprivate-field=secret\n"
                    ),
                ) as runner,
                patch.object(worker_backend, "curl") as backup,
            ):
                result = w.observe()
                self.assertEqual(result["country"], "US")
                self.assertEqual(result["runtime_id"], "r")
                self.assertNotIn("secret", json.dumps(result))
                backup.assert_not_called()
                self.assertEqual(runner.call_count, 1)

    def test_observation_refuses_expired_main_state(self):
        import mplane

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "status.json").write_text(json.dumps(dict(state="running", time=0)))
            with (
                patch.object(w, "RUN", root),
                patch.object(mplane, "net") as runner,
                self.assertRaises(ValueError),
            ):
                w.observe()
            runner.assert_not_called()

    def test_protocol_choice_does_not_change_capture_rules(self):
        import masque_config as c

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "china.txt").write_text("1.0.1.0/24\n")
            cfg = dict(
                main=dict(
                    exit="11111111-1111-4111-8111-111111111111",
                    scope="lan",
                    lan_network="10.77.50.0/24",
                ),
                policies=[],
            )
            first = c.compile_config(cfg, root)
            for protocol in ["MASQUE", "WireGuard", "auto"]:
                cfg["main"]["warp_protocol"] = protocol
                result = c.compile_config(cfg, root)
                self.assertEqual(result["nft"], first["nft"])
                self.assertEqual(result["vnet"], first["vnet"])

    def test_organization_save_can_only_write_its_own_keys(self):
        import runtime
        import worker_backend

        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "input"
            file.write_text(json.dumps({"organization": "new-team"}))
            cfg = {
                "main": {
                    "organization": "old",
                    "apply_nonce": "old-nonce",
                    "enabled": "0",
                    "exit": "unchanged",
                    "dns_foreign": "1.1.1.1",
                }
            }
            with (
                patch.object(w, "ORGANIZATION_INPUT", file),
                patch.object(w, "read_config", return_value=cfg),
                patch.object(worker_backend, "guard", return_value=contextlib.nullcontext()),
                patch.object(runtime, "command") as cmd,
            ):
                out = w.save_organization(stage(w.ORGANIZATION_INPUT))
            self.assertEqual(out["organization"], "new-team")
            self.assertEqual(cfg["main"]["exit"], "unchanged")
            self.assertFalse(file.exists())
            self.assertEqual(
                [c.args[0] for c in cmd.call_args_list],
                [
                    ["uci", "set", "routepilot.main.organization=new-team"],
                    ["uci", "set", "routepilot.main.apply_nonce=old-nonce"],
                    ["uci", "commit", "routepilot"],
                ],
            )

    def test_organization_save_refuses_live_account_change_and_foreign_fields(self):
        import runtime
        import worker_backend

        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "input"
            for payload in [{"organization": "other"}, {"organization": "same", "exit": "bad"}]:
                file.write_text(json.dumps(payload))
                with (
                    patch.object(w, "ORGANIZATION_INPUT", file),
                    patch.object(
                        w,
                        "read_config",
                        return_value={"main": {"organization": "same", "enabled": "1"}},
                    ),
                    patch.object(worker_backend, "guard", return_value=contextlib.nullcontext()),
                    patch.object(runtime, "command") as cmd,
                    self.assertRaises(ValueError),
                ):
                    w.save_organization(stage(w.ORGANIZATION_INPUT))
                cmd.assert_not_called()
                self.assertFalse(file.exists())


if __name__ == "__main__":
    unittest.main()
