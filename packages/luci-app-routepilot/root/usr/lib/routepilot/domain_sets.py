"""Carry active DNS classifications across atomic replacement of an owned table."""

import ipaddress
import math
import re

SET_NAME = re.compile(r"rp[dpg]_[A-Za-z0-9_]{1,32}")
ELEMENT = re.compile(r"([0-9.]+)(?:\s+timeout\s+\w+)?\s+expires\s+(\w+)")
DURATION = re.compile(r"(\d+)(ms|[dhms])")
UNITS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


def classification_fingerprints(compiled):
    devices = {
        device["dns_port"]: (device["id"], device["ip"], device["mac"], device["lan_interface"])
        for device in compiled["devices"]
        if "dns_port" in device
    }
    fingerprints = {}
    for policy in compiled["dns"].get("policies", ()):
        identity = devices[policy["port"]]
        for set_key, domains_key in (
            ("direct_set", "custom_direct_domains"),
            ("proxy_set", "custom_proxy_domains"),
            ("mode_set", "mode_domains"),
        ):
            domains = tuple(sorted({domain.lower() for domain in policy.get(domains_key, ())}))
            mode = policy["mode"] if set_key == "mode_set" else None
            fingerprints[policy[set_key]] = (identity, mode, domains)
    return fingerprints


def migration_commands(text, old, new, elapsed):
    matching = {
        name for name, value in new.items() if old.get(name) == value and SET_NAME.fullmatch(name)
    }
    if not matching:
        return ""
    result = []
    for match in re.finditer(r"\bset (\w+)\s*\{[^{}]*?\belements\s*=\s*\{([^{}]*)\}", text):
        name, body = match.groups()
        if name not in matching:
            continue
        elements = []
        for item in body.split(","):
            parsed = ELEMENT.fullmatch(item.strip())
            if not parsed:
                continue
            raw_ip, duration = parsed.groups()
            units = DURATION.findall(duration)
            if "".join(amount + unit for amount, unit in units) != duration:
                continue
            try:
                ip = ipaddress.IPv4Address(raw_ip)
                remaining = math.floor(
                    sum(int(amount) * UNITS[unit] for amount, unit in units) - elapsed
                )
            except (ValueError, OverflowError):
                continue
            if 0 < remaining <= 3600:
                elements.append(f"{ip} timeout {remaining}s")
        if elements:
            result.append(f"add element inet routepilot {name} {{ {', '.join(elements)} }}\n")
    return "".join(result)
