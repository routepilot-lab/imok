"""Choose a resolver for a new WARP namespace before the client starts."""

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import fcntl
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

from runtime import RUN, RULES, atomic, command, median_number
from public_errors import ControlError

FALLBACK = "223.5.5.5"
DOMAINS = ("api.devices.cloudflare.com", "connectivity.cloudflareclient.com")
PROBE_TIMEOUT = 0.7
MAX_PEERS = 2
NAMESPACE_NETWORK = ipaddress.IPv4Network("10.254.239.0/30")
BACKUP_NETWORK = ipaddress.IPv4Network("10.254.238.0/30")
OPTIONS = "options timeout:1 attempts:2\n"
HISTORY = RULES.parent / "warp-bootstrap-resolver.json"
FIELDS = ("warp_dns_mode", "warp_dns_pinned", "warp_dns_custom")
TEST_FILE = RUN / "warp-dns-tests.json"
LAN_TEST_FILES = {kind: RUN / f"lan-dns-{kind}.json" for kind in ("domestic", "foreign")}
CANDIDATES = (
    ("国内", "阿里", "223.5.5.5"),
    ("国内", "阿里", "223.6.6.6"),
    ("国内", "腾讯 DNSPod", "119.29.29.29"),
    ("国内", "114", "114.114.114.114"),
    ("国内", "百度", "180.76.76.76"),
    ("海外", "Cloudflare", "1.1.1.1"),
    ("海外", "Cloudflare", "1.0.0.1"),
    ("海外", "Google", "8.8.8.8"),
    ("海外", "Google", "8.8.4.4"),
    ("海外", "Quad9", "9.9.9.9"),
    ("海外", "OpenDNS", "208.67.222.222"),
)


def candidate_address(value):
    if not isinstance(value, str):
        return None
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return None
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.packed[0] == 0
        or address in NAMESPACE_NETWORK
        or address in BACKUP_NETWORK
    ):
        return None
    return str(address)


def wan_peers(wan, deadline=None, *, host=False):
    deadline = deadline if deadline is not None else time.monotonic() + 1

    def read(args, *, check=True):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("WAN DNS discovery timed out")
        return command(args, timeout=remaining, check=check).stdout

    report = json.loads(read(["ubus", "call", "network.interface", "dump"]))
    links = report.get("interface") if isinstance(report, dict) else None
    if not isinstance(links, list):
        return []
    peers = []
    for link in links:
        if not isinstance(link, dict):
            continue
        name = link.get("interface", "")
        reported = link.get("dns-server")
        if (
            link.get("up") is not True
            or link.get("l3_device") != wan
            or not isinstance(reported, list)
            or not reported
            or not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name)
        ):
            continue
        option = "network." + name + "."
        if read(["uci", "-q", "get", option + "peerdns"], check=False).strip().lower() in (
            "0",
            "false",
            "no",
            "off",
            "disabled",
        ):
            continue
        # netifd reports manually configured and peer-provided addresses together.
        manual = set(read(["uci", "-q", "get", option + "dns"], check=False).split())
        for raw in reported:
            address = candidate_address(raw)
            if (
                address is None
                or (address == FALLBACK and not host)
                or address in manual
                or address in peers
            ):
                continue
            routes = json.loads(
                read(
                    [
                        "ip",
                        "-j",
                        "-4",
                        "route",
                        "get",
                        address,
                        *([] if host else ["from", "10.254.239.2", "iif", "rp-host"]),
                    ],
                    check=False,
                )
                or "[]"
            )
            if isinstance(routes, list) and any(
                isinstance(item, dict) and item.get("dev") == wan for item in routes
            ):
                peers.append(address)
                if len(peers) == MAX_PEERS:
                    return peers
    return peers


def resolver_result(endpoint, timeout=PROBE_TIMEOUT, interface=None, *, tcp=False):
    from dns_diagnostics import make_query
    from native import answer_addresses, question, read_tcp_response

    started = time.monotonic()
    deadline = started + timeout
    try:
        with socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
        ) as conn:
            if interface:
                conn.setsockopt(
                    socket.SOL_SOCKET,
                    getattr(socket, "SO_BINDTODEVICE", 25),
                    interface.encode() + b"\0",
                )
            conn.settimeout(timeout)
            conn.connect(endpoint)
            for name in DOMAINS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, "timeout"
                conn.settimeout(remaining)
                query = make_query(name)
                if tcp:
                    response = read_tcp_response(conn, query, deadline)
                else:
                    conn.send(query)
                    response = conn.recv(65535)
                if question(response, True)[:3] != question(query)[:3]:
                    return None, "unexpected_answer"
                if response[3] & 15:
                    return None, "dns_error"
                addresses, _ = answer_addresses(response, name)
                ipv4 = [
                    ipaddress.IPv4Address(address) for address in addresses if ":" not in address
                ]
                if not ipv4:
                    return None, "empty_answer"
                if any(not address.is_global or address.is_multicast for address in ipv4):
                    return None, "unexpected_answer"
    except TimeoutError:
        return None, "timeout"
    except (OSError, ValueError):
        return None, "unavailable"
    return (time.monotonic() - started) * 1000, None


