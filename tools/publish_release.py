#!/usr/bin/env python3
"""Build the reviewed public artifacts; publish only with an ephemeral Actions token."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "routepilot-lab/imok"
API = "https://api.github.com/repos/" + REPOSITORY


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


PUBLIC_DOCS = {
    "docs/CLIENT_ACCESS.md",
    "docs/FAQ.md",
    "docs/HARDWARE_ACCEPTANCE.md",
    "docs/HISTORICAL_MANUAL_INSTALL.md",
    "docs/INSTALL_KIT.md",
    "docs/KNOWN_LIMITS.md",
    "docs/USER_FEEDBACK.md",
    "docs/WARP_RUNTIME.md",
    "docs/WORKERS_SETUP.md",
    "docs/quality/system-audit-20261109.md",
    "docs/quality/warp-maintenance-0.2.37-1.md",
}
PUBLIC_TOP = {
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.txt",
    ".gitignore",
    "tools/publish_release.py",
    "tools/test_publish_release.py",
    "pyproject.toml",
    "eslint.config.mjs",
    "requirements-dev.txt",
    "package.json",
    "package-lock.json",
    ".prettierrc.json",
}
PUBLIC_IMAGES = {
    "docs/images/imok-overview.png",
    "docs/images/imok-clients.png",
    "docs/images/imok-workers.png",
    "docs/images/imok-client-protocol.png",
    "docs/images/imok-device-traffic.png",
}


def public_paths(names):
    """Explicit public scope; never infer safety from .gitignore alone."""
    result = []
    for name in sorted(set(names)):
        p = Path(name)
        if p.is_absolute() or any(
            part in {".git", ".local", "dist", "runtime", "__pycache__", "node_modules", ".."}
            or part.startswith(".env")
            for part in p.parts
        ):
            continue
        if p.suffix.lower() in {
            ".key",
            ".pem",
            ".db",
            ".sqlite",
            ".sqlite3",
            ".pyc",
            ".ipk",
            ".tgz",
        }:
            continue
        if (
            name in PUBLIC_TOP | PUBLIC_DOCS | PUBLIC_IMAGES
            or name.startswith(("packages/luci-app-routepilot/", ".github/"))
            or re.fullmatch(
                r"docs/releases/v[0-9.-]+\.md|docs/evidence/[0-9.-]+/PUBLICATION_CHECKS\.json", name
            )
        ):
            result.append(name)
    return result


def write_zip(target, entries):
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data, mode in sorted(entries):
            item = zipfile.ZipInfo(name, date_time=(2026, 11, 9, 0, 0, 0))
            item.create_system = 3
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(item, data)


def first_install_script(version, ipk_bytes, *, reinstall=False):
    """Build an iStore .run using temporary, version-matched signed package feeds."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[0-9]+", version):
        raise ValueError("Invalid imOK package version")
    package = f"luci-app-routepilot_{version}_all.ipk"
    checksum = hashlib.sha256(ipk_bytes).hexdigest()
    header = r"""#!/bin/sh
set -eu
umask 077

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

[ -r /etc/openwrt_release ] || fail '请把 .run 上传到路由器的 iStore 手动安装。'
. /etc/openwrt_release
case "${DISTRIB_RELEASE:-}:$(uname -m)" in
    24.10:aarch64|24.10.*:aarch64) ;;
    *) fail '此安装包适用于 ARM64 OpenWrt 24.10。' ;;
esac
[ "$(id -u)" = 0 ] || fail '请通过 iStore 或路由器管理员终端运行。'
command -v opkg >/dev/null || fail '当前固件没有 opkg。'
unset OFFLINE_ROOT IPKG_INSTROOT OPKG_CONF_DIR OPKG_KEYS

is_installed() {
    printf '%s\n' "$1" | grep -q '^Status: .* installed$'
}
is_target_installed() {
    # opkg may retain more than one stanza for a locally uploaded package.
    printf '%s\n' "$1" | awk -v version='@VERSION@' '
        BEGIN { RS = ""; FS = "\n" }
        {
            named = matched = installed = 0
            for (i = 1; i <= NF; i++) {
                if ($i == "Package: luci-app-routepilot") named = 1
                if ($i == "Version: " version) matched = 1
                if ($i ~ /^Status: .* installed$/) installed = 1
            }
            if (named && matched && installed) found = 1
        }
        END { exit !found }
    '
}
status=$(opkg status luci-app-routepilot) || fail '无法读取已安装的软件包状态。'
if is_installed "$status"; then
    fail '已有 imOK。请使用同版或新版的普通 IPK 升级。'
fi
pristine_install() {
    [ @REINSTALL@ = 0 ] || return 1
    for path in /etc/config/routepilot /etc/routepilot /opt/routepilot; do
        if [ -e "$path" ] || [ -L "$path" ]; then return 1; fi
    done
    return 0
}
queue_warp=0
if pristine_install; then queue_warp=1; fi
reset_switches=0
if [ -e /etc/config/routepilot ] || [ -L /etc/config/routepilot ]; then
    [ -f /etc/config/routepilot ] && [ ! -L /etc/config/routepilot ] ||
        fail '保留的 imOK 配置文件类型异常，暂未安装。'
    enabled=$(uci -q get routepilot.main.enabled) || fail '检测到已有配置，请先核对 imOK 的启用状态。'
    preparing=$(uci -q get routepilot.main.preparing || :)
    case "$enabled:${preparing:-0}" in
        0:0) ;;
        0:1|1:0|1:1)
            [ @REINSTALL@ = 1 ] ||
                fail '检测到卸载后保留的启用或准备设置。请使用保留配置的重装包。'
            reset_switches=1
            ;;
        *) fail '保留的 imOK 开关值无法识别，请核对后重试。' ;;
    esac
fi

work=$(mktemp -d /tmp/imok-first-install.XXXXXX) ||
    fail '无法创建安装暂存目录，请检查路由器 /tmp 剩余空间和写入权限。'
prefetch_root="$work/prefetch"
cleanup() {
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
tail -n +@PAYLOAD_LINE@ "$0" > "$work/@PACKAGE@" ||
    fail '无法暂存附带的 IPK，请检查 /tmp 剩余空间和写入权限，再重新上传。'
printf '%s  %s\n' '@CHECKSUM@' "$work/@PACKAGE@" | sha256sum -c - >/dev/null ||
    fail '附带的 IPK 校验失败，请重新下载并上传。'

copy_sources() {
    # Change only the official release mirror. Keep the firmware and architecture paths.
    awk -v release_base="$release_base" '
        $1 == "lists_dir" { next }
        $1 == "option" {
            if ($2 ~ /^(cache|http_timeout|check_signature|verify_program|force_.*|nodeps)$/ ||
                $2 ~ /^(no_check_certificate|offline_root|noaction|test|download_only)$/) next
        }
        $1 ~ /^src(\/gz)?$/ && $3 ~ /^https?:\/\/downloads\.openwrt\.org\/releases\/24\.10([.\/])/ {
            sub(/https?:\/\/downloads\.openwrt\.org\/releases\//, release_base)
        }
        { print }
    ' "$1"
}
[ -r /etc/opkg.conf ] || fail '无法读取路由器现有软件包配置。'
installer_opkg() {
    OPKG_CONF_DIR="$prefetch_root/etc/opkg" LC_ALL=C opkg -f "$work/opkg.conf" -d root "$@"
}

prepare_dependencies() {
    release_base=$1
    echo "正在刷新安装用的软件源索引：$2"
    # Each mirror uses fresh indexes, package cache and an offline opkg root.
    rm -rf -- "$prefetch_root" "$work/cache" ||
        fail '无法清理临时软件源索引和缓存，请检查 /tmp 写入权限。'
    mkdir -p "$prefetch_root/etc/opkg" "$prefetch_root/lists" "$prefetch_root/usr/lib/opkg/info" \
        "$prefetch_root/var/lock" "$prefetch_root/tmp" "$work/cache" ||
        fail '无法创建依赖暂存目录，请检查路由器 /tmp 剩余空间和写入权限。'
    copy_sources /etc/opkg.conf >"$work/opkg.conf" || fail '无法读取原软件源配置。'
    printf 'lists_dir ext %s\noption check_signature\noption cache %s\noption http_timeout 25\n' \
        "$prefetch_root/lists" "$work/cache" >>"$work/opkg.conf" || fail '无法写入临时软件源配置。'
    for conf in /etc/opkg/*.conf; do
        [ -f "$conf" ] || continue
        copy_sources "$conf" >"$prefetch_root/etc/opkg/${conf##*/}" || fail '无法读取原软件源配置。'
    done
    : >"$work/opkg-update.log" ||
        fail '无法暂存软件源更新日志，请检查 /tmp 剩余空间和写入权限。'
    update_code=0
    installer_opkg update >"$work/opkg-update.log" 2>&1 || update_code=$?
    cat "$work/opkg-update.log" || fail '无法读取软件源更新日志，尚未安装 imOK。'
    if grep -Eqi 'Signature check failed|Failed to download.*\.sig([[:space:][:punct:]]|$)' "$work/opkg-update.log"; then
        signature_rejected=1
        echo "${2}索引签名校验失败，已拒绝该来源。"
        return 1
    fi
    if ! grep -q '^Signature check passed\.' "$work/opkg-update.log"; then
        return 1
    fi
    if [ "$update_code" -ne 0 ]; then
        echo '部分软件源未更新，继续使用已校验的索引，由 opkg 检查 imOK 所需依赖。'
    fi
    echo '正在下载并校验缺少的依赖，暂未执行安装…'
    cp /usr/lib/opkg/status "$prefetch_root/usr/lib/opkg/status" ||
        fail '无法复制已安装软件包清单，请检查 /tmp 剩余空间和写入权限。'
    # Even in download-only mode, opkg reads the installed file lists under its offline root.
    for package_list in /usr/lib/opkg/info/*.list; do
        [ -f "$package_list" ] && [ ! -L "$package_list" ] || continue
        cp "$package_list" "$prefetch_root/usr/lib/opkg/info/" ||
            fail '无法暂存已安装软件包的文件列表，请检查 /tmp 剩余空间和写入权限。'
    done
    sed 's|^lists_dir .*|lists_dir ext /lists|' "$work/opkg.conf" >"$work/opkg-prefetch.conf" ||
        fail '无法写入依赖准备配置。'
    LC_ALL=C opkg --offline-root "$prefetch_root" -f "$work/opkg-prefetch.conf" -d root \
        --noaction --download-only install "$work/@PACKAGE@"
}

signature_rejected=0
if ! prepare_dependencies 'https://mirrors.tuna.tsinghua.edu.cn/openwrt/releases/' '清华镜像'; then
    echo '清华镜像未能备齐安装依赖，正在尝试南京大学镜像…'
    if ! prepare_dependencies 'https://mirrors.nju.edu.cn/openwrt/releases/' '南京大学镜像'; then
        echo '国内镜像未能备齐安装依赖，正在尝试 OpenWrt 官方源…'
        if ! prepare_dependencies 'https://downloads.openwrt.org/releases/' 'OpenWrt 官方源'; then
            if [ "$signature_rejected" = 1 ]; then
                fail '未找到可用且通过签名校验的软件源，尚未安装 imOK。请查看上方各来源的错误。'
            fi
            fail '未能下载并校验所有依赖，尚未安装 imOK。请查看上方具体包名和错误。'
        fi
    fi
fi

if [ "$reset_switches" = 1 ]; then
    status=$(opkg status luci-app-routepilot) || fail '无法再次确认软件包状态。'
    if is_installed "$status"; then fail 'imOK 已被安装，请刷新后核对。'; fi
    services=$(ubus call service list '{"name":"routepilot"}') ||
        fail '无法确认旧 imOK 服务已停止，暂未重装。'
    [ "$(printf '%s' "$services" | tr -d '[:space:]')" = '{}' ] ||
        fail '旧 imOK 服务仍在系统中注册，暂未重装。'
    [ -f /etc/config/routepilot ] && [ ! -L /etc/config/routepilot ] ||
        fail '保留配置文件类型异常，暂未重装。'
    changes=$(uci -q changes routepilot) || fail '无法检查 imOK 配置。'
    [ -z "$changes" ] || fail 'imOK 配置有未提交的更改，请先处理后重试。'
    [ "$(uci -q get routepilot.main.enabled)" = "$enabled" ] &&
        [ "$(uci -q get routepilot.main.preparing || :)" = "$preparing" ] ||
        fail 'imOK 开关在准备期间已变化，请核对后重试。'
    backup=$(mktemp /etc/config/.imok-before-reinstall.XXXXXX) ||
        fail '无法创建重装前配置备份。'
    cp /etc/config/routepilot "$backup" || fail '无法备份旧 imOK 配置。'
    echo "已保存重装前配置备份：$backup"
    mkdir "$work/uci" || fail '无法暂存重装配置，请检查 /tmp 剩余空间和写入权限。'
    uci -t "$work/uci" -q set routepilot.main.enabled=0 &&
        uci -t "$work/uci" -q set routepilot.main.preparing=0 &&
        uci -t "$work/uci" -q commit routepilot ||
        fail '无法将保留的 imOK 开关设为关闭，暂未重装。'
    [ "$(uci -q get routepilot.main.enabled)" = 0 ] &&
        [ "$(uci -q get routepilot.main.preparing)" = 0 ] ||
        fail '无法确认 imOK 已设为关闭，暂未重装。'
    echo '已将 imOK 设为关闭。账户、出口和分流设置已保留。'
fi

status=$(opkg status luci-app-routepilot) || fail '无法再次确认软件包状态。'
if is_installed "$status"; then fail 'imOK 已被安装，请刷新后核对。'; fi
if [ -e /etc/config/routepilot ] || [ -L /etc/config/routepilot ]; then
    [ -f /etc/config/routepilot ] && [ ! -L /etc/config/routepilot ] ||
        fail '准备期间检测到异常的 imOK 配置文件，暂未安装。'
    current_enabled=$(uci -q get routepilot.main.enabled) ||
        fail '无法核对现有 imOK 的启用状态，暂未安装。'
    current_preparing=$(uci -q get routepilot.main.preparing || :)
    [ "$current_enabled:${current_preparing:-0}" = 0:0 ] ||
        fail '准备期间检测到已启用或正在准备的 imOK 配置，暂未安装。'
fi
if ! pristine_install; then queue_warp=0; fi
echo '依赖已准备，正在安装 imOK…'
if ! installer_opkg install "$work/@PACKAGE@"; then
    if [ "$reset_switches" = 1 ]; then
        echo '旧 imOK 设置已备份，两个开关保持关闭。' >&2
    fi
    fail '安装未完成，请查看上方依赖下载或配置错误。'
fi
status=$(opkg status luci-app-routepilot) || fail '无法确认 imOK 安装结果，请查看上方日志。'
if ! is_target_installed "$status"; then
    fail '未确认目标版本已完成安装，请查看上方日志。'
fi
if [ "$reset_switches" = 1 ]; then
    [ "$(uci -q get routepilot.main.enabled)" = 0 ] &&
        [ "$(uci -q get routepilot.main.preparing)" = 0 ] ||
        fail '已安装软件包，但未能核对 imOK 的关闭状态。请先检查总览。'
fi
if [ "$queue_warp" = 1 ]; then
    echo 'imOK 已安装，正在提交 WARP 后台安装任务…'
    helper_status=0
    receipt=$(/usr/libexec/routepilot-runtime-first-install 2>/dev/null) || helper_status=$?
    summary=$(printf '%s\n' "$receipt" | /usr/bin/python3 -I -X utf8 -c '
import json
import sys

data = sys.stdin.buffer.read(4097)
if len(data) > 4096:
    sys.exit(1)
payload = json.loads(data)
if not isinstance(payload, dict):
    sys.exit(1)
if sys.argv[1] == "0" and payload.get("queued") is True:
    print("queued")
    sys.exit(0)
preflight_reasons = {
    "目前自动安装只支持 ARM64 OpenWrt 24.10",
    "需要路由器管理员权限",
    "缺少必要系统组件，请通过正常插件安装流程补齐依赖",
    "路由器可用空间不足，自动准备需要至少 1.6 GB",
    "路由器可用内存不足，请释放内存后重试",
}
actionable_errors = {
    "已有维护任务正在执行，请等待结果",
    "已有版本检查或维护任务正在执行，请稍后再试",
    "WARP 运行环境仍被占用，请等待关闭完成后刷新",
    "WARP 环境还有进程运行，未替换文件",
    "WARP 环境仍有挂载，请由管理员检查，未自动卸载",
    "已检测到运行环境，不会覆盖安装；请使用更新",
    "请先在总览关闭 imOK，并结束登录准备，再进行安装、升级或回滚；版本检查不受影响",
    "请先核对上次未完成的维护",
    "维护进程未能启动",
}
error = payload.get("error")
if isinstance(error, str) and (
    error in actionable_errors or set(error.split("；")) <= preflight_reasons
):
    print(error)
' "$helper_status" 2>/dev/null) || summary=
    if [ "$summary" = queued ]; then
        echo 'WARP 安装任务已提交。请到 LuCI → 服务 → imOK → WARP 安装与更新查看进度。'
    else
        if [ -n "$summary" ]; then printf 'WARP 安装暂未开始：%s。\n' "$summary"; fi
        echo 'imOK 已安装，WARP 后台安装任务未获确认。请到 LuCI → 服务 → imOK → WARP 安装与更新查看状态，需要时可重试。'
    fi
else
    echo 'imOK 已安装。请到 LuCI → 服务 → imOK → WARP 安装与更新继续设置。'
fi
exit 0
"""
    # The binary payload follows a terminal exit and is never evaluated.
    payload_line = header.count("\n") + 1
    return (
        header.replace("@PACKAGE@", package)
        .replace("@VERSION@", version)
        .replace("@REINSTALL@", "1" if reinstall else "0")
        .replace("@CHECKSUM@", checksum)
        .replace("@PAYLOAD_LINE@", str(payload_line))
        .encode()
        + ipk_bytes
    )


