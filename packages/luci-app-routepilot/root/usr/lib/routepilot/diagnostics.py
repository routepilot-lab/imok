"""Bounded HTTPS reachability checks. No configurable shell/URL execution."""

import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import time

from runtime import RUN, atomic, command
from mplane import net

TARGETS = {
    "google": ("Google", "https://www.google.com/", "proxy"),
    "youtube": ("YouTube", "https://www.youtube.com/", "proxy"),
    "github": ("GitHub", "https://github.com/", "proxy"),
    "taobao": ("淘宝", "https://taobao.com/", "direct"),
    "baidu": ("百度", "https://www.baidu.com/", "direct"),
}
IDENTITY = ("exit", "exit_ip", "generation", "applied_nonce", "runtime_id", "backend")
REFRESH_SECONDS = 240


def snapshot(name, run=None):
    try:
        value = json.loads(((RUN if run is None else run) / name).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def ready(status, now):
    updated = status.get("time")
    return (
        status.get("state") == "running"
        and type(updated) in (int, float)
        and -5 <= now - updated <= 15
        and status.get("backend") in ("masque", "worker")
        and status.get("tunnel_status") != "unknown"
        and status.get("egress_guard") not in ("blocking", "unverified")
        and all(status.get(key) for key in ("runtime_id", "generation", "exit", "exit_ip"))
    )


def same_line(first, second):
    return all(first.get(key) == second.get(key) for key in IDENTITY)


def recent_result(result, status, now):
    finished = result.get("finished_at")
    rows = result.get("results")
    return (
        result.get("state") == "done"
        and not result.get("stale")
        and same_line(result, status)
        and type(finished) in (int, float)
        and -5 <= now - finished < REFRESH_SECONDS
        and isinstance(rows, list)
        and len(rows) == len(TARGETS)
        and all(
            isinstance(row, dict) and row.get("state") in ("ok", "failed", "restricted")
            for row in rows
        )
        and all(any(row.get("id") == key for row in rows) for key in TARGETS)
    )


def classify(code, output):
    parts = output.strip().split()
    http = int(parts[0]) if parts and parts[0].isdigit() else 0
    try:
        ms = round(float(parts[1]) * 1000)
    except (ValueError, IndexError):
        ms = None
    if code == 0 and 200 <= http < 400:
        return dict(state="ok", message="可连接", http=http, ms=ms)
    if code == 0 and http >= 400:
        return dict(state="restricted", message="可达，但返回 HTTP " + str(http), http=http, ms=ms)
    messages = {
        6: "DNS 解析失败",
        7: "连接失败",
        18: "响应传输未完成",
        28: "超时",
        35: "TLS 握手失败",
        47: "重定向次数超限",
        60: "证书验证失败",
    }
    return dict(state="failed", message=messages.get(code, "未收到 HTTPS 响应"), http=http, ms=ms)


def probe(key, backend):
    name, url, route = TARGETS[key]
    args = [
        "curl",
        "-4",
        "-I",
        "-sS",
        "-L",
        "--max-redirs",
        "3",
        "--connect-timeout",
        "4",
        "--max-time",
        "10",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code} %{time_total}",
        url,
    ]
    executor = command
    if route == "proxy" and backend == "worker":
        from worker_backend import curl as executor
    else:
        options = ["-q", "--noproxy", "*"]
        if route == "proxy":
            executor = net
            options += ["--interface", "if!CloudflareWARP"]
        args[1:1] = options
    try:
        result = executor(args, check=False)
        result = classify(result.returncode, result.stdout)
    except Exception:
        result = dict(state="failed", message="检测未完成", http=0, ms=None)
    return dict(id=key, name=name, route=route, **result)


def worker(key="all", automatic=False):
    if key != "all" and key not in TARGETS:
        raise ValueError("Unknown target")
    with (RUN / "connectivity.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        run_check(key, automatic)


def run_check(key, automatic):
    keys = list(TARGETS) if key == "all" else [key]
    current = snapshot("status.json")
    now = time.time()
    if automatic and (
        not ready(current, now) or recent_result(snapshot("connectivity.json"), current, now)
    ):
        return
    items = {
        k: dict(id=k, name=TARGETS[k][0], route=TARGETS[k][2], state="pending", message="等待检测")
        for k in keys
    }
    state = dict(
        state="running",
        started_at=int(time.time()),
        finished_at=0,
        **{key: current.get(key) for key in IDENTITY},
        automatic=automatic,
        results=list(items.values()),
        stale=False,
    )

    def publish():
        state["results"] = list(items.values())
        atomic(RUN / "connectivity.json", json.dumps(state, ensure_ascii=False), 0o644)

    publish()
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(probe, k, current.get("backend")): k for k in keys}
        for future in concurrent.futures.as_completed(futures):
            items[futures[future]] = future.result()
            publish()
    after = snapshot("status.json")
    state["stale"] = (
        after.get("state") != "running"
        or not same_line(current, after)
        or (automatic and not ready(after, time.time()))
    )
    state.update(state="done", finished_at=int(time.time()))
    publish()


def start(key="all", *, automatic=False, run=None):
    if key != "all" and key not in TARGETS:
        raise ValueError("Unknown target")
    data = snapshot("connectivity.json", run)
    started = data.get("started_at")
    now = time.time()
    if (
        data.get("state") == "running"
        and type(started) in (int, float)
        and 0 <= started <= now + 5
        and now - started < 25
    ):
        return
    return subprocess.Popen(
        [sys.executable, "-B", __file__, "--auto" if automatic else key],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class AutomaticCheck:
    def __init__(self, run):
        self.run = run
        self.next_check = 0
        self.child = None

    def tick(self):
        if self.child is not None:
            if self.child.poll() is None:
                return
            self.child = None
        now = time.monotonic()
        if now < self.next_check:
            return
        self.next_check = now + 10
        current = snapshot("status.json", self.run)
        now = time.time()
        if ready(current, now) and not recent_result(
            snapshot("connectivity.json", self.run), current, now
        ):
            self.child = start(automatic=True, run=self.run)


if __name__ == "__main__":
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) == 2 else "all"
    if action == "--auto":
        worker(automatic=True)
    else:
        worker(action)