def usable_resolver(endpoint, timeout=PROBE_TIMEOUT):
    return resolver_result(endpoint, timeout)[0] is not None


def preferences(main, local=()):
    mode = main.get("warp_dns_mode", "auto")
    pinned = main.get("warp_dns_pinned", "")
    custom = main.get("warp_dns_custom", [])
    if mode not in ("auto", "pinned", "custom") or not isinstance(custom, list) or len(custom) > 4:
        raise ControlError("WARP 启动 DNS 模式或自定义地址数量无效")
    if (mode == "pinned" and not pinned) or (mode == "custom" and not custom):
        raise ControlError("请填写 WARP 启动 DNS 服务器")
    for raw in ([pinned] if pinned else []) + custom:
        if not candidate_address(raw) or raw in local:
            raise ControlError("WARP 启动 DNS 地址必须是可用的 IPv4，且不能指向路由器自身")
    return mode, pinned, custom


def local_addresses(timeout=1):
    data = json.loads(command(["ip", "-j", "-4", "addr", "show"], timeout=timeout).stdout)
    return {
        entry["local"] for row in data for entry in row.get("addr_info", []) if "local" in entry
    }


def candidates(peers, custom):
    return list(dict.fromkeys([*peers, *(row[2] for row in CANDIDATES), *custom]))


def namespace_probe(execute, addresses, deadline, *, attempts=1, interface=None, tcp=False):
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not addresses:
        return {}
    args = [sys.executable, "-B", str(Path(__file__).resolve()), "--probe"]
    result = execute(
        args,
        data=json.dumps(
            dict(
                addresses=addresses,
                attempts=attempts,
                interface=interface,
                tcp=tcp,
                timeout=min(1, remaining),
            )
        ),
        timeout=remaining,
        check=False,
    )
    if result.returncode:
        return {}
    return json.loads(result.stdout)


def probe_group(raw):
    addresses = raw["addresses"]
    attempts = raw.get("attempts", 1)
    interface = raw.get("interface")
    if (
        len(addresses) > 20
        or attempts not in (1, 3)
        or (interface is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface))
        or any(not candidate_address(ip) for ip in addresses)
    ):
        raise ValueError("Invalid resolver probe")
    timeout = min(1.0, float(raw["timeout"]))

    def measure(ip):
        rows = [
            resolver_result((ip, 53), timeout, interface, tcp=raw.get("tcp", False))
            for _ in range(attempts)
        ]
        valid = [ms for ms, _ in rows if ms is not None]
        return dict(
            ms=round(median_number(valid), 2) if len(valid) > attempts // 2 else None,
            reason=next((reason for _, reason in rows if reason), None),
        )

    with ThreadPoolExecutor(max_workers=max(1, len(addresses))) as pool:
        return dict(zip(addresses, pool.map(measure, addresses)))


