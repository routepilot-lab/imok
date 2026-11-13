"""Manual VLESS/WS/TLS backup; secrets stay outside readable UCI/telemetry.

No automatic failover, subscription execution or TLS
verification bypass. Probe instances bind only loopback and never add routes.
"""

from contextlib import contextmanager
import ctypes
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit, parse_qsl, urlencode
import uuid

from runtime import RUN, atomic, command, read_config
from public_errors import ControlError
from native import BACKUP_SOCKS_PORT

NODE = Path("/etc/routepilot/backup/node.json")
OWNER = RUN / "backup-owner.json"
INPUT = RUN / "backup-input"
TEST = RUN / "backup-test.json"
TUN = "rp-backup"
SOCKS_PORT = BACKUP_SOCKS_PORT
HOST = re.compile(
    r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}"
)


def host(value):
    value = str(value or "").lower()
    try:
        addr = ipaddress.IPv4Address(value)
        if not addr.is_global:
            raise ValueError("备用入口必须使用公网地址")
        return str(addr)
    except ipaddress.AddressValueError:
        if not HOST.fullmatch(value) or value.endswith(
            (".local", ".lan", ".internal", ".localhost")
        ):
            raise ValueError("备用入口域名格式无效")
        return value


def validate(node):
    if not isinstance(node, dict):
        raise ValueError("备用节点格式无效")
    try:
        identity = uuid.UUID(node.get("uuid", ""))
        if identity.version != 4:
            raise ValueError()
        ident = str(identity)
        port = int(node.get("port", 443))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("备用节点凭据或端口无效") from None
    if port != 443:
        raise ValueError("备用入口端口无效")
    path = str(node.get("path", "/"))
    if (
        not path.startswith("/")
        or len(path) > 2048
        or any(ord(c) < 32 or ord(c) == 127 for c in path)
    ):
        raise ValueError("WebSocket 路径无效")
    name = str(node.get("name", "Workers 备用"))[:64]
    if not name or any(ord(c) < 32 for c in name):
        raise ValueError("备用名称无效")
    sni = host(node.get("sni"))
    if not HOST.fullmatch(sni):
        raise ValueError("证书身份必须使用 Workers 域名")
    return dict(
        uuid=ident, address=host(node.get("address")), port=port, sni=sni, path=path, name=name
    )


def fingerprint(node):
    return hashlib.sha256(json.dumps(validate(node), sort_keys=True).encode()).hexdigest()


def load():
    if NODE.is_symlink() or NODE.stat().st_mode & 0o077:
        raise ValueError("备用节点文件权限无效")
    if NODE.stat().st_size > 8192:
        raise ValueError("备用节点文件过大")
    return validate(json.loads(NODE.read_text()))


def relay_fingerprint(node):
    u = urlsplit(node["path"])
    q = [
        (k, v)
        for k, v in parse_qsl(u.query, keep_blank_values=True)
        if k not in ("rp_relays", "rp_force", "rp_probe")
    ]
    return fingerprint({**node, "path": urlunsplit(("", "", u.path, urlencode(q), ""))})


def metadata(node):
    result = {k: node[k] for k in ("name", "address", "port", "sni")}
    result["relay_fingerprint"] = relay_fingerprint(node)
    order = dict(parse_qsl(urlsplit(node["path"]).query)).get("rp_relays")
    result["relay_order_hash"] = hashlib.sha256(order.encode()).hexdigest() if order else None
    return result


def inventory():
    data = json.loads(command(["ubus", "call", "uci", "get", '{"config":"shadowsocksr"}']).stdout)[
        "values"
    ]
    return {
        key: v
        for key, v in data.items()
        if v.get(".type") == "servers"
        and v.get("type") == "v2ray"
        and v.get("v2ray_protocol") == "vless"
        and v.get("transport") == "ws"
        and v.get("tls") == "1"
    }


