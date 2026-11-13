"""Shared public-rule reader and validation constants for current WARP routing."""

import ipaddress
import re
from pathlib import Path


PRIVATE4 = [
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
]


ID = re.compile(r"^[A-Za-z0-9_]{1,64}$")


MAC = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


class ConfigError(ValueError):
    """Safe-to-display validation error; never includes credential values."""


def read_rules(directory, name, kind):
    path = Path(directory) / name
    if not path.exists():
        return []
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ConfigError("Rule file exceeds 8 MiB: " + name)
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if kind == "cidr":
            try:
                net = ipaddress.ip_network(line, strict=True)
            except ValueError:
                raise ConfigError("Invalid IPv4 CIDR in " + name) from None
            if net.version != 4:
                raise ConfigError("Only IPv4 CIDRs are supported in " + name)
            # Private and default routes do not belong in a public country list.
            if net.prefixlen < 8 or not net.network_address.is_global:
                raise ConfigError("Non-public network in " + name)
            line = str(net)
        else:
            line = line.lower().rstrip(".")
            if not DOMAIN.fullmatch(line) or "." not in line:
                raise ConfigError("Invalid plain domain in " + name)
            line = "domain:" + line
        result.append(line)
        if len(result) > 200000:
            raise ConfigError("Too many rules in " + name)
    return list(dict.fromkeys(result))
