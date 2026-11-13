"""Own and apply the routing data plane."""

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

from runtime import atomic, command, uci_value, RUN, RULES
from masque_config import (
    TABLE,
    PRIORITY,
)
from mplane import Plane, cli, fw4_remove, net
from native import NativeDNS
from egress_guard import Guard
from dns_runtime import classification_control
from domain_sets import classification_fingerprints, migration_commands
from worker_backend import WorkerLineFailure, WorkerPlane

from rules_update import finish_interrupted_update
from warp_cli import TunnelStatus, UnknownTunnelStatus, read_status
from warp_bootstrap_dns import FIELDS as WARP_DNS_FIELDS

FALLBACK_MESSAGE = {
    "blocked": "已阻断启用断网保护设备的外网",
    "direct": "已撤回接管，允许宽带直连",
    "unknown": "当前网络路径未确认",
}

DHCP_LEASES = Path("/tmp/dhcp.leases")
UNKNOWN_PROBE_SECONDS = 4
UNKNOWN_STATUS_SECONDS = 300
GUARDED_STATUS_TIMEOUT = 2
UNKNOWN_RENEWAL_RESERVE = 2


def guard_fallback(guard, direct):
    if guard.state == "blocking":
        return "blocked"
    if guard.selectors or guard.state == "unverified":
        return "unknown"
    return "direct" if direct else "unknown"


class CaptureUnavailable(RuntimeError):
    def __init__(self, message, *, withdrawn=False):
        super().__init__(message)
        self.withdrawn = withdrawn


class CaptureCleanupUnconfirmed(RuntimeError):
    def __init__(self):
        super().__init__("接管清理未确认，正在停止线路")


def preflight(cfg):
    if uci_value("shadowsocksr.@global[0].global_server") not in ("", "nil"):
        raise ValueError("SSR 当前正在接管设备；本插件未改动 SSR，请先解决接管冲突")
    if (
        uci_value("openclash.config.enable") == "1"
        or uci_value("passwall.@global[0].enabled") == "1"
    ):
        raise CaptureUnavailable("其他分流软件正在启用，未接管任何设备")
    waiting = {}
    selected = []
    if cfg["main"].get("scope", "devices") != "lan":
        try:
            lines = DHCP_LEASES.read_text().splitlines()
        except FileNotFoundError:
            lines = []  # dnsmasq may not have written its first lease after boot.
        leases = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 3:
                leases.setdefault(parts[2], set()).add(parts[1].lower())
        known_macs = set().union(*leases.values())
        for policy in cfg["policies"]:
            if policy.get("enabled") != "1" or policy.get("mode") == "direct":
                continue
            ip, mac = policy.get("ip"), policy.get("mac", "").lower()
            if leases.get(ip) == {mac}:
                selected.append((ip, mac))
                continue
            reason = "lease_mismatch" if ip in leases or mac in known_macs else "lease_missing"
            waiting[policy["id"]] = dict(
                name=str(policy.get("name") or policy["id"])[:64], reason=reason
            )
    legacy = (
        cfg["main"].get("legacy_phone_ip", ""),
        cfg["main"].get("legacy_phone_mac", "").lower(),
    )
    if uci_value("warp_phone.main.enabled") == "1" and selected and legacy not in selected:
        raise ValueError("原手机 WARP 正在启用；本次仅支持对原手机进行替换试用")
    owned = (RUN / "active-owner").exists()
    if not owned:
        if command(["nft", "list", "table", "inet", "routepilot"], check=False).returncode == 0:
            raise ValueError("专用 nftables 表被占用")
        if command(["ip", "-4", "route", "show", "table", str(TABLE)], check=False).stdout.strip():
            raise ValueError("专用策略路由表被占用")
        if any(
            line.startswith(str(PRIORITY) + ":")
            for line in command(["ip", "-4", "rule", "show"]).stdout.splitlines()
        ):
            raise ValueError("专用路由优先级被占用")
    return waiting


def waiting_device_message(waiting):
    return "；".join(
        row["name"]
        + (
            "：等待 DHCP 租约"
            if row["reason"] == "lease_missing"
            else "：DHCP 地址与保存的策略不符"
        )
        for row in waiting.values()
    )


