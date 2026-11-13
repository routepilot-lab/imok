"""Prepare an official Debian + WARP installation without touching live runtime.

Downloads and APT's download-only operation may run before confirmation. Package
installation scripts run only after the separate five-minute confirmation plan.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from public_errors import ControlError

ROOTFS = {
    "commit": "3b26c3995c7fae47d95ba678632a3520e97054f7",
    "url": "https://raw.githubusercontent.com/debuerreotype/docker-debian-artifacts/3b26c3995c7fae47d95ba678632a3520e97054f7/bookworm/slim/oci/blobs/rootfs.tar.gz",
    "sha256": "2d66c782e4847b8495b17a4b86be1159aa13d140fe4fd17cb1d536953d5114c6",
    "size": 28144358,
}
OWNER = "imok-official-bootstrap-v1"
READY_AGE = 86400


def detect(m):
    machine = os.uname().machine
    try:
        release = Path("/etc/openwrt_release").read_text()
    except OSError:
        release = ""
    match = re.search(r"DISTRIB_RELEASE=['\"]?([^'\"\s]+)", release)
    version = match[1] if match else "未识别"
    path = m.BASE
    while not path.exists() and path.parent != path:
        path = path.parent
    free = shutil.disk_usage(path).free
    available = None
    try:
        text = Path("/proc/meminfo").read_text()
        value = re.search(r"^MemAvailable:\s+(\d+) kB", text, re.M)
        if value:
            available = int(value[1]) * 1024
    except OSError:
        pass
    missing = [name for name in ("curl", "chroot") if shutil.which(name) is None]
    issues = []
    if not m.supported():
        issues.append("目前自动安装只支持 ARM64 OpenWrt 24.10")
    if os.geteuid() != 0:
        issues.append("需要路由器管理员权限")
    if missing:
        issues.append("缺少必要系统组件，请通过正常插件安装流程补齐依赖")
    if free < m.MAX_UNPACKED + 64 * 1024 * 1024:
        issues.append("路由器可用空间不足，自动准备需要至少 1.6 GB")
    if available is not None and available < 192 * 1024 * 1024:
        issues.append("路由器可用内存不足，请释放内存后重试")
    return {
        "architecture": machine,
        "system": "OpenWrt " + version,
        "free_bytes": free,
        "available_memory": available,
        "can_prepare": not issues,
        "issues": issues,
        "source": "Debian 官方 ARM64 基础环境 + Cloudflare 官方 WARP",
    }


def stage_path(m, ident):
    if not isinstance(ident, str) or not re.fullmatch("[a-f0-9]{32}", ident):
        raise ControlError("自动安装记录无效，请重新准备")
    return m.BASE / (".runtime-ready-" + ident)


def ready(m, verify=False):
    value = m.document(m.PRIVATE / "online-ready.json")
    if not value:
        return None
    if value.get("owner") != OWNER or value.get("state") != "ready":
        return None
    root = stage_path(m, value.get("id"))
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.geteuid():
        raise ControlError("已准备环境的目录异常，未继续安装")
    marker = m.document(root / ".imok-bootstrap.json")
    if marker != {"owner": OWNER, "id": value["id"], "source_sha256": ROOTFS["sha256"]}:
        raise ControlError("已准备环境的来源标记无效，未继续安装")
    if not -5 <= time.time() - value.get("prepared_at", 0) < READY_AGE:
        return None
    if verify and m.fingerprint(root) != value.get("fingerprint"):
        raise ControlError("已下载文件发生变化，请重新准备；未安装")
    return value


def summary(m):
    state = m.document(m.PRIVATE / "online.json", {"state": "idle"})
    result = {k: state[k] for k in ("state", "message", "version") if k in state}
    if result.get("state") == "ready":
        try:
            value = ready(m)
        except ControlError as e:
            return {"state": "failed", "message": str(e)}
        if value:
            result.update(
                version=value["version"],
                download_bytes=value["download_bytes"],
                prepared_at=value["prepared_at"],
            )
        else:
            result.update(state="expired", message="准备记录已过期，请重新下载准备")
    return result


def cleanup_ready(m):
    # Only an explicitly requested re-download discards our own unused cache.
    value = m.document(m.PRIVATE / "online-ready.json")
    if not value:
        return
    if value.get("owner") != OWNER:
        raise ControlError("准备目录归属不能确认，未清理")
    root = stage_path(m, value.get("id"))
    if root.exists() or root.is_symlink():
        if root.is_symlink() or m.document(root / ".imok-bootstrap.json") != {
            "owner": OWNER,
            "id": value["id"],
            "source_sha256": ROOTFS["sha256"],
        }:
            raise ControlError("准备目录归属不能确认，未清理")
        shutil.rmtree(root)
    (m.PRIVATE / "online-ready.json").unlink()


def validate_base(root):
    try:
        text = (root / "usr/lib/os-release").read_text()
        with (root / "usr/bin/dpkg").open("rb") as f:
            header = f.read(20)
        if (
            "VERSION_CODENAME=bookworm" not in text
            or header[:6] != b"\x7fELF\x02\x01"
            or int.from_bytes(header[18:20], "little") != 183
        ):
            raise ValueError()
        for name in (
            "usr/bin/apt-get",
            "usr/bin/gpgv",
            "usr/share/keyrings/debian-archive-keyring.gpg",
        ):
            if not (root / name).is_file():
                raise ValueError()
        for name in ("dev", "proc", "sys", "run", "tmp"):
            p = root / name
            if p.is_symlink():
                raise ValueError()
            p.mkdir(exist_ok=True)
    except (OSError, ValueError):
        raise ControlError("官方基础环境结构或架构校验失败，未执行程序") from None


def configure_apt(m, root):
    keyfile = m.PRIVATE / "bootstrap-key.download"
    try:
        key = m.download("pubkey.gpg", keyfile, 65536)
        if hashlib.sha256(key).hexdigest() != m.KEY_SHA256:
            raise ControlError("Cloudflare 官方公钥已变化，需审核后才能安装")
        m.internal(root, "usr/share/keyrings/imok-cloudflare.asc", key.decode())
    finally:
        keyfile.unlink(missing_ok=True)
    # The minimal official image has APT/GPG but may not yet contain ca-certificates.
    # Bootstrap TLS from the router's already installed public ca-bundle, never
    # disable certificate verification or fetch an unsigned dependency.
    try:
        ca = Path("/etc/ssl/certs/ca-certificates.crt").read_text()
    except OSError:
        raise ControlError("缺少系统证书包，未关闭 HTTPS 验证；请先补齐插件依赖") from None
    if "-----BEGIN CERTIFICATE-----" not in ca:
        raise ControlError("系统证书包不可用，未继续下载")
    m.internal(root, "etc/ssl/certs/ca-certificates.crt", ca)
    sources = (
        "deb [arch=arm64 signed-by=/usr/share/keyrings/imok-cloudflare.asc] "
        + m.SOURCE
        + " bookworm main\n"
    )
    sources += "deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian bookworm main\n"
    sources += "deb [arch=arm64 signed-by=/usr/share/keyrings/debian-archive-keyring.gpg] https://deb.debian.org/debian-security bookworm-security main\n"
    m.internal(root, "etc/apt/sources.list.d/imok-warp.list", sources)
    m.internal(root, "usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n", 0o755)
    m.internal(root, "etc/resolv.conf", Path("/etc/resolv.conf").read_text())
    for name, minor in [("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)]:
        p = root / "dev" / name
        if p.exists() or p.is_symlink():
            raise ControlError("基础环境设备目录异常")
        os.mknod(p, stat.S_IFCHR | 0o666, os.makedev(1, minor))
    (root / "var/lib/apt/lists-imok/partial").mkdir(parents=True, exist_ok=True)


def apt_args(root):
    return [
        "chroot",
        str(root),
        "/usr/bin/apt-get",
        "-o",
        "Dir::Etc::sourcelist=sources.list.d/imok-warp.list",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        "Dir::State::lists=lists-imok",
        "-o",
        "Acquire::Retries=1",
        "-o",
        "Acquire::http::Timeout=30",
        "-o",
        "Acquire::https::Timeout=30",
        "-o",
        "Acquire::https::CaInfo=/etc/ssl/certs/ca-certificates.crt",
    ]


def install_args(version):
    return [
        "--yes",
        "--no-remove",
        "--no-install-recommends",
        "install",
        "cloudflare-warp=" + version,
        "dbus",
        "ca-certificates",
    ]


def packages(m, root, candidate):
    cache = root / "var/cache/apt/archives"
    rows = []
    for p in sorted(cache.glob("*.deb")):
        if (
            p.is_symlink()
            or not p.is_file()
            or not re.fullmatch(r"[A-Za-z0-9+._~%:-]{1,200}\.deb", p.name)
        ):
            raise ControlError("下载的软件包清单异常")
        rows.append({"name": p.name, "size": p.stat().st_size, "sha256": m.file_hash(p)})
    warp = [r for r in rows if r["name"].startswith("cloudflare-warp_")]
    if (
        len(warp) != 1
        or warp[0]["size"] != candidate["size"]
        or warp[0]["sha256"] != candidate["sha256"]
    ):
        raise ControlError("WARP 下载结果与官方目录不一致，未准备安装")
    if len(rows) > 256 or len(json.dumps(rows)) > 60000:
        raise ControlError("软件下载清单超限")
    return rows


def phase(m, message):
    m.save(m.PRIVATE / "online.json", {"state": "preparing", "message": message})
    m.progress(message)


def download_step(m, args, timeout, failure):
    try:
        return m.run(args, timeout)
    except ControlError:
        raise ControlError(failure) from None


def prepare(m):
    with m.job_lock():
        m.inactive()
        env = detect(m)
        if not env["can_prepare"]:
            raise ControlError("；".join(env["issues"]))
        if (m.BASE / "runtime").exists() or (m.BASE / "runtime").is_symlink():
            raise ControlError("已检测到运行环境，请使用更新；不会覆盖首次安装")
        if (m.BASE / ".runtime-transaction.json").exists():
            raise ControlError("请先核对上次未完成的维护")
        cleanup_ready(m)
        ident = uuid.uuid4().hex
        work = m.BASE / (".runtime-prepare-" + ident)
        m.directory(work)
        root = work / "root"
        root.mkdir(mode=0o700)
        target = stage_path(m, ident)
        try:
            phase(m, "环境检查通过，正在自动下载 Debian 官方基础文件；尚未安装")
            archive = work / "base.tgz"
            download_step(
                m,
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--proto",
                    "=https",
                    "--connect-timeout",
                    "15",
                    "--max-time",
                    "600",
                    "--max-filesize",
                    str(ROOTFS["size"]),
                    "--output",
                    str(archive),
                    ROOTFS["url"],
                ],
                610,
                "Debian 官方基础环境下载失败，请检查路由器访问官方来源的网络及存储空间；尚未安装",
            )
            if archive.stat().st_size != ROOTFS["size"] or m.file_hash(archive) != ROOTFS["sha256"]:
                raise ControlError("官方基础环境下载不完整或校验不符，请重试；未安装")
            phase(m, "基础文件校验通过，正在自动准备所需环境")
            m.unpack(archive, root)
            validate_base(root)
            catalog = work / "Packages"
            candidate = m.candidate(
                m.download("dists/bookworm/main/binary-arm64/Packages", catalog, 8388608).decode()
            )
            configure_apt(m, root)
            phase(m, "正在验证官方签名并下载 WARP 与必要组件；尚未执行软件安装")
            download_step(
                m,
                apt_args(root) + ["update"],
                180,
                "官方软件源连接或签名校验失败，请检查网络、系统时间和证书；尚未安装",
            )
            download_step(
                m,
                apt_args(root) + ["--download-only"] + install_args(candidate["version"]),
                1200,
                "WARP 或必要组件下载未完成，请检查官方源网络和存储空间后重试；尚未安装",
            )
            downloaded = packages(m, root, candidate)
            m.inactive()
            if (m.BASE / "runtime").exists():
                raise ControlError("准备期间检测到现有环境，未覆盖；请刷新核对")
            m.save(
                root / ".imok-bootstrap.json",
                {"owner": OWNER, "id": ident, "source_sha256": ROOTFS["sha256"]},
            )
            os.rename(root, target)
            m.sync_directory(m.BASE)
            record = {
                "owner": OWNER,
                "state": "ready",
                "id": ident,
                "version": candidate["version"],
                "candidate": candidate,
                "prepared_at": time.time(),
                "packages": downloaded,
                "fingerprint": m.fingerprint(target),
                "download_bytes": ROOTFS["size"] + sum(p["size"] for p in downloaded),
            }
            m.save(m.PRIVATE / "online-ready.json", record)
            m.save(
                m.PRIVATE / "online.json",
                {
                    "state": "ready",
                    "version": candidate["version"],
                    "message": "文件已自动下载并校验，尚未安装；请确认安装到路由器",
                },
            )
            m.progress("文件已准备好，尚未安装；请点击确认安装到路由器", "success")
        except Exception as e:
            text = (
                str(e)
                if isinstance(e, ControlError)
                else "自动准备未完成，请检查软件源网络和存储空间后重试"
            )
            m.save(m.PRIVATE / "online.json", {"state": "failed", "message": text})
            if target.exists() and not target.is_symlink():
                shutil.rmtree(target)
            raise ControlError(text) from None
        finally:
            if work.exists() and not work.is_symlink():
                shutil.rmtree(work)


def plan(m):
    value = ready(m, verify=True)
    if not value:
        raise ControlError("请先让系统自动下载并准备安装文件")
    _root = stage_path(m, value["id"])
    need = 256 * 1024 * 1024 + sum(r["size"] for r in value["packages"]) * 4
    return {"ready_id": value["id"], "version": value["version"], "required_bytes": need}


def install(m, plan, stage):
    value = ready(m, verify=True)
    if not value or value["id"] != plan.get("ready_id") or value["version"] != plan.get("version"):
        raise ControlError("安装文件或版本已变化，请重新检查后确认")
    source = stage_path(m, value["id"])
    os.rename(source, stage)
    m.sync_directory(m.BASE)
    m.save(m.PRIVATE / "online-ready.json", {**value, "state": "consumed"})
    m.save(m.PRIVATE / "online.json", {"state": "installing", "version": value["version"]})
    if packages(m, stage, value["candidate"]) != value["packages"]:
        raise ControlError("缓存软件包已变化，未执行安装")
    m.progress("已确认安装，正在独立环境中安装已校验组件；未启动隧道")
    m.run(apt_args(stage) + ["--no-download"] + install_args(value["version"]), 1200, isolated=True)
    registration = stage / "var/lib/cloudflare-warp"
    if registration.is_symlink() or registration.exists() and any(registration.iterdir()):
        raise ControlError("首次安装出现非预期注册资料，未提交")
    m.test_runtime(stage, value["version"])
