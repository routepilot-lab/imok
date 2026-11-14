"""Release protection tests use a fake API and temporary files, never a live repository."""

import hashlib
import json
import os
import stat
import subprocess
import sys
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
            "docs/HARDWARE_ACCEPTANCE.md",
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
            "docs/WARP_RUNTIME.md",
            "docs/HARDWARE_ACCEPTANCE.md",
            "packages/luci-app-routepilot/INSTALL.zh-CN.md",
            "LICENSE",
            "THIRD_PARTY_NOTICES.txt",
        ]:
            (root / path).parent.mkdir(parents=True, exist_ok=True)
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
            installer = prefix + "imOK-1.2.3-1-first-install.run"
            self.assertEqual(
                archive.read(installer),
                release.first_install_script("1.2.3-1", archive.read(prefix + self.file.name)),
            )
            reinstall = prefix + "imOK-1.2.3-1-reinstall-cn-keep-settings.run"
            self.assertEqual(
                archive.read(reinstall),
                release.first_install_script("1.2.3-1", self.file.read_bytes(), reinstall=True),
            )
            for script in (installer, reinstall):
                self.assertEqual((archive.getinfo(script).external_attr >> 16) & 0o777, 0o755)
            self.assertIn(prefix + "packages/luci-app-routepilot/INSTALL.zh-CN.md", names)
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


