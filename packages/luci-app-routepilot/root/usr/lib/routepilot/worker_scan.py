from public_errors import ControlError

"""Bounded independent Workers entry selection; no routing/sysctl writes."""
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

from runtime import RUN, atomic, read_config
import worker_backend as w

STATE = RUN / "backup-scan.json"
# Official https://www.cloudflare.com/ips-v4, verified 2026-10-31.
NETWORKS = tuple(
    ipaddress.IPv4Network(s)
    for s in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
    )
)
SEEDS = (
    "104.21.39.117",
    "172.67.145.44",
    "104.16.0.1",
    "104.17.0.1",
    "104.18.0.1",
    "104.19.0.1",
    "104.20.0.1",
    "162.159.128.1",
)


def cf_ip(value):
    try:
        addr = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        return False
    return addr.is_global and any(addr in n for n in NETWORKS)


def candidates(node):
    addresses = []
    if cf_ip(node["address"]):
        addresses.append(node["address"])
    try:
        addresses.extend(
            x[4][0]
            for x in socket.getaddrinfo(
                node["sni"].encode("ascii"), 443, socket.AF_INET, socket.SOCK_STREAM
            )
        )
    except OSError:
        pass
    return list(dict.fromkeys(x for x in addresses + list(SEEDS) if cf_ip(x)))[:8]


def measure(port, url, size=0):
    result = w.curl(
        [
            "curl",
            "-4",
            "-sS",
            "--connect-timeout",
            "5",
            "--max-time",
            "8",
            "--max-filesize",
            "131072",
            "-o",
            "/dev/null",
            "-w",
            "%{json}",
            url,
        ],
        port,
        check=False,
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = {}
    count = int(data.get("size_download") or 0)
    code = int(data.get("http_code") or 0)
    good = result.returncode == 0 and 200 <= code < 300 and (not size or size == count)
    return dict(
        ok=good,
        http=code,
        curl_code=result.returncode,
        ms=round(float(data.get("time_total") or 0) * 1000),
        bytes=count,
        mibps=float(data.get("speed_download") or 0) / 1048576 if good and size else None,
        warning="站点限流" if code == 429 else None,
    )


def rank(rows):
    eligible = [
        r for r in rows if len(r.get("checks", [])) == 2 and all(c["ok"] for c in r["checks"])
    ]
    # All-site TLS success is required; short downloads break latency ties only.
    return sorted(
        eligible,
        key=lambda r: (
            sum(c["ms"] for c in r["checks"]) / 2,
            -(r.get("download", {}).get("mibps") or 0),
            r["ip"],
        ),
    )


def scan(lock_fd):
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = dict(state="running", started_at=time.time(), rows=[], progress=0)

        def publish():
            atomic(STATE, json.dumps(state, ensure_ascii=False), 0o644)

        def alarm(*_):
            raise TimeoutError("scan deadline")

        signal.signal(signal.SIGALRM, alarm)
        signal.signal(signal.SIGTERM, alarm)
        signal.alarm(240)
        publish()
        try:
            node = w.load()
            state.update(fingerprint=w.fingerprint(node), boot=w.boot_id())
            ips = candidates(node)
            if not ips:
                raise ControlError("没有找到 Cloudflare 公网 IPv4 候选")
            state["total"] = len(ips)
            publish()
            with tempfile.TemporaryDirectory(prefix="entry-scan-", dir=RUN) as folder:
                folder = Path(folder)
                for index, ip in enumerate(ips):
                    row = dict(ip=ip, checks=[])
                    p = None
                    try:
                        with socket.socket() as sock:
                            sock.bind(("127.0.0.1", 0))
                            port = sock.getsockname()[1]
                        cfg = folder / "core.json"
                        atomic(cfg, json.dumps(w.config({**node, "address": ip}, port)))
                        p = w.spawn(["xray", "run", "-config", str(cfg)])
                        w.ready(p, port)
                        for url in (
                            "https://www.google.com/generate_204",
                            "https://github.com/robots.txt",
                        ):
                            row["checks"].append(measure(port, url))
                        if all(c["ok"] for c in row["checks"]):
                            row["download"] = measure(
                                port, "https://speed.cloudflare.com/__down?bytes=65536", 65536
                            )
                    except Exception:
                        row["message"] = "入口测试未完成，不作为推荐"
                    finally:
                        if p:
                            w.stop_process(p)
                    state["rows"].append(row)
                    state["progress"] = round((index + 1) / len(ips) * 100)
                    publish()
            if w.fingerprint(w.load()) != w.fingerprint(node):
                raise ControlError("节点已改变，请重新优选")
            best = rank(state["rows"])
            state.update(
                state="done",
                best_ip=best[0]["ip"] if best else None,
                message="已自动选出本轮推荐入口，点击应用仅修改备用入口，不启用线路"
                if best
                else "本轮没有同时通过 Google 与 GitHub 的入口；保留原备用配置",
            )
        except Exception as error:
            state.update(
                state="failed",
                message=str(error) if isinstance(error, ValueError) else "优选未完成，原入口未改变",
            )
        finally:
            signal.alarm(0)
        state.update(finished_at=time.time(), finished_uptime=w.uptime())
        publish()


def start():
    w.load()
    with (RUN / "backup-scan.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(state="running")
        atomic(
            STATE,
            json.dumps(
                dict(
                    state="queued",
                    started_at=time.time(),
                    message="自动优选已提交；不接管设备",
                    rows=[],
                )
            ),
            0o644,
        )
        subprocess.Popen(
            [sys.executable, __file__, str(lock.fileno())],
            pass_fds=(lock.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    return dict(state="submitted")


def apply_best():
    with w.guard():
        if read_config()["main"].get("backend", "masque") == "worker":
            raise ControlError("请先切回主线路，再应用备用入口")
        node = w.load()
        proof = json.loads(STATE.read_text())
        ip = proof.get("best_ip")
        if (
            proof.get("state") != "done"
            or proof.get("fingerprint") != w.fingerprint(node)
            or proof.get("boot") != w.boot_id()
            or not 0 <= w.uptime() - float(proof.get("finished_uptime", -1e9)) <= 300
            or not cf_ip(ip)
        ):
            raise ControlError("优选结果已过期或节点已变化，请重新优选")
        ranked = rank(proof.get("rows", []))
        if not ranked or ranked[0]["ip"] != ip:
            raise ControlError("推荐入口证据不完整")
        node = w.validate({**node, "address": ip})
        atomic(w.NODE, json.dumps(node))
        (w.NODE.parent / "last-entry.json").unlink(missing_ok=True)
        atomic(
            w.RUN / "backup-node.json",
            json.dumps(
                dict(configured=True, fingerprint=w.fingerprint(node), **w.metadata(node)),
                ensure_ascii=False,
            ),
            0o644,
        )
        proof["applied_fingerprint"] = w.fingerprint(node)
        atomic(STATE, json.dumps(proof, ensure_ascii=False), 0o644)
        return dict(saved=True, message="已应用推荐备用入口，主线路未改变；启用前请重新独立检测")


if __name__ == "__main__":
    os.umask(0o077)
    if len(sys.argv) != 2:
        raise SystemExit(1)
    scan(int(sys.argv[1]))
