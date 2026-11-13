"""Bounded, explicit router-side probes of the active per-policy DNS listeners."""

import concurrent.futures
import fcntl
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import uuid

from runtime import RUN, atomic
from dns_runtime import current_context
from native import question, answer_addresses, dns_route

TARGETS = [("domestic", "www.baidu.com"), ("proxy", "www.google.com")]
IDENTITY = ("runtime_id", "generation", "applied_nonce", "exit", "exit_ip")


def load_context(run=RUN):
    run = Path(run)
    status = json.loads((run / "status.json").read_text())
    state = json.loads((run / "dns-state.json").read_text())
    spec = json.loads((run / "dns.json").read_text())
    if not current_context(status, state) or any(
        spec.get(k) != status.get(k) for k in ("runtime_id", "generation")
    ):
        raise ValueError("Active DNS unavailable")
    if not 1 <= len(spec.get("policies", [])) <= 8:
        raise ValueError("Invalid DNS policy count")
    for policy in spec["policies"]:
        if not isinstance(policy.get("port"), int) or not 12100 <= policy["port"] <= 12107:
            raise ValueError("Invalid DNS policy port")
    return status, state, spec


def context_same(before, dns_before, after, dns_after):
    return (
        current_context(after, dns_after)
        and all(before.get(k) == after.get(k) for k in IDENTITY)
        and dns_before.get("process_instance") == dns_after.get("process_instance")
    )


def make_query(name):
    ident = int.from_bytes(os.urandom(2), "big")
    qname = (
        b"".join(bytes([len(label)]) + label.encode("ascii") for label in name.split(".")) + b"\0"
    )
    return struct.pack("!6H", ident, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", 1, 1)


def recv_exact(conn, size, deadline):
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("DNS deadline exceeded")
        conn.settimeout(remaining)
        part = conn.recv(size - len(data))
        if not part:
            raise ValueError("DNS stream truncated")
        data.extend(part)
    return bytes(data)


def probe(policy, index, target, protocol):
    category, domain = target
    server, _, selected_route = dns_route(domain, policy)
    result = dict(
        policy=index + 1,
        label=str(policy.get("label") or "受管范围 " + str(index + 1))[:64],
        target=category,
        domain=domain,
        protocol=protocol,
        upstream=policy.get("proxy_dns_override", server) if selected_route == "proxy" else server,
        upstream_transport="doh"
        if selected_route == "proxy" and policy.get("proxy_dns_override")
        else "tcp"
        if protocol == "tcp"
        else policy.get("transport", "tcp"),
        configured_route=selected_route,
        state="failed",
        ms=None,
        addresses=[],
    )
    query = make_query(domain)
    started, deadline = time.monotonic(), time.monotonic() + 8
    try:
        with socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
        ) as conn:
            conn.settimeout(8)
            conn.connect(("127.0.0.1", policy["port"]))
            if protocol == "tcp":
                conn.sendall(struct.pack("!H", len(query)) + query)
                size = struct.unpack("!H", recv_exact(conn, 2, deadline))[0]
                if not 12 <= size <= 65535:
                    raise ValueError("Invalid DNS frame")
                response = recv_exact(conn, size, deadline)
            else:
                conn.send(query)
                conn.settimeout(max(0.001, deadline - time.monotonic()))
                response = conn.recv(65535)
        ident, name, qtype, _ = question(response, True)
        if (ident, name, qtype) != question(query)[:3]:
            raise ValueError("DNS response mismatch")
        flags = struct.unpack("!H", response[2:4])[0]
        result["rcode"] = flags & 15
        if flags & 0x0200:
            result.update(state="warning", message="响应被截断，需 TCP 重试")
        elif flags & 15:
            result.update(
                state="failed" if (flags & 15) != 3 else "warning",
                message={
                    2: "DNS 服务返回 SERVFAIL",
                    3: "DNS 已响应但域名不存在",
                    5: "DNS 服务拒绝查询",
                }.get(flags & 15, "DNS 返回错误"),
            )
        else:
            addresses, _ = answer_addresses(response, domain)
            result.update(
                state="ok" if addresses else "warning",
                message="解析成功" if addresses else "已响应但无 A 记录",
                addresses=addresses[:4],
            )
    except (socket.timeout, TimeoutError):
        result["message"] = "解析检测超时"
    except ConnectionRefusedError:
        result.update(state="unavailable", message="当前 DNS 入口未监听")
    except (OSError, ValueError, struct.error):
        result["message"] = "响应格式错误或入口不可达"
    result["ms"] = round((time.monotonic() - started) * 1000)
    return result