class FirstInstall(unittest.TestCase):
    payload = b"\x1f\x8b\x08\x00fixture IPK\nexit 99\n\x00\xff"

    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="imok-installer-test-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.release_file = self.root / "openwrt_release"
        self.release_file.write_text(
            "DISTRIB_ID='OpenWrt'\n"
            "DISTRIB_RELEASE='24.10.4'\n"
            "DISTRIB_ARCH='aarch64_cortex-a53'\n"
            "DISTRIB_TARGET='mediatek/filogic'\n"
        )
        self.config = self.root / "routepilot-config"
        self.settings = self.root / "routepilot-settings"
        self.runtime = self.root / "routepilot-runtime-data"
        self.log = self.root / "opkg.log"
        self.warp_log = self.root / "warp.log"
        self.installed = self.root / "installed.ipk"
        self.opkg_status = self.root / "status"
        self.opkg_status.write_text("fixture installed packages\n")
        self.opkg_info = self.root / "opkg-info"
        self.opkg_info.mkdir()
        self.opkg_config = self.root / "opkg.conf"
        self.feeds = self.root / "feeds"
        self.feeds.mkdir()
        self.opkg_config.write_text(
            "dest root /\ndest ram /tmp\nlists_dir ext /var/opkg-lists\n"
            "option force_signature\noption no_check_certificate\noption force_depends\noption force_maintainer\n"
        )
        (self.feeds / "distfeeds.conf").write_text(
            "src/gz packages https://downloads.openwrt.org/releases/24.10.4/packages/aarch64_cortex-a53/packages\n"
            "src/gz core http://downloads.openwrt.org/releases/24.10.4/targets/mediatek/filogic/packages\n"
        )
        (self.feeds / "customfeeds.conf").write_text(
            "src/gz vendor https://firmware.example.test/packages\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.fake("uname", 'printf "%s\\n" "$FIXTURE_ARCH"')
        self.fake("id", 'printf "%s\\n" "$FIXTURE_UID"')
        self.fake(
            "uci",
            r"""if [ "$1" = -t ]; then
    staged=$2
    shift 2
fi
[ "$1" = -q ] || exit 94
case "$2:$3" in
    get:routepilot.main.enabled)
        if [ -f "$FIXTURE_COMMITTED" ]; then
            echo 0
        elif [ "$FIXTURE_DURING_PREFETCH" = enable ] && [ -f "$FIXTURE_PREFETCH_DONE" ]; then
            echo 1
        else
            echo "$FIXTURE_CONFIG_ENABLED"
        fi ;;
    get:routepilot.main.preparing)
        if [ -f "$FIXTURE_COMMITTED" ]; then echo 0; else echo "$FIXTURE_CONFIG_PREPARING"; fi ;;
    changes:routepilot) printf '%s' "$FIXTURE_CHANGES" ;;
    set:routepilot.main.enabled=0|set:routepilot.main.preparing=0)
        printf '%s\n' "$3" >> "$staged/routepilot" ;;
    commit:routepilot)
        [ "$FIXTURE_COMMIT_FAIL" = 0 ] || exit 94
        [ "$(wc -l < "$staged/routepilot")" -eq 2 ] || exit 94
        cp "$staged/routepilot" "$FIXTURE_COMMITTED"
        rm -f "$staged/routepilot"
        ;;
    *) exit 94 ;;
esac""",
        )
        self.fake(
            "ubus",
            '[ "$1" = call ] && [ "$2" = service ] && [ "$3" = list ] || exit 94\n'
            'printf "%s\\n" "$FIXTURE_SERVICES"',
        )
        self.fake(
            "opkg",
            r"""[ -z "${OFFLINE_ROOT:-}${IPKG_INSTROOT:-}${OPKG_KEYS:-}" ] || exit 96
offline=
if [ "$1" = --offline-root ]; then
    offline=$2
    shift 2
fi
if [ "$1" = -f ]; then
    [ "$3" = -d ] && [ "$4" = root ] || exit 95
    prefetch_root="${2%/*}/prefetch"
    if [ -n "$offline" ]; then
        [ "$offline" = "$prefetch_root" ] || exit 97
        cmp "$FIXTURE_OPKG_STATUS" "$offline/usr/lib/opkg/status" || exit 97
        [ -d "$offline/usr/lib/opkg/info" ] || exit 97
        if [ -n "$FIXTURE_PACKAGE_LIST" ]; then
            cmp "$FIXTURE_PACKAGE_LIST" "$offline/usr/lib/opkg/info/${FIXTURE_PACKAGE_LIST##*/}" || exit 97
            [ "$(ls -A "$offline/usr/lib/opkg/info")" = "${FIXTURE_PACKAGE_LIST##*/}" ] || exit 97
        fi
    else
        [ "$OPKG_CONF_DIR" = "$prefetch_root/etc/opkg" ] || exit 97
    fi
    cat "$2" "$prefetch_root/etc/opkg"/*.conf > "$FIXTURE_CONF_CAPTURE"
    cache=$(awk '$1 == "option" && $2 == "cache" {print $3}' "$2")
    [ "$cache" = "${2%/*}/cache" ] && [ -d "$cache" ] || exit 99
    lists=$(awk '$1 == "lists_dir" {print $3}' "$2")
    lists="$offline$lists"
    [ "$lists" = "$prefetch_root/lists" ] && [ -d "$lists" ] || exit 98
    shift 4
    if [ "$1" = update ]; then
        # A replacement feed must not inherit anything from a rejected attempt.
        if find "$lists" "$cache" "$prefetch_root/tmp" -mindepth 1 -print | grep -q .; then
            printf '%s\n' 'Fixture detected stale files across mirrors' >&2
            exit 98
        fi
        touch "$lists/fixture" "$lists/fixture.sig"
    fi
fi
if [ "$1" = --noaction ]; then
    [ -n "$offline" ] && [ "$2" = --download-only ] && [ "$3" = install ] && [ -f "$4" ] || exit 90
    printf '%s\n' prefetch >> "$FIXTURE_LOG"
    printf '%s\n' fixture-dependency > "$cache/fixture-first.ipk"
    if grep -q 'mirrors.tuna.tsinghua.edu.cn' "$FIXTURE_CONF_CAPTURE" && [ "$FIXTURE_TUNA_FAIL" = 1 ]; then
        printf '%s\n' 'Failed to download python3-email' >&2
        exit 1
    fi
    if grep -q 'mirrors.nju.edu.cn' "$FIXTURE_CONF_CAPTURE" && [ "$FIXTURE_NJU_FAIL" = 1 ]; then
        exit 1
    fi
    [ "$FIXTURE_PREFETCH_FAIL" = 0 ] || exit 1
    printf '%s\n' fixture-dependency > "$cache/fixture-ready.ipk"
    case "$FIXTURE_DURING_PREFETCH" in
        runtime) mkdir "$FIXTURE_RUNTIME" ;;
        config) touch "$FIXTURE_CONFIG" ;;
    esac
    touch "$FIXTURE_PREFETCH_DONE"
    exit 0
fi
printf '%s\n' "$1" >> "$FIXTURE_LOG"
case "$1" in
    status)
        [ "$2" = luci-app-routepilot ] || exit 91
        if [ "$FIXTURE_INSTALLED" = 1 ] ||
                { [ "$FIXTURE_DURING_PREFETCH" = package ] && [ -f "$FIXTURE_PREFETCH_DONE" ]; } ||
                { [ -f "$FIXTURE_CAPTURE" ] && [ "$FIXTURE_CONFIRM_INSTALL" = 1 ]; }; then
            printf '%s\n' 'Package: luci-app-routepilot' 'Status: install user installed'
            printf 'Version: %s\n' "$FIXTURE_INSTALLED_VERSION"
            if [ -n "$FIXTURE_EXTRA_STATUS" ]; then printf '\n%s\n' "$FIXTURE_EXTRA_STATUS"; fi
        fi
        ;;
    update)
        if grep -q 'mirrors.tuna.tsinghua.edu.cn' "$FIXTURE_CONF_CAPTURE" && [ "$FIXTURE_TUNA_BAD_SIGNATURE" = 1 ]; then
            touch "$cache/.partial" "$prefetch_root/tmp/failed"
            printf '%s\n' 'Signature check passed.' 'Signature check failed.' >&2
            exit 0
        fi
        printf '%s\n' "$FIXTURE_UPDATE_OUTPUT" >&2
        exit "$FIXTURE_UPDATE_CODE"
        ;;
    install)
        [ -z "$offline" ] && [ "$#" -eq 2 ] && [ -f "$2" ] || exit 92
        [ -f "$cache/fixture-ready.ipk" ] || exit 90
        [ "$FIXTURE_EXPECT_OFF" = 0 ] || [ -f "$FIXTURE_COMMITTED" ] || exit 94
        cp "$2" "$FIXTURE_CAPTURE"
        exit "$FIXTURE_INSTALL_CODE"
        ;;
    *) exit 93 ;;
esac""",
        )
        self.fake(
            "routepilot-runtime-first-install",
            r"""[ "$#" -eq 0 ] && [ -f "$FIXTURE_CAPTURE" ] || exit 89
[ "$(tail -n 1 "$FIXTURE_LOG")" = status ] || exit 89
printf '%s\n' first-install >> "$FIXTURE_WARP_LOG"
printf '%s\n' "$FIXTURE_WARP_OUTPUT"
exit "$FIXTURE_WARP_CODE"
""",
        )
        self.env = {
            **os.environ,
            "PATH": os.pathsep.join((str(self.bin), os.defpath, "/sbin", "/usr/sbin")),
            "TMPDIR": str(self.root),
            "FIXTURE_UID": "0",
            "FIXTURE_ARCH": "aarch64",
            "FIXTURE_INSTALLED": "0",
            "FIXTURE_CONFIRM_INSTALL": "1",
            "FIXTURE_INSTALLED_VERSION": "1.2.3-1",
            "FIXTURE_EXTRA_STATUS": "",
            "FIXTURE_CONFIG_ENABLED": "0",
            "FIXTURE_CONFIG_PREPARING": "0",
            "FIXTURE_COMMITTED": str(self.root / "committed"),
            "FIXTURE_COMMIT_FAIL": "0",
            "FIXTURE_CHANGES": "",
            "FIXTURE_SERVICES": "{}",
            "FIXTURE_EXPECT_OFF": "0",
            "FIXTURE_UPDATE_CODE": "0",
            "FIXTURE_TUNA_FAIL": "0",
            "FIXTURE_TUNA_BAD_SIGNATURE": "0",
            "FIXTURE_NJU_FAIL": "0",
            "FIXTURE_PREFETCH_FAIL": "0",
            "FIXTURE_UPDATE_OUTPUT": "Updated list of available packages in /test\nSignature check passed.",
            "FIXTURE_INSTALL_CODE": "0",
            "FIXTURE_LOG": str(self.log),
            "FIXTURE_WARP_LOG": str(self.warp_log),
            "FIXTURE_WARP_OUTPUT": '{"queued": true}',
            "FIXTURE_WARP_CODE": "0",
            "FIXTURE_RUNTIME": str(self.runtime),
            "FIXTURE_CONFIG": str(self.config),
            "FIXTURE_DURING_PREFETCH": "",
            "FIXTURE_PREFETCH_DONE": str(self.root / "prefetch-done"),
            "FIXTURE_CAPTURE": str(self.installed),
            "FIXTURE_CONF_CAPTURE": str(self.root / "effective.conf"),
            "FIXTURE_OPKG_STATUS": str(self.opkg_status),
            "FIXTURE_PACKAGE_LIST": "",
        }

    def fake(self, command, body):
        file = self.bin / command
        file.write_text("#!/bin/sh\n" + body + "\n")
        file.chmod(0o700)

    def run_script(self, *, corrupt=False, reinstall=False, **environment):
        packaged = release.first_install_script("1.2.3-1", self.payload, reinstall=reinstall)
        self.assertTrue(packaged.startswith(b"#!/bin/sh\n"))
        self.assertTrue(packaged.endswith(self.payload))
        header = packaged[: -len(self.payload)]
        for original, temporary in (
            (b"/etc/openwrt_release", self.release_file),
            (b"cp /usr/lib/opkg/status", "cp " + str(self.opkg_status)),
            (b"for package_list in /usr/lib/opkg/info/*.list", f"for package_list in '{self.opkg_info}'/*.list"),
            (b"/etc/config/routepilot", self.config),
            (b"/etc/routepilot", self.settings),
            (b"/opt/routepilot", self.runtime),
            (b"/usr/libexec/routepilot-runtime-first-install", self.bin / "routepilot-runtime-first-install"),
            (b"/usr/bin/python3", sys.executable),
            (b"/etc/config/.imok-before-reinstall.XXXXXX", self.root / "backup.XXXXXX"),
            (b"/etc/opkg.conf", self.opkg_config),
            (b"for conf in /etc/opkg/*.conf", f"for conf in '{self.feeds}'/*.conf"),
            (b"/tmp/imok-first-install.XXXXXX", self.root / "imok-first-install.XXXXXX"),
        ):
            self.assertIn(original, header)
            header = header.replace(original, os.fsencode(temporary))
        payload = self.payload[:-1] + b"\x01" if corrupt else self.payload
        script = self.root / "imOK first install.run"
        script.write_bytes(header + payload)
        script.chmod(0o755)
        result = subprocess.run(
            [str(script)],
            env={**self.env, **environment},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertFalse(list(self.root.glob("imok-first-install.*")))
        return result

    def package_actions(self):
        if self.log.exists():
            return self.log.read_text().splitlines()
        return []

    def test_installs_exact_payload_after_updating_package_lists(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "status", "install", "status"])
        self.assertEqual(self.installed.read_bytes(), self.payload)
        self.assertEqual(self.warp_log.read_text().splitlines(), ["first-install"])
        self.assertIn("WARP 安装任务已提交", result.stdout)

    def test_warp_enqueue_failure_does_not_undo_a_confirmed_plugin_install(self):
        secret = "fixture-private-token"
        for code, output in (
            ("1", '{"error": "fixture"}'),
            ("1", json.dumps({"error": "路由器可用空间不足，自动准备需要至少 1.6 GB；\x1b\n" + secret})),
            ("1", json.dumps({"error": "路由器可用空间不足，自动准备需要至少 1.6 GB", "extra": secret})),
            ("0", "{}"),
            ("0", "invalid"),
            ("1", '{"queued": true}'),
        ):
            with self.subTest(code=code, output=output):
                for path in (self.log, self.installed, self.warp_log):
                    path.unlink(missing_ok=True)
                result = self.run_script(FIXTURE_WARP_CODE=code, FIXTURE_WARP_OUTPUT=output)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.installed.read_bytes(), self.payload)
                self.assertEqual(self.warp_log.read_text().splitlines(), ["first-install"])
                self.assertIn("WARP 后台安装任务未获确认", result.stdout)
                self.assertIn("WARP 安装与更新", result.stdout)
                self.assertNotIn(output, result.stdout)
                self.assertNotIn(secret, result.stdout + result.stderr)
                self.assertNotIn("\x1b", result.stdout + result.stderr)
                self.assertNotIn("WARP 安装任务已提交", result.stdout)

    def test_warp_preflight_reports_actionable_space_and_memory_errors(self):
        reasons = "路由器可用空间不足，自动准备需要至少 1.6 GB；路由器可用内存不足，请释放内存后重试"
        result = self.run_script(
            FIXTURE_WARP_CODE="1", FIXTURE_WARP_OUTPUT=json.dumps({"error": reasons}, ensure_ascii=False)
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("WARP 安装暂未开始：" + reasons, result.stdout)
        self.assertIn("WARP 安装与更新", result.stdout)
        self.assertNotIn("WARP 安装任务已提交", result.stdout)
        self.assertEqual(self.installed.read_bytes(), self.payload)

    def test_staging_directory_failure_reports_space_or_permissions(self):
        self.fake("mkdir", 'printf "%s\\n" "mkdir: No space left on device" >&2\nexit 1')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("无法创建依赖暂存目录", result.stderr)
        self.assertIn("/tmp 剩余空间和写入权限", result.stderr)
        self.assertEqual(self.package_actions(), ["status"])
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.warp_log.exists())

    def test_retained_data_prevents_automatic_warp_installation(self):
        for path in (self.config, self.settings, self.runtime):
            with self.subTest(path=path):
                path.touch()
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(self.warp_log.exists())
                self.assertTrue(path.exists())
                path.unlink()
                self.installed.unlink()

    def test_reinstall_never_enqueues_warp_even_without_retained_files(self):
        result = self.run_script(reinstall=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.warp_log.exists())

    def test_existing_runtime_symlink_prevents_automatic_warp_installation(self):
        self.runtime.symlink_to(self.root / "unavailable")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.runtime.is_symlink())
        self.assertFalse(self.warp_log.exists())

    def test_runtime_appearing_during_dependency_preparation_is_preserved(self):
        result = self.run_script(FIXTURE_DURING_PREFETCH="runtime")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.runtime.is_dir())
        self.assertFalse(self.warp_log.exists())

    def test_inactive_config_appearing_during_dependency_preparation_prevents_warp_enqueue(self):
        result = self.run_script(FIXTURE_DURING_PREFETCH="config")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.config.exists())
        self.assertFalse(self.warp_log.exists())

    def test_active_config_appearing_during_dependency_preparation_stops_before_install(self):
        result = self.run_script(FIXTURE_DURING_PREFETCH="config", FIXTURE_CONFIG_ENABLED="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("准备期间检测到已启用", result.stderr)
        self.assertTrue(self.config.exists())
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.warp_log.exists())

    def test_existing_config_enabled_during_dependency_preparation_stops_before_install(self):
        self.config.touch()
        result = self.run_script(FIXTURE_DURING_PREFETCH="enable")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("准备期间检测到已启用", result.stderr)
        self.assertTrue(self.config.exists())
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.warp_log.exists())

    def test_package_appearing_during_dependency_preparation_stops_before_install(self):
        result = self.run_script(FIXTURE_DURING_PREFETCH="package")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("imOK 已被安装", result.stderr)
        self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "status"])
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.warp_log.exists())

    def test_update_failure_stops_before_package_installation(self):
        result = self.run_script(
            FIXTURE_UPDATE_CODE="8",
            FIXTURE_UPDATE_OUTPUT="Failed to download the package list",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), ["status", "update", "update", "update"])
        self.assertFalse(self.installed.exists())

    def test_all_signature_failures_stop_install_even_when_refresh_exits_zero(self):
        result = self.run_script(
            FIXTURE_UPDATE_OUTPUT="Signature check passed.\nSignature check failed."
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), ["status", "update", "update", "update"])
        self.assertIn("未找到可用且通过签名校验的软件源", result.stderr)
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.warp_log.exists())

    def test_signature_failure_uses_independently_verified_mirror(self):
        result = self.run_script(FIXTURE_TUNA_BAD_SIGNATURE="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("清华镜像索引签名校验失败，已拒绝该来源", result.stdout)
        self.assertNotIn("Fixture detected stale files", result.stdout + result.stderr)
        self.assertEqual(self.package_actions(), ["status", "update", "update", "prefetch", "status", "install", "status"])
        effective = (self.root / "effective.conf").read_text()
        self.assertIn("https://mirrors.nju.edu.cn/openwrt/releases/24.10.4/", effective)
        self.assertNotIn("mirrors.tuna.tsinghua.edu.cn", effective)

    def test_missing_signature_rejects_attempt_even_with_other_signed_indexes(self):
        result = self.run_script(
            FIXTURE_UPDATE_CODE="1",
            FIXTURE_UPDATE_OUTPUT=(
                "Signature check passed.\n"
                "Failed to download https://example.test/Packages.sig, wget returned 4."
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), ["status", "update", "update", "update"])
        self.assertIn("未找到可用且通过签名校验的软件源", result.stderr)
        self.assertFalse(self.installed.exists())

    def test_partial_feed_failure_installs_from_fresh_signed_indexes(self):
        result = self.run_script(
            FIXTURE_UPDATE_CODE="1",
            FIXTURE_UPDATE_OUTPUT=(
                "Failed to download the package list\n"
                "Updated list of available packages in /test\nSignature check passed."
            ),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("部分软件源未更新", result.stdout)
        self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "status", "install", "status"])

    def test_dependency_download_failure_switches_mirror_before_install(self):
        result = self.run_script(FIXTURE_TUNA_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "update", "prefetch", "status", "install", "status"])
        effective = (self.root / "effective.conf").read_text()
        self.assertIn("https://mirrors.nju.edu.cn/openwrt/releases/24.10.4/", effective)
        self.assertNotIn("mirrors.tuna.tsinghua.edu.cn", effective)

    def test_prefetch_stages_only_regular_installed_file_lists(self):
        installed_files = self.opkg_info / "existing-package.list"
        installed_files.write_text("/usr/bin/existing-package\n")
        (self.opkg_info / "existing-package.postinst").write_text("exit 99\n")
        (self.opkg_info / "link.list").symlink_to(installed_files)
        (self.opkg_info / "directory.list").mkdir()

        result = self.run_script(FIXTURE_PACKAGE_LIST=str(installed_files), FIXTURE_TUNA_FAIL="1")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.package_actions().count("prefetch"), 2)
        self.assertEqual(self.package_actions().count("install"), 1)

    def test_mirror_failures_fall_back_to_official_release(self):
        result = self.run_script(FIXTURE_TUNA_FAIL="1", FIXTURE_NJU_FAIL="1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.package_actions().count("prefetch"), 3)
        self.assertEqual(self.package_actions().count("install"), 1)
        self.assertIn(
            "https://downloads.openwrt.org/releases/24.10.4/",
            (self.root / "effective.conf").read_text(),
        )

    def test_temporary_config_only_rewrites_official_release_feeds(self):
        paths = [self.opkg_config, *sorted(self.feeds.glob("*.conf"))]
        before = {path: path.read_bytes() for path in paths}
        result = self.run_script(
            OFFLINE_ROOT="/unused", IPKG_INSTROOT="/unused", OPKG_KEYS="/unused"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        effective = (self.root / "effective.conf").read_text()
        mirror = "https://mirrors.tuna.tsinghua.edu.cn/openwrt/releases/24.10.4/"
        self.assertIn(mirror + "packages/aarch64_cortex-a53/packages", effective)
        self.assertIn(mirror + "targets/mediatek/filogic/packages", effective)
        self.assertIn("src/gz vendor https://firmware.example.test/packages", effective)
        self.assertIn("option check_signature\n", effective)
        self.assertIn("dest root /\n", effective)
        self.assertNotIn("/var/opkg-lists", effective)
        self.assertNotIn("force_signature", effective)
        self.assertNotIn("no_check_certificate", effective)
        self.assertNotIn("force_depends", effective)
        self.assertNotIn("force_maintainer", effective)
        self.assertEqual({path: path.read_bytes() for path in paths}, before)

    def test_install_failure_is_reported_to_istore(self):
        result = self.run_script(FIXTURE_INSTALL_CODE="8")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "status", "install"])
        self.assertFalse(self.warp_log.exists())

    def test_success_requires_confirmed_installed_version(self):
        for settings in (
            {"FIXTURE_CONFIRM_INSTALL": "0"},
            {"FIXTURE_INSTALLED_VERSION": "1.2.3-2"},
        ):
            with self.subTest(settings=settings):
                self.log.unlink(missing_ok=True)
                self.installed.unlink(missing_ok=True)
                result = self.run_script(**settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "status", "install", "status"])
                self.assertFalse(self.warp_log.exists())

    def test_stale_target_stanza_does_not_confirm_install_of_another_version(self):
        result = self.run_script(
            FIXTURE_INSTALLED_VERSION="1.2.3-2",
            FIXTURE_EXTRA_STATUS=(
                "Package: luci-app-routepilot\nVersion: 1.2.3-1\n"
                "Status: install prefer,user not-installed"
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未确认目标版本已完成安装", result.stderr)
        self.assertFalse(self.warp_log.exists())

    def test_stale_older_stanza_does_not_hide_installed_target(self):
        result = self.run_script(
            FIXTURE_EXTRA_STATUS=(
                "Package: luci-app-routepilot\nVersion: 1.2.3-0\n"
                "Status: install prefer,user not-installed"
            ),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.warp_log.exists())

    def test_existing_installation_does_not_refresh_lists_or_install(self):
        result = self.run_script(FIXTURE_INSTALLED="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), ["status"])

    def test_active_config_without_package_stops_before_update(self):
        for option in ("ENABLED", "PREPARING"):
            with self.subTest(option=option):
                self.config.touch()
                self.log.unlink(missing_ok=True)
                result = self.run_script(**{f"FIXTURE_CONFIG_{option}": "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.package_actions(), ["status"])

    def test_reinstall_backs_up_retained_config_and_sets_only_two_switches(self):
        saved = b"config main 'main'\n option exit 'fixture'\n"
        self.config.write_bytes(saved)
        result = self.run_script(
            reinstall=True, FIXTURE_CONFIG_PREPARING="1", FIXTURE_EXPECT_OFF="1"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.package_actions(), ["status", "update", "prefetch", "status", "status", "install", "status"])
        backups = list(self.root.glob("backup.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), saved)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(
            (self.root / "committed").read_text().splitlines(),
            ["routepilot.main.enabled=0", "routepilot.main.preparing=0"],
        )

    def test_reinstall_does_not_touch_config_when_update_or_safety_check_fails(self):
        self.config.touch()
        for settings in (
            {"FIXTURE_SERVICES": '{"routepilot":{"instances":{"main":{}}}}'},
            {"FIXTURE_CHANGES": "routepilot.main.exit='pending'"},
            {"FIXTURE_UPDATE_OUTPUT": "Signature check failed."},
            {"FIXTURE_PREFETCH_FAIL": "1"},
        ):
            with self.subTest(settings=settings):
                self.log.unlink(missing_ok=True)
                result = self.run_script(reinstall=True, FIXTURE_CONFIG_ENABLED="1", **settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("install", self.package_actions())
                self.assertFalse((self.root / "committed").exists())
                self.assertFalse(list(self.root.glob("backup.*")))

    def test_reinstall_does_not_replace_an_installed_package(self):
        result = self.run_script(reinstall=True, FIXTURE_INSTALLED="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), ["status"])

    def test_corrupt_payload_does_not_refresh_lists_or_install(self):
        result = self.run_script(corrupt=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(set(self.package_actions()) <= {"status"})
        self.assertFalse(self.installed.exists())

    def test_unsupported_architecture_does_not_call_opkg(self):
        result = self.run_script(FIXTURE_ARCH="x86_64")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.package_actions(), [])


if __name__ == "__main__":
    unittest.main()
