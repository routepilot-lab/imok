"""Discover eligible IPv4 LAN interfaces."""

import ipaddress
import json
import re

from runtime import command


def lan_interfaces(include_inactive=False):
    addresses = json.loads(command(["ip", "-j", "-4", "addr", "show"]).stdout)
    wan = {
        v.get("dev")
        for v in json.loads(command(["ip", "-j", "-4", "route", "show", "default"]).stdout)
    }
    private = [ipaddress.ip_network(v) for v in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    result = []
    for item in addresses:
        name = item.get("ifname", "")
        if (
            name in wan
            or name in ("lo", "rp-host", "CloudflareWARP")
            or "POINTOPOINT" in item.get("flags", [])
        ):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", name):
            continue
        nets = []
        for a in item.get("addr_info", []):
            if (
                a.get("family") != "inet"
                or a.get("scope") != "global"
                or not 16 <= a.get("prefixlen", 0) <= 30
            ):
                continue
            net = ipaddress.ip_network(str(a["local"]) + "/" + str(a["prefixlen"]), strict=False)
            if any(net.subnet_of(p) for p in private):
                nets.append(str(net))
        if len(set(nets)) == 1:
            result.append({"name": name, "network": nets[0], "up": "UP" in item.get("flags", [])})
    if include_inactive:
        config = json.loads(
            command(["ubus", "call", "uci", "get", '{"config":"network"}']).stdout
        ).get("values", {})
        known = {v["name"] for v in result}
        for name, item in config.items():
            dev = item.get("device", item.get("ifname", ""))
            if (
                item.get(".type") != "interface"
                or not isinstance(dev, str)
                or "wan" in name.lower()
                or not (dev.startswith("br-") or name in ("lan", "guest", "iot"))
                or dev in known
                or dev in wan
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", dev)
            ):
                continue
            address = item.get("ipaddr", "")
            if not isinstance(address, str) or not address:
                continue
            try:
                net = ipaddress.ip_network(
                    address
                    if "/" in address
                    else address + "/" + str(item.get("netmask", "255.255.255.0")),
                    strict=False,
                )
            except ValueError:
                continue
            if (
                net.version == 4
                and 16 <= net.prefixlen <= 30
                and any(net.subnet_of(p) for p in private)
            ):
                result.append({"name": dev, "network": str(net), "up": False})
                known.add(dev)
    return result
