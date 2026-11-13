"""Early, independent firewall for imOK's optional fail-closed mode."""

import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

from compiler import MAC
from connection_events import record
from public_errors import ControlError
from runtime import RUN, command

TABLE = "routepilot_egress_guard"
PROXY_MARK = 0x5A000000
DIRECT_MARK = 0x5B000000
MARK_MASK = 0xFF000000
LEASE_SECONDS = 12
BOOT_LINK = Path("/etc/rc.d/S18routepilot-egress-guard")
FIREWALL_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    subprocess.TimeoutExpired,
)
OFFLOAD_ERROR = "检测到防火墙流量卸载。请先关闭软、硬件流量卸载再启用断网保护"


def policy_protected(policy):
    if policy.get("enabled") != "1" or policy.get("mode") == "direct":
        return False
    value = policy.get("egress_guard", "0")
    if value not in ("inherit", "0", "1"):
        raise ValueError("设备断网保护设置无效")
    return value == "1"


def scope(cfg):
    """Use device MACs, which exist in config before DHCP leases do."""
    mode = cfg["main"].get("scope", "devices")
    if mode not in ("lan", "devices"):
        raise ValueError("断网保护的接管范围无效")
    if mode == "lan":
        return ()
    macs = {str(p.get("mac", "")).lower() for p in cfg["policies"] if policy_protected(p)}
    if any(not MAC.fullmatch(mac) or int(mac[:2], 16) & 1 for mac in macs):
        raise ValueError("断网保护需要为所选设备填写有效的 MAC 地址")
    return tuple("ether saddr " + mac for mac in sorted(macs))


def rules(selectors):
    mark = f"meta mark & {MARK_MASK:#010x}"
    reserved = f"{{ {PROXY_MARK:#010x}, {DIRECT_MARK:#010x} }}"
    return "\n".join(
        [
            f"table inet {TABLE} {{",
            f" set ready {{ type mark; flags timeout; timeout {LEASE_SECONDS}s; }}",
            " chain reset_marks { type filter hook prerouting priority -91; policy accept;",
            *(
                f"  {match} {mark} {reserved} meta mark set (meta mark & 0x00ffffff)"
                for match in selectors
            ),
            " }",
            " chain forward_guard { type filter hook forward priority -15; policy accept;",
            *(f"  {match} jump protected_forward" for match in selectors),
            " }",
            " chain protected_forward {",
            f'  meta nfproto ipv4 {mark} == {PROXY_MARK:#010x} {mark} @ready oifname {{ "rp-host", "rp-backup" }} return',
            f'  meta nfproto ipv4 {mark} == {DIRECT_MARK:#010x} {mark} @ready oifname != {{ "rp-host", "rp-backup" }} return',
            "  counter drop",
            " }",
            " chain input_guard { type filter hook input priority -15; policy accept;",
            *(
                f"  {match} meta l4proto {{ tcp, udp }} th dport {{ 53, 12100-12107 }} jump protected_dns"
                for match in selectors
            ),
            " }",
            " chain protected_dns {",
            f"  meta nfproto ipv4 th dport 12100-12107 ct status dnat {mark} @ready return",
            "  counter drop",
            " }",
            "}",
            "",
        ]
    )


def boot_config():
    """Read committed UCI without rpcd or netifd, which may not be up yet."""
    with tempfile.TemporaryDirectory(prefix="imok-uci-") as delta:
        data = command(["uci", "-q", "-t", delta, "export", "routepilot"], timeout=5).stdout
    cfg = {"main": {}, "policies": []}
    row = None
    for line in data.splitlines():
        parts = shlex.split(line, comments=True)
        if not parts:
            continue
        if parts[0] == "config":
            row = {} if len(parts) >= 2 and parts[1] in ("main", "policy") else None
            if row is not None:
                if parts[1] == "main":
                    cfg["main"] = row
                else:
                    cfg["policies"].append(row)
        elif row is not None and len(parts) == 3:
            _, key, value = parts
            if key not in ("egress_guard", "scope", "enabled", "mode", "mac"):
                continue
            if row is cfg["main"] and key == "egress_guard":
                continue  # Obsolete whole-network setting from older conffiles.
            if parts[0] == "list":
                row.setdefault(key, []).append(value)
            elif parts[0] == "option":
                row[key] = value
    return cfg


def present():
    tables = json.loads(command(["nft", "-j", "list", "tables"], timeout=5).stdout)["nftables"]
    return any(
        item.get("table", {}).get("name") == TABLE and item["table"].get("family") == "inet"
        for item in tables
    )


def transact(text, *, timeout=5):
    command(["nft", "-f", "-"], data=text, timeout=timeout)


def offload_active(*, timeout=5):
    entries = json.loads(command(["nft", "-j", "list", "flowtables"], timeout=timeout).stdout)[
        "nftables"
    ]
    if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
        raise ValueError("Invalid nftables result")
    return any("flowtable" in item for item in entries)


