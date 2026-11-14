"""Isolated official-client namespace. Never changes the host default route."""

import json
import ipaddress
import os
import re
from pathlib import Path
import signal
import stat
import subprocess
import time

from runtime import command, atomic, RUN
from udp_buffers import tune
from runtime_guard import guarded_start, release_plane
from warp_bootstrap_dns import bootstrap_resolver, select_resolver
from warp_cli import (
    InvalidDirectory,
    TunnelStatus,
    UnknownTunnelStatus,
    parse_directory,
    parse_selected_network,
    read_status,
)

ROOT = Path("/opt/routepilot/runtime")
NS = "routepilot"
EXIT_SELECTION_MISMATCH = "WARP 当前出口与已保存出口不同；检测未切换出口"
HOST = "rp-host"
PEER = "rp-client"
NETNS_CONFIG = Path("/etc/netns") / NS / "resolv.conf"


def net(args, **kwargs):
    return command(["ip", "netns", "exec", NS] + args, **kwargs)


def cli(*args, check=True, timeout=None):
    return net(
        ["chroot", str(ROOT), "/usr/bin/warp-cli", "--accept-tos", *args],
        check=check,
        timeout=timeout
        if timeout is not None
        else (5 if "--help" in args or args and args[0] in ("status", "settings") else 30),
    )


def fw4_remove(backend="masque", *, verify=False):
    if backend not in ("masque", "worker"):
        raise ValueError("专用防火墙归属无效")
    clean = True
    for chain in ("forward", "input"):
        args = ["nft", "-a", "list", "chain", "inet", "fw4", chain]
        listing = command(args, check=False)
        for line in listing.stdout.splitlines():
            worker = 'comment "routepilot-worker-' in line
            if 'comment "routepilot-' in line and worker == (backend == "worker"):
                handle = line.rsplit(" # handle ", 1)[-1].strip()
                if handle.isdigit():
                    command(
                        ["nft", "delete", "rule", "inet", "fw4", chain, "handle", handle],
                        check=False,
                    )
        if verify:
            confirmed = command(args, check=False)
            clean = (
                clean
                and confirmed.returncode == 0
                and not any(
                    'comment "routepilot-worker-' in line for line in confirmed.stdout.splitlines()
                )
            )
    return clean


def fw4_install(devices, proxy="rp-host", control=True):
    if proxy not in ("rp-host", "rp-backup"):
        raise ValueError("专用接口无效")
    routes = json.loads(command(["ip", "-j", "-4", "route", "get", "1.1.1.1"]).stdout)
    wan = routes[0].get("dev", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", wan) or wan in (
        "rp-host",
        "rp-backup",
        "CloudflareWARP",
        "warp_test",
    ):
        raise ValueError("无法确定独立的宽带接口，未修改分流")
    backend = "worker" if proxy == "rp-backup" else "masque"
    fw4_remove(backend)
    command(["nft", "-f", "-"], data=fw4_rule_text(devices, wan, proxy, control))


def fw4_rule_text(devices, wan, proxy, control, *, preview_offload=False):
    """Render the production rules or the offline offload acceptance candidate."""
    backend = "worker" if proxy == "rp-backup" else "masque"
    lines = [
        'insert rule inet fw4 forward iifname "rp-host" ip saddr 10.254.239.2 oifname "eth1" accept comment "routepilot-control-out"',
        'insert rule inet fw4 forward oifname "rp-host" ip daddr 10.254.239.2 ct state established,related accept comment "routepilot-control-return"',
    ]
    if not control:
        lines = []
    for index, p in enumerate(devices):
        if p["mode"] == "direct":
            continue
        # Before fw4's flow-add rule; selected flows cannot retain a stale exit.
        source = (
            ('iifname "%s" ip saddr %s' % (p["lan_interface"], p["ip"]))
            if p.get("scope") == "lan"
            else ("ether saddr %s ip saddr %s" % (p["mac"], p["ip"]))
        )
        lines.append(
            'insert rule inet fw4 input iifname "%s" ip saddr %s meta l4proto { tcp, udp } th dport %d ct status dnat accept comment "routepilot-device-dns"'
            % (p["lan_interface"], p["ip"], p.get("dns_port", 12100 + index))
        )
        if preview_offload and p.get("scope") == "device" and p.get("egress_guard") is False:
            egress = '"rp-host"'
            ingress = 'iifname "rp-host" '
        else:
            egress = '{ "rp-host", "eth1" }'
            ingress = ""
        lines.extend(
            [
                'insert rule inet fw4 forward %s oifname %s accept comment "routepilot-device-out"'
                % (source, egress),
                'insert rule inet fw4 forward %sip daddr %s ct state established,related accept comment "routepilot-device-return"'
                % (ingress, p["ip"]),
            ]
        )
    text = (
        "\n".join(lines).replace('"eth1"', '"' + wan + '"').replace('"rp-host"', '"' + proxy + '"')
        + "\n"
    )
    if backend == "worker":
        text = text.replace('comment "routepilot-', 'comment "routepilot-worker-')
        # Reject only packets classified for this backup. Domestic/direct UDP
        # and the DNS redirect retain the user's existing policy.
        text += 'insert rule inet fw4 forward oifname "rp-backup" meta l4proto udp reject with icmp type port-unreachable comment "routepilot-worker-udp"\n'
    return text


