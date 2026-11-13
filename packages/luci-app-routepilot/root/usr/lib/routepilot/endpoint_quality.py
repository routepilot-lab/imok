"""Measure handshakes against the official client's existing IPv4 candidate list."""

from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
import socket
import ssl
import subprocess
import sys
import time

from endpoint_control import CACHE, address, optional
from mplane import PEER, net
from public_errors import ControlError
from runtime import RUN, atomic, command, median_number
from speed_test import same_line, status

RESULT = RUN / "endpoint-quality.json"
SNI = "consumer-masque.cloudflareclient.com"
ATTEMPTS = 3
ATTEMPT_SECONDS = 0.9
MEASUREMENT_SECONDS = 28


def candidates():
    data, invalid = optional(CACHE)
    entries = data.get("entries", [])
    if invalid or not isinstance(entries, list) or len(entries) > 32:
        raise ControlError("入口候选无效，请先重新读取")
    normalized = list(dict.fromkeys(address(value) for value in entries))
    return [value for value in normalized if not value.startswith("[")]


def publish(record):
    atomic(RESULT, json.dumps(record, ensure_ascii=False), 0o644)


def start():
    current = status()
    if current["backend"] != "masque" or current.get("protocol") != "MASQUE":
        raise ControlError("候选测量需要运行中的 MASQUE 主线路")
    if not candidates():
        raise ControlError("请先读取 IPv4 入口候选")
    with (RUN / "endpoint-quality.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        publish(dict(state="running", started_at=time.time()))
        try:
            subprocess.Popen(
                [sys.executable, "-B", __file__, "--worker", str(lock.fileno())],
                pass_fds=(lock.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            publish(dict(state="error", finished_at=time.time()))
            raise
    return dict(state="submitted")


def quic_handshake(host, port):
    result = command(
        [
            "curl",
            "-q",
            "-4",
            "-sS",
            "--head",
            "--http3-only",
            "--noproxy",
            "*",
            "--interface",
            "if!" + PEER,
            "--proto",
            "=https",
            "--max-time",
            str(ATTEMPT_SECONDS),
            "--resolve",
            f"{SNI}:{port}:{host}",
            "-o",
            "/dev/null",
            "-w",
            "%{time_appconnect}",
            f"https://{SNI}:{port}/",
        ],
        timeout=ATTEMPT_SECONDS + 0.15,
        check=False,
    )
    seconds = float(result.stdout)
    return (
        seconds * 1000 if result.returncode in (0, 28) and 0 < seconds <= ATTEMPT_SECONDS else None
    )


def tcp_handshake(host, port):
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.set_alpn_protocols(["h2"])
    start_at = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
        conn.setsockopt(
            socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), PEER.encode() + b"\0"
        )
        conn.settimeout(ATTEMPT_SECONDS)
        conn.connect((host, int(port)))
        remaining = ATTEMPT_SECONDS - (time.monotonic() - start_at)
        if remaining <= 0:
            return None
        conn.settimeout(remaining)
        with context.wrap_socket(conn, server_hostname=SNI) as encrypted:
            if encrypted.selected_alpn_protocol() != "h2":
                return None
    return (time.monotonic() - start_at) * 1000


def measure(entries, probe):
    def one(entry):
        host, port = entry.rsplit(":", 1)
        values = []
        for _ in range(ATTEMPTS):
            try:
                ms = probe(host, port)
                if ms is not None and math.isfinite(ms) and ms > 0:
                    values.append(ms)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                pass
        return dict(
            address=entry,
            median_ms=round(median_number(values), 2) if values else None,
            failed=ATTEMPTS - len(values),
            attempts=ATTEMPTS,
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(one, entries))
    viable = [row for row in rows if row["failed"] <= 1]
    recommended = min(viable, key=lambda row: (row["failed"], row["median_ms"])) if viable else None
    return dict(rows=rows, recommended=recommended["address"] if recommended else None)


def in_namespace(entries):
    if entries != candidates():
        raise ControlError("候选已变化，请重新测量")
    features = command(["curl", "-V"], timeout=2).stdout
    http3 = any(
        "HTTP3" in line.split() for line in features.splitlines() if line.startswith("Features:")
    )
    transport = "quic" if http3 else "tls_tcp"
    result = measure(entries, quic_handshake if http3 else tcp_handshake)
    return dict(result, transport=transport)


def worker(fd):
    with os.fdopen(fd, "a"):
        try:
            current = status()
            entries = candidates()
            result = json.loads(
                net(
                    [sys.executable, "-B", __file__, "--probe"],
                    data=json.dumps(entries),
                    timeout=MEASUREMENT_SECONDS,
                ).stdout
            )
            if not same_line(current, status()) or entries != candidates():
                publish(dict(state="stale", finished_at=time.time()))
                return
            publish(dict(result, state="done", finished_at=time.time()))
        except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired):
            publish(dict(state="error", finished_at=time.time()))


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe"]:
        print(json.dumps(in_namespace(json.load(sys.stdin))))
    elif len(sys.argv) == 3 and sys.argv[1] == "--worker":
        worker(int(sys.argv[2]))
