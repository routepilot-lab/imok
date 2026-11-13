#!/usr/bin/env python3
"""Install an independent loopback-only Xray + Cloudflare Tunnel service.

Downloads nothing, modifies no existing proxy service/firewall and never executes
remote commands. Supply official Linux binaries plus verified release SHA-256s.
"""

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlencode, quote

VERSION = "0.1.0-preview"
XRAY_VERSION = "26.10.29"
CF_VERSION = "2026.10.6"
ROOT = Path("/opt/routepilot-server")
UNITS = ("routepilot-server.service", "routepilot-cloudflared.service")
DOMAIN = re.compile(
    r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_binary(path, expected, name):
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or not re.fullmatch(r"[a-fA-F0-9]{64}", expected or ""):
        raise ValueError(name + " 需要本机二进制文件及完整 SHA-256")
    if digest(path) != expected.lower():
        raise ValueError(name + " SHA-256 不匹配；已停止")
    return path


def check_version(path, expected, name):
    result = subprocess.run([str(path), "version"], capture_output=True, text=True, timeout=10)
    output = result.stdout + result.stderr
    if result.returncode or not re.search(
        r"(?<![\d.])" + re.escape(expected) + r"(?![\d.])", output
    ):
        raise ValueError(name + " 版本不符；需要 " + expected)


def units(directory):
    common = """[Install]
WantedBy=multi-user.target
"""
    xray = (
        """[Unit]
Description=imok independent VLESS WebSocket backend
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
DynamicUser=yes
LoadCredential=config.json:{root}/config.json
ExecStart={root}/xray run -config %d/config.json
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
"""
        + common
    )
    cloudflared = (
        """[Unit]
Description=imok independent Cloudflare Tunnel connector
After=network-online.target routepilot-server.service
Wants=network-online.target routepilot-server.service
[Service]
Type=simple
DynamicUser=yes
LoadCredential=tunnel-token:{root}/tunnel-token
ExecStart={root}/cloudflared --no-autoupdate tunnel run --token-file %d/tunnel-token
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
"""
        + common
    )
    return {UNITS[0]: xray.format(root=directory), UNITS[1]: cloudflared.format(root=directory)}


def prepare(directory, hostname, token, xray, cloudflared, service_root, create=True):
    directory = Path(directory)
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    user_id = str(uuid.uuid4())
    ws_path = "/rp-" + secrets.token_hex(16)
    config = {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "listen": "127.0.0.1",
                "port": 18082,
                "tag": "routepilot-ws",
                "protocol": "vless",
                "settings": {"clients": [{"id": user_id}], "decryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"path": ws_path}},
            }
        ],
        "outbounds": [{"protocol": "freedom", "tag": "direct", "settings": {}}],
    }
    (directory / "config.json").write_text(json.dumps(config, indent=2) + "\n")
    (directory / "tunnel-token").write_text(token + "\n")
    for source, name in ((xray, "xray"), (cloudflared, "cloudflared")):
        shutil.copyfile(source, directory / name)
        (directory / name).chmod(0o755)
    params = urlencode(
        {
            "encryption": "none",
            "security": "tls",
            "sni": hostname,
            "type": "ws",
            "host": hostname,
            "path": ws_path,
        }
    )
    uri = "vless://%s@%s:443?%s#%s" % (user_id, hostname, params, quote("imok-" + hostname))
    (directory / "client-link.txt").write_text(uri + "\n")
    (directory / "install-state.json").write_text(
        json.dumps(
            {
                "owner": "routepilot-server",
                "version": VERSION,
                "hostname": hostname,
                "xray_version": XRAY_VERSION,
                "cloudflared_version": CF_VERSION,
                "xray_sha256": digest(xray),
                "cloudflared_sha256": digest(cloudflared),
                "units": list(UNITS),
            },
            indent=2,
        )
        + "\n"
    )
    for name, content in units(service_root).items():
        (directory / name).write_text(content)
    for child in directory.iterdir():
        if child.name not in ("xray", "cloudflared"):
            child.chmod(0o600)
    return uri


def validate_hostname(hostname):
    hostname = hostname.strip().lower()
    try:
        ipaddress.ip_address(hostname)
        is_ip = True
    except ValueError:
        is_ip = False
    if is_ip or not DOMAIN.fullmatch(hostname):
        raise ValueError("请输入自己的完整子域名，例如 node.example.com")
    return hostname


def validate_token(token):
    token = token.strip()
    # A token is an opaque secret; enforce only a conservative encoding envelope.
    if not 20 <= len(token) <= 16384 or not re.fullmatch(r"[A-Za-z0-9_+=/.-]+", token):
        raise ValueError("Tunnel token 格式不正确")
    return token