def routing_digest(cfg, directory=None):
    data = json.loads(json.dumps(cfg))
    for key in list(data["main"]):
        if key in ("apply_nonce", "update_nonce", "update_hours", *WARP_DNS_FIELDS) or key.endswith(
            "_url"
        ):
            data["main"].pop(key, None)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode())
    for path in sorted((directory if directory is not None else RULES).glob("*.txt")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


class TunnelPending(RuntimeError):
    """Withdraw capture while the existing official client reconnects."""


def clear_dns_redirects():
    spec = RUN / "dns.json"
    if not spec.is_file():
        return
    for policy in json.loads(spec.read_text())["policies"]:
        port = int(policy["port"])
        if not 12100 <= port <= 12107:
            raise ValueError("独立 DNS 端口超出接管范围")
        address = str(ipaddress.IPv4Network(policy["ip"]))
        for protocol in ("udp", "tcp"):
            # Existing DNAT connections outlive removal of their nft rule.
            command(
                [
                    "conntrack",
                    "-D",
                    "-p",
                    protocol,
                    "-s",
                    address,
                    "--dst-nat",
                    "--dport",
                    "53",
                    "--reply-port-src",
                    str(port),
                ],
                check=False,
                timeout=3,
            )
    spec.unlink()


class Service:
    def __init__(self):
        self.backend = "masque"
        self.plane = Plane()
        self.dns = NativeDNS()
        self.egress_guard = Guard()
        self.compiled = None
        self.exit_ip = ""
        self.runtime_id = ""
        self.update_job = None
        self.worker_failed_nonce = None
        self.recovery_started = None
        self.recovery_connect_sent = False
        self.unknown_since = None
        self.expected_exit = ""
        self.verified_exit = None
        self.table_epoch = None
        self.last_fw4_check = None

    def note_unknown_status(self):
        if self.unknown_since is None:
            self.unknown_since = time.monotonic()
        self.recovery_started = None
        self.recovery_connect_sent = False

    def verify_unknown_exit(self, vnet, expected, *, timeout=UNKNOWN_PROBE_SECONDS):
        self.note_unknown_status()
        if not expected and self.verified_exit is not None:
            plane, previous_vnet, ip = self.verified_exit
            if plane is self.plane and previous_vnet == vnet:
                expected = ip
        try:
            if timeout <= 0 or not ipaddress.IPv4Address(expected).is_global:
                raise ValueError("missing confirmed exit")
            started = time.monotonic()
            observed = ipaddress.IPv4Address(self.plane.probe(strict=True, timeout=timeout))
            if str(observed) != expected or time.monotonic() - started > timeout:
                raise ValueError("exit unconfirmed")
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            self.egress_guard.block("reconnect")
            raise UnknownTunnelStatus() from None
        return str(observed)

    def tunnel_ready(self):
        guarded = bool(self.egress_guard.selectors)
        state = read_status(cli, timeout=GUARDED_STATUS_TIMEOUT if guarded else None)
        if state is TunnelStatus.UNKNOWN:
            self.note_unknown_status()
            if not guarded and self.compiled:
                self.verify_unknown_exit(self.compiled["vnet"], self.expected_exit)
            return
        self.unknown_since = None
        if state is TunnelStatus.CONNECTED:
            self.recovery_started = None
            self.recovery_connect_sent = False
            return
        now = time.monotonic()
        if self.recovery_started is None:
            self.recovery_started = now
        if now - self.recovery_started >= 20:
            self.recovery_started = None
            raise RuntimeError("WARP 恢复等待超时，正在重建本插件专用客户端")
        if state is TunnelStatus.DISCONNECTED and not self.recovery_connect_sent:
            self.recovery_connect_sent = True
            try:
                cli("connect", check=False)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        raise TunnelPending("WARP 暂时中断，等待原客户端自动恢复")

    def suspend_capture(self, *, protection_reason="reconnect"):
        direct = self.withdraw(protection_reason=protection_reason)
        if self.backend == "worker" and protection_reason == "capture_unavailable":
            direct = fw4_remove("worker", verify=True) and direct
        self.dns.stop()
        self.compiled = None
        self.runtime_id = ""
        return direct

    def withdraw(self, *, protection_reason="capture_stopped"):
        self.egress_guard.block(protection_reason)
        owner = RUN / "active-owner"
        clean = True
        if owner.exists():
            # Keep the journal until all owned capture resources are verified absent.
            for args in (
                ["nft", "delete", "table", "inet", "routepilot"],
                [
                    "ip",
                    "-4",
                    "rule",
                    "del",
                    "pref",
                    str(PRIORITY),
                    "fwmark",
                    "0x5a000000/0xff000000",
                    "lookup",
                    str(TABLE),
                ],
                [
                    "ip",
                    "-4",
                    "route",
                    "del",
                    "default",
                    "via",
                    "10.254.239.2",
                    "dev",
                    "rp-host",
                    "table",
                    str(TABLE),
                ],
                ["ip", "-4", "route", "del", "default", "dev", "rp-backup", "table", str(TABLE)],
            ):
                try:
                    command(args, check=False)
                except Exception:
                    clean = False
            try:
                tables = json.loads(command(["nft", "-j", "list", "tables"]).stdout)["nftables"]
                rules = json.loads(command(["ip", "-j", "-4", "rule", "show"]).stdout)
                routes = json.loads(
                    command(["ip", "-j", "-4", "route", "show", "table", "all"]).stdout
                )
                clean = clean and not any(
                    t.get("table", {}).get("name") == "routepilot"
                    and t["table"].get("family") == "inet"
                    for t in tables
                )
                clean = clean and not any(r.get("priority") == PRIORITY for r in rules)
                clean = clean and not any(str(r.get("table")) == str(TABLE) for r in routes)
            except Exception:
                clean = False
            if clean:
                owner.unlink(missing_ok=True)
        try:
            self.dns.stop()
        except Exception:
            clean = False
        self.compiled = None
        self.table_epoch = None
        self.last_fw4_check = None
        return clean

    def stop(self, restore=True):
        self.unknown_since = None
        self.verified_exit = None
        self.expected_exit = ""
        self.recovery_started = None
        self.recovery_connect_sent = False
        clean = True
        if self.update_job and self.update_job.poll() is None:
            try:
                os.killpg(self.update_job.pid, signal.SIGTERM)
                self.update_job.wait(timeout=3)
            except Exception:
                try:
                    os.killpg(self.update_job.pid, signal.SIGKILL)
                    self.update_job.wait(timeout=3)
                except Exception:
                    clean = False
        self.update_job = None
        finish_interrupted_update()
        try:
            clean = self.withdraw() and clean
        except Exception:
            clean = False
        try:
            self.plane.stop()  # Includes only imok-owned resources.
            if self.backend != "worker":
                WorkerPlane().stop()
        except Exception:
            clean = False
        if restore and clean:
            try:
                clear_dns_redirects()
            except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
                clean = False
        legacy = "无需恢复旧手机策略"
        marker = RUN / "phone-paused"
        if marker.exists():
            legacy = "旧手机策略保持暂停"
            if restore:
                try:
                    if uci_value("warp_phone.main.enabled") == "1":
                        command(["/etc/init.d/warp-phone", "start"])
                        legacy = "旧手机策略启动命令已成功执行"
                    else:
                        legacy = "旧手机策略已由配置关闭，无需恢复"
                    marker.unlink(missing_ok=True)
                except Exception:
                    legacy = "旧手机策略恢复失败，请检查服务"
                    clean = False
        return dict(direct=clean, legacy=legacy)

    def tunnel_mss(self):
        try:
            args = [
                "ip",
                "-j",
                "link",
                "show",
                "dev",
                "rp-backup" if self.backend == "worker" else "CloudflareWARP",
            ]
            execute = command if self.backend == "worker" else net
            mtu = int(json.loads(execute(args, timeout=2).stdout)[0]["mtu"])
            if 576 <= mtu <= 65535:
                return mtu - 40
        except (OSError, RuntimeError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
            pass
        return 1280 - 40

    def install_capture_table(self, compiled, *, exists, old=None, live_dns=False):
        old_epoch = self.table_epoch
        epoch = uuid.uuid4().hex
        if live_dns:
            self.egress_guard.block("reconfigure")
            classification_control(RUN, self.runtime_id, old_epoch)
        transaction = ("delete table inet routepilot\n" if exists else "") + compiled["nft"]
        if exists and old:
            old_sets = classification_fingerprints(old)
            new_sets = classification_fingerprints(compiled)
            if any(old_sets.get(name) == value for name, value in new_sets.items()):
                started = time.monotonic()
                snapshot = command(["nft", "-n", "list", "table", "inet", "routepilot"], timeout=3)
                transaction += "\n" + migration_commands(
                    snapshot.stdout, old_sets, new_sets, time.monotonic() - started
                )
        command(["nft", "-f", "-"], data=transaction)
        if live_dns:
            classification_control(RUN, self.runtime_id, old_epoch, resume=epoch)
        self.table_epoch = epoch
        compiled["dns"]["table_epoch"] = epoch

    def apply(self, cfg, compiled):
        if self.backend != "worker":
            return self._apply(cfg, compiled)
        try:
            return self._apply(cfg, compiled)
        except (WorkerLineFailure, UnknownTunnelStatus):
            raise
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            try:
                clean = self.suspend_capture(protection_reason="capture_unavailable")
            except Exception:
                clean = False
            if not clean:
                raise CaptureCleanupUnconfirmed() from error
            message = (
                str(error) if isinstance(error, (ValueError, RuntimeError)) else "本地接管暂不可用"
            )
            raise CaptureUnavailable(message, withdrawn=True) from error

    def _apply(self, cfg, compiled):
        self.egress_guard.block("reconfigure")
        # Validate the complete nft transaction before pausing the original phone policy.
        exists = (
            command(["nft", "list", "table", "inet", "routepilot"], check=False).returncode == 0
        )
        candidate = ("delete table inet routepilot\n" if exists else "") + compiled["nft"]
        command(["nft", "-c", "-f", "-"], data=candidate)
        if self.backend != "worker":
            self.plane.protocol_preference = cfg["main"].get("warp_protocol", "auto")
        try:
            state = self.plane.connect(compiled["vnet"])
        except UnknownTunnelStatus:
            self.note_unknown_status()
            raise
        if compiled["nft"].startswith("table inet routepilot {"):
            from masque_config import mss_clamp

            compiled["nft"] = mss_clamp(compiled["nft"], self.backend, self.tunnel_mss())
            command(
                ["nft", "-c", "-f", "-"],
                data=("delete table inet routepilot\n" if exists else "") + compiled["nft"],
            )
        guarded = bool(self.egress_guard.selectors)
        expected = (
            "" if self.backend == "worker" else cfg["main"].get("expected_" + compiled["exit"], "")
        )
        if state is TunnelStatus.UNKNOWN:
            self.exit_ip = self.verify_unknown_exit(compiled["vnet"], expected)
        else:
            self.unknown_since = None
            self.exit_ip = (
                self.plane.probe(strict=True)
                if guarded and self.backend == "masque"
                else self.plane.probe()
            )
        observed_ip = ipaddress.IPv4Address(self.exit_ip)
        if guarded and not observed_ip.is_global:
            raise ValueError("未确认可用的公网出口，断网保护保持阻断")
        if expected and self.exit_ip != expected:
            raise ValueError("实际公网出口不符合所选服务器；未应用新设备策略")
        # Never change the original UCI or boot enablement. Pause its runtime only.
        taking_phone = any(
            d["mode"] != "direct"
            and (
                (
                    d.get("scope") == "lan"
                    and cfg["main"].get("legacy_phone_ip")
                    and ipaddress.ip_address(cfg["main"]["legacy_phone_ip"])
                    in ipaddress.ip_network(d["ip"])
                )
                or (
                    d["ip"] == cfg["main"].get("legacy_phone_ip")
                    and d["mac"] == cfg["main"].get("legacy_phone_mac", "").lower()
                )
            )
            for d in compiled["devices"]
        )
        if taking_phone and uci_value("warp_phone.main.enabled") == "1":
            atomic(RUN / "phone-paused", "restore original phone service on stop\n")
            command(["/etc/init.d/warp-phone", "stop"])
        elif not taking_phone and (RUN / "phone-paused").exists():
            if uci_value("warp_phone.main.enabled") == "1":
                command(["/etc/init.d/warp-phone", "start"])
            (RUN / "phone-paused").unlink()
        old = self.compiled
        self.dns.stop()
        try:
            self.plane.forward(compiled["devices"])
            self.last_fw4_check = time.monotonic()
            atomic(RUN / "active-owner", "routepilot devices and route table 51990\n")
            command(
                ["ip", "-4", "route", "replace", "default"]
                + (
                    ["dev", "rp-backup"]
                    if self.backend == "worker"
                    else ["via", "10.254.239.2", "dev", "rp-host"]
                )
                + ["table", str(TABLE)]
            )
            rules = command(["ip", "-4", "rule", "show"]).stdout
            if not any(line.startswith(str(PRIORITY) + ":") for line in rules.splitlines()):
                command(
                    [
                        "ip",
                        "-4",
                        "rule",
                        "add",
                        "pref",
                        str(PRIORITY),
                        "fwmark",
                        "0x5a000000/0xff000000",
                        "lookup",
                        str(TABLE),
                    ]
                )
            # fw4 reload triggered by the original service may recreate its table only.
            exists = (
                command(["nft", "list", "table", "inet", "routepilot"], check=False).returncode == 0
            )
            self.install_capture_table(compiled, exists=exists, old=old)
            runtime_id = uuid.uuid4().hex
            compiled["dns"]["runtime_id"] = runtime_id
            if self.backend == "worker":
                compiled["dns"]["proxy_dns_override"] = "https://dns.google/dns-query"
                for policy in compiled["dns"]["policies"]:
                    policy["proxy_dns_override"] = compiled["dns"]["proxy_dns_override"]
            atomic(RUN / "dns.json", json.dumps(compiled["dns"]), 0o600)
            self.dns.start(RUN / "dns.json")
            time.sleep(0.5)
            if not self.dns.alive():
                raise RuntimeError("独立 DNS 未启动，已撤回设备策略")
            self.compiled = compiled
            self.runtime_id = runtime_id
            current_addresses = {d["ip"] for d in compiled["devices"]}
            previous_addresses = {d["ip"] for d in old["devices"]} if old else set()
            if (
                old
                and old.get("config_digest") == compiled.get("config_digest")
                and old.get("config_digest")
                and current_addresses != previous_addresses
            ):
                addresses = current_addresses ^ previous_addresses
            else:
                addresses = current_addresses | previous_addresses
            for address in sorted(addresses):
                command(["conntrack", "-D", "-s", address], check=False)
                if self.backend != "worker":
                    net(["conntrack", "-D", "-s", address], check=False)
        except Exception:
            if self.backend != "worker":
                self.stop(restore=False)
            raise
        self.expected_exit = expected
        if self.backend == "masque" and observed_ip.is_global:
            self.verified_exit = (self.plane, compiled["vnet"], self.exit_ip)

    def confirm_egress_ready(self):
        if not self.egress_guard.selectors:
            return
        deadline = None
        if getattr(self, "unknown_since", None) is not None:
            now = time.monotonic()
            deadline = min(
                self.egress_guard.renewal_deadline(),
                now + UNKNOWN_PROBE_SECONDS + UNKNOWN_RENEWAL_RESERVE,
            )
            timeout = min(UNKNOWN_PROBE_SECONDS, deadline - now - UNKNOWN_RENEWAL_RESERVE)
            self.verify_unknown_exit(self.compiled["vnet"], self.expected_exit, timeout=timeout)

        try:
            dns = json.loads((RUN / "dns-state.json").read_text())
            ready = (
                isinstance(dns, dict)
                and dns.get("state") == "running"
                and self.runtime_id
                and dns.get("runtime_id") == self.runtime_id
                and (
                    getattr(self, "table_epoch", None) is None
                    or dns.get("table_epoch") == self.table_epoch
                    and not dns.get("classification_paused")
                )
                and -1 <= time.time() - float(dns.get("time", 0)) <= 5
            )
        except (OSError, ValueError, TypeError, OverflowError):
            ready = False
        if not ready:
            self.egress_guard.block("dns_not_ready")
            return False
        if deadline is None:
            self.egress_guard.allow()
            return True

        try:
            self.egress_guard.allow(deadline=deadline)
        except (TimeoutError, subprocess.TimeoutExpired):
            self.egress_guard.block("reconnect")
            raise UnknownTunnelStatus() from None
        return True

    def health(self):
        if not self.compiled:
            return
        log = RUN / "client.log"
        if log.exists() and log.stat().st_size > 2 * 1024 * 1024:
            log.write_text("")
        try:
            if not self.dns.alive():
                raise RuntimeError("设备 DNS 已停止，将撤回当前线路接管")
            if self.backend != "worker":
                self.tunnel_ready()
            if self.unknown_since is None:
                self.plane.optimize()
            self.check_capture()
        except WorkerLineFailure:
            raise
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
            if self.backend != "worker":
                raise
            raise CaptureUnavailable("备用线路本地接管暂不可用，正在恢复") from error

    def check_capture(self):
        now = time.monotonic()
        if (
            self.egress_guard.selectors
            or self.last_fw4_check is None
            or now - self.last_fw4_check >= 12
        ):
            listing = command(["nft", "-a", "list", "chain", "inet", "fw4", "forward"]).stdout
            tag = (
                "routepilot-worker-device-out"
                if self.backend == "worker"
                else "routepilot-device-out"
            )
            if (
                tag not in listing
                or (self.backend == "worker" and "routepilot-worker-udp" not in listing)
                or (self.backend != "worker" and "routepilot-control-out" not in listing)
            ):
                self.plane.forward(self.compiled["devices"])
            self.last_fw4_check = now
        if command(["nft", "list", "table", "inet", "routepilot"], check=False).returncode:
            self.install_capture_table(self.compiled, exists=False, live_dns=True)
        if not any(
            line.startswith(str(PRIORITY) + ":")
            for line in command(["ip", "-4", "rule", "show"]).stdout.splitlines()
        ):
            raise RuntimeError("专用策略路由规则已丢失，将撤回 WARP 接管")
        if not command(
            ["ip", "-4", "route", "show", "table", str(TABLE)], check=False
        ).stdout.strip():
            raise RuntimeError("专用路由已丢失，将撤回 WARP 接管")
