"""Shared imok I/O and rule validation; the service entry is masque.main.

No shell evaluation or writes to SSR/network/firewall/DHCP configuration.
"""

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

RUN = Path("/var/run/routepilot")
RULES = Path("/etc/routepilot/rules")


def command(args, *, data=None, check=True, timeout=30):
    result = subprocess.run(
        args, input=data, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )
    if check and result.returncode:
        # Never surface subprocess diagnostics: xray may quote secret configuration.
        raise RuntimeError(args[0] + " 检查失败；请查看本机管理员诊断")
    return result


def median_number(values):
    """Median of a nonempty sequence of numeric measurements."""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def atomic(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_config(*, timeout=None):
    # ubus avoids shell parsing of UCI values and preserves list types.
    args = ["ubus", "call", "uci", "get", '{"config":"routepilot"}']
    options = {} if timeout is None else {"timeout": timeout}
    values = json.loads(command(args, **options).stdout)["values"]
    cfg = {"main": {}, "connections": [], "policies": []}
    for key, value in values.items():
        value = dict(value)
        kind = value.pop(".type", "")
        value["id"] = key
        if kind == "main":
            cfg["main"] = value
        elif kind == "connection":
            cfg["connections"].append(value)
        elif kind == "policy":
            cfg["policies"].append(value)
    return cfg


def uci_value(key):
    return command(["uci", "-q", "get", key], check=False).stdout.strip()


def validate_rule_text(kind, text):
    if len(text.encode()) > 4 * 1024 * 1024:
        raise ValueError("规则超过 4MB")
    clean = []
    for raw in text.splitlines():
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if kind == "china":
            net = ipaddress.ip_network(value, strict=False)
            if net.version != 4 or net.prefixlen < 8 or not net.is_global:
                raise ValueError("中国 IP 规则必须是公网 IPv4 CIDR，前缀至少 /8")
            value = str(net)
        else:
            if len(value) > 253 or not re.fullmatch(
                r"(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}", value
            ):
                raise ValueError(
                    "域名规则格式错误：只接受每行一个普通域名，不接受脚本/GFWList 编码"
                )
            value = value.lower()
        clean.append(value)
    if not clean:
        raise ValueError("规则为空，保留上一版本")
    if len(clean) > 100000:
        raise ValueError("规则条目过多")
    return "\n".join(sorted(set(clean))) + "\n"