def list_saved():
    rows = []
    try:
        saved = inventory()
    except Exception:
        saved = {}
    for key, value in saved.items():
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", key):
            continue
        rows.append(
            dict(
                id=key,
                name=str(value.get("alias") or "已保存 TLS 节点")[:64],
                address=str(value.get("server", ""))[:253],
                port=str(value.get("server_port", ""))[:5],
            )
        )
    info = None
    try:
        node = load()
        info = dict(configured=True, fingerprint=fingerprint(node), **metadata(node))
    except (OSError, ValueError):
        pass
    return dict(nodes=rows, node=info)


def parse_input(data):
    if not isinstance(data, dict):
        raise ValueError("备用导入格式无效")
    if data.get("source") == "current":
        node = load()
    elif data.get("source") == "saved":
        key = data.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", key):
            raise ValueError("请选择已有节点")
        row = inventory().get(key)
        if not row:
            raise ValueError("已有 TLS/WebSocket 节点不存在")
        ident = next(
            (
                row[k]
                for k in ("vmess_id", "vless_id", "vless_uuid", "uuid", "v2ray_uuid")
                if row.get(k)
            ),
            "",
        )
        node = dict(
            uuid=ident,
            address=row.get("server"),
            port=row.get("server_port"),
            sni=row.get("tls_host") or row.get("ws_host"),
            path=row.get("ws_path", "/"),
            name=row.get("alias") or "Workers 备用",
        )
    elif data.get("source") == "uri":
        uri = str(data.get("uri", ""))
        if len(uri) > 4096:
            raise ValueError("节点链接过长")
        try:
            url = urlsplit(uri)
            query = parse_qs(url.query, strict_parsing=True)
            if url.scheme != "vless" or url.password or url.port is None:
                raise ValueError()
            if any(len(v) != 1 for v in query.values()):
                raise ValueError()
            if query.get("security") != ["tls"] or query.get("type") != ["ws"]:
                raise ValueError()
            if (
                query.get("allowInsecure", ["0"])[0].lower() not in ("0", "false")
                or query.get("flow", [""])[0]
            ):
                raise ValueError()
            if query.get("encryption", ["none"])[0] != "none":
                raise ValueError()
            sni = query.get("sni", query.get("host", [url.hostname]))[0]
            if query.get("host", [sni])[0].lower() != sni.lower():
                raise ValueError()
            node = dict(
                uuid=unquote(url.username or ""),
                address=url.hostname,
                port=url.port,
                sni=sni,
                path=query.get("path", ["/"])[0],
                name=unquote(url.fragment) or "Workers 备用",
            )
        except (ValueError, TypeError, AttributeError):
            raise ValueError("仅支持开启证书验证的 VLESS＋WebSocket＋TLS 链接") from None
    else:
        raise ValueError("请选择已有节点或粘贴 VLESS 链接")
    node = validate(node)
    mode = data.get("entry", "saved")
    if mode == "domain":
        node.update(address=node["sni"], port=443)
    elif mode == "ip":
        addr = ipaddress.IPv4Address(data.get("ip", ""))
        if not addr.is_global:
            raise ValueError("优选入口只接受公网 IPv4")
        node["address"] = str(addr)
    elif mode != "saved":
        raise ValueError("备用入口方式无效")
    return validate(node)


@contextmanager
def guard():
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "backup-config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def import_node(request_id=None):
    with guard():
        return _import_node(request_id)


def _import_node(request_id):
    from request_input import claim_request

    with claim_request(INPUT, request_id, 8192) as payload:
        if read_config()["main"].get("backend", "masque") == "worker":
            raise ValueError("请先切回主线路，再修改备用节点")
        node = parse_input(payload)
        NODE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        NODE.parent.chmod(0o700)
        atomic(NODE, json.dumps(node, ensure_ascii=False), 0o600)
        atomic(
            RUN / "backup-node.json",
            json.dumps(
                dict(configured=True, fingerprint=fingerprint(node), **metadata(node)),
                ensure_ascii=False,
            ),
            0o644,
        )
        return dict(saved=True, request_id=request_id.split(":")[0], **metadata(node))


