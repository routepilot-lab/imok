#!/usr/bin/env python3
"""Remove only this wizard's exact services and known files, preserving others."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/routepilot-server")
UNITS = ("routepilot-server.service", "routepilot-cloudflared.service")
FILES = {
    "xray",
    "cloudflared",
    "config.json",
    "tunnel-token",
    "client-link.txt",
    "install-state.json",
    *UNITS,
}


def uninstall(ask=input):
    if os.geteuid() != 0:
        raise ValueError("需要 root 权限")
    if ROOT.is_symlink() or not ROOT.is_dir():
        raise ValueError("没有找到向导创建的独立安装目录")
    state = json.loads((ROOT / "install-state.json").read_text())
    if state.get("owner") != "routepilot-server" or state.get("units") != list(UNITS):
        raise ValueError("安装归属信息不匹配；拒绝删除")
    paths = []
    for name in UNITS:
        target = Path("/etc/systemd/system") / name
        if target.is_symlink() or (ROOT / name).is_symlink():
            raise ValueError("服务文件已变化；拒绝自动删除")
        if target.exists():
            if target.read_bytes() != (ROOT / name).read_bytes():
                raise ValueError("服务文件已被修改；请人工检查后再卸载")
            paths.append(target)
    print("只删除 imok 的两个服务及其已知配置文件，不更改 Cloudflare 网页里的隧道或DNS。")
    if ask("输入 REMOVE 确认删除此安装的密钥与节点配置：") != "REMOVE":
        raise ValueError("已取消")
    if paths:
        subprocess.run(["systemctl", "disable", "--now", *[p.name for p in paths]], check=True)
        for path in paths:
            path.unlink()
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    for name in FILES:
        target = ROOT / name
        if target.is_file() and not target.is_symlink():
            target.unlink()
    if not any(ROOT.iterdir()):
        ROOT.rmdir()
    else:
        print("目录中有未知文件或符号链接，已保留它们。")
    print("卸载完成。若不再使用，请自行删除对应 Cloudflare 专用隧道和域名路由。")


if __name__ == "__main__":
    try:
        uninstall()
    except (ValueError, OSError, subprocess.SubprocessError):
        print("卸载未完成；服务文件可能已变更，请人工核对。未继续删除未知内容。", file=sys.stderr)
        sys.exit(1)