def install_kit(out, version, ipk, source):
    prefix = "imOK-" + version + "/"
    package_bytes = ipk.read_bytes()
    entries = [
        (prefix + ipk.name, package_bytes, 0o644),
        (
            prefix + f"imOK-{version}-first-install.run",
            first_install_script(version, package_bytes),
            0o755,
        ),
        (
            prefix + f"imOK-{version}-reinstall-cn-keep-settings.run",
            first_install_script(version, package_bytes, reinstall=True),
            0o755,
        ),
        (prefix + source.name, source.read_bytes(), 0o644),
    ]
    for name, origin in [
        ("开始阅读.md", ROOT / "docs/INSTALL_KIT.md"),
        ("本版更新.md", ROOT / "docs/releases" / ("v" + version + ".md")),
        ("packages/luci-app-routepilot/INSTALL.zh-CN.md", ROOT / "packages/luci-app-routepilot/INSTALL.zh-CN.md"),
        ("docs/WARP_RUNTIME.md", ROOT / "docs/WARP_RUNTIME.md"),
        ("docs/HARDWARE_ACCEPTANCE.md", ROOT / "docs/HARDWARE_ACCEPTANCE.md"),
        ("LICENSE", ROOT / "LICENSE"),
        ("THIRD_PARTY_NOTICES.txt", ROOT / "THIRD_PARTY_NOTICES.txt"),
    ]:
        entries.append((prefix + name, origin.read_bytes(), 0o644))
    sums = "".join(
        hashlib.sha256(data).hexdigest() + "  " + name.removeprefix(prefix) + "\n"
        for name, data, _ in entries
    )
    entries.append((prefix + "SHA256SUMS", sums.encode(), 0o644))
    target = out / ("imOK-" + version + "-install-kit.zip")
    write_zip(target, entries)
    return target