def pinned(node):
    node = validate(node)
    retryable = {
        socket.EAI_AGAIN,
        socket.EAI_NONAME,
        getattr(socket, "EAI_NODATA", socket.EAI_NONAME),
    }
    for attempt in range(2):
        try:
            answers = socket.getaddrinfo(
                node["address"].encode("ascii"), node["port"], socket.AF_INET, socket.SOCK_STREAM
            )
            break
        except socket.gaierror as error:
            if attempt or error.errno not in retryable:
                raise ValueError(
                    "备用入口 IPv4 解析未完成；未切换到其他节点，请稍后重新检测"
                ) from None
            time.sleep(0.2)
    addresses = sorted({row[4][0] for row in answers})
    if not addresses or any(not ipaddress.IPv4Address(a).is_global for a in addresses):
        raise ValueError("备用域名未解析到可信公网 IPv4")
    return {**node, "address": addresses[0]}


def uptime():
    return time.clock_gettime(getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC))


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def mark_failed(nonce):
    atomic(RUN / "backup-failure.json", json.dumps(dict(nonce=nonce, boot=boot_id())))


def check_retry(nonce):
    path = RUN / "backup-failure.json"
    if path.exists():
        try:
            failed = json.loads(path.read_text())
            allowed = failed.get("boot") == boot_id() and failed.get("nonce") != nonce
        except (OSError, ValueError, TypeError):
            allowed = False
        if not allowed:
            raise ValueError("备用线路已失败，当前保持直连；请重新检测后手动启用，或切回主线路")


def verified_endpoint(node):
    entry = json.loads((NODE.parent / "last-entry.json").read_text())
    ip = str(ipaddress.IPv4Address(entry["ip"]))
    if (
        entry.get("fingerprint") != fingerprint(node)
        or entry.get("boot") != boot_id()
        or not 0 <= uptime() - float(entry.get("tested_uptime", -1e9)) <= 300
        or not ipaddress.IPv4Address(ip).is_global
    ):
        raise ValueError("备用入口未验证，请先独立检测")
    return {**node, "address": ip}


def config(node, port, tun=False):
    node = validate(node)
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("备用本机端口无效")
    inbounds = [
        dict(
            tag="backup-local",
            listen="127.0.0.1",
            port=port,
            protocol="socks",
            settings=dict(auth="noauth", udp=False),
        )
    ]
    if tun:
        inbounds.append(dict(tag="backup-tun", protocol="tun", settings=dict(name=TUN, mtu=1280)))
    return dict(
        log=dict(loglevel="none"),
        inbounds=inbounds,
        routing=dict(rules=[dict(type="field", network="udp", outboundTag="backup-block")]),
        outbounds=[
            dict(
                tag="backup",
                protocol="vless",
                settings=dict(
                    vnext=[
                        dict(
                            address=node["address"],
                            port=node["port"],
                            users=[dict(id=node["uuid"], encryption="none")],
                        )
                    ]
                ),
                streamSettings=dict(
                    network="ws",
                    security="tls",
                    tlsSettings=dict(
                        serverName=node["sni"], allowInsecure=False, alpn=["http/1.1"]
                    ),
                    wsSettings=dict(path=node["path"], headers={"Host": node["sni"]}),
                ),
                mux=dict(enabled=False),
            ),
            dict(tag="backup-block", protocol="blackhole"),
        ],
    )


def curl(args, port=SOCKS_PORT, check=True):
    if args[0] != "curl":
        raise ValueError("仅允许固定 curl 调用")
    return command(
        args[:1]
        + ["-q", "--noproxy", "", "--proxy", "socks5h://127.0.0.1:" + str(port)]
        + args[1:],
        check=check,
    )


def spawn(args):
    import threading

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("备用核心只能由单线程主管进程启动")
    parent = os.getpid()

    def child_setup():
        # Linux child exits if its supervisor disappears, including probe cancellation.
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != parent:
            os._exit(1)

    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=child_setup,
    )