class Plane:
    def __init__(self):
        self.owned = False
        self.client_pid = None
        self.last_buffer_check = 0
        self.protocol = None
        self.protocol_managed = None
        self.protocol_preference = "auto"
        self.organization = ""
        self.protocol_checked = 0
        self.protocol_observed_at = 0
        self.protocol_warning = ""
        self.dns_preferences = None
        self.warp_dns = None

    def exists(self):
        return any(
            line.split()[0] == NS
            for line in command(["ip", "netns", "list"]).stdout.splitlines()
            if line.split()
        )

    @guarded_start
    def start(self):
        if self.owned and self.exists():
            return
        if not (ROOT / "usr/bin/warp-svc").exists():
            raise ValueError("尚未安装适用于此路由器的 MASQUE 运行环境")
        if self.exists() or command(["ip", "link", "show", HOST], check=False).returncode == 0:
            raise ValueError("专用网络名称被占用，未修改现有网络")
        if (
            command(["nft", "list", "table", "inet", "routepilot_control"], check=False).returncode
            == 0
        ):
            raise ValueError("专用控制表被占用")
        for route in json.loads(
            command(["ip", "-j", "-4", "route", "show", "table", "main"]).stdout
        ):
            if route.get("dst", "default") != "default" and ipaddress.ip_network(
                route["dst"], strict=False
            ).overlaps(ipaddress.ip_network("10.254.239.0/30")):
                raise ValueError("专用隧道网段与现有网络重叠，未修改现有网络")
        wan = json.loads(command(["ip", "-j", "-4", "route", "get", "1.1.1.1"]).stdout)[0].get(
            "dev", ""
        )
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", wan):
            raise ValueError("宽带接口名称无效")
        # These identifiers are private to this package. Journal intent first.
        atomic(RUN / "masque-owner.json", json.dumps({"namespace": NS, "root": str(ROOT)}))
        self.owned = True
        try:
            for name in (
                "proc",
                "sys",
                "dev/net",
                "run/dbus",
                "tmp",
                "root",
                "var/lib/cloudflare-warp",
            ):
                (ROOT / name).mkdir(parents=True, exist_ok=True)
            from warp_control import configure_protocol

            configure_protocol(ROOT, self.protocol_preference, self.organization)
            (ROOT / "tmp").chmod(0o1777)
            for name, minor in (("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)):
                path = ROOT / "dev" / name
                if not path.exists():
                    os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(1, minor))
                    path.chmod(0o666)
            tun = ROOT / "dev/net/tun"
            if not tun.exists():
                os.mknod(tun, stat.S_IFCHR | 0o600, os.makedev(10, 200))
            command(["ip", "netns", "add", NS])
            command(["ip", "link", "add", HOST, "type", "veth", "peer", "name", PEER])
            command(["ip", "link", "set", PEER, "netns", NS])
            command(["ip", "addr", "add", "10.254.239.1/30", "dev", HOST])
            command(["ip", "link", "set", HOST, "up"])
            net(["ip", "link", "set", "lo", "up"])
            net(["ip", "addr", "add", "10.254.239.2/30", "dev", PEER])
            net(["ip", "link", "set", PEER, "up"])
            net(["ip", "route", "add", "default", "via", "10.254.239.1"])
            net(["sysctl", "-w", "net.ipv4.ip_forward=1"])
            net(["sysctl", "-w", "net.ipv4.conf.all.rp_filter=0"])
            net(["sysctl", "-w", "net.ipv4.conf.rp-client.rp_filter=0"])
            command(
                ["nft", "-f", "-"],
                data="""table inet routepilot_control {
 chain namespace_nat { type nat hook postrouting priority 99; policy accept;
  ip saddr 10.254.239.2 oifname "eth1" masquerade
 }
}
""".replace('"eth1"', '"' + wan + '"'),
            )
            fw4_install([])
            if self.dns_preferences is None:
                resolver = bootstrap_resolver(wan, net)
            else:
                resolver, self.warp_dns = select_resolver(wan, net, self.dns_preferences)
            atomic(NETNS_CONFIG, resolver, 0o644)
            atomic(ROOT / "etc/resolv.conf", resolver, 0o644)
            if not os.path.ismount(ROOT / "proc"):
                command(["mount", "-t", "proc", "proc", str(ROOT / "proc")])
            # Regenerate machine id only on a fresh installation, not a migration.
            net(["chroot", str(ROOT), "dbus-uuidgen", "--ensure=/etc/machine-id"])
            for path in (ROOT / "run/dbus/pid", ROOT / "run/dbus/system_bus_socket"):
                path.unlink(missing_ok=True)
            (ROOT / "run/dbus").mkdir(parents=True, exist_ok=True)
            net(["chroot", str(ROOT), "dbus-daemon", "--system", "--fork", "--nopidfile"])
            # No shared host systemd, D-Bus socket, resolv.conf or credentials.
            with open(RUN / "client.log", "ab") as log:
                client = subprocess.Popen(
                    ["ip", "netns", "exec", NS, "chroot", str(ROOT), "/usr/bin/warp-svc"],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                    env={**os.environ, "RUST_LOG": "warn"},
                )
                self.client_pid = client.pid
            time.sleep(2)
        except Exception:
            self.stop()
            raise

    def optimize(self):
        if self.client_pid and time.monotonic() - self.protocol_checked >= 12:
            from warp_control import UnrecognizedProtocol, parse_settings

            self.protocol_checked = time.monotonic()
            try:
                settings = parse_settings(cli("settings").stdout)
                effective = settings["protocol"]
            except UnrecognizedProtocol:
                self.protocol_warning = "客户端有效协议无法识别，当前版本可能不受支持"
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                # Optional telemetry must not destroy a connected data plane.
                self.protocol_warning = "协议查询暂不可用；当前连接保留，协议显示为上次确认值"
            else:
                self.protocol = effective
                self.protocol_managed = settings["managed"]
                self.protocol_observed_at = time.time()
                self.protocol_warning = ""
                if self.protocol_preference != "auto" and effective != self.protocol_preference:
                    raise ValueError("客户端有效协议已改变；未把其他协议误报为所选协议")
        if (
            self.protocol != "MASQUE"
            or not self.client_pid
            or time.monotonic() - self.last_buffer_check < 5
        ):
            return
        self.last_buffer_check = time.monotonic()
        try:
            # Never target an unrelated process if the child has exited.
            if os.readlink("/proc/%d/exe" % self.client_pid) != str(ROOT / "usr/bin/warp-svc"):
                return
            tune(self.client_pid)
        except (OSError, ValueError):
            pass  # Older kernels may not provide pidfd_getfd; keep the tunnel usable.

    def connect(self, vnet, *, deadline=None, checkpoint=None, keep_selection=False):
        def bounded_timeout(default):
            if checkpoint:
                checkpoint()
            if deadline is None:
                return default
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("线路连接测试超时")
            return min(default, remaining)

        def tunnel_cli(*args, **options):
            if deadline is not None:
                options["timeout"] = bounded_timeout(options.get("timeout", 5))
            return cli(*args, **options)

        current = tunnel_cli("vnet", "--no-paginate", check=False).stdout
        selected = parse_selected_network(current)
        if selected != vnet:
            if keep_selection:
                raise ValueError(EXIT_SELECTION_MISMATCH)
            try:
                directory = parse_directory(current)
            except InvalidDirectory:
                raise UnknownTunnelStatus() from None
            if vnet not in {item["id"] for item in directory.networks}:
                raise ValueError("所选出口已不在账户目录中；未切换到其他出口")
        state = read_status(tunnel_cli)
        if state is TunnelStatus.UNKNOWN and selected != vnet:
            raise UnknownTunnelStatus()
        from warp_control import (
            ProtocolMismatch,
            UnrecognizedProtocol,
            ensure_protocol,
            parse_settings,
        )
        from public_errors import ControlError

        try:
            effective = ensure_protocol(self.protocol_preference, tunnel_cli)
        except UnrecognizedProtocol:
            raise UnknownTunnelStatus() from None
        except (ControlError, OSError, RuntimeError, subprocess.TimeoutExpired):
            if state is TunnelStatus.UNKNOWN:
                raise UnknownTunnelStatus() from None
            raise
        self.protocol = effective["protocol"]
        self.protocol_managed = effective["managed"]
        self.protocol_observed_at = time.time()
        self.protocol_warning = ""
        self.protocol_checked = time.monotonic()
        if state is not TunnelStatus.UNKNOWN:
            if selected != vnet:
                tunnel_cli("vnet", vnet)
                state = read_status(tunnel_cli)
            if state is TunnelStatus.UNKNOWN:
                raise UnknownTunnelStatus()
            if state is not TunnelStatus.CONNECTED:
                tunnel_cli("connect")
            for _ in range(20):
                state = read_status(tunnel_cli)
                if state is TunnelStatus.CONNECTED:
                    break
                if state is TunnelStatus.UNKNOWN:
                    raise UnknownTunnelStatus()
                time.sleep(bounded_timeout(1))
            else:
                raise RuntimeError("WARP 隧道尚未连接，未接管设备")
            try:
                effective = parse_settings(tunnel_cli("settings").stdout)
                self.protocol = effective["protocol"]
                self.protocol_managed = effective["managed"]
            except UnrecognizedProtocol:
                raise UnknownTunnelStatus() from None
            if self.protocol_preference != "auto" and self.protocol != self.protocol_preference:
                raise ProtocolMismatch("有效协议与选择不一致，未应用新的设备分流")
        if deadline is None:
            self.optimize()
        options = {"timeout": bounded_timeout(3)} if deadline is not None else {}
        if "CloudflareWARP" not in net(["ip", "-o", "link", "show"], **options).stdout:
            if state is TunnelStatus.UNKNOWN:
                raise UnknownTunnelStatus()
            raise RuntimeError("隧道接口尚未就绪")
        # Host curl inside the namespace uses the same resolver as the client.
        atomic(NETNS_CONFIG, (ROOT / "etc/resolv.conf").read_text(), 0o644)
        return state

    def forward(self, devices):
        ips = [d["ip"] for d in devices if d["mode"] != "direct"]
        # Guard every forwarded phone/DNS packet against accidental plain-WAN fallback.
        sources = ", ".join(ips + ["10.254.239.1"])
        text = """table inet routepilot_forward {
 chain guard { type filter hook forward priority -5; policy accept;
  iifname "rp-client" ip saddr { %s } oifname != "CloudflareWARP" reject
 }
 chain namespace_nat { type nat hook postrouting priority 99; policy accept;
  ip saddr { %s } oifname "CloudflareWARP" masquerade
 }
}
""" % (sources, sources)
        exists = (
            net(["nft", "list", "table", "inet", "routepilot_forward"], check=False).returncode == 0
        )
        net(
            ["nft", "-f", "-"],
            data=("delete table inet routepilot_forward\n" if exists else "") + text,
        )
        fw4_install(devices)

    def probe(self, *, strict=False, timeout=None):
        connect_timeout = min(5, timeout) if timeout is not None else 5
        args = [
            "curl",
            "-4",
            "-fsS",
            "--connect-timeout",
            str(connect_timeout),
            "--max-time",
            str(timeout if timeout is not None else 10),
        ]
        if strict:
            args[1:1] = ["-q", "--noproxy", "*", "--interface", "if!CloudflareWARP"]
        options = {"timeout": timeout} if timeout is not None else {}
        result = net(args + ["https://www.cloudflare.com/cdn-cgi/trace"], **options)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return fields.get("ip", "")

    def stop(self):
        if not self.owned and not (RUN / "masque-owner.json").exists():
            return
        for pid in command(["ip", "netns", "pids", NS], check=False).stdout.split():
            if pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(1)
        for pid in command(["ip", "netns", "pids", NS], check=False).stdout.split():
            if pid.isdigit():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        fw4_remove()
        command(["nft", "delete", "table", "inet", "routepilot_control"], check=False)
        command(["ip", "link", "del", HOST], check=False)
        command(["ip", "netns", "del", NS], check=False)
        if os.path.ismount(ROOT / "proc"):
            command(["umount", str(ROOT / "proc")], check=False)
        NETNS_CONFIG.unlink(missing_ok=True)
        try:
            NETNS_CONFIG.parent.rmdir()
        except OSError:
            pass
        (RUN / "masque-owner.json").unlink(missing_ok=True)
        self.owned = False
        release_plane(self)
