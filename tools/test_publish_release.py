"""Release protection tests use a fake API and temporary files, never a live repository."""

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import publish_release as release


class ReleaseProtection(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.file = Path(self.folder.name) / "example.ipk"
        self.file.write_bytes(b"reviewed package")
        self.asset = {
            "name": self.file.name,
            "size": self.file.stat().st_size,
            "state": "uploaded",
            "digest": "sha256:" + release.digest(self.file),
        }
        self.head = "a" * 40

    def test_changed_attachment_is_rejected(self):
        release.verify_assets({"assets": [self.asset]}, [self.file])
        self.file.write_bytes(b"changed")
        with self.assertRaises(AssertionError):
            release.verify_assets({"assets": [self.asset]}, [self.file])

    def test_existing_tag_is_never_rewritten(self):
        with patch.object(
            release, "api", return_value={"object": {"type": "commit", "sha": "b" * 40}}
        ) as api:
            with self.assertRaises(AssertionError):
                release.publish("0.2.35-6", [self.file], self.head, "notes")
            self.assertEqual(api.call_count, 1)

    def test_upload_failure_leaves_draft_unpublished(self):
        calls = []

        def api(method, path, payload=None, **kwargs):
            calls.append((method, path, payload))
            if method == "GET":
                return None
            if path == "/releases":
                self.assertTrue(payload["draft"])
                return {
                    "id": 1,
                    "assets": [],
                    "upload_url": "https://uploads.github.com/repos/routepilot-lab/imok/releases/1/assets{?name}",
                }
            raise RuntimeError("simulated upload failure")

        with patch.object(release, "api", side_effect=api):
            with self.assertRaises(RuntimeError):
                release.publish("0.2.35-6", [self.file], self.head, "notes")
        self.assertFalse(any(method == "PATCH" for method, _, _ in calls))

    def test_existing_public_release_is_only_verified(self):
        public = {"draft": False, "prerelease": False, "assets": [self.asset]}
        with patch.object(
            release, "api", side_effect=[{"object": {"type": "commit", "sha": self.head}}, public]
        ) as api:
            self.assertEqual(release.publish("0.2.35-6", [self.file], self.head, "notes"), public)
            self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))

    def test_public_archive_allowlist_excludes_private_and_internal_material(self):
        public = [
            "README.md",
            "packages/luci-app-routepilot/root/usr/lib/routepilot/client_access.py",
            "docs/CLIENT_ACCESS.md",
            "docs/images/imok-clients.png",
            "docs/quality/warp-maintenance-0.2.37-1.md",
        ]
        private = [
            ".local/receipt.json",
            "releases/runtime.tgz",
            "AGENTS.md",
            "scripts/opus.sh",
            "docs/client-access/LIVE_ENROLLMENT.md",
            "docs/quality/warp-router-update-20261110.md",
            "docs/quality/warp-auto-install-20261110.md",
            "packages/luci-app-routepilot/.env",
            "packages/luci-app-routepilot/test.key",
            "../README.md",
        ]
        self.assertEqual(release.public_paths(public + private), sorted(public))

    def test_install_kit_is_deterministic_and_every_attachment_has_a_checksum(self):
        root = Path(self.folder.name)
        (root / "docs/releases").mkdir(parents=True)
        for path in [
            "docs/INSTALL_KIT.md",
            "docs/releases/v1.2.3-1.md",
            "LICENSE",
            "THIRD_PARTY_NOTICES.txt",
        ]:
            (root / path).write_text("reviewed public " + path)
        source = root / "imOK-1.2.3-1-public-source.zip"
        source.write_bytes(b"public source fixture")
        with patch.object(release, "ROOT", root):
            kit = release.install_kit(root, "1.2.3-1", self.file, source)
            first = kit.read_bytes()
            self.assertEqual(
                release.install_kit(root, "1.2.3-1", self.file, source).read_bytes(), first
            )
        with zipfile.ZipFile(kit) as archive:
            self.assertIsNone(archive.testzip())
            prefix = "imOK-1.2.3-1/"
            names = set(archive.namelist())
            checks = archive.read(prefix + "SHA256SUMS").decode().splitlines()
            self.assertEqual(len(checks), len(names) - 1)
            for line in checks:
                checksum, name = line.split("  ", 1)
                self.assertEqual(checksum, hashlib.sha256(archive.read(prefix + name)).hexdigest())
            self.assertFalse(any("runtime" in name or ".local" in name for name in names))

    def test_prerelease_never_promotes_the_stable_latest(self):
        calls = []
        draft = {
            "id": 1,
            "target_commitish": self.head,
            "draft": True,
            "prerelease": True,
            "assets": [],
            "upload_url": "https://uploads.github.com/repos/routepilot-lab/imok/releases/1/assets{?name}",
        }

        def api(method, path, payload=None, **kwargs):
            calls.append((method, path, payload))
            if method == "GET" and path.startswith("/git/ref/"):
                return (
                    {"object": {"type": "commit", "sha": self.head}}
                    if any(c[0] == "PATCH" for c in calls)
                    else None
                )
            if method == "GET" and path.startswith("/releases/tags/"):
                return None
            if method == "POST" and path == "/releases":
                return dict(draft)
            if method == "POST" and path.startswith("https://uploads.github.com/"):
                draft["assets"] = [self.asset]
                return self.asset
            if method == "GET":
                return dict(draft)
            if method == "PATCH":
                draft.update(payload)
                return dict(draft)
            raise AssertionError("Unexpected API request")

        with patch.object(release, "api", side_effect=api):
            result = release.publish(
                "0.2.37-1",
                [self.file],
                self.head,
                "reviewed notes",
                name="imOK 2.0 · WARP 安装与更新",
                prerelease=True,
            )
        self.assertTrue(result["prerelease"])
        self.assertFalse(result["draft"])
        for method, _, payload in calls:
            if method in ("POST", "PATCH") and payload:
                self.assertTrue(payload["prerelease"])
                self.assertEqual(payload["make_latest"], "false")

    def test_existing_prerelease_is_only_verified(self):
        public = {"draft": False, "prerelease": True, "assets": [self.asset]}
        with patch.object(
            release, "api", side_effect=[{"object": {"type": "commit", "sha": self.head}}, public]
        ) as api:
            self.assertEqual(
                release.publish("0.2.37-1", [self.file], self.head, "notes", prerelease=True),
                public,
            )
            self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))

    def test_existing_stable_cannot_be_relabelled_as_prerelease(self):
        public = {"draft": False, "prerelease": False, "assets": [self.asset]}
        with patch.object(
            release, "api", side_effect=[{"object": {"type": "commit", "sha": self.head}}, public]
        ) as api:
            with self.assertRaises(AssertionError):
                release.publish("0.2.37-1", [self.file], self.head, "notes", prerelease=True)
            self.assertTrue(all(c.args[0] == "GET" for c in api.call_args_list))

    def test_release_metadata_does_not_accept_implicit_flags(self):
        good = {"release": {"name": "imOK 2.0 · WARP 安装与更新", "prerelease": True}}
        self.assertEqual(release.release_options("0.2.37-1", good), good["release"])
        for value in ["true", 1, None]:
            with self.assertRaises(AssertionError):
                release.release_options(
                    "0.2.37-1", {"release": {"name": "imOK test", "prerelease": value}}
                )
        with self.assertRaises(AssertionError):
            release.release_options(
                "0.2.37-1",
                {"release": {"name": "imOK test", "prerelease": True, "make_latest": True}},
            )

    def test_dirty_workspace_is_rejected_before_any_build_or_upload(self):
        with patch.object(release.subprocess, "check_output", return_value=b" M README.md"):
            with self.assertRaisesRegex(AssertionError, "Commit reviewed source"):
                release.prepare()


if __name__ == "__main__":
    unittest.main()
