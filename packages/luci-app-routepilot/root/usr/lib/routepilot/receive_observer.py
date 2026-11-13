"""Bounded read-only UDP receiver telemetry, independent of forwarding health.

No sockets are opened/duplicated, and no kernel or UCI settings are written.
Only the observer's JSON in /var/run is published. Peaks are sampled, not exact.
"""

from collections import deque
import ipaddress
import json
import os
from pathlib import Path
import signal
import time
import uuid

from runtime import atomic, RUN
from udp_buffers import INGRESS

ROOT = Path("/opt/routepilot/runtime")
NETNS = Path("/var/run/netns/routepilot")
MAX_PROCESSES = 4096
MAX_FDS = 2048
MAX_SOCKETS = 32
MAX_ROWS = 4096
PERIOD = 0.1
STOP = False


def process_key(pid):
    proc = Path("/proc") / str(pid)
    if os.readlink(proc / "exe") != str(ROOT / "usr/bin/warp-svc"):
        raise ValueError("not the dedicated client")
    ns, expected = (proc / "ns/net").stat(), NETNS.stat()
    if (ns.st_dev, ns.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValueError("wrong namespace")
    # comm can contain both spaces and parentheses.
    fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
    return (int(pid), int(fields[19]), ns.st_dev, ns.st_ino)


def discover():
    for number, path in enumerate(Path("/proc").glob("[0-9]*")):
        if number >= MAX_PROCESSES:
            raise ValueError("process scan limit")
        try:
            return process_key(int(path.name))
        except (OSError, ValueError, IndexError):
            continue
    return None


def find_client(previous):
    if previous:
        try:
            if process_key(previous[0]) == previous:
                return previous
        except (OSError, ValueError, IndexError):
            pass
    return discover()


def udp_address(raw):
    address, port = raw.split(":")
    if len(address) not in (8, 32):
        raise ValueError("address format")
    data = b"".join(bytes.fromhex(address[i : i + 8])[::-1] for i in range(0, len(address), 8))
    ip = ipaddress.ip_address(data)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip, int(port, 16)


def parse_udp(lines, wanted=None):
    records, complete = {}, True
    for number, line in enumerate(lines):
        if number == 0:
            if "local_address" not in line or "drops" not in line:
                raise ValueError("UDP header format")
            continue
        if number > MAX_ROWS:
            complete = False
            break
        try:
            fields = line.split()
            inode = str(int(fields[9]))
            if wanted is not None and inode not in wanted:
                continue
            ip, port = udp_address(fields[2])
            queue = int(fields[4].split(":")[1], 16)
            drops = int(fields[-1])
            if queue < 0 or drops < 0:
                raise ValueError("negative counter")
            records[inode] = dict(
                ip=ip, port=port, connected=fields[3] == "01", queue=queue, drops=drops
            )
        except (ValueError, IndexError):
            complete = False
    return records, complete


def read_udp(pid, wanted=None):
    rows, complete = {}, True
    for family in ("udp", "udp6"):
        try:
            with (Path("/proc") / str(pid) / "net" / family).open() as stream:
                part, ok = parse_udp(stream, wanted)
            rows.update(part)
            complete = complete and ok
        except FileNotFoundError:
            if family == "udp":
                raise
        except (OSError, ValueError):
            complete = False
    return rows, complete


def owned_sockets(pid, rows):
    owned, complete = {}, True
    for number, path in enumerate((Path("/proc") / str(pid) / "fd").iterdir()):
        if number >= MAX_FDS:
            complete = False
            break
        try:
            value = os.readlink(path)
            if not value.startswith("socket:["):
                continue
            inode = value[8:-1]
            row = rows.get(inode)
            if (
                not row
                or not row["connected"]
                or not row["port"]
                or not row["ip"].is_global
                or row["ip"].is_multicast
            ):
                continue
            category = (
                "known_ingress" if any(row["ip"] in net for net in INGRESS) else "other_public_udp"
            )
            if inode not in owned and len(owned) >= MAX_SOCKETS:
                complete = False
                continue
            owned[inode] = dict(fd=int(path.name), category=category)
        except OSError:
            complete = False
    return owned, complete


class Tracker:
    """Counters use per-inode baselines; kernel lifetime totals aren't new loss."""

    def __init__(self, identity, wall):
        self.identity = identity
        self.session_id = uuid.uuid4().hex
        self.started = wall
        self.sockets = {}
        self.windows = deque(maxlen=60)
        self.total_drops = 0
        self.resets = 0
        self.gaps = 0
        self.missing = 0
        self.ever_partial = False
        self.period_drops = 0
        self.period_peak = 0
        self.period_samples = 0
        self.period_complete = True
        self.last_mono = None

    def sample(self, rows, owned, complete, wall, mono):
        gap = self.last_mono is not None and mono - self.last_mono > 0.3
        if gap:
            self.gaps += 1
        self.last_mono = mono
        self.period_complete = self.period_complete and complete and not gap
        self.ever_partial = self.ever_partial or not complete or gap
        # Remove closed sockets without keeping an unbounded inode history.
        for inode in list(self.sockets):
            if inode not in owned:
                del self.sockets[inode]
        queues = []
        for inode, meta in owned.items():
            row = rows.get(inode)
            old = self.sockets.get(inode)
            if row is None:
                self.period_complete = False
                self.ever_partial = True
                self.missing += 1
                if old:
                    old["valid"] = False
                continue
            queues.append(row["queue"])
            reset = old is not None and (not old["valid"] or row["drops"] < old["drops_kernel"])
            if reset:
                self.resets += 1
                self.period_complete = False
                self.ever_partial = True
            if old is None:
                old = dict(first_seen=wall, peak_queue=0, observed_drops=0)
                self.sockets[inode] = old
            elif not reset and not gap:
                delta = row["drops"] - old["drops_kernel"]
                self.total_drops += delta
                self.period_drops += delta
                old["observed_drops"] += delta
            old.update(
                meta, valid=True, drops_kernel=row["drops"], queue=row["queue"], last_seen=wall
            )
            old["peak_queue"] = max(old["peak_queue"], row["queue"])
        self.period_peak = max(self.period_peak, sum(queues))
        self.period_samples += 1

    def publish(self, wall, state, complete):
        valid = [dict(id=k, **v) for k, v in self.sockets.items() if v["valid"]]
        known = [v for v in valid if v["category"] == "known_ingress"]
        window = dict(
            time=wall,
            peak_queue=self.period_peak,
            new_drops=self.period_drops,
            samples=self.period_samples,
            complete=bool(complete and self.period_complete),
        )
        self.windows.append(window)
        report = dict(
            schema=1,
            state=state,
            time=wall,
            started_at=self.started,
            runtime_id=self.identity[-2],
            generation=self.identity[-1],
            session_id=self.session_id,
            sample_interval_ms=100,
            publish_interval_ms=1000,
            coverage="partial" if self.ever_partial or not complete else "complete",
            observed_drops=self.total_drops,
            queue_now=sum(v["queue"] for v in valid),
            peak_queue=max((w["peak_queue"] for w in self.windows), default=0),
            socket_count=len(valid),
            known_ingress_count=len(known),
            other_public_udp_count=len(valid) - len(known),
            gaps=self.gaps,
            counter_resets=self.resets,
            missing_samples=self.missing,
            sockets=valid,
            windows=list(self.windows),
        )
        self.period_drops = self.period_peak = self.period_samples = 0
        self.period_complete = True
        if state == "observing" and not valid:
            return unavailable("unavailable", wall)
        return report


def unavailable(state, wall):
    return dict(
        schema=1,
        state=state,
        time=wall,
        sockets=[],
        windows=[],
        coverage="unknown",
        observed_drops=None,
        queue_now=None,
        peak_queue=None,
    )


def run():
    global STOP

    def halt(*_):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    try:
        os.nice(10)
    except OSError:
        pass
    tracker, key, owned = None, None, {}
    refresh = publish = 0
    complete = False
    ready = False
    while not STOP:
        wall, mono = time.time(), time.monotonic()
        report = None
        try:
            if not ready:
                RUN.mkdir(parents=True, exist_ok=True)
                ready = True
            if mono >= refresh:
                status = json.loads((RUN / "status.json").read_text())
                age = wall - float(status.get("time", 0))
                candidate = (
                    find_client(key)
                    if status.get("state") == "running"
                    and -5 <= age <= 15
                    and status.get("runtime_id")
                    and status.get("generation")
                    else None
                )
                identity = (
                    (*candidate, status["runtime_id"], status["generation"]) if candidate else None
                )
                if identity is None:
                    tracker, key, owned = None, None, {}
                    report = unavailable("inactive", wall)
                else:
                    key = candidate
                    if tracker is None or tracker.identity != identity:
                        tracker = Tracker(identity, wall)
                    rows, ok = read_udp(key[0])
                    owned, coverage = owned_sockets(key[0], rows)
                    complete = ok and coverage
                    if not owned and not complete:
                        raise OSError("socket coverage unavailable")
                refresh = mono + 1
            if tracker and key:
                # Verify PID starttime and namespace on every sample, including FD refresh races.
                if process_key(key[0]) != key:
                    raise ValueError("client identity changed")
                rows, ok = read_udp(key[0], set(owned))
                if process_key(key[0]) != key:
                    raise ValueError("client identity changed")
                tracker.sample(rows, owned, complete and ok, wall, mono)
                if mono >= publish:
                    report = tracker.publish(
                        wall, "observing" if owned else "no_sockets", complete and ok
                    )
        except Exception:
            tracker, key, owned = None, None, {}
            report = unavailable("unavailable", wall)
            refresh = mono + 1
        if report is not None and mono >= publish:
            try:
                atomic(RUN / "receive.json", json.dumps(report, separators=(",", ":")), 0o644)
            except OSError:
                pass  # An observer failure must never change forwarding or service health.
            publish = mono + 1
        time.sleep(max(0.01, PERIOD - (time.monotonic() - mono)))
    try:
        atomic(RUN / "receive.json", json.dumps(unavailable("inactive", time.time())), 0o644)
    except OSError:
        pass


if __name__ == "__main__":
    run()
