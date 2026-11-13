"""Bounded router-origin speed measurements over a verified network path."""

import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import sys
import time
import uuid

from mplane import NS, net
from public_errors import ControlError
from runtime import RUN, atomic, command, median_number
from worker_backend import SOCKS_PORT

RESULT = RUN / "speed-tests.json"
LOCK = RUN / "speed-test.lock"
DOWNLOAD = "https://speed.cloudflare.com/__down?bytes="
TRACE = "https://speed.cloudflare.com/cdn-cgi/trace"
PATH_BUDGET = 200_000_000
DOWNLOAD_SAMPLE = 75_000_000
BATCH_BUDGET = 1_000_000_000
SESSION_SECONDS = 60
DOWNLOAD_SECONDS = 8
STATUS_CHECK_SECONDS = 0.25
CPU_STAT = Path("/proc/stat")
IDENTITY = ("runtime_id", "generation", "applied_nonce", "exit", "exit_ip", "backend", "protocol")


def status():
    value = json.loads((RUN / "status.json").read_text())
    if (
        not isinstance(value, dict)
        or value.get("state") != "running"
        or not isinstance(value.get("time"), (int, float))
        or not -1 <= time.time() - value["time"] <= 15
    ):
        raise ControlError("请先启用并确认当前线路")
    if not ipaddress.IPv4Address(value["exit_ip"]).is_global:
        raise ControlError("当前公网出口尚未确认")
    return value


def same_line(before, after):
    return all(before.get(k) == after.get(k) for k in IDENTITY)


def route(backend, path):
    if path == "proxy":
        if backend == "worker":
            return command, ["--proxy", f"socks5h://127.0.0.1:{SOCKS_PORT}", "--noproxy", ""]
        return net, ["--interface", "if!CloudflareWARP", "--noproxy", "*"]
    rows = json.loads(command(["ip", "-j", "-4", "route", "get", "1.1.1.1"], timeout=2).stdout)
    device = rows[0].get("dev", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", device) or device in (
        "rp-host",
        "rp-backup",
        "CloudflareWARP",
        "lo",
    ):
        raise ControlError("无法确认宽带直连接口")
    return command, ["--interface", "if!" + device, "--noproxy", "*"]


def curl_args(path_options, url, limit, options):
    return [
        "curl",
        "-q",
        "-4",
        "-s",
        "--fail",
        "--proto",
        "=https",
        "--connect-timeout",
        str(min(2, limit)),
        "--max-time",
        str(limit),
        *path_options,
        *options,
        url,
    ]


def request(execute, path_options, url, deadline, *, options=(), limit=3):
    remaining = min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise TimeoutError("测速超时")
    return execute(
        curl_args(path_options, url, remaining, options),
        timeout=remaining + 0.2,
        check=False,
    )


def download_rates(samples, duration):
    total = sum(size for _, size in samples)
    buckets = [0] * DOWNLOAD_SECONDS
    for offset, size in samples:
        buckets[min(int(offset), len(buckets) - 1)] += size
    stable_start = DOWNLOAD_SECONDS - 5
    completed_seconds = min(int(duration + 0.000001), DOWNLOAD_SECONDS)
    complete = buckets[:completed_seconds]
    stable = complete[stable_start:]
    return dict(
        average_mbps=round(total * 8 / max(duration, 0.001) / 1_000_000, 2),
        stable_mbps=round(sum(stable) * 8 / len(stable) / 1_000_000, 2)
        if len(stable) == 5
        else None,
        peak_mbps=round(max(complete) * 8 / 1_000_000, 2) if complete else None,
    )