def validate_activation():
    """Read-only check before persisting a requested protection setting."""
    try:
        defaults = command(["uci", "-q", "show", "firewall.@defaults[0]"], timeout=5).stdout
        options = {
            key.rsplit(".", 1)[-1]: value
            for key, value in (line.split("=", 1) for line in defaults.splitlines() if "=" in line)
        }
        if not any(line.endswith("=defaults") for line in defaults.splitlines()):
            raise ValueError("Missing firewall defaults")
        configured = any(
            options.get(key, "").strip("'\"").lower() in ("1", "true", "on", "yes", "enabled")
            for key in ("flow_offloading", "flow_offloading_hw")
        )
        conflict = configured or offload_active()
    except FIREWALL_ERRORS:
        raise ControlError("无法确认防火墙流量卸载状态，本次未保存。请稍后重试。") from None
    if conflict:
        raise ControlError(OFFLOAD_ERROR + "。本次未保存，原联网设置保持不变。")


class Guard:
    def __init__(self):
        self.selectors = ()
        self.scope_mode = None
        self.state = "off"
        self.renewed_at = 0.0
        self.initialized = False
        self.last_record = None

    def _record(self, state, reason):
        event = {"state": state, "reason": reason, "scope": self.scope_mode}
        if event != self.last_record:
            record(RUN, "protection", event)
            self.last_record = event

    def _transact(self, text, **options):
        try:
            transact(text, **options)
        except FIREWALL_ERRORS:
            self._record("unverified", "firewall_error")
            raise

    def configure(self, cfg):
        main = cfg["main"]
        mode = main.get("scope", "devices")
        self.scope_mode = mode if mode in ("lan", "devices") else None
        try:
            active = main.get("enabled") == "1"
            selected = scope(cfg) if active else ()
        except ValueError:
            self.block(None)
            self.state = "unverified"
            self._record(self.state, "invalid_config")
            raise
        if self.initialized and self.state == "off" and not selected:
            return  # Existing installations should not run extra nft commands.
        try:
            exists = present()
        except FIREWALL_ERRORS:
            self.state = "unverified"
            self._record(self.state, "firewall_error")
            raise
        if selected != self.selectors or bool(selected) != exists:
            remove = f"delete table inet {TABLE}\n" if exists else ""
            self._transact(remove + (rules(selected) if selected else ""))
            reason = (
                "startup"
                if not self.initialized
                else "firewall_restored"
                if selected == self.selectors
                else "settings"
            )
            self.selectors = selected
            self.state = "blocking" if selected else "off"
            self._record(self.state, reason if selected else "opt_out" if active else "disabled")
        elif not selected:
            if not self.initialized or self.state != "off":
                self._record("off", "opt_out" if self.initialized else "startup")
            self.state = "off"
        self.initialized = True

    def block(self, reason="capture_stopped"):
        if self.state == "ready":
            self._transact(f"flush set inet {TABLE} ready\n")
            self.state = "blocking"
            if reason:
                self._record(self.state, reason)

    def require_supported(self, **options):
        if not self.selectors:
            return
        if not BOOT_LINK.is_symlink() or BOOT_LINK.resolve() != Path(
            "/etc/init.d/routepilot-egress-guard"
        ):
            self.block(None)
            self.state = "unverified"
            self._record(self.state, "boot_hook")
            raise ValueError("断网保护的早期启动入口未启用，请核对插件安装")
        try:
            offloaded = offload_active(**options)
        except FIREWALL_ERRORS:
            self._record("unverified", "firewall_error")
            raise
        if offloaded:
            self.block(None)
            self.state = "unverified"
            self._record(self.state, "flow_offload")
            raise ValueError(OFFLOAD_ERROR)

    def renewal_deadline(self):
        if self.state == "ready":
            # Reserve time before kernel expiry, including time spent installing the lease.
            return self.renewed_at + LEASE_SECONDS - 0.5
        return float("inf")

    def allow(self, *, deadline=None):
        if not self.selectors:
            return

        def remaining():
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise TimeoutError("断网保护续期预算已用尽")
            return timeout

        if deadline is None:
            self.require_supported()
            options = {}
        else:
            self.require_supported(timeout=remaining())
            options = {"timeout": remaining()}
        started = time.monotonic()
        self._transact(
            f"flush set inet {TABLE} ready\n"
            f"add element inet {TABLE} ready {{ {PROXY_MARK:#010x}, {DIRECT_MARK:#010x} }}\n",
            **options,
        )
        self.renewed_at = started
        self.state = "ready"
        self._record(self.state, "verified")


if __name__ == "__main__":
    if sys.argv[1:] == ["boot"]:
        cfg = boot_config()
        Guard().configure(cfg)
    elif sys.argv[1:] == ["remove"]:
        if present():
            transact(f"delete table inet {TABLE}\n")
    else:
        raise SystemExit("Usage: egress_guard.py boot|remove")
