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
from urllib.parse import urlsplit

from public_errors import ControlError

ROOTFS = {
    "commit": "3b26c3995c7fae47d95ba678632a3520e97054f7",
    "url": "https://raw.githubusercontent.com/debuerreotype/docker-debian-artifacts/3b26c3995c7fae47d95ba678632a3520e97054f7/bookworm/slim/oci/blobs/rootfs.tar.gz",
    "sha256": "2d66c782e4847b8495b17a4b86be1159aa13d140fe4fd17cb1d536953d5114c6",
    "size": 28144358,
}
ROOTFS_MIRROR = "https://docker.m.daocloud.io/v2/library/debian/blobs/sha256:"
ROOTFS_PUBLIC_MIRROR = "https://dockerproxy.net/v2/library/debian/blobs/sha256:"
HTTPS_REDIRECTS = ("--location", "--proto-redir", "=https", "--max-redirs", "3")
ROOTFS_MIRROR_TOKEN = (
    "https://m.daocloud.io/auth/token"
    "?service=docker.m.daocloud.io&scope=repository:library/debian:pull"
)
DEBIAN_APT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn"
DEBIAN_APT_BACKUP = "https://mirrors.nju.edu.cn"
WARP_DOWNLOAD = "https://downloads.cloudflareclient.com/v1/download/bookworm-arm/version/"
COMPONENTS = ("debian", "warp", "dependencies")
DEB_NAME = r"[A-Za-z0-9+._~%:-]{1,200}\.deb"
MAX_PACKAGES = 512
OWNER = "imok-official-bootstrap-v1"
READY_AGE = 86400
ABANDONED_AGE = 7200


class OnlineSelfTestError(ControlError):
    """The disposable install failed its probe; the prepared files may be retried."""


class StalePreparation(ControlError):
    """Rebuild the temporary environment from independently verified downloads."""


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


def ready_marker(ident):
    return {"owner": OWNER, "id": ident, "source_sha256": ROOTFS["sha256"]}


def ready(m, verify=False):
    value = m.document(m.PRIVATE / "online-ready.json")
    if not value:
        return None
    if value.get("owner") != OWNER or value.get("state") != "ready":
        return None
    root = stage_path(m, value.get("id"))
    try:
        info = root.lstat()
    except FileNotFoundError:
        raise StalePreparation("准备目录已丢失，请重新准备；仍有效的下载文件会复用") from None
    if not private_directory(info):
        raise ControlError("已准备环境的目录异常，未继续安装")
    marker = m.document(root / ".imok-bootstrap.json")
    if marker != ready_marker(value["id"]):
        raise ControlError("已准备环境的来源标记无效，未继续安装")
    if not -5 <= time.time() - value.get("prepared_at", 0) < READY_AGE:
        return None
    if verify:
        try:
            fingerprint = m.fingerprint(root)
        except (OSError, ControlError):
            raise StalePreparation("准备文件无法校验，请重新准备；未安装") from None
        if fingerprint != value.get("fingerprint"):
            raise StalePreparation("准备文件校验失败，请重新准备；未安装")
    return value


def invalidate_ready(m, error):
    # Retain the owned record so the next preparation can safely remove its tree.
    m.save(m.PRIVATE / "online.json", {"state": "expired", "message": str(error)})


def summary(m):
    state = m.document(m.PRIVATE / "online.json", {"state": "idle"})
    result = {k: state[k] for k in ("state", "message", "version") if k in state}
    if result.get("state") == "ready":
        try:
            value = ready(m)
        except StalePreparation as e:
            value = None
            result.update(state="expired", message=str(e))
        except ControlError as e:
            value = None
            result.update(state="failed", message=str(e))
        if value:
            result.update(
                version=value["version"],
                download_bytes=value["download_bytes"],
                prepared_at=value["prepared_at"],
            )
        elif result["state"] == "ready":
            result.update(state="expired", message="准备记录已过期，请重新准备")
    result["components"] = (
        dict.fromkeys(COMPONENTS, "ready") if result.get("state") == "ready" else cache_status(m)
    )
    return result


