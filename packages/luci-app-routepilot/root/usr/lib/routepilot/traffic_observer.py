"""Counter-only, best-effort managed IPv4 forwarding telemetry.

Never changes forwarding marks/verdicts, offload, UCI, routes or buffers.
Flow-offloaded packets may completely bypass these counters: NOT billing data.
"""

from collections import deque
from datetime import datetime
import ctypes
import fcntl
import os
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import signal
import subprocess
import time
import uuid

from compiler import PRIVATE4
from runtime import RUN, atomic

TABLE = "routepilot_usage"
PREFIX = "routepilot-usage:v1:"
STORE = Path("/etc/routepilot/usage/state.json")
KEYS = ("proxy_up", "proxy_down", "direct_up", "direct_down")
STOP = False


def zero():
    return dict.fromkeys(KEYS, 0)


def call(args, data=None, timeout=8):
    return subprocess.run(
        args,
        input=data,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )


def valid_counts(value):
    return (
        isinstance(value, dict)
        and set(value) == set(KEYS)
        and all(type(n) is int and 0 <= n <= 2**64 for n in value.values())
    )


def save(data):
    payload = json.dumps(data, allow_nan=False)
    try:
        if STORE.read_text() == payload:
            return False
    except FileNotFoundError:
        pass
    atomic(STORE, payload)
    return True
    fd = os.open(str(STORE.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def clock_synced():
    # Linux adjtimex with modes=0 only reads synchronization state; no clock writes.
    try:
        buf = ctypes.create_string_buffer(512)
        fn = ctypes.CDLL(None, use_errno=True).adjtimex
        fn.argtypes, fn.restype = [ctypes.c_void_p], ctypes.c_int
        return fn(ctypes.byref(buf)) in (0, 1, 2, 3, 4)
    except Exception:
        return False


def zone():
    return datetime.now().astimezone().strftime("%Z %z")


def day(wall):
    return datetime.fromtimestamp(wall).strftime("%Y-%m-%d") if wall >= 1735689600 else None


class Ledger:
    def __init__(self, data=None, wall=None):
        wall = time.time() if wall is None else wall
        self.data = (
            data
            if data is not None
            else dict(
                schema=1,
                started_at=wall,
                total=zero(),
                days={},
                unallocated=zero(),
                gaps=0,
                anchor=None,
            )
        )
        d = self.data
        if (
            not isinstance(d, dict)
            or d.get("schema") != 1
            or not valid_counts(d.get("total"))
            or not valid_counts(d.get("unallocated"))
            or not isinstance(d.get("days"), dict)
            or len(d["days"]) > 90
            or type(d.get("gaps")) is not int
            or d["gaps"] < 0
            or not isinstance(d.get("started_at"), (int, float))
            or not math.isfinite(d["started_at"])
        ):
            raise ValueError("invalid ledger")
        for name, bucket in d["days"].items():
            if (
                not re.fullmatch(r"\d{4}-\d{2}-\d{2}", name)
                or not valid_counts(bucket.get("bytes"))
                or type(bucket.get("partial")) is not bool
            ):
                raise ValueError("invalid day")
        a = d.get("anchor")
        if a is not None and (
            not isinstance(a, dict)
            or not valid_counts(a.get("raw"))
            or not isinstance(a.get("key"), str)
            or not isinstance(a.get("wall"), (int, float))
            or not isinstance(a.get("mono"), (int, float))
            or not math.isfinite(a["wall"])
            or not math.isfinite(a["mono"])
        ):
            raise ValueError("invalid anchor")

    def sample(self, key, raw, wall, mono, partial=True, synced=True):
        if not valid_counts(raw):
            raise ValueError("counter format")
        d, a = self.data, self.data["anchor"]
        delta, rate = zero(), None
        if a and a["key"] == key:
            reset = any(raw[k] < a["raw"][k] for k in KEYS)
            if reset:
                d["gaps"] += 1  # Lost interval unknown. Do not guess from new raw value.
            else:
                delta = {k: raw[k] - a["raw"][k] for k in KEYS}
                elapsed = mono - a["mono"]
                clock_ok = elapsed > 0 and abs((wall - a["wall"]) - elapsed) < 10
                same_day = (
                    synced
                    and a.get("synced")
                    and clock_ok
                    and day(wall) is not None
                    and day(wall) == a.get("day")
                    and zone() == a.get("zone")
                )
                if same_day:
                    bucket = d["days"].setdefault(day(wall), dict(bytes=zero(), partial=partial))
                    bucket["partial"] = bucket["partial"] or partial or elapsed > 20
                    for k in KEYS:
                        bucket["bytes"][k] += delta[k]
                    if elapsed <= 20:
                        rate = {k: delta[k] / elapsed for k in KEYS}
                else:
                    for k in KEYS:
                        d["unallocated"][k] += delta[k]
                    d["gaps"] += 1
                for k in KEYS:
                    d["total"][k] += delta[k]
        elif a:
            d["gaps"] += 1
        d["anchor"] = dict(
            key=key, raw=dict(raw), wall=wall, mono=mono, synced=synced, day=day(wall), zone=zone()
        )
        if synced and day(wall):
            d["days"].setdefault(day(wall), dict(bytes=zero(), partial=True))
        d["days"] = dict(sorted(d["days"].items())[-90:])
        return rate

    def gap(self):
        self.data["gaps"] += 1
        self.data["anchor"] = None


def scope(status):
    rows = status.get("devices")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError("no scope")
    result = []
    for row in rows:
        if row.get("mode") == "direct":
            continue
        iface = row.get("lan_interface", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", iface) or iface in ("lo", "rp-host"):
            raise ValueError("interface")
        net = ipaddress.IPv4Network(row.get("ip", ""), strict=True)
        if not any(
            net.subnet_of(ipaddress.IPv4Network(n))
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise ValueError("scope address")
        mac = "" if row.get("scope") == "lan" else row.get("mac", "")
        if mac and not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac):
            raise ValueError("mac")
        if row.get("scope") != "lan" and (not mac or net.prefixlen != 32):
            raise ValueError("device binding")
        if any(net.overlaps(ipaddress.IPv4Network(item["network"])) for item in result):
            raise ValueError("overlap")
        result.append(dict(interface=iface, network=str(net), mac=mac))
    if not result:
        raise ValueError("empty scope")
    return result


def rules(rows, wan, epoch, proxy="rp-host"):
    if proxy not in ("rp-host", "rp-backup"):
        raise ValueError("proxy interface")
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", wan)
        or wan in ("lo", "rp-host")
        or any(r["interface"] == wan for r in rows)
    ):
        raise ValueError("WAN")
    if not re.fullmatch("[0-9a-f]{32}", epoch):
        raise ValueError("epoch")
    lines = ["table inet " + TABLE + " {", ' comment "' + PREFIX + epoch + '"']
    lines += [" counter " + key + " {}" for key in KEYS]
    lines += [" chain measure { type filter hook forward priority 200; policy accept;"]
    # Exclude LAN, special/private destinations and namespace tunnel endpoints.
    private = "{ " + ", ".join(PRIVATE4) + " }"
    for row in rows:
        iface, network, mac = row["interface"], row["network"], row["mac"]
        src = 'iifname "%s" ip saddr %s ' % (iface, network)
        if mac:
            src += "ether saddr " + mac + " "
        for name, remote in (("proxy", proxy), ("direct", wan)):
            lines += [
                "  "
                + src
                + "ip daddr != "
                + private
                + ' oifname "'
                + remote
                + '" counter name '
                + name
                + "_up"
            ]
            # MAC destination is not available at forward; use compiled IP binding.
            lines += [
                '  iifname "%s" oifname "%s" ip daddr %s ip saddr != %s counter name %s_down'
                % (remote, iface, network, private, name)
            ]
    return "\n".join(lines + [" }", "}", ""])


def table():
    r = call(["nft", "-j", "list", "table", "inet", TABLE])
    if r.returncode:
        # Distinguish missing table from a general nft failure.
        all_tables = call(["nft", "-j", "list", "tables"])
        if all_tables.returncode:
            raise ValueError("nft unavailable")
        if any(
            x.get("table", {}).get("name") == TABLE and x["table"].get("family") == "inet"
            for x in json.loads(all_tables.stdout)["nftables"]
        ):
            raise ValueError("counter read failed")
        return None
    items = json.loads(r.stdout)["nftables"]
    meta = next(x["table"] for x in items if "table" in x)
    owner = meta.get("comment", "")
    if not owner.startswith(PREFIX) or not re.fullmatch("[0-9a-f]{32}", owner[len(PREFIX) :]):
        raise ValueError("table name occupied")
    raw = {x["counter"]["name"]: x["counter"]["bytes"] for x in items if "counter" in x}
    if not valid_counts(raw):
        raise ValueError("incomplete counters")
    handle = meta.get("handle")
    if type(handle) is not int or handle < 1:
        raise ValueError("table handle")
    return owner[len(PREFIX) :] + "-" + str(handle), raw


def remove_owned():
    if table() is not None:
        if call(["nft", "delete", "table", "inet", TABLE]).returncode:
            raise ValueError("counter removal failed")


def environment(status):
    now = time.time()
    if status.get("state") != "running" or not -5 <= now - float(status.get("time", 0)) <= 15:
        raise ValueError("inactive")
    rows = scope(status)
    r = call(["ip", "-j", "-4", "route", "get", "1.1.1.1"])
    if r.returncode:
        raise ValueError("unknown WAN")
    wan = json.loads(r.stdout)[0]["dev"]
    key = hashlib.sha256(
        json.dumps(
            [
                status.get("runtime_id"),
                status.get("generation"),
                status.get("proxy_interface", "rp-host"),
                rows,
                wan,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return rows, wan, key


def offload():
    # Optional metadata failure must never reset counters or interrupt collection.
    try:
        r = call(["nft", "-j", "list", "table", "inet", "fw4"], timeout=3)
        if r.returncode:
            return "unknown"
        items = json.loads(r.stdout)["nftables"]
        return "enabled" if any("flowtable" in x for x in items) else "not_detected"
    except Exception:
        return "unknown"


def client_version():
    try:
        r = call(["chroot", "/opt/routepilot/runtime", "/usr/bin/warp-cli", "--version"], timeout=3)
        from warp_cli import parse_version

        return parse_version(r.stdout) if r.returncode == 0 else None
    except Exception:
        return None


def refresh_timezone():
    # OpenWrt stores POSIX TZ here, independently of the procd environment.
    try:
        tz = Path("/etc/TZ").read_text().strip()
        if not tz or len(tz) > 128 or any(ord(c) < 32 or ord(c) > 126 for c in tz):
            raise ValueError("timezone")
        os.environ["TZ"] = tz
        if callable(getattr(time, "tzset", None)):
            time.tzset()
        else:
            # OpenWrt python3-light omits time.tzset; musl still exports it.
            fn = ctypes.CDLL(None).tzset
            fn.argtypes, fn.restype = [], None
            fn()
        return True
    except Exception:
        return False


def flush_on_stop(ledger, boot, epoch, active):
    try:
        current = table()
        if active and current and current[0] == epoch:
            ledger.sample(
                boot + ":" + epoch,
                current[1],
                time.time(),
                time.monotonic(),
                synced=clock_synced() and refresh_timezone(),
            )
    except Exception:
        pass
    # Even if the last counter read failed, preserve all already known totals.
    ledger.gap()
    save(ledger.data)


def run():
    global STOP
    STOP = False
    RUN.mkdir(parents=True, exist_ok=True)
    with (RUN / "traffic.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        _observe()


def _observe():
    signal.signal(signal.SIGTERM, lambda *_: request_stop())
    signal.signal(signal.SIGINT, lambda *_: request_stop())
    ledger, error = None, None
    try:
        if STORE.exists() and STORE.stat().st_size > 256 * 1024:
            raise ValueError("ledger size")
        ledger = Ledger(json.loads(STORE.read_text()) if STORE.exists() else None)
    except Exception:
        error = "历史统计文件无法读取，已保留原文件；本轮不覆盖、不清零。"
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    signature, epoch, checkpoint, previous_day = None, None, 0, None
    gap_checkpointed = False
    offload_state, version, checked, version_at = "unknown", None, 0, None
    history = deque(maxlen=60)
    persisted = STORE.stat().st_mtime if STORE.exists() else None
    try:
        while not STOP:
            wall, mono = time.time(), time.monotonic()
            state, reason, rate = "paused", None, None
            timezone_known = refresh_timezone()
            synced = clock_synced() and timezone_known
            try:
                if error:
                    raise ValueError("ledger unavailable")
                st = json.loads((RUN / "status.json").read_text())
                rows, wan, sig = environment(st)
                current = table()
                if sig != signature or current is None or current[0] != epoch:
                    if not gap_checkpointed:
                        # On process restart, recover existing counters using persisted epoch.
                        anchor = ledger.data.get("anchor")
                        if (
                            signature is None
                            and current
                            and anchor
                            and anchor["key"] == boot + ":" + current[0]
                        ):
                            ledger.sample(
                                boot + ":" + current[0], current[1], wall, mono, synced=synced
                            )
                        elif signature is not None and current and current[0] == epoch:
                            ledger.sample(boot + ":" + epoch, current[1], wall, mono, synced=synced)
                        ledger.gap()
                        # Persist reconciled totals before replacing the counter table.
                        save(ledger.data)
                        persisted, checkpoint = wall, mono
                        # If installation fails, the retry has no new counters to persist.
                        gap_checkpointed = True
                    epoch = uuid.uuid4().hex
                    text = ("delete table inet " + TABLE + "\n" if current else "") + rules(
                        rows, wan, epoch, proxy=st.get("proxy_interface", "rp-host")
                    )
                    if call(["nft", "-f", "-"], text).returncode:
                        raise ValueError("counter install failed")
                    signature = sig
                    current = table()
                    if current is None:
                        raise ValueError("counter table disappeared")
                    epoch = current[0]
                    ledger.sample(boot + ":" + epoch, zero(), wall, mono, synced=synced)
                    gap_checkpointed = False
                # Reject a status transition while rules were being read/installed.
                if environment(json.loads((RUN / "status.json").read_text()))[2] != sig:
                    raise ValueError("scope changed")
                rate = ledger.sample(boot + ":" + epoch, current[1], wall, mono, synced=synced)
                state = "observing"
                if mono - checked > 60 or checked == 0:
                    offload_state, version, checked, version_at = (
                        offload(),
                        client_version(),
                        mono,
                        wall,
                    )
                if mono - checkpoint >= 300 or previous_day != day(wall):
                    if save(ledger.data):
                        persisted = wall
                    checkpoint = mono
                previous_day = day(wall)
            except Exception:
                reason = error or "采集已暂停：运行状态、接口或计数器暂无法确认；数字仅保留历史。"
                if ledger and signature is not None and not gap_checkpointed:
                    ledger.gap()
                signature = None
                try:
                    remove_owned()
                except Exception:
                    pass
            history.append(dict(time=wall, rate=rate))
            output = dict(
                schema=1,
                state="error" if error else state,
                time=wall,
                partial=True,
                offload=offload_state,
                message=reason,
                persisted_at=persisted,
                timezone=zone() if timezone_known else "未知",
                clock_synced=synced,
                client_version=version,
                version_checked_at=version_at,
                today=day(wall) if synced else None,
                history=list(history),
            )
            if ledger:
                output.update(
                    {
                        k: ledger.data[k]
                        for k in ("total", "days", "started_at", "unallocated", "gaps")
                    }
                )
            atomic(RUN / "traffic.json", json.dumps(output, allow_nan=False), 0o644)
            for _ in range(10):
                if STOP:
                    break
                time.sleep(0.5)
    finally:
        if ledger:
            try:
                flush_on_stop(ledger, boot, epoch, signature is not None)
            except Exception:
                pass
        try:
            remove_owned()
        except Exception:
            pass


def request_stop():
    global STOP
    STOP = True