def preflight():
    if platform.system() != "Linux" or platform.machine() not in (
        "x86_64",
        "aarch64",
        "amd64",
        "arm64",
    ):
        raise ValueError("正式安装只支持 Debian/Ubuntu Linux amd64/arm64")
    if os.geteuid() != 0:
        raise ValueError("正式安装需要 root 权限")
    if not Path("/run/systemd/system").exists() or not shutil.which("systemctl"):
        raise ValueError("需要 systemd 系统")
    os_release = Path("/etc/os-release").read_text()
    if not re.search(r'^ID=(?:"?)(?:debian|ubuntu)(?:"?)$', os_release, re.M):
        raise ValueError("第一版只接受 Debian 或 Ubuntu")
    version = subprocess.run(
        ["systemctl", "--version"], capture_output=True, text=True, check=True
    ).stdout
    match = re.search(r"systemd (\d+)", version)
    if not match or int(match.group(1)) < 247:
        raise ValueError("需要 systemd 247 或以上（LoadCredential 支持）")
    if ROOT.exists() or ROOT.is_symlink():
        raise ValueError("专用安装目录已存在；为避免覆盖，已停止")
    for name in UNITS:
        for base in (
            "/etc/systemd/system",
            "/run/systemd/system",
            "/lib/systemd/system",
            "/usr/lib/systemd/system",
        ):
            if (Path(base) / name).exists() or (Path(base) / name).is_symlink():
                raise ValueError("同名服务已存在；为避免覆盖，已停止")
    # Bind without reuse: includes listeners bound on all IPv4 interfaces.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 18082))


def install(args, ask=input, secret=getpass.getpass):
    xray = check_binary(args.xray, args.xray_sha256, "Xray")
    cf = check_binary(args.cloudflared, args.cloudflared_sha256, "cloudflared")
    hostname = validate_hostname(args.hostname or ask("节点完整域名（例如 node.example.com）："))
    token = validate_token(secret("Cloudflare Tunnel token（输入不显示）："))
    if args.dry_run:
        destination = Path(args.dry_run).expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise ValueError("演练输出目录必须不存在，避免覆盖文件")
        print("演练：只在指定目录生成文件；不会运行二进制、systemctl 或修改防火墙。")
        print("二进制的版本尚未执行验证；正式安装会核对固定版本。")
        uri = prepare(destination, hostname, token, xray, cf, str(ROOT))
    else:
        preflight()
        check_version(xray, XRAY_VERSION, "Xray")
        check_version(cf, CF_VERSION, "cloudflared")
        print("安装摘要：仅创建 /opt/routepilot-server 和两个独立 systemd 服务。")
        print("Xray 仅监听 127.0.0.1:18082；不修改旧节点、旧隧道或防火墙。")
        print("域名：" + hostname)
        if ask("确认新建专用 Cloudflare Tunnel，且域名未占用。输入 INSTALL 安装：") != "INSTALL":
            raise ValueError("未确认，未安装")
        preflight()
        created_units = []
        owned_directory = False
        try:
            ROOT.mkdir(mode=0o700, exist_ok=False)
            owned_directory = True
            uri = prepare(ROOT, hostname, token, xray, cf, str(ROOT), create=False)
            # DynamicUser must traverse this directory; every secret stays 0600.
            ROOT.chmod(0o755)
            # Test Xray's complete configuration before exposing services.
            subprocess.run(
                [str(ROOT / "xray"), "run", "-test", "-config", str(ROOT / "config.json")],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            for name in UNITS:
                destination = Path("/etc/systemd/system") / name
                # Exclusive creation protects against a concurrent install.
                with destination.open("x") as stream:
                    stream.write((ROOT / name).read_text())
                destination.chmod(0o644)
                created_units.append(destination)
            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "--now", *UNITS], check=True)
            subprocess.run(["systemctl", "is-active", "--quiet", *UNITS], check=True)
        except Exception:
            if created_units:
                subprocess.run(
                    ["systemctl", "disable", "--now", *[p.name for p in created_units]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                for path in created_units:
                    path.unlink(missing_ok=True)
                subprocess.run(
                    ["systemctl", "daemon-reload"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if owned_directory and ROOT.is_dir() and not ROOT.is_symlink():
                shutil.rmtree(ROOT)
            raise ValueError(
                "安装失败，已撤回本次创建的服务；未修改原有服务。请检查依赖、版本和系统日志。"
            ) from None
    print("下一步在 Cloudflare 新隧道的 Published application routes 中设置：")
    print("Hostname: " + hostname)
    print("Service: HTTP / 127.0.0.1:18082；不要配置路径匹配条件。")
    print("保存并确认隧道在线后，将下面节点链接导入路由器：")
    print(uri)
    print("链接包含私人凭证，请勿公开。服务启动成功并不代表公网连接已验证。")
    return uri


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xray", required=True, help="官方 Linux Xray 二进制的本机路径")
    parser.add_argument("--xray-sha256", required=True, help="已核对的官方解压后二进制 SHA-256")
    parser.add_argument(
        "--cloudflared", required=True, help="官方 Linux cloudflared 二进制的本机路径"
    )
    parser.add_argument("--cloudflared-sha256", required=True)
    parser.add_argument("--hostname")
    parser.add_argument(
        "--dry-run", metavar="NEW_DIRECTORY", help="只生成文件，不安装、启动或执行二进制"
    )
    args = parser.parse_args()
    try:
        install(args)
    except (ValueError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        # Do not echo arbitrary subprocess output, which may contain secrets.
        if isinstance(error, ValueError):
            print(str(error), file=sys.stderr)
        else:
            print("操作未完成；未继续安装。请检查文件、权限或运行环境。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