def stream_download(execute, path_options, deadline, budget, cancelled):
    remaining = min(deadline - time.monotonic(), DOWNLOAD_SECONDS + 3)
    if remaining <= 0:
        raise TimeoutError("测速超时")
    sample_limit = min(budget, DOWNLOAD_SAMPLE)
    argv = curl_args(
        path_options,
        DOWNLOAD + str(sample_limit),
        remaining,
        (
            "--no-buffer",
            "--include",
            "--suppress-connect-headers",
            "--max-filesize",
            str(sample_limit),
            "-w",
            "%{stderr}%{http_code} %{time_starttransfer} %{time_total}\n",
        ),
    )
    if execute is net:
        argv = ["ip", "netns", "exec", NS, *argv]
    samples = []
    first = None
    total = 0
    headers = bytearray()
    stop = "duration"
    end = time.monotonic() + remaining
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    if cancelled():
                        stop = "cancelled"
                        break
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    if not selector.select(min(left, 0.1)):
                        continue
                    chunk = os.read(child.stdout.fileno(), min(65536, sample_limit - total))
                    if not chunk:
                        stop = "eof"
                        break
                    now = time.monotonic()
                    if first is None and headers is not None:
                        headers.extend(chunk)
                        while b"\r\n\r\n" in headers:
                            header, _, tail = headers.partition(b"\r\n\r\n")
                            status_line = header.split(b"\r\n", 1)[0].split()
                            if len(status_line) < 2 or status_line[1] not in (
                                b"100",
                                b"103",
                                b"200",
                            ):
                                break
                            headers = tail
                            if status_line[1] == b"200":
                                chunk = tail
                                break
                        else:
                            if len(headers) <= 16384:
                                continue
                            status_line = []
                        if len(status_line) < 2 or status_line[1] != b"200":
                            stop = "error"
                            break
                        if not chunk:
                            # The next readable bytes are the start of the transfer.
                            headers = None
                            continue
                    if first is None:
                        first = now
                        end = min(end, first + DOWNLOAD_SECONDS)
                    chunk = chunk[: sample_limit - total]
                    samples.append((now - first, len(chunk)))
                    total += len(chunk)
                    if total == sample_limit:
                        stop = "budget" if sample_limit == budget else "sample_limit"
                        break
        finally:
            finished = time.monotonic()
            if child.poll() is None:
                if stop != "eof":
                    child.terminate()
                try:
                    child.wait(timeout=1 if stop == "eof" else 0.3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        duration = max(0, min(finished, end) - first) if first is not None else 0
        report = child.stderr.read().decode("ascii", "replace").strip()
        if stop == "eof":
            fields = report.split()
            if len(fields) != 3 or fields[0] != "200" or child.returncode not in (0, 28):
                return total, duration, "error", {}
            if child.returncode == 0:
                duration = max(0, float(fields[2]) - float(fields[1]))
                if total < sample_limit:
                    stop = "short"
            else:
                stop = "duration"
        if total == sample_limit and stop != "cancelled":
            stop = "budget" if sample_limit == budget else "sample_limit"
        return total, duration, stop, download_rates(samples, duration)


def trace_ip(execute, path_options, deadline, *, metadata=None):
    reply = request(execute, path_options, TRACE, deadline, options=("--max-filesize", "4096"))
    if reply.returncode:
        raise ControlError("无法确认测速路径的公网出口")
    fields = dict(line.split("=", 1) for line in reply.stdout.splitlines() if "=" in line)
    address = ipaddress.IPv4Address(fields["ip"])
    if not address.is_global:
        raise ControlError("测速路径的公网出口无效")
    if metadata is not None and re.fullmatch(r"[A-Z]{2}", fields.get("loc", "")):
        metadata["region"] = fields["loc"]
    return str(address)


def cpu_times():
    """Return (total, inactive) jiffies per CPU and for the whole router."""
    counters = {}
    try:
        with CPU_STAT.open() as source:
            for line in source:
                cpu, *fields = line.split()
                if not re.fullmatch(r"cpu[0-9]*", cpu):
                    break
                ticks = [int(field) for field in fields[:8]]
                if len(ticks) < 5 or any(tick < 0 for tick in ticks):
                    return {}
                counters[cpu] = (sum(ticks), ticks[3] + ticks[4])
    except (OSError, ValueError):
        return {}
    return counters


def cpu_usage(before, after):
    busy = {}
    for cpu in before.keys() & after.keys():
        total = after[cpu][0] - before[cpu][0]
        inactive = after[cpu][1] - before[cpu][1]
        if 0 <= inactive <= total and total > 0:
            busy[cpu] = round(100 * (total - inactive) / total, 1)
    if "cpu" not in busy:
        return {}
    aggregate = busy.pop("cpu")
    result = dict(cpu_busy_percent=aggregate)
    if busy:
        core = max(sorted(busy), key=busy.get)
        result.update(cpu_busiest_core=core, cpu_busiest_core_percent=busy[core])
    return result


def measure_path(backend, path, expected, deadline, budget=PATH_BUDGET, cancelled=lambda: False):
    if not 0 < budget <= PATH_BUDGET:
        raise ValueError("Invalid measurement budget")
    execute, path_options = route(backend, path)
    metadata = {}
    first_ip = trace_ip(execute, path_options, deadline, metadata=metadata)
    if (first_ip == expected) != (path == "proxy"):
        return dict(path=path, state="invalid", bytes=0, message="测速路径与已确认出口不一致")
    latency = []
    for _ in range(5):
        if cancelled():
            return dict(path=path, state="cancelled", bytes=0)
        reply = request(
            execute,
            path_options,
            TRACE,
            deadline,
            options=("-o", "/dev/null", "-w", "%{time_appconnect}", "--max-filesize", "4096"),
        )
        if reply.returncode == 0:
            latency.append(float(reply.stdout) * 1000)
    before_cpu = cpu_times()
    total, elapsed, stop, rates = stream_download(
        execute, path_options, deadline, budget, cancelled
    )
    usage = cpu_usage(before_cpu, cpu_times())
    row = dict(path=path, bytes=total, exit_ip=first_ip, **metadata)
    if stop == "cancelled" or cancelled():
        return dict(row, state="cancelled")
    try:
        confirmed = trace_ip(execute, path_options, deadline)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        confirmed = None
    if confirmed != first_ip:
        return dict(row, state="invalid", message="测速期间出口发生变化或无法确认")
    if not total or elapsed <= 0 or stop in ("error", "short") or not latency:
        return dict(row, state="error", message="测速目标未返回有效测量")
    return dict(
        row,
        state="done",
        elapsed=round(elapsed, 3),
        measurement_version=2,
        stop_reason=stop,
        https_connect_median_ms=round(median_number(latency), 2),
        https_connect_worst_ms=round(max(latency), 2),
        **rates,
        **usage,
    )


def read_result():
    try:
        value = json.loads(RESULT.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def publish(value):
    atomic(RESULT, json.dumps(value, ensure_ascii=False), 0o644)


def start():
    from speed_compare import REQUEST

    status()
    with LOCK.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("已有测速正在进行") from None
        if REQUEST.exists():
            raise ControlError("已有出口测速等待执行")
        job = uuid.uuid4().hex
        history = read_result().get("history", [])[-10:]
        publish(dict(state="running", id=job, started_at=time.time(), rows=[], history=history))
        try:
            subprocess.Popen(
                [sys.executable, "-B", __file__, str(handle.fileno()), job],
                pass_fds=(handle.fileno(),),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            publish(dict(state="error", id=job, message="测速未能启动", history=history))
            raise
    return dict(state="submitted", id=job)


def worker(fd, job):
    with os.fdopen(fd, "a"):
        data = read_result()
        if data.get("id") != job:
            return
        deadline = time.monotonic() + SESSION_SECONDS
        try:
            before = status()
            next_check = 0
            stale = False

            def line_changed(force=False):
                nonlocal next_check, stale
                now = time.monotonic()
                if not stale and (force or now >= next_check):
                    next_check = now + STATUS_CHECK_SECONDS
                    try:
                        stale = not same_line(before, status())
                    except (OSError, ValueError, KeyError):
                        stale = True
                return stale

            data.update(backend=before["backend"], protocol=before.get("protocol"))
            for path in ("proxy", "direct"):
                if line_changed(force=True):
                    break
                try:
                    row = measure_path(
                        before["backend"], path, before["exit_ip"], deadline, cancelled=line_changed
                    )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    IndexError,
                    subprocess.TimeoutExpired,
                ):
                    row = dict(path=path, state="error", message="测速失败或超时", bytes=0)
                data["rows"].append(row)
                publish(data)
            data["state"] = "stale" if line_changed(force=True) else "done"
        except (OSError, ValueError, KeyError):
            data.update(state="stale", message="当前线路状态已变化")
        data["finished_at"] = time.time()
        data["bytes"] = sum(row["bytes"] for row in data["rows"])
        history = data.pop("history")
        data["history"] = [*history, dict(data)][-10:]
        publish(data)


if __name__ == "__main__" and len(sys.argv) == 3:
    worker(int(sys.argv[1]), sys.argv[2])