def cache_root(m):
    return m.BASE / "runtime-downloads"


def private_directory(info):
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o077


def private_dir(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return private_directory(info)


def file_size(path, *, apt_owned=False, strict=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return 0
    if stat.S_ISREG(info.st_mode) and (apt_owned or info.st_uid == os.geteuid()):
        return info.st_size
    if strict:
        raise ControlError("安装缓存文件类型或所有者异常")
    return 0


def matches(m, path, expected, *, apt_owned=False):
    return (
        file_size(path, apt_owned=apt_owned) == expected["size"]
        and m.file_hash(path) == expected["sha256"]
    )


def cache_status(m):
    cache = cache_root(m)
    result = dict.fromkeys(COMPONENTS, "missing")
    if not private_dir(cache):
        return result
    if file_size(cache / "base.tgz") == ROOTFS["size"]:
        result["debian"] = "cached"
    try:
        record = m.document(cache / "packages.json", {})
    except ControlError:
        return result
    for component in ("warp", "dependencies"):
        rows = record.get(component)
        if (
            isinstance(rows, list)
            and (rows or component == "dependencies")
            and all(
                isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and re.fullmatch(DEB_NAME, row["name"])
                and type(row.get("size")) is int
                and row["size"] > 0
                and file_size(cache / row["name"]) == row["size"]
                for row in rows
            )
        ):
            result[component] = "cached"
    return result


def cleanup_ready(m):
    # Keep the verified downloads when discarding an owned, unused preparation.
    value = m.document(m.PRIVATE / "online-ready.json")
    if not value:
        return
    if value.get("owner") != OWNER:
        raise ControlError("准备目录归属不能确认，未清理")
    root = stage_path(m, value.get("id"))
    if root.exists() or root.is_symlink():
        if not private_dir(root) or m.document(root / ".imok-bootstrap.json") != ready_marker(
            value["id"]
        ):
            raise ControlError("准备目录归属不能确认，未清理")
        shutil.rmtree(root)
    (m.PRIVATE / "online-ready.json").unlink()


def cleanup_orphaned_ready(m):
    # A worker can stop between renaming the ready tree and saving its record.
    if not any(m.BASE.glob(".runtime-ready-*")):
        return
    current = m.document(m.PRIVATE / "online-ready.json", {}).get("id")
    try:
        mounts = m.mount_points()
    except (OSError, IndexError):
        return
    cleanup_abandoned(
        m, ".runtime-ready-", ".imok-bootstrap.json", ready_marker, current=current, mounts=mounts
    )


def cleanup_abandoned(m, prefix, marker_name, marker_for, *, current=None, mounts=()):
    # Outwait the entire bounded download sequence in case a child outlives its worker.
    for work in m.BASE.glob(prefix + "*"):
        ident = work.name.removeprefix(prefix)
        if ident == current or not re.fullmatch("[a-f0-9]{32}", ident):
            continue
        try:
            info = work.lstat()
        except FileNotFoundError:
            continue
        if not private_directory(info) or time.time() - info.st_mtime < ABANDONED_AGE:
            continue
        root = str(work)
        if any(path == root or path.startswith(root + "/") for path in mounts):
            continue
        try:
            marker = m.document(work / marker_name)
        except ControlError:
            continue
        if marker == marker_for(ident):
            shutil.rmtree(work)


def recover_base(m, archive):
    for work in sorted(m.BASE.glob(".runtime-prepare-*"))[-8:]:
        ident = work.name.removeprefix(".runtime-prepare-")
        if (
            work == archive.parent
            or not re.fullmatch("[a-f0-9]{32}", ident)
            or not private_dir(work)
        ):
            continue
        try:
            marker = m.document(work / ".imok-download.json")
            if marker != {"owner": OWNER, "id": ident}:
                continue
            if cached_file(m, work / "base.tgz", archive, ROOTFS):
                return True
        except (ControlError, FileNotFoundError):
            continue
    return False


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


def write_apt_sources(m, root, debian_origin):
    sources = (
        "deb [arch=arm64 signed-by=/usr/share/keyrings/imok-cloudflare.asc] "
        + m.SOURCE
        + " bookworm main\n"
    )
    keyring = "/usr/share/keyrings/debian-archive-keyring.gpg"
    sources += f"deb [arch=arm64 signed-by={keyring}] {debian_origin}/debian bookworm main\n"
    sources += f"deb [arch=arm64 signed-by={keyring}] {debian_origin}/debian-security bookworm-security main\n"
    m.internal(root, "etc/apt/sources.list.d/imok-warp.list", sources)


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
    write_apt_sources(m, root, "https://deb.debian.org")
    m.internal(root, "usr/sbin/policy-rc.d", "#!/bin/sh\nexit 101\n", 0o755)
    m.internal(root, "etc/resolv.conf", Path("/etc/resolv.conf").read_text())
    prepare_devices(root)
    (root / "var/lib/apt/lists-imok/partial").mkdir(parents=True, exist_ok=True)


def prepare_devices(root):
    for name, minor in [("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)]:
        p = root / "dev" / name
        if p.exists() or p.is_symlink():
            raise ControlError("基础环境设备目录异常")
        os.mknod(p, stat.S_IFCHR | 0o666, os.makedev(1, minor))


def apt_args(root, command="apt-get"):
    return [
        "chroot",
        str(root),
        "/usr/bin/" + command,
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
        if p.is_symlink() or not p.is_file() or not re.fullmatch(DEB_NAME, p.name):
            raise ControlError("下载的软件包清单异常")
        rows.append({"name": p.name, "size": p.stat().st_size, "sha256": m.file_hash(p)})
    warp = [r for r in rows if r["name"].startswith("cloudflare-warp_")]
    if (
        len(warp) != 1
        or warp[0]["size"] != candidate["size"]
        or warp[0]["sha256"] != candidate["sha256"]
    ):
        raise ControlError("WARP 下载结果与官方目录不一致，未准备安装")
    if len(rows) > MAX_PACKAGES or len(json.dumps(rows)) > 60000:
        raise ControlError("软件下载清单超限")
    return rows


def check_plan_size(rows):
    manifest = [{key: row[key] for key in ("name", "size", "sha256")} for row in rows]
    if len(json.dumps(manifest)) > 60000:
        raise ControlError("软件下载清单超限")


def download_progress(m, ident, phase="download_base"):
    if not isinstance(ident, str) or not re.fullmatch("[a-f0-9]{32}", ident):
        return {}
    work = m.BASE / (".runtime-prepare-" + ident)
    try:
        if not private_dir(work):
            return {}
        if phase == "download_base":
            return {
                "bytes_done": min(file_size(work / "base.tgz", strict=True), ROOTFS["size"]),
                "bytes_total": ROOTFS["size"],
            }
        plan = m.document(work / "progress.json", {})
        if phase == "download_warp":
            size = plan.get("warp_size")
            if type(size) is int and 0 < size <= m.MAX_UPLOAD:
                return {
                    "bytes_done": min(file_size(work / "warp.deb", strict=True), size),
                    "bytes_total": size,
                }
        rows = plan.get("dependencies")
        if (
            phase == "download_packages"
            and isinstance(rows, dict)
            and rows
            and len(rows) <= MAX_PACKAGES
        ):
            archive = work / "root/var/cache/apt/archives"
            if not all(
                re.fullmatch(DEB_NAME, name) and type(size) is int and 0 < size <= m.MAX_UPLOAD
                for name, size in rows.items()
            ):
                return {}
            downloaded = sum(
                min(
                    size,
                    max(
                        file_size(archive / name, apt_owned=True, strict=True),
                        file_size(archive / "partial" / name, apt_owned=True, strict=True),
                    ),
                )
                for name, size in rows.items()
            )
            return {"bytes_done": downloaded, "bytes_total": sum(rows.values())}
    except (OSError, ControlError):
        pass
    return {}


def phase(m, message, name, *, download_id=None):
    m.save(m.PRIVATE / "online.json", {"state": "preparing", "message": message})
    m.progress(message, phase=name, download_id=download_id)


def download_step(m, args, timeout, failure):
    try:
        return m.run(args, timeout)
    except ControlError:
        raise ControlError(failure) from None


def update_apt(m, root, origin):
    if origin != "https://deb.debian.org":
        phase(m, "正在尝试备用 Debian 软件源，重新校验签名；尚未安装", "verify_sources")
        lists = root / "var/lib/apt/lists-imok"
        shutil.rmtree(lists)
        (lists / "partial").mkdir(parents=True)
        write_apt_sources(m, root, origin)
    download_step(
        m,
        apt_args(root) + ["--error-on=any", "update"],
        180,
        "软件源连接或签名校验失败，请检查路由器能否访问 Debian 和 Cloudflare 官方源；尚未安装",
    )


def signed_plan(m, root, candidate, origin):
    empty = root / "var/cache/apt/imok-empty"
    empty.mkdir(parents=True, exist_ok=True)
    listing = m.run(
        apt_args(root)
        + [
            "-o",
            "Dir::Cache::Archives=/var/cache/apt/imok-empty",
            "-o",
            "Acquire::ForceHash=SHA256",
            "--print-uris",
            "--download-only",
        ]
        + install_args(candidate["version"]),
        60,
    )
    rows = []
    seen = set()
    for line in listing.splitlines():
        if not line.startswith("'"):
            continue
        match = re.fullmatch(
            r"'(https://[^'\s]+)' (" + DEB_NAME + r") ([0-9]{1,10}) SHA256:([a-fA-F0-9]{64})", line
        )
        if not match:
            raise ControlError("签名软件源未提供可校验的下载清单")
        url, name, size, sha256 = match.groups()
        if (
            name in seen
            or not 0 < int(size) <= m.MAX_UPLOAD
            or urlsplit(url).netloc not in (urlsplit(m.SOURCE).netloc, urlsplit(origin).netloc)
        ):
            raise ControlError("签名下载清单含未知来源、重复文件或异常大小")
        seen.add(name)
        rows.append({"url": url, "name": name, "size": int(size), "sha256": sha256.lower()})
    warp = [row for row in rows if row["name"].startswith("cloudflare-warp_")]
    if (
        len(rows) > MAX_PACKAGES
        or sum(row["size"] for row in rows) > m.MAX_UNPACKED
        or len(warp) != 1
        or warp[0]["size"] != candidate["size"]
        or warp[0]["sha256"] != candidate["sha256"]
        or not warp[0]["url"].startswith(m.SOURCE)
    ):
        raise ControlError("签名下载清单与 WARP 版本或校验值不一致")
    check_plan_size(rows)
    return warp[0], [row for row in rows if row is not warp[0]]


def cached_file(m, source, target, row):
    if not matches(m, source, row):
        return False
    target.unlink(missing_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o644)
    return True


def save_package(m, source, cache, row):
    if not matches(m, source, row, apt_owned=True):
        return False
    target = cache / row["name"]
    if not matches(m, target, row):
        staged = cache / ".incoming.deb"
        staged.unlink(missing_ok=True)
        shutil.copyfile(source, staged)
        staged.chmod(0o600)
        if not matches(m, staged, row):
            staged.unlink()
            return False
        os.replace(staged, target)
    return True


def download_warp(m, work, row, candidate, ident):
    m.save(work / "progress.json", {"warp_size": row["size"]})
    dest = work / "warp.deb"
    for label, url in (
        ("Cloudflare 软件源", row["url"]),
        ("Cloudflare 官方下载站", WARP_DOWNLOAD + candidate["version"]),
    ):
        phase(m, f"正在从{label}下载 WARP；尚未安装", "download_warp", download_id=ident)
        try:
            options = HTTPS_REDIRECTS
            if url == row["url"]:
                options += (
                    "--speed-limit",
                    str(min(row["size"] // 900, 64 * 1024)),
                    "--speed-time",
                    "60",
                )
            m.run(curl_download(url, dest, row["size"], 900, options=options), 910)
            if matches(m, dest, row):
                return dest
        except ControlError:
            pass
        dest.unlink(missing_ok=True)
    raise ControlError("两个 Cloudflare 官方下载地址均未取得可校验的 WARP；尚未安装")


def prepare_packages(m, root, work, cache, ident, refresh):
    archives = root / "var/cache/apt/archives"
    archives.mkdir(parents=True, exist_ok=True)
    record = {}
    try:
        record = m.document(cache / "packages.json", {})
    except ControlError:
        pass  # Existing bytes will still be compared with the fresh signed plan.
    failure = ControlError("未取得可用的签名软件源")
    refreshed = set()
    for origin in ("https://deb.debian.org", DEBIAN_APT_MIRROR, DEBIAN_APT_BACKUP):
        try:
            update_apt(m, root, origin)
            candidate = m.candidate(
                m.run(apt_args(root, "apt-cache") + ["show", "cloudflare-warp"], 45)
            )
            warp, dependencies = signed_plan(m, root, candidate, origin)
        except ControlError as e:
            failure = e
            continue
        warp_path = archives / warp["name"]
        if refresh == "warp" or not cached_file(m, cache / warp["name"], warp_path, warp):
            fetched = download_warp(m, work, warp, candidate, ident)
            os.replace(fetched, warp_path)
            save_package(m, warp_path, cache, warp)
        record["warp"] = [{"name": warp["name"], "size": warp["size"]}]
        m.save(cache / "packages.json", record)
        keep = {row["name"] for row in dependencies} | {warp["name"]}
        for path in cache.glob("*.deb"):
            if path.name not in keep:
                path.unlink()
        for path in archives.glob("*.deb"):
            if path != warp_path:
                path.unlink()
        for row in dependencies:
            if refresh != "dependencies" or row["sha256"] in refreshed:
                cached_file(m, cache / row["name"], archives / row["name"], row)
        if refresh == "warp":
            refresh = None
        m.save(
            work / "progress.json", {"dependencies": {r["name"]: r["size"] for r in dependencies}}
        )
        phase(m, "正在下载缺少的 Debian 依赖；尚未安装", "download_packages", download_id=ident)
        try:
            if any(not (archives / row["name"]).is_file() for row in dependencies):
                download_step(
                    m,
                    apt_args(root) + ["--download-only"] + install_args(candidate["version"]),
                    1200,
                    "Debian 依赖下载未完成，已保留通过校验的文件；尚未安装",
                )
            missing = [
                row
                for row in dependencies
                if not matches(m, archives / row["name"], row, apt_owned=True)
            ]
            if missing:
                raise ControlError("部分 Debian 依赖未通过签名索引校验；尚未安装")
            complete = True
        except ControlError as e:
            complete, failure = False, e
        for row in dependencies:
            if save_package(m, archives / row["name"], cache, row):
                refreshed.add(row["sha256"])
        record["dependencies"] = [
            {"name": row["name"], "size": row["size"]} for row in dependencies
        ]
        m.save(cache / "packages.json", record)
        if complete:
            return candidate
    raise failure


def curl_download(url, destination, size, seconds, *, options=()):
    return [
        "curl",
        "--disable",
        *options,
        "--fail",
        "--silent",
        "--show-error",
        "--proto",
        "=https",
        "--connect-timeout",
        "15",
        "--max-time",
        str(seconds),
        "--max-filesize",
        str(size),
        "--output",
        str(destination),
        url,
    ]


def fetch_base(m, archive, ident):
    # A steady 40 KiB/s would miss the 600-second budget for the pinned archive.
    primary = curl_download(
        ROOTFS["url"],
        archive,
        ROOTFS["size"],
        600,
        options=("--speed-limit", str(40 * 1024), "--speed-time", "45"),
    )
    try:
        m.run(primary, 610)
        if matches(m, archive, ROOTFS):
            return
    except ControlError:
        pass
    archive.unlink(missing_ok=True)
    phase(
        m,
        "GitHub 下载失败、持续过慢或校验不符，正在尝试 DockerProxy 镜像；尚未安装",
        "download_base",
        download_id=ident,
    )
    try:
        m.run(
            curl_download(
                ROOTFS_PUBLIC_MIRROR + ROOTFS["sha256"],
                archive,
                ROOTFS["size"],
                600,
                options=HTTPS_REDIRECTS + ("--speed-limit", str(40 * 1024), "--speed-time", "45"),
            ),
            610,
        )
        if matches(m, archive, ROOTFS):
            return
    except ControlError:
        pass
    archive.unlink(missing_ok=True)
    phase(
        m,
        "DockerProxy 下载失败、持续过慢或校验不符，正在尝试 DaoCloud 镜像；尚未安装",
        "download_base",
        download_id=ident,
    )
    auth = archive.with_name("mirror-token.json")
    try:
        m.run(curl_download(ROOTFS_MIRROR_TOKEN, auth, 32768, 25), 30)
        if auth.stat().st_size > 32768:
            raise ValueError()
        token = json.loads(auth.read_bytes()).get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+,/=-]{1,8192}", token):
            raise ValueError()
        args = curl_download(
            ROOTFS_MIRROR + ROOTFS["sha256"],
            archive,
            ROOTFS["size"],
            600,
            options=("--config", "-") + HTTPS_REDIRECTS,
        )
        m.run(args, 610, input_data=('header = "Authorization: Bearer ' + token + '"\n').encode())
        if not matches(m, archive, ROOTFS):
            raise ValueError()
    except (ControlError, AttributeError, ValueError, OSError):
        raise ControlError(
            "Debian 基础环境在线下载失败：GitHub、DockerProxy 和 DaoCloud 均未取得通过校验的文件。"
            "请检查路由器的 DNS 和 HTTPS 连通性后重试；尚未安装"
        ) from None
    finally:
        auth.unlink(missing_ok=True)


def prepare(m, refresh=None, *, continuous=False):
    if refresh is not None and refresh not in COMPONENTS:
        raise ControlError("请选择有效的下载组件")
    with m.job_lock():
        m.inactive()
        if (m.BASE / "runtime").exists() or (m.BASE / "runtime").is_symlink():
            raise ControlError("已检测到运行环境，请使用更新；不会覆盖首次安装")
        if m.recovery_required():
            raise ControlError("请先核对上次未完成的维护")
        cleanup_orphaned_ready(m)
        env = detect(m)
        if not env["can_prepare"]:
            raise ControlError("；".join(env["issues"]))
        cleanup_ready(m)
        cache = cache_root(m)
        m.directory(cache)
        ident = uuid.uuid4().hex
        work = m.BASE / (".runtime-prepare-" + ident)
        m.directory(work)
        root = work / "root"
        root.mkdir(mode=0o700)
        target = stage_path(m, ident)
        try:
            m.save(work / ".imok-download.json", {"owner": OWNER, "id": ident})
            archive = work / "base.tgz"
            have_base = refresh != "debian" and (
                cached_file(m, cache / "base.tgz", archive, ROOTFS) or recover_base(m, archive)
            )
            cleanup_abandoned(
                m,
                ".runtime-prepare-",
                ".imok-download.json",
                lambda ident: {"owner": OWNER, "id": ident},
            )
            if not have_base:
                phase(
                    m,
                    "正在下载 Debian 官方基础文件；尚未安装",
                    "download_base",
                    download_id=ident,
                )
                fetch_base(m, archive, ident)
                phase(m, "基础文件下载结束，正在校验；尚未安装", "verify_base")
            else:
                phase(m, "正在校验并复用本地 Debian 基础文件", "verify_base")
            if not matches(m, archive, ROOTFS):
                raise ControlError("官方基础环境下载不完整或校验不符，请重试；未安装")
            if not matches(m, cache / "base.tgz", ROOTFS) or refresh == "debian":
                os.replace(archive, cache / "base.tgz")
                os.link(cache / "base.tgz", archive)
            m.unpack(archive, root)
            validate_base(root)
            phase(m, "正在读取官方软件源并验证签名；尚未安装", "verify_sources")
            configure_apt(m, root)
            candidate = prepare_packages(m, root, work, cache, ident, refresh)
            phase(m, "正在校验已下载的 WARP 与依赖；尚未安装", "verify_packages")
            downloaded = packages(m, root, candidate)
            m.inactive()
            if (m.BASE / "runtime").exists():
                raise ControlError("准备期间检测到现有环境，未覆盖；请刷新核对")
            m.save(root / ".imok-bootstrap.json", ready_marker(ident))
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
            if continuous:
                m.progress("文件已下载并校验，正在准备安装", phase="prepare_install")
            else:
                m.progress("文件已准备好，尚未安装；请点击确认安装到路由器", "success", phase="ready")
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
    try:
        value = ready(m, verify=True)
        if not value:
            raise StalePreparation("请先重新准备 WARP 安装文件")
    except StalePreparation as e:
        invalidate_ready(m, e)
        raise
    root = stage_path(m, value["id"])
    need = m.estimate(root) + 256 * 1024 * 1024 + sum(r["size"] for r in value["packages"]) * 4
    return {"ready_id": value["id"], "version": value["version"], "required_bytes": need}


def install(m, plan, stage):
    value = ready(m, verify=True)
    if not value:
        raise StalePreparation("准备文件已失效，请重新准备；未安装")
    if value["id"] != plan.get("ready_id") or value["version"] != plan.get("version"):
        raise ControlError("安装文件或版本已变化，请重新检查后确认")
    source = stage_path(m, value["id"])
    m.copy_runtime(source, stage)
    if m.fingerprint(stage) != value["fingerprint"]:
        raise StalePreparation("准备文件校验失败，请重新准备；未安装")
    (stage / "tmp").chmod(0o1777)
    prepare_devices(stage)
    m.save(m.PRIVATE / "online.json", {"state": "installing", "version": value["version"]})
    if packages(m, stage, value["candidate"]) != value["packages"]:
        raise ControlError("缓存软件包已变化，未执行安装")
    m.install_apt(
        apt_args(stage) + ["--no-download"] + install_args(value["version"]),
        1200,
        "已确认安装，正在独立环境中安装已校验组件；未启动隧道",
    )
    m.progress("组件安装命令已结束，正在自检；现用环境尚未提交", phase="verify_install")
    registration = stage / "var/lib/cloudflare-warp"
    if registration.is_symlink() or registration.exists() and any(registration.iterdir()):
        raise ControlError("首次安装出现非预期注册资料，未提交")
    try:
        m.test_runtime(stage, value["version"])
    except ControlError as e:
        raise OnlineSelfTestError(str(e)) from None


def retry_state(m, plan, error):
    if (m.BASE / "runtime").exists() or (m.BASE / "runtime").is_symlink():
        return None
    if m.recovery_required():
        return None
    try:
        value = ready(m)
    except ControlError:
        return None
    if not value or value["id"] != plan.get("ready_id") or value["version"] != plan.get("version"):
        return None
    return {"state": "ready", "version": value["version"], "message": str(error)}