def ready(process, port):
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError("备用核心启动失败")
        try:
            with socket.socket() as sock:
                sock.settimeout(0.1)
                sock.connect(("127.0.0.1", port))
            return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("备用核心未就绪")


def proc_key(pid):
    p = Path("/proc") / str(pid)
    return int(p.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19])


def owned_process(pid, start):
    if type(pid) is not int or pid < 2 or proc_key(pid) != start:
        return False
    parts = Path("/proc/%s/cmdline" % pid).read_bytes().rstrip(b"\0").split(b"\0")
    expected = [b"run", b"-config", str(RUN / "backup-core.json").encode()]
    return len(parts) == 4 and parts[0] in (b"xray", b"/usr/bin/xray") and parts[1:] == expected


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


class WorkerLineFailure(RuntimeError):
    """A failed Workers connection or core, as distinct from router setup errors."""


class WorkerStartupFailure(WorkerLineFailure):
    """A core startup failure eligible for a single retry."""


class WorkerPlane:
    def __init__(self):
        self.child = None
        self.node = None
        self.last_check = 0
        self.failures = 0

    @property
    def running(self):
        return self.child is not None and self.child.poll() is None

    def start(self):
        if self.running:
            return
        with guard():
            self._start()

    def _start(self):
        if OWNER.exists():
            self.stop()
        if command(["ip", "link", "show", TUN], check=False).returncode == 0:
            raise ValueError("备用网络接口名称被占用，未修改现有网络")
        for row in json.loads(command(["ip", "-j", "-4", "route", "show", "table", "main"]).stdout):
            if row.get("dst", "default") != "default" and ipaddress.IPv4Network(
                row["dst"], strict=False
            ).overlaps(ipaddress.IPv4Network("10.254.238.0/30")):
                raise ValueError("备用专用网段与现有网络重叠")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", SOCKS_PORT))
        version = command(["xray", "version"]).stdout.splitlines()[0]
        match = re.search(r"Xray (\d+)\.(\d+)\.(\d+)", version)
        if not match or tuple(map(int, match.groups())) < (26, 10, 21):
            raise ValueError("备用需要 Xray 26.10.21 或更新版本")
        self.node = require_test()
        self.failures = 0
        atomic(
            RUN / "backup-node.json",
            json.dumps(
                dict(configured=True, fingerprint=fingerprint(self.node), **metadata(self.node)),
                ensure_ascii=False,
            ),
            0o644,
        )
        text = json.dumps(config(verified_endpoint(self.node), SOCKS_PORT, tun=True))
        cfg = RUN / "backup-core.json"
        atomic(cfg, text)
        try:
            command(["xray", "run", "-test", "-config", str(cfg)])
            self.child = spawn(["xray", "run", "-config", str(cfg)])
            atomic(
                OWNER,
                json.dumps(
                    dict(pid=self.child.pid, start=proc_key(self.child.pid), boot=boot_id())
                ),
            )
            ready(self.child, SOCKS_PORT)
            if command(["ip", "link", "show", TUN], check=False).returncode:
                raise RuntimeError("备用 TUN 能力不可用，请核对核心版本")
            command(["ip", "addr", "replace", "10.254.238.1/30", "dev", TUN])
            command(["ip", "link", "set", "dev", TUN, "mtu", "1280", "up"])
        except (OSError, RuntimeError, subprocess.TimeoutExpired):
            if self.child or OWNER.exists():
                self.stop()  # Retain ownership unless process and TUN cleanup is confirmed.
            else:
                cfg.unlink(missing_ok=True)
            raise WorkerStartupFailure("备用核心未能启动，请核对 Xray 和 TUN 能力") from None

    def connect(self, _=""):
        self.start()
        self.probe()

    def probe(self):
        try:
            result = curl(
                [
                    "curl",
                    "-4",
                    "-fsS",
                    "--connect-timeout",
                    "4",
                    "--max-time",
                    "8",
                    "https://www.cloudflare.com/cdn-cgi/trace",
                ],
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise WorkerLineFailure("备用线路出口检测超时") from None
        if result.returncode != 0:
            raise WorkerLineFailure("备用线路未能通过出口检测")
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        try:
            value = str(ipaddress.IPv4Address(fields.get("ip", "")))
        except ipaddress.AddressValueError:
            raise WorkerLineFailure("未能确认备用线路出口地址") from None
        self.last_check = time.monotonic()
        return value

    def forward(self, devices):
        from mplane import fw4_install

        fw4_install(devices, proxy=TUN, control=False)

    def optimize(self):
        if not self.running:
            raise WorkerLineFailure("备用核心已退出")
        links = json.loads(command(["ip", "-j", "link", "show", "dev", TUN]).stdout)
        routes = json.loads(command(["ip", "-j", "-4", "route", "show", "table", "51990"]).stdout)
        if (
            not links
            or "UP" not in links[0].get("flags", [])
            or not any(r.get("dev") == TUN and r.get("dst") == "default" for r in routes)
        ):
            raise RuntimeError("备用接口或专用路由不可用")
        if time.monotonic() - self.last_check >= 30:
            self.last_check = time.monotonic()
            # A single rate-limited observation site must not withdraw a
            # working line. Check real TLS on independent fixed targets.
            for target in ("https://www.google.com/generate_204", "https://github.com/robots.txt"):
                try:
                    result = curl(
                        [
                            "curl",
                            "-4",
                            "-sS",
                            "--connect-timeout",
                            "3",
                            "--max-time",
                            "6",
                            "-o",
                            "/dev/null",
                            "-w",
                            "%{http_code}",
                            target,
                        ],
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    continue
                if (
                    result.returncode == 0
                    and result.stdout.strip().isdigit()
                    and 200 <= int(result.stdout) < 500
                ):
                    self.failures = 0
                    return
            self.failures += 1
            if self.failures >= 2:
                raise WorkerLineFailure("备用线路连续两次联网检查失败")

    def stop(self):
        from mplane import fw4_remove

        if not self.child and not OWNER.exists():
            return
        # This backend can never delete MASQUE forwarding. The supervisor owns
        # shared capture withdrawal and holds its lifetime flock.
        try:
            command(
                ["ip", "-4", "route", "del", "default", "dev", TUN, "table", "51990"], check=False
            )
        finally:
            fw4_remove("worker")
        if self.child:
            stop_process(self.child)
            self.child = None
        elif OWNER.exists():
            data = json.loads(OWNER.read_text())
            if data.get("boot") == Path("/proc/sys/kernel/random/boot_id").read_text().strip():
                pid = data.get("pid")
                try:
                    if not owned_process(pid, data["start"]):
                        raise RuntimeError("备用进程归属不能确认，未终止其他进程")
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(30):
                        try:
                            if proc_key(pid) != data["start"]:
                                break
                        except FileNotFoundError:
                            break
                        time.sleep(0.1)
                    else:
                        os.kill(pid, signal.SIGKILL)
                        for _ in range(20):
                            try:
                                if proc_key(pid) != data["start"]:
                                    break
                            except FileNotFoundError:
                                break
                            time.sleep(0.1)
                except FileNotFoundError:
                    pass
        else:
            return
        for _ in range(30):
            if command(["ip", "link", "show", TUN], check=False).returncode != 0:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("备用接口清理未能确认")
        OWNER.unlink(missing_ok=True)
        (RUN / "backup-core.json").unlink(missing_ok=True)


def https_result(name, url, body, port):
    attempts = []
    for timeout in (6, 8) if name == "github" else (6,):
        body.unlink(missing_ok=True)
        result = curl(
            [
                "curl",
                "-4",
                "-sS",
                "--connect-timeout",
                str(timeout),
                "--max-time",
                str(timeout + 6),
                "--max-filesize",
                "2097152",
                "-o",
                str(body),
                "-w",
                "%{json}",
                url,
            ],
            port,
            check=False,
        )
        try:
            stats = json.loads(result.stdout)
        except ValueError:
            stats = {}
        http = int(stats.get("http_code") or 0)
        seconds = float(stats.get("time_total") or 0)
        tls = float(stats.get("time_appconnect") or 0)
        first = float(stats.get("time_starttransfer") or 0)
        message = (
            (
                "连接或 TLS 握手超时"
                if tls == 0
                else "等待网站响应超时"
                if first == 0
                else "下载传输超时"
            )
            if result.returncode == 28
            else {6: "解析失败", 7: "本机代理连接失败", 35: "TLS 握手失败", 60: "证书验证失败"}.get(
                result.returncode, "未收到完整响应"
            )
        )
        ok = result.returncode == 0 and 200 <= http < 400
        state = "ok" if ok else "restricted" if result.returncode == 0 and http >= 400 else "failed"
        row = dict(
            id=name,
            state=state,
            http=http,
            ms=round(seconds * 1000),
            curl_code=result.returncode,
            message="可访问"
            if ok
            else "网站返回 HTTP " + str(http)
            if state == "restricted"
            else message,
            timing={
                k: stats.get(k)
                for k in ("time_connect", "time_appconnect", "time_starttransfer", "time_total")
            },
        )
        attempts.append({k: v for k, v in row.items() if k != "id"})
        if ok or result.returncode not in (7, 28, 35, 52, 55, 56):
            break
    row.update(attempts=attempts, attempt_count=len(attempts))
    if row["state"] == "ok" and len(attempts) > 1:
        row["message"] = "重试后可访问（本次存在波动）"
    return row


def test_worker(lock_fd=None):
    lock = os.fdopen(lock_fd, "a") if lock_fd is not None else (RUN / "backup-test.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return
    state = dict(
        state="running",
        started_at=time.time(),
        finished_at=None,
        boot=boot_id(),
        results=[],
        job_id=os.environ.get("ROUTEPILOT_BACKUP_TEST_ID") or uuid.uuid4().hex,
    )
    (NODE.parent / "last-entry.json").unlink(missing_ok=True)

    def publish():
        atomic(TEST, json.dumps(state, ensure_ascii=False), 0o644)

    publish()
    try:
        node = load()
        endpoint = pinned(node)
        state.update(
            fingerprint=fingerprint(node), node=metadata(node), entry_ip=endpoint["address"]
        )
        with tempfile.TemporaryDirectory(prefix="backup-probe-", dir=RUN) as folder:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            cfg = Path(folder) / "core.json"
            atomic(cfg, json.dumps(config(endpoint, port)))
            command(["xray", "run", "-test", "-config", str(cfg)])
            p = spawn(["xray", "run", "-config", str(cfg)])
            try:
                ready(p, port)
                targets = [
                    ("trace", "https://www.cloudflare.com/cdn-cgi/trace"),
                    ("google", "https://www.google.com/generate_204"),
                    ("github", "https://github.com/robots.txt"),
                    (
                        "dns",
                        "https://dns.google/dns-query?dns=MDkBAAABAAAAAAAAA3d3dwZnb29nbGUDY29tAAABAAE",
                    ),
                    ("download", "https://speed.cloudflare.com/__down?bytes=1048576"),
                ]
                for name, url in targets:
                    body = Path(folder) / "body"
                    body.unlink(missing_ok=True)
                    try:
                        if name == "dns":
                            from native import backup_doh, question, answer_addresses
                            import base64

                            query = base64.b64decode("MDkBAAABAAAAAAAAA3d3dwZnb29nbGUDY29tAAABAAE=")
                            started = time.monotonic()
                            response = backup_doh(query, port)
                            good = question(query)[:3] == question(response, True)[:3] and bool(
                                answer_addresses(response, "www.google.com")[0]
                            )
                            state["results"].append(
                                dict(
                                    id=name,
                                    state="ok" if good else "failed",
                                    ms=round((time.monotonic() - started) * 1000),
                                )
                            )
                            publish()
                            continue
                        row = https_result(name, url, body, port)
                        http = row["http"]
                        ok = row["state"] == "ok"
                        if name == "download":
                            size = body.stat().st_size
                            row["bytes"] = size
                            if ok and size != 1048576:
                                row["message"] = "下载字节数不符，不能计为有效测速"
                            row["state"] = (
                                "ok"
                                if ok and size == 1048576
                                else (
                                    "restricted"
                                    if row["curl_code"] == 0 and http == 429
                                    else "failed"
                                )
                            )
                        if name == "trace" and ok:
                            trace = dict(
                                line.split("=", 1)
                                for line in body.read_text().splitlines()
                                if "=" in line
                            )
                            state["exit_ip"] = str(ipaddress.IPv4Address(trace["ip"]))
                        state["results"].append(row)
                    except Exception:
                        state["results"].append(dict(id=name, state="failed"))
                    publish()
            finally:
                stop_process(p)
        state["state"] = "done"
        state["usable"] = proof_rows(state["results"])
        state["all_passed"] = all(r.get("state") == "ok" for r in state["results"])
        if state["usable"] and fingerprint(load()) == fingerprint(node):
            atomic(
                NODE.parent / "last-entry.json",
                json.dumps(
                    dict(
                        fingerprint=fingerprint(node),
                        ip=endpoint["address"],
                        tested_at=time.time(),
                        tested_uptime=uptime(),
                        boot=boot_id(),
                    )
                ),
            )
    except Exception:
        state.update(state="failed", message="备用检测未完成；未切换主线路，未显示凭据")
    state["finished_at"] = time.time()
    state["finished_uptime"] = uptime()
    publish()


def start_test():
    load()
    with (RUN / "backup-test.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                job_id = json.loads(TEST.read_text()).get("job_id")
            except (OSError, ValueError, AttributeError):
                job_id = None
            return dict(state="running", job_id=job_id)
        job_id = uuid.uuid4().hex
        # Inherit the same open-file-description lock so there is no unlocked
        # gap between submission and worker startup. Invalidate old UI proof.
        atomic(
            TEST,
            json.dumps(
                dict(
                    state="queued",
                    job_id=job_id,
                    started_at=time.time(),
                    finished_at=None,
                    usable=False,
                    results=[],
                )
            ),
            0o644,
        )
        subprocess.Popen(
            [sys.executable, __file__, "test", str(lock.fileno())],
            pass_fds=(lock.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "ROUTEPILOT_BACKUP_TEST_ID": job_id},
        )
    return dict(state="submitted", job_id=job_id)


def proof_rows(rows):
    if (
        not isinstance(rows, list)
        or len(rows) != 5
        or {r.get("id") for r in rows} != {"trace", "google", "github", "dns", "download"}
    ):
        return False
    by_id = {r["id"]: r for r in rows}
    return all(by_id[k].get("state") == "ok" for k in ("trace", "dns")) and any(
        by_id[k].get("state") == "ok" for k in ("google", "github")
    )


def require_test():
    try:
        return _require_test()
    except (OSError, ValueError, KeyError, TypeError):
        raise ControlError("请先对当前备用节点完成独立检测；同次开机5分钟内有效") from None


def _require_test():
    node = load()
    result = json.loads(TEST.read_text())
    age = uptime() - float(result.get("finished_uptime", -1e9))
    endpoint = verified_endpoint(node)
    if (
        result.get("boot") != boot_id()
        or result.get("fingerprint") != fingerprint(node)
        or result.get("entry_ip") != endpoint["address"]
        or result.get("state") != "done"
        or not 0 <= age <= 300
        or not proof_rows(result.get("results", []))
    ):
        raise ControlError("请先对当前备用节点完成独立检测；同次开机5分钟内有效")
    return node


if __name__ == "__main__":
    if not sys.argv[1:] or sys.argv[1] != "test" or len(sys.argv) not in (2, 3):
        raise SystemExit(1)
    os.umask(0o077)
    test_worker(int(sys.argv[2]) if len(sys.argv) == 3 else None)