def publication_checks(version):
    return json.loads((ROOT / "docs/evidence" / version / "PUBLICATION_CHECKS.json").read_text())


def prepare():
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT).strip(), (
        "Commit reviewed source before preparing a public release"
    )
    package = ROOT / "packages/luci-app-routepilot"
    sys.path.insert(0, str(package / "tools"))
    spec = importlib.util.spec_from_file_location("release_build", package / "tools/build.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    checks = publication_checks(builder.VERSION)
    assert checks["version"] == builder.VERSION and checks["all_passed"], (
        "Unreviewed package version"
    )
    version = builder.VERSION
    out = ROOT / ".local/release" / version
    out.mkdir(parents=True, exist_ok=True)
    ipk = builder.build(out)
    assert digest(ipk) == checks["integrity"]["package_sha256"], (
        "IPK differs from the reviewed package"
    )
    archive = out / ("imOK-" + version + "-public-source.zip")
    records = (
        subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=ROOT).decode().split("\0")
    )
    modes = {}
    for record in records:
        if not record:
            continue
        info, name = record.split("\t", 1)
        mode, _, stage = info.split()
        assert stage == "0", "Unresolved index"
        modes[name] = int(mode, 8)
    entries = []
    for name in public_paths(modes):
        f = ROOT / name
        assert f.is_file() and not f.is_symlink(), name
        entries.append(("imOK-" + version + "/" + name, f.read_bytes(), modes[name] & 0o777))
    write_zip(archive, entries)
    kit = install_kit(out, version, ipk, archive)
    sums = out / "SHA256SUMS"
    sums.write_text("".join(digest(p) + "  " + p.name + "\n" for p in [ipk, archive, kit]))
    return version, out, [ipk, archive, kit, sums]


