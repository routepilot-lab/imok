"""Automatic setup uses official-shaped synthetic archives and fake child processes."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "root/usr/lib/routepilot"))
import runtime_maintenance as m
import runtime_bootstrap as b
from public_errors import ControlError


class Bootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "owned"
        self.private = self.root / "private"
        self.run = self.root / "run"
        for p in (self.base, self.private, self.run):
            p.mkdir(mode=0o700)
        self.cfg = {"main": {"enabled": "0", "preparing": "0", "apply_nonce": "SYNTHETIC"}}
        self.calls = []
        self.job_updates = []
        self.warp = b"SYNTHETIC WARP PACKAGE".ljust(2048, b"X")
        self.dbus = b"SYNTHETIC DBUS PACKAGE"
        self.corrupt = False
        self.register = False
        self.archive = self.archive_bytes()
        for name, value in [
            ("BASE", self.base),
            ("PRIVATE", self.private),
            ("RUN", self.run),
            ("read_config", lambda: self.cfg),
            ("supported", lambda: True),
            ("inactive", lambda: None),
            ("run", self.command),
            ("download", self.download),
            ("test_runtime", lambda root, version=None: m.package_info(root)),
        ]:
            p = patch.object(m, name, value)
            p.start()
            self.addCleanup(p.stop)
        pin = {
            **b.ROOTFS,
            "size": len(self.archive),
            "sha256": hashlib.sha256(self.archive).hexdigest(),
        }
        p = patch.object(b, "ROOTFS", pin)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(b, "detect", return_value={"can_prepare": True, "issues": []})
        self.real_detect = p.get_original()[0]
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(
            b,
            "configure_apt",
            side_effect=lambda _, root: (root / "var/cache/apt/archives").mkdir(
                parents=True, exist_ok=True
            ),
        )
        self.real_configure = p.get_original()[0]
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(b, "prepare_devices")
        self.real_prepare_devices = p.get_original()[0]
        p.start()
        self.addCleanup(p.stop)

    def archive_bytes(self, arch=183):
        data = {
            "usr/lib/os-release": b"VERSION_CODENAME=bookworm\n",
            "usr/bin/apt-get": b"SYNTHETIC APT",
            "usr/bin/gpgv": b"SYNTHETIC GPG",
            "usr/share/keyrings/debian-archive-keyring.gpg": b"SYNTHETIC PUBLIC KEYRING",
            "var/lib/dpkg/status": b"Package: base-files\nStatus: install ok installed\nArchitecture: arm64\nVersion: 12\n",
        }
        header = bytearray(20)
        header[:6] = b"\x7fELF\x02\x01"
        header[18:20] = arch.to_bytes(2, "little")
        data["usr/bin/dpkg"] = bytes(header)
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tf:
            for name, raw in data.items():
                item = tarfile.TarInfo(name)
                item.size = len(raw)
                tf.addfile(item, io.BytesIO(raw))
        return out.getvalue()

    def download(self, relative, dest, limit):
        if relative.endswith("/Packages"):
            return (
                "Package: cloudflare-warp\nVersion: 2026.8.2115.0\nArchitecture: arm64\nSize: "
                + str(len(self.warp))
                + "\nSHA256: "
                + hashlib.sha256(self.warp).hexdigest()
                + "\nFilename: pool/bookworm/main/c/cloudflare-warp/cloudflare-warp_2026.8.2115.0_arm64.deb\n"
            ).encode()
        raise AssertionError("Unexpected network access")

    def command(self, args, *unused, **kw):
        self.calls.append((args, kw))
        if args[0] == "curl":
            archive = Path(args[args.index("--output") + 1])
            payload = self.warp if archive.name == "warp.deb" else self.archive
            self.job_updates.append(m.status()["job"])
            archive.write_bytes(payload[:64])
            self.job_updates.append(m.status()["job"])
            archive.write_bytes(payload)
        elif args[0] == "chroot":
            root = Path(args[1])
            cache = root / "var/cache/apt/archives"
            if args[2].endswith("apt-cache"):
                return self.download("dists/bookworm/main/binary-arm64/Packages", None, 0).decode()
            if "--print-uris" in args:
                return "\n".join(
                    f"'{url}' {name} {len(blob)} SHA256:{hashlib.sha256(blob).hexdigest()}"
                    for url, name, blob in [
                        (
                            m.SOURCE + "pool/cloudflare-warp_2026.8.2115.0_arm64.deb",
                            "cloudflare-warp_2026.8.2115.0_arm64.deb",
                            self.warp,
                        ),
                        (
                            self.debian_origin(root) + "/debian/pool/dbus_1_arm64.deb",
                            "dbus_1_arm64.deb",
                            self.dbus,
                        ),
                    ]
                )
            if "--download-only" in args:
                self.job_updates.append(m.status()["job"])
                (cache / "dbus_1_arm64.deb").write_bytes(b"BAD" if self.corrupt else self.dbus)
            elif "--no-download" in args:
                fd = kw["pass_fds"][0]
                os.write(fd, b"pmstatus:dpkg-exec:0.0000:Running dpkg\n")
                self.job_updates.append(m.status()["job"])
                os.write(fd, b"pmstatus:cloudflare-warp:52.0436:Unpacking cloudflare-warp (arm64)\n")
                self.job_updates.append(m.status()["job"])
                os.write(fd, b"pmstatus:cloudflare-warp:99.5475:Installed cloudflare-warp (arm64)\n")
                self.job_updates.append(m.status()["job"])
                for name in ["warp-cli", "warp-svc"]:
                    (root / "usr/bin" / name).write_text("SYNTHETIC BINARY")
                with (root / "var/lib/dpkg/status").open("a") as f:
                    f.write(
                        "\nPackage: cloudflare-warp\nStatus: install ok installed\nArchitecture: arm64\nVersion: 2026.8.2115.0\n"
                    )
                if self.register:
                    reg = root / "var/lib/cloudflare-warp"
                    reg.mkdir()
                    (reg / "reg.json").write_text("SYNTHETIC UNEXPECTED IDENTITY")
            elif args[-1] != "update":
                raise AssertionError(args)
        else:
            raise AssertionError(args)
        return ""

    @staticmethod
    def debian_origin(root):
        sources = root / "etc/apt/sources.list.d/imok-warp.list"
        if sources.exists():
            return sources.read_text().splitlines()[1].split("] ")[1].split("/debian")[0]
        return "https://deb.debian.org"

    def plan(self):
        with patch.object(
            m.shutil, "disk_usage", return_value=type("Disk", (), {"free": 10**11})()
        ):
            return m.make_plan({"operation": "online-install"})

    def test_detection_is_local_and_does_not_create_or_install(self):
        with patch.object(m, "supported", return_value=False):
            d = self.real_detect(m)
        self.assertFalse(d["can_prepare"])
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.base.iterdir()), [])

    def test_low_disk_is_detected_before_download(self):
        with (
            patch.object(b.os, "geteuid", return_value=0),
            patch.object(b.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()),
        ):
            d = self.real_detect(m)
        self.assertFalse(d["can_prepare"])
        self.assertTrue(any("空间不足" in x for x in d["issues"]))

    def test_prepare_downloads_and_verifies_but_never_installs(self):
        b.prepare(m)
        r = b.ready(m, verify=True)
        self.assertEqual(r["version"], "2026.8.2115.0")
        self.assertEqual(b.summary(m)["state"], "ready")
        self.assertFalse((self.base / "runtime").exists())
        self.assertFalse(any("--no-download" in a for a, _ in self.calls))
        self.assertTrue(any("--download-only" in a for a, _ in self.calls))
        self.assertEqual(self.cfg["main"]["enabled"], "0")

    def test_status_tracks_actual_base_bytes_and_other_stages_without_guessing_percent(self):
        real_progress = m.progress
        phases = []

        def report(*args, **kw):
            real_progress(*args, **kw)
            phases.append(m.status()["job"]["phase"])

        with patch.object(m, "progress", side_effect=report):
            b.prepare(m)
        self.assertEqual(
            phases,
            [
                "download_base",
                "verify_base",
                "verify_sources",
                "download_warp",
                "download_packages",
                "verify_packages",
                "ready",
            ],
        )
        empty, partial, warp_empty, warp_partial, packages = self.job_updates
        self.assertEqual(empty["phase"], "download_base")
        self.assertEqual(empty["bytes_done"], 0)
        self.assertEqual(partial["phase"], "download_base")
        self.assertEqual(partial["bytes_done"], 64)
        self.assertEqual(partial["bytes_total"], len(self.archive))
        self.assertNotIn("download_id", partial)
        self.assertEqual(warp_empty["bytes_done"], 0)
        self.assertEqual(warp_partial["bytes_done"], 64)
        self.assertEqual(warp_partial["bytes_total"], len(self.warp))
        self.assertEqual(packages["phase"], "download_packages")
        self.assertEqual(packages["bytes_done"], 0)
        self.assertEqual(packages["bytes_total"], len(self.dbus))
        self.assertFalse(m.status()["installed"])
        self.assertEqual(m.status()["job"]["state"], "success")

    def test_status_does_not_expose_download_id_or_use_symlink(self):
        ident = "a" * 32
        owned = self.base / (".runtime-prepare-" + ident)
        owned.mkdir(mode=0o700)
        private = self.root / "private-data"
        private.write_bytes(b"DO NOT READ")
        (owned / "base.tgz").symlink_to(private)
        m.progress("下载中", phase="download_base", download_id=ident)
        job = m.status()["job"]
        self.assertEqual(job["phase"], "download_base")
        self.assertNotIn("bytes_done", job)
        self.assertNotIn("download_id", job)

    def test_wrong_base_hash_is_rejected_before_apt(self):
        self.archive = b"NOT AN ARCHIVE".ljust(len(self.archive), b"X")
        with self.assertRaises(ControlError):
            b.prepare(m)
        self.assertFalse(any(a[0] == "chroot" for a, _ in self.calls))
        self.assertEqual(b.summary(m)["state"], "failed")
        self.assertFalse(list(self.base.glob(".runtime-prepare-*")))

    def test_download_failure_has_actionable_safe_message_and_no_install(self):
        with patch.object(m, "run", side_effect=ControlError("SYNTHETIC INTERNAL COMMAND")):
            with self.assertRaisesRegex(ControlError, "Debian 基础环境在线下载失败"):
                b.prepare(m)
        state = b.summary(m)
        self.assertEqual(state["state"], "failed")
        self.assertNotIn("SYNTHETIC INTERNAL", state["message"])
        self.assertFalse((self.base / "runtime").exists())
        self.assertFalse(list(self.base.glob(".runtime-prepare-*")))

    def test_base_fallback_uses_same_pinned_archive_and_private_token(self):
        seen = []

        def command(args, *unused, **kw):
            if args[0] != "curl" or args[args.index("--output") + 1].endswith("/warp.deb"):
                return self.command(args, **kw)
            seen.append(args[-1])
            path = Path(args[args.index("--output") + 1])
            if args[-1] == b.ROOTFS["url"]:
                self.assertEqual(args[args.index("--speed-limit") + 1], "40960")
                self.assertEqual(args[args.index("--speed-time") + 1], "45")
                path.write_bytes(b"NOT DEBIAN".ljust(b.ROOTFS["size"], b"X"))
                return ""
            if args[-1] == b.ROOTFS_PUBLIC_MIRROR + b.ROOTFS["sha256"]:
                self.assertFalse(path.exists())
                self.assertNotIn("input_data", kw)
                path.write_bytes(b"partial mirror")
                raise ControlError("SYNTHETIC failure")
            if args[-1] == b.ROOTFS_MIRROR_TOKEN:
                path.write_text('{"token":"fixture,token"}')
                return ""
            self.assertEqual(args[-1], b.ROOTFS_MIRROR + b.ROOTFS["sha256"])
            self.assertIn("--proto-redir", args)
            self.assertNotIn("--location-trusted", args)
            self.assertNotIn("--speed-limit", args)
            self.assertNotIn("--speed-time", args)
            self.assertFalse(any("fixture,token" in arg for arg in args))
            self.assertIn(b"Bearer fixture,token", kw["input_data"])
            self.assertFalse(path.exists())
            path.write_bytes(self.archive)
            return ""

        with patch.object(m, "run", side_effect=command):
            b.prepare(m)
        self.assertEqual(
            seen[:3],
            [b.ROOTFS["url"], b.ROOTFS_PUBLIC_MIRROR + b.ROOTFS["sha256"], b.ROOTFS_MIRROR_TOKEN],
        )
        self.assertEqual(b.summary(m)["state"], "ready")

    def test_public_mirror_needs_no_token_and_can_finish_preparation(self):
        def command(args, *unused, **kw):
            if args[0] == "curl" and args[-1] == b.ROOTFS["url"]:
                raise ControlError("SYNTHETIC primary failure")
            if args[0] == "curl" and args[args.index("--output") + 1].endswith("/base.tgz"):
                self.assertEqual(args[-1], b.ROOTFS_PUBLIC_MIRROR + b.ROOTFS["sha256"])
                self.assertNotIn("input_data", kw)
                self.assertIn("--proto-redir", args)
            return self.command(args, **kw)

        with patch.object(m, "run", side_effect=command):
            b.prepare(m)
        self.assertEqual(b.summary(m)["state"], "ready")
        self.assertFalse((self.base / "runtime").exists())

    def test_apt_download_retries_signed_debian_sources_without_installing(self):
        def configure(module, root):
            (root / "var/cache/apt/archives").mkdir(parents=True)
            (root / "var/lib/apt/lists-imok/partial").mkdir(parents=True)
            (root / "var/lib/apt/lists-imok/stale-index").touch()
            b.write_apt_sources(module, root, "https://deb.debian.org")

        def command(args, *unused, **kw):
            if args[0] == "chroot":
                root = Path(args[1])
                sources = (root / "etc/apt/sources.list.d/imok-warp.list").read_text()
                if args[-1] == "update":
                    self.assertIn("--error-on=any", args)
                if "--download-only" in args and b.DEBIAN_APT_MIRROR not in sources:
                    raise ControlError("SYNTHETIC Debian download failure")
                if b.DEBIAN_APT_MIRROR in sources:
                    self.assertFalse((root / "var/lib/apt/lists-imok/stale-index").exists())
                    self.assertIn(m.SOURCE, sources)
                    self.assertEqual(sources.count("signed-by="), 3)
            return self.command(args, **kw)

        with (
            patch.object(b, "configure_apt", side_effect=configure),
            patch.object(m, "run", side_effect=command),
        ):
            b.prepare(m)
        self.assertEqual(b.summary(m)["state"], "ready")
        self.assertFalse(any("--no-download" in a for a, _ in self.calls))

    def test_retry_cleans_only_marked_and_old_downloads(self):
        folders = [self.base / (".runtime-prepare-" + suffix * 32) for suffix in "abcd"]
        for p in folders:
            m.directory(p)
            (p / "base.tgz").write_bytes(b"partial")
        for p in folders[:2]:
            m.save(p / ".imok-download.json", {"owner": b.OWNER, "id": p.name[-32:]})
        for p in (folders[0], folders[2], folders[3]):
            os.utime(p, (0, 0))
        shutil.rmtree(folders[3])
        folders[3].symlink_to(folders[2], target_is_directory=True)
        b.prepare(m)
        self.assertFalse(folders[0].exists())
        self.assertTrue(all(p.exists() for p in folders[1:]))

    def test_base_fallback_cannot_bypass_hash_verification(self):
        def command(args, *unused, **kw):
            path = Path(args[args.index("--output") + 1])
            if args[-1] == b.ROOTFS["url"]:
                raise ControlError("SYNTHETIC failure")
            if args[-1] == b.ROOTFS_MIRROR_TOKEN:
                path.write_text('{"token":"fixture-token"}')
            else:
                path.write_bytes(b"X" * b.ROOTFS["size"])
            return ""

        with patch.object(m, "run", side_effect=command):
            with self.assertRaisesRegex(ControlError, "均未取得通过校验的文件"):
                b.prepare(m)
        self.assertEqual(b.summary(m)["state"], "failed")
        self.assertFalse(list(b.cache_root(m).iterdir()))

    def test_wrong_base_architecture_is_rejected(self):
        self.archive = self.archive_bytes(62)
        with patch.object(
            b,
            "ROOTFS",
            {
                **b.ROOTFS,
                "size": len(self.archive),
                "sha256": hashlib.sha256(self.archive).hexdigest(),
            },
        ):
            with self.assertRaises(ControlError):
                b.prepare(m)
        self.assertFalse(any(a[0] == "chroot" for a, _ in self.calls))

    def test_existing_runtime_is_never_overwritten_by_prepare(self):
        (self.base / "runtime").mkdir()
        (self.base / "runtime/identity").write_text("SYNTHETIC KEEP")
        with self.assertRaises(ControlError):
            b.prepare(m)
        self.assertEqual(self.calls, [])
        self.assertEqual((self.base / "runtime/identity").read_text(), "SYNTHETIC KEEP")

    def test_wrong_package_hash_prevents_prepared_state(self):
        self.corrupt = True
        with self.assertRaises(ControlError):
            b.prepare(m)
        self.assertIsNone(b.ready(m))
        self.assertFalse((self.base / "runtime").exists())
        self.assertFalse(any("--no-download" in a for a, _ in self.calls))

    def test_dependency_failure_keeps_verified_base_and_warp_for_retry(self):
        self.corrupt = True
        with self.assertRaises(ControlError):
            b.prepare(m)
        self.assertEqual(
            b.summary(m)["components"],
            {"debian": "cached", "warp": "cached", "dependencies": "missing"},
        )
        self.corrupt = False
        self.calls.clear()
        b.prepare(m)
        self.assertFalse(any(args[0] == "curl" for args, _ in self.calls))
        self.assertEqual(b.summary(m)["state"], "ready")

    def test_manual_refresh_redownloads_only_selected_component(self):
        b.prepare(m)
        for refresh, expected in (
            ("debian", ["base.tgz"]),
            ("warp", ["warp.deb"]),
            ("dependencies", []),
        ):
            with self.subTest(refresh=refresh):
                self.calls.clear()
                b.prepare(m, refresh=refresh)
                actual = [
                    Path(a[a.index("--output") + 1]).name for a, _ in self.calls if a[0] == "curl"
                ]
                self.assertEqual(actual, expected)
                self.assertEqual(
                    any("--download-only" in a and "--print-uris" not in a for a, _ in self.calls),
                    refresh == "dependencies",
                )
                self.assertEqual(b.summary(m)["state"], "ready")

    def test_existing_verified_download_from_prior_version_is_recovered(self):
        ident = "a" * 32
        work = self.base / (".runtime-prepare-" + ident)
        m.save(work / ".imok-download.json", {"owner": b.OWNER, "id": ident})
        (work / "base.tgz").write_bytes(self.archive)
        b.prepare(m)
        self.assertFalse(any(a[0] == "curl" and a[-1] == b.ROOTFS["url"] for a, _ in self.calls))
        self.assertEqual((b.cache_root(m) / "base.tgz").read_bytes(), self.archive)

    def test_warp_uses_official_download_hostname_when_repository_fetch_fails(self):
        expected = b.WARP_DOWNLOAD + "2026.8.2115.0"
        attempts = []

        def command(args, *unused, **kw):
            if args[0] == "curl" and args[args.index("--output") + 1].endswith("/warp.deb"):
                attempts.append(args[-1])
                if len(attempts) == 1:
                    raise ControlError("SYNTHETIC unreachable")
                self.assertEqual(args[-1], expected)
            return self.command(args, **kw)

        with patch.object(m, "run", side_effect=command):
            b.prepare(m)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(b.summary(m)["state"], "ready")

    def test_install_plan_requires_prepared_files(self):
        with self.assertRaises(ControlError):
            self.plan()

    def test_prepare_summary_does_not_rehash_large_files_on_poll(self):
        b.prepare(m)
        with patch.object(m, "fingerprint", side_effect=AssertionError("No hash work on poll")):
            self.assertEqual(b.summary(m)["state"], "ready")

    def test_missing_ready_cache_shows_retry_message_instead_of_breaking_status(self):
        b.prepare(m)
        r = b.ready(m)
        shutil.rmtree(b.stage_path(m, r["id"]))
        self.assertEqual(b.summary(m)["state"], "expired")
        self.assertIn("准备目录已丢失", b.summary(m)["message"])
        with self.assertRaises(b.StalePreparation):
            self.plan()
        self.assert_automatic_install_reuses_downloads()

    def test_tampered_prepared_tree_invalidates_confirmation_plan(self):
        b.prepare(m)
        r = b.ready(m)
        (b.stage_path(m, r["id"]) / "usr/bin/apt-get").write_text("CHANGED")
        with self.assertRaises(b.StalePreparation):
            self.plan()
        status = b.summary(m)
        self.assertEqual(status["state"], "expired")
        self.assertIn("准备文件校验失败", status["message"])
        self.assertEqual(set(status["components"].values()), {"cached"})
        self.assert_automatic_install_reuses_downloads()

    def test_automatic_install_rebuilds_tampered_tree_before_creating_plan(self):
        b.prepare(m)
        stage = b.stage_path(m, b.ready(m)["id"])
        (stage / "usr/bin/apt-get").write_text("CHANGED")
        self.assert_automatic_install_reuses_downloads()
        self.assertEqual((self.base / "runtime/usr/bin/apt-get").read_text(), "SYNTHETIC APT")

    def assert_automatic_install_reuses_downloads(self):
        self.calls.clear()
        with patch.object(m, "launch") as launch:
            m.first_install()
        m.job(*launch.call_args.args)
        state = m.status()
        self.assertTrue(state["installed"], state["job"])
        self.assertEqual(self.cfg["main"]["enabled"], "0")
        self.assertFalse(any(
            args[0] == "curl" or "--download-only" in args and "--print-uris" not in args
            for args, _ in self.calls
        ))
        self.assertEqual(sum("--no-download" in args for args, _ in self.calls), 1)

    def test_expired_preparation_requires_download_again(self):
        b.prepare(m)
        r = m.document(self.private / "online-ready.json")
        r["prepared_at"] -= b.READY_AGE + 1
        m.save(self.private / "online-ready.json", r)
        self.assertIsNone(b.ready(m))
        self.assertEqual(b.summary(m)["state"], "expired")

    def test_install_only_after_confirmation_and_in_offline_child(self):
        b.prepare(m)
        source = b.stage_path(m, b.ready(m)["id"])
        p = self.plan()
        with self.assertRaises(ControlError):
            m.execute(p["id"], "NO")
        self.assertFalse((self.base / "runtime").exists())
        m.execute(p["id"], "CONFIRM")
        self.assertEqual(m.package_info(self.base / "runtime")["version"], "2026.8.2115.0")
        self.assertEqual(b.stat.S_IMODE((self.base / "runtime/tmp").stat().st_mode), 0o1777)
        self.assertEqual(self.cfg["main"]["enabled"], "0")
        (install_args, options), = [(a, kw) for a, kw in self.calls if "--no-download" in a]
        self.assertTrue(options["isolated"])
        self.assertIn(f"APT::Status-Fd={options['pass_fds'][0]}", install_args)
        self.assertGreaterEqual(options["pass_fds"][0], 3)
        self.assertIn("Dpkg::Use-Pty=0", install_args)
        self.assertEqual(b.summary(m)["state"], "installed")
        self.assertFalse(source.exists())
        started, unpacking, installing = self.job_updates[-3:]
        self.assertEqual(started["install_percent"], 0)
        self.assertEqual(unpacking["install_percent"], 52)
        self.assertEqual(unpacking["install_step"], "unpack")
        self.assertEqual(installing["phase"], "install_packages")
        self.assertEqual(installing["install_percent"], 99)
        self.assertEqual(installing["install_step"], "configure")
        self.assertGreaterEqual(installing["elapsed_seconds"], 0)
        self.assertNotIn("apt_status_name", installing)
        self.assertNotIn("bytes_done", installing)
        self.assertEqual(m.status()["job"]["phase"], "installed")
        self.assertNotIn("install_percent", m.status()["job"])
        self.assertEqual(list(self.run.glob("warp-apt-*")), [])
        self.assertTrue(m.status()["installed"])
        with self.assertRaises(ControlError):
            m.execute(p["id"], "CONFIRM")

    def test_fresh_installer_queues_continuous_install_without_starting_tunnel(self):
        with patch.object(m, "launch") as launch:
            self.assertEqual(m.first_install(), {"queued": True})
            with self.assertRaisesRegex(ControlError, "已有维护任务"):
                m.first_install()
        launch.assert_called_once()
        self.assertFalse((self.base / "runtime").exists())
        m.job(*launch.call_args.args)
        self.assertTrue(m.status()["installed"])
        self.assertEqual(m.status()["job"]["phase"], "installed")
        self.assertTrue(all(job["state"] == "running" for job in self.job_updates))
        self.assertEqual(self.cfg["main"], {"enabled": "0", "preparing": "0", "apply_nonce": "SYNTHETIC"})
        with self.assertRaises(ControlError):
            m.job(*launch.call_args.args)
        with self.assertRaisesRegex(ControlError, "不会覆盖"):
            m.first_install()

    def test_fresh_install_rechecks_configuration_after_download_and_reuses_prepared_files(self):
        def prepare(*args, **kwargs):
            b_prepare(*args, **kwargs)
            self.cfg["main"]["apply_nonce"] = "CHANGED"

        b_prepare = b.prepare
        with patch.object(m, "launch") as launch:
            m.first_install()
            with patch.object(b, "prepare", side_effect=prepare):
                m.job(*launch.call_args.args)
            self.assertFalse(m.status()["installed"])
            self.assertEqual(m.status()["job"]["state"], "failed")
            self.assertIn("配置已变化", m.status()["job"]["message"])
            self.assertFalse(any("--no-download" in args for args, _ in self.calls))
            self.assertEqual(b.summary(m)["state"], "ready")
            self.calls.clear()
            m.first_install()
            m.job(*launch.call_args.args)
        self.assertTrue(m.status()["installed"])
        self.assertTrue(all("--no-download" in args for args, _ in self.calls))

    def test_web_fresh_install_requires_bound_confirmation(self):
        def request(ident, payload):
            raw = json.dumps({"request_id": ident, "payload": payload}).encode()
            slot = self.run / "requests" / ("runtime-input-" + ident + ".json")
            slot.write_bytes(raw)
            slot.chmod(0o600)
            return ident + ":" + hashlib.sha256(raw).hexdigest()

        m.prepare()
        with patch.object(m, "launch") as launch:
            with self.assertRaisesRegex(ControlError, "确认首次安装"):
                m.dispatch("first-install", request("a" * 32, {}))
            launch.assert_not_called()
            ident = "b" * 32
            result = m.dispatch("first-install", request(ident, {"confirmation": "CONFIRM"}))
        self.assertEqual(result, {"request_id": ident, "submitted": True})
        self.assertEqual(launch.call_args.args[0], "first-install")

    def test_failed_selftest_preserves_pristine_files_for_new_confirmation(self):
        b.prepare(m)
        original = b.ready(m, verify=True)
        source = b.stage_path(m, original["id"])
        plan = self.plan()
        self.calls.clear()
        with patch.object(m, "test_runtime", side_effect=ControlError("SYNTHETIC probe timeout")):
            with self.assertRaisesRegex(ControlError, "probe timeout"):
                m.execute(plan["id"], "CONFIRM")
        self.assertFalse((self.base / "runtime").exists())
        self.assertFalse((self.base / ".runtime-transaction.json").exists())
        self.assertFalse(list(self.base.glob(".runtime-stage-*")))
        self.assertFalse((source / "usr/bin/warp-cli").exists())
        self.assertEqual(b.ready(m, verify=True), original)
        self.assertEqual(b.summary(m)["state"], "ready")
        self.assertEqual(m.status()["job"]["state"], "failed")
        with self.assertRaises(ControlError):
            m.execute(plan["id"], "CONFIRM")
        retry = self.plan()
        self.assertNotEqual(plan["id"], retry["id"])
        m.execute(retry["id"], "CONFIRM")
        self.assertEqual([kw["isolated"] for args, kw in self.calls if "--no-download" in args],
                         [True, True])
        self.assertTrue(all("--no-download" in args for args, _ in self.calls))

    def test_post_install_cache_cleanup_does_not_fail_committed_install(self):
        b.prepare(m)
        plan = self.plan()
        with patch.object(b, "cleanup_ready", side_effect=OSError("SYNTHETIC cleanup failure")):
            m.execute(plan["id"], "CONFIRM")
        self.assertTrue(m.status()["installed"])
        self.assertEqual(b.summary(m)["state"], "installed")

    def test_package_tampering_after_confirmation_is_rejected(self):
        b.prepare(m)
        p = self.plan()
        r = b.ready(m)
        (b.stage_path(m, r["id"]) / "var/cache/apt/archives/dbus_1_arm64.deb").write_text("CHANGED")
        with self.assertRaises(ControlError):
            m.execute(p["id"], "CONFIRM")
        self.assertFalse((self.base / "runtime").exists())
        self.assertFalse(any("--no-download" in a for a, _ in self.calls))
        self.assertEqual(b.summary(m)["state"], "expired")
        self.assert_automatic_install_reuses_downloads()

    def test_unexpected_registration_during_first_install_is_not_committed(self):
        b.prepare(m)
        p = self.plan()
        self.register = True
        with self.assertRaises(ControlError):
            m.execute(p["id"], "CONFIRM")
        self.assertFalse((self.base / "runtime").exists())
        self.assertEqual(b.summary(m)["state"], "failed")

    def test_unknown_signing_key_is_rejected_before_apt_configuration(self):
        root = self.root / "stage"
        root.mkdir()
        with patch.object(m, "download", return_value=b"SYNTHETIC WRONG KEY"):
            with self.assertRaisesRegex(ControlError, "公钥已变化"):
                self.real_configure(m, root)
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(self.calls, [])

    def test_download_sources_keep_tls_signature_and_service_start_guards(self):
        root = self.root / "stage"
        root.mkdir()
        (root / "dev").mkdir()
        key = b"SYNTHETIC PUBLIC KEY"
        read_text = Path.read_text

        def public_file(p, *a, **kw):
            if str(p) == "/etc/ssl/certs/ca-certificates.crt":
                return "-----BEGIN CERTIFICATE-----\nSYNTHETIC PUBLIC CERTIFICATE\n-----END CERTIFICATE-----\n"
            if str(p) == "/etc/resolv.conf":
                return "nameserver 192.0.2.1\n"
            return read_text(p, *a, **kw)

        with (
            patch.object(m, "download", return_value=key),
            patch.object(m, "KEY_SHA256", hashlib.sha256(key).hexdigest()),
            patch.object(Path, "read_text", public_file),
        ):
            self.real_configure(m, root)
        b.prepare_devices.assert_called_once_with(root)
        sources = (root / "etc/apt/sources.list.d/imok-warp.list").read_text()
        self.assertEqual(sources.count("signed-by="), 3)
        self.assertEqual(sources.count("https://"), 3)
        self.assertNotIn("trusted=yes", sources)
        self.assertEqual((root / "usr/sbin/policy-rc.d").read_text(), "#!/bin/sh\nexit 101\n")
        args = b.apt_args(root)
        self.assertIn("Acquire::https::CaInfo=/etc/ssl/certs/ca-certificates.crt", args)
        self.assertNotIn("Acquire::https::Verify-Peer=false", args)
        self.assertEqual(
            self.calls, [], "Configuring sources must not launch package scripts or services"
        )

    def test_prepare_devices_creates_only_expected_character_nodes(self):
        root = self.root / "stage"
        (root / "dev").mkdir(parents=True)
        with patch.object(b.os, "mknod") as create:
            self.real_prepare_devices(root)
        self.assertEqual(
            [args[0].name for args, _ in create.call_args_list],
            ["null", "zero", "random", "urandom"],
        )
        self.assertTrue(all(b.stat.S_ISCHR(args[1]) for args, _ in create.call_args_list))

    def test_new_download_discards_only_our_unused_ready_cache(self):
        b.prepare(m)
        old = b.stage_path(m, b.ready(m)["id"])
        b.prepare(m)
        self.assertFalse(old.exists())
        self.assertIsNotNone(b.ready(m))

    def test_unknown_cache_owner_is_not_deleted(self):
        m.save(self.private / "online-ready.json", {"owner": "another-tool", "id": "a" * 32})
        with self.assertRaises(ControlError):
            b.cleanup_ready(m)
        self.assertTrue((self.private / "online-ready.json").exists())

    def test_untrusted_prepared_directory_does_not_trigger_rebuild_or_deletion(self):
        for change in ("symlink", "not-private", "foreign-marker"):
            with self.subTest(change=change):
                b.prepare(m)
                ident = b.ready(m)["id"]
                stage = b.stage_path(m, ident)
                relocated = self.root / ("outside-" + ident)
                if change == "symlink":
                    stage.rename(relocated)
                    stage.symlink_to(relocated, target_is_directory=True)
                elif change == "not-private":
                    stage.chmod(0o755)
                else:
                    m.save(stage / ".imok-bootstrap.json", {"owner": "another-tool", "id": ident})
                self.calls.clear()
                with patch.object(m, "launch") as launch:
                    m.first_install()
                m.job(*launch.call_args.args)
                self.assertEqual(m.status()["job"]["state"], "failed")
                self.assertFalse(m.status()["installed"])
                self.assertEqual(self.calls, [])
                with self.assertRaises(ControlError):
                    b.cleanup_ready(m)
                self.assertTrue(stage.exists())
                if change == "symlink":
                    stage.unlink()
                else:
                    stage.chmod(0o700)
                    m.save(stage / ".imok-bootstrap.json", b.ready_marker(ident))
                b.cleanup_ready(m)

    def test_retry_cleans_only_old_owned_orphan_ready_trees(self):
        b.prepare(m)
        old = b.stage_path(m, b.ready(m)["id"])
        orphan = b.stage_path(m, "a" * 32)
        young = b.stage_path(m, "b" * 32)
        foreign = b.stage_path(m, "c" * 32)
        link = b.stage_path(m, "d" * 32)
        mounted = b.stage_path(m, "e" * 32)
        for path in (orphan, young, foreign, mounted):
            m.save(path / ".imok-bootstrap.json", b.ready_marker(path.name[-32:]))
        m.save(foreign / ".imok-bootstrap.json", {"owner": "another-tool"})
        link.symlink_to(foreign, target_is_directory=True)
        for path in (old, orphan, foreign, mounted):
            os.utime(path, (0, 0))
        with m.job_lock(), patch.object(m, "mount_points", return_value=[str(mounted / "proc")]):
            b.cleanup_orphaned_ready(m)
        self.assertFalse(orphan.exists())
        self.assertTrue(all(p.exists() for p in (old, young, foreign, link, mounted)))


if __name__ == "__main__":
    unittest.main()