def worker(lock_fd, request_id, run=RUN):
    run = Path(run)
    with os.fdopen(lock_fd, "a"):
        path = run / "dns-tests.json"
        record = json.loads(path.read_text())
        if record.get("request_id") != request_id:
            return

        def publish():
            atomic(path, json.dumps(record, ensure_ascii=False), 0o644)

        try:
            status, dns_state, spec = load_context(run)
            if not context_same(record, record, status, dns_state):
                raise ValueError("DNS context changed before start")
        except Exception:
            record.update(
                state="failed",
                stale=True,
                finished_at=time.time(),
                message="检测环境已改变，未执行本次查询",
            )
            publish()
            return
        try:
            policies = []
            for raw in spec["policies"]:
                policy = {
                    **{k: spec[k] for k in ("dns_domestic", "dns_foreign", "transport")},
                    **raw,
                }
                policies.append(policy)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                jobs = [
                    pool.submit(probe, policy, index, target, proto)
                    for index, policy in enumerate(policies)
                    for target in TARGETS
                    for proto in ("udp", "tcp")
                ]
                for future in concurrent.futures.as_completed(jobs):
                    record["results"].append(future.result())
                    publish()
            now_status, now_dns, _ = load_context(run)
            record.update(
                state="done", stale=not context_same(status, dns_state, now_status, now_dns)
            )
        except Exception:
            record.update(
                state="failed", stale=True, message="检测环境已改变或作业未完成，请重新检测"
            )
        record["finished_at"] = time.time()
        publish()


def start(run=RUN):
    run = Path(run)
    try:
        status, dns_state, _ = load_context(run)
    except (OSError, ValueError, TypeError, KeyError):
        return dict(
            state="unavailable", message="分流或独立 DNS 未运行，未启动检测；请先确认总览状态"
        )
    lock = open(run / "dns-tests.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        try:
            existing = json.loads((run / "dns-tests.json").read_text())
        except (OSError, ValueError):
            existing = {}
        return dict(
            state="running",
            request_id=existing.get("request_id"),
            message="已有 DNS 检测作业，请等待结果",
        )
    request_id = uuid.uuid4().hex
    try:
        atomic(
            run / "dns-tests.json",
            json.dumps(
                dict(
                    schema=1,
                    request_id=request_id,
                    state="running",
                    started_at=time.time(),
                    finished_at=0,
                    **{k: status.get(k) for k in IDENTITY},
                    process_instance=dns_state["process_instance"],
                    expected_count=len(dns_state["policies"]) * 4,
                    scope="router_policy_listener",
                    results=[],
                    stale=False,
                )
            ),
            0o644,
        )
        # Keep the acquired open-file lock across exec; return before any DNS query.
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(lock.fileno()), request_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=(lock.fileno(),),
        )
    except Exception:
        atomic(
            run / "dns-tests.json",
            json.dumps(
                dict(
                    schema=1,
                    request_id=request_id,
                    state="failed",
                    stale=True,
                    time=time.time(),
                    message="DNS 检测进程未启动",
                )
            ),
            0o644,
        )
        lock.close()
        raise
    lock.close()
    return dict(state="submitted", request_id=request_id, message="DNS 检测已提交，最长约 40 秒")


if __name__ == "__main__":
    if len(sys.argv) != 3 or not sys.argv[1].isdigit() or len(sys.argv[2]) != 32:
        raise SystemExit(1)
    try:
        worker(int(sys.argv[1]), sys.argv[2])
    except Exception:
        raise SystemExit(1)