def api(method, path, payload=None, data=None, content_type="application/json"):
    assert os.environ.get("GITHUB_REPOSITORY") == REPOSITORY, (
        "Publishing is limited to the upstream repository"
    )
    token = os.environ.get("GITHUB_TOKEN")
    assert token, "No Actions token available"
    url = path if path.startswith("https://uploads.github.com/") else API + path
    assert urllib.parse.urlsplit(url).hostname in ["api.github.com", "uploads.github.com"]
    raw = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else data
    req = urllib.request.Request(
        url,
        data=raw,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": content_type,
            "User-Agent": "imok-release",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if method == "GET" and error.code == 404:
            return None
        raise RuntimeError("GitHub request failed: " + method + " " + str(error.code)) from None


def verify_assets(release, files):
    assets = {a["name"]: a for a in release.get("assets", [])}
    assert set(assets) == {p.name for p in files}, "Unexpected release assets"
    for p in files:
        a = assets[p.name]
        assert (
            a.get("state") == "uploaded"
            and a.get("size") == p.stat().st_size
            and a.get("digest") == "sha256:" + digest(p)
        ), "Release asset mismatch: " + p.name


def release_options(version, checks):
    value = checks.get(
        "release",
        {"name": "imOK 2.0 · 客户端接入与稳定性版（v" + version + "）", "prerelease": False},
    )
    assert set(value) == {"name", "prerelease"}, "Unsupported publication metadata"
    assert type(value["prerelease"]) is bool, "Release kind must be a boolean"
    assert (
        isinstance(value["name"], str)
        and value["name"].startswith("imOK ")
        and 1 <= len(value["name"]) <= 120
        and "\n" not in value["name"]
    ), "Invalid release title"
    return dict(value)


def publish(version, files, head, body, *, name=None, prerelease=False):
    assert re.fullmatch("[0-9a-f]{40}", head), "Invalid source commit"
    assert type(prerelease) is bool, "Release kind must be explicit"
    name = name or "imOK 2.0 · 客户端接入与稳定性版（v" + version + "）"
    tag = "v" + version
    ref = api("GET", "/git/ref/tags/" + tag)
    assert not ref or ref["object"]["type"] == "commit" and ref["object"]["sha"] == head, (
        "Refuse to replace an existing tag"
    )
    release = api("GET", "/releases/tags/" + tag)
    if release and not release.get("draft"):
        assert ref and release.get("prerelease") is prerelease, "Existing public release differs"
        verify_assets(release, files)
        return release
    if release:
        assert release["target_commitish"] == head and release.get("prerelease") is prerelease, (
            "Unrelated draft release"
        )
    else:
        release = api(
            "POST",
            "/releases",
            {
                "tag_name": tag,
                "target_commitish": head,
                "name": name,
                "body": body,
                "draft": True,
                "prerelease": prerelease,
                "make_latest": "false",
            },
        )
    existing = {a["name"]: a for a in release.get("assets", [])}
    assert set(existing) <= {p.name for p in files}, "Unexpected draft attachments"
    for p in files:
        if p.name in existing:
            verify_assets({"assets": [existing[p.name]]}, [p])
            continue
        url = release["upload_url"].split("{")[0] + "?" + urllib.parse.urlencode({"name": p.name})
        uploaded = api(
            "POST",
            url,
            data=p.read_bytes(),
            content_type="application/zip" if p.suffix == ".zip" else "application/octet-stream",
        )
        verify_assets({"assets": [uploaded]}, [p])
    staged = api("GET", "/releases/" + str(release["id"]))
    verify_assets(staged, files)
    final = api(
        "PATCH",
        "/releases/" + str(release["id"]),
        {
            "draft": False,
            "prerelease": prerelease,
            "make_latest": "false" if prerelease else "true",
        },
    )
    verify_assets(final, files)
    final_ref = api("GET", "/git/ref/tags/" + tag)
    assert (
        final_ref["object"]["sha"] == head
        and not final["draft"]
        and final["prerelease"] is prerelease
    ), "Publication state mismatch"
    return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    version, out, files = prepare()
    options = release_options(version, publication_checks(version))
    report = {
        "version": version,
        "release": options,
        "assets": [{"name": p.name, "sha256": digest(p), "size": p.stat().st_size} for p in files],
    }
    if args.publish:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        assert head == os.environ.get("GITHUB_SHA"), "Checkout differs from the triggering commit"
        release = publish(
            version,
            files,
            head,
            (ROOT / "docs/releases" / ("v" + version + ".md")).read_text(),
            **options,
        )
        report.update(url=release["html_url"], published=True, source_commit=head)
    (out / "receipt.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