def manual_probe(execute, addresses, deadline, interface, *, lan=False, tcp=False):
    def probe(tcp):
        try:
            return namespace_probe(
                execute, addresses, deadline, attempts=3, interface=interface, tcp=tcp
            )
        except subprocess.TimeoutExpired:
            if not lan:
                raise
            return {}

    if not lan:
        return probe(False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        udp_results = pool.submit(probe, False)
        tcp_results = pool.submit(probe, True)
    udp, stream = udp_results.result(), tcp_results.result()
    timeout = {"ms": None, "reason": "timeout"}
    rows = {}
    for address in addresses:
        protocols = {"udp": udp.get(address, timeout), "tcp": stream.get(address, timeout)}
        rows[address] = {**protocols["tcp" if tcp else "udp"], **protocols}
    return rows


def fastest(rows, addresses):
    valid = [
        (rows[ip]["ms"], index, ip)
        for index, ip in enumerate(addresses)
        if ip in rows and rows[ip].get("ms") is not None
    ]
    return min(valid)[2] if valid else None


def select_resolver(wan, execute, main):
    start = time.monotonic()
    deadline = start + 3
    try:
        previous = candidate_address(json.loads(HISTORY.read_text()).get("address"))
    except (OSError, ValueError, AttributeError):
        previous = None

    def probe(addresses, until):
        try:
            rows = namespace_probe(execute, addresses, until)
            return rows if time.monotonic() <= until else {}
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            return {}

    try:
        local = local_addresses(timeout=0.3)
        mode, pinned, custom = preferences(main, local)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        local, mode, pinned, custom = set(), "auto", "", []
    if previous in local:
        previous = None
    selected, source, fallback = None, None, False
    tried = set()
    if mode != "auto":
        group = [pinned] if mode == "pinned" else custom
        tried.update(group)
        selected = fastest(probe(group, start + 1), group)
        source = mode if selected else None
        fallback = selected is None
    if not selected:
        # Auto shares the first second between discovery and probing. When a
        # configured group fails, leave at least the final second for public candidates.
        wan_deadline = start + (1 if mode == "auto" else 2)
        try:
            peers = [
                ip
                for ip in wan_peers(wan, min(wan_deadline, time.monotonic() + 0.7))
                if ip not in local and ip not in tried
            ]
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
            peers = []
        rows = probe(peers, min(wan_deadline, time.monotonic() + 0.7))
        selected = next((ip for ip in peers if rows.get(ip, {}).get("ms") is not None), None)
        if selected:
            source = "wan"
        else:
            tried.update(peers)
            group = [ip for ip in candidates([], custom) if ip not in local and ip not in tried]
            selected = fastest(probe(group, deadline), group)
            source = "candidate" if selected else "fallback"
    defaults = [ip for ip in dict.fromkeys([previous, "1.1.1.1", FALLBACK]) if ip]
    if selected and selected != previous:
        try:
            atomic(HISTORY, json.dumps({"address": selected}))
        except OSError:
            pass
    if source == "wan":
        lines = [selected, FALLBACK]
    elif source in ("custom", "pinned"):
        lines = [selected, FALLBACK if selected != FALLBACK else "1.1.1.1"]
    elif selected:
        lines = [selected, next(ip for ip in defaults if ip != selected)]
    else:
        lines = defaults
    text = "".join(f"nameserver {ip}\n" for ip in lines) + OPTIONS
    return text, dict(address=lines[0], source=source, fallback=fallback)


def validate_preferences(request_id):
    from request_input import claim_request

    with claim_request(RUN / "warp-dns-input", request_id, 1024) as raw:
        if not isinstance(raw, dict) or set(raw) != set(FIELDS):
            raise ControlError("WARP 启动 DNS 配置格式无效")
        preferences(raw, local_addresses())
    return {"request_id": request_id.split(":")[0]}


def start_manual_test(request_id=None):
    from mplane import NS
    from request_input import claim_request
    from runtime import read_config

    if not any(
        line.split()[0] == NS
        for line in command(["ip", "netns", "list"]).stdout.splitlines()
        if line
    ):
        raise ControlError("请先准备 WARP 环境")
    cfg = read_config()["main"]
    if request_id:
        with claim_request(RUN / "warp-dns-input", request_id, 1024) as raw:
            if not isinstance(raw, dict) or set(raw) != {"addresses"}:
                raise ControlError("DNS 测试请求无效")
            cfg = {**cfg, "warp_dns_mode": "auto", "warp_dns_custom": raw["addresses"]}
    _, _, custom = preferences(cfg, local_addresses())
    with (RUN / "warp-dns-test.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("已有 WARP 启动 DNS 测试正在运行") from None
        atomic(TEST_FILE, json.dumps(dict(state="running", started_at=time.time())), 0o644)
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(Path(__file__).resolve()),
                    "--manual",
                    str(lock.fileno()),
                ],
                pass_fds=(lock.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={**os.environ, "ROUTEPILOT_DNS_CANDIDATES": json.dumps(custom)},
            )
        except OSError:
            atomic(
                TEST_FILE,
                json.dumps(dict(state="error", finished_at=time.time())),
                0o644,
            )
            raise
    return dict(state="submitted", request_id=request_id.split(":")[0] if request_id else None)


def request_reselect():
    from mplane import NS
    from runtime import read_config

    cfg = read_config()["main"]
    if cfg.get("backend") == "worker" or not (
        cfg.get("enabled") == "1" or cfg.get("preparing") == "1"
    ):
        raise ControlError("请先准备或启用 WARP 主线路")
    names = command(["ip", "netns", "list"], timeout=2).stdout.splitlines()
    if not any(line.split() and line.split()[0] == NS for line in names):
        raise ControlError("请先准备 WARP 环境")
    atomic(RUN / "warp-dns-reselect", "requested\n")
    return dict(state="submitted")


def start_lan_test(kind):
    from speed_test import status

    current = status() if kind == "foreign" else None
    if current and current["backend"] != "masque":
        raise ControlError("代理 DNS 候选测试需要已就绪的 WARP 主线路")
    with (RUN / f"lan-dns-{kind}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("该 DNS 候选测试正在运行") from None
        atomic(
            LAN_TEST_FILES[kind], json.dumps(dict(state="running", started_at=time.time())), 0o644
        )
        try:
            subprocess.Popen(
                [sys.executable, "-B", __file__, "--lan", str(lock.fileno()), kind],
                pass_fds=(lock.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            atomic(
                LAN_TEST_FILES[kind],
                json.dumps(dict(state="error", finished_at=time.time())),
                0o644,
            )
            raise
    return dict(state="submitted")


def manual_worker(fd, kind=None):
    from mplane import net
    from runtime import read_config
    from speed_test import route, same_line, status

    path = LAN_TEST_FILES[kind] if kind else TEST_FILE
    with os.fdopen(fd, "a"):
        try:
            deadline = time.monotonic() + 14
            local = local_addresses()
            labels = {ip: (group, name) for group, name, ip in CANDIDATES}
            current = None
            peer_status = None
            tcp = False
            if kind:
                cfg = read_config()["main"]
                tcp = cfg.get("dns_transport", "tcp") == "tcp"
                selected = cfg.get(f"dns_{kind}")
                addresses = candidates([], [selected] if candidate_address(selected) else [])
                if kind == "foreign":
                    current = status()
                    if current["backend"] != "masque":
                        raise ControlError("代理 DNS 候选测试需要已就绪的 WARP 主线路")
                    execute, interface = net, "CloudflareWARP"
                else:
                    execute, options = route("masque", "direct")
                    interface = options[1].removeprefix("if!")
                    try:
                        peers = [ip for ip in wan_peers(interface, host=True) if ip not in local]
                        peer_status = "included" if peers else "unavailable"
                    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                        peers = []
                        peer_status = "error"
                    labels.update({ip: ("宽带", "运营商下发") for ip in peers})
                    addresses = list(dict.fromkeys([*peers, *addresses]))
            else:
                custom = json.loads(os.environ["ROUTEPILOT_DNS_CANDIDATES"])
                wan = json.loads(
                    command(["ip", "-j", "-4", "route", "get", "1.1.1.1"], timeout=1).stdout
                )[0]["dev"]
                peers = wan_peers(wan)
                labels.update({ip: ("宽带", "运营商下发") for ip in peers})
                addresses = candidates(peers, custom)
                execute, interface = net, "rp-client"
            addresses = [ip for ip in addresses if ip not in local]
            tested = manual_probe(execute, addresses, deadline, interface, lan=bool(kind), tcp=tcp)
            if current and not same_line(current, status()):
                raise ControlError("测试期间主线路已变化，请重试")
            rows = []
            for ip in addresses:
                group, name = labels.get(ip, ("自定义", "用户填写"))
                rows.append(
                    dict(
                        address=ip,
                        group=group,
                        name=name,
                        **tested.get(ip, {"ms": None, "reason": "timeout"}),
                    )
                )
            result = dict(state="done", rows=rows, finished_at=time.time())
            if kind:
                result["transport"] = "tcp" if tcp else "udp"
            if peer_status:
                result["wan_dns"] = peer_status
        except (OSError, ValueError, RuntimeError, KeyError, IndexError, subprocess.TimeoutExpired):
            result = dict(state="error", message="未能完成 DNS 候选测试", finished_at=time.time())
        atomic(path, json.dumps(result, ensure_ascii=False), 0o644)


def bootstrap_resolver(wan, run_in_namespace):
    """Return resolv.conf contents even if WAN discovery or probing fails."""
    default = f"nameserver {FALLBACK}\n"
    try:
        peers = wan_peers(wan)
        if peers:
            result = run_in_namespace(
                [sys.executable, "-B", str(Path(__file__).resolve()), *peers],
                check=False,
                timeout=2,
            )
            primary = result.stdout.strip()
            if result.returncode == 0 and primary in peers:
                return f"nameserver {primary}\n{default}options timeout:1 attempts:2\n"
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        pass
    return default


def main(candidates):
    for candidate in candidates[:MAX_PEERS]:
        if candidate_address(candidate) and usable_resolver((candidate, 53)):
            print(candidate)
            return 0
    return 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(probe_group(json.load(sys.stdin))))
    elif len(sys.argv) == 3 and sys.argv[1] == "--manual":
        manual_worker(int(sys.argv[2]))
    elif len(sys.argv) == 4 and sys.argv[1] == "--lan" and sys.argv[3] in LAN_TEST_FILES:
        manual_worker(int(sys.argv[2]), sys.argv[3])
    else:
        raise SystemExit(main(sys.argv[1:]))
