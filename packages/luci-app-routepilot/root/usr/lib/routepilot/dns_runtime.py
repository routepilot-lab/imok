"""Private control and aggregate observation for the plugin DNS child only."""

from contextlib import ExitStack
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import struct
import threading
import time
import uuid

from runtime import RUN, atomic

CONTROL = RUN / "dns-control.sock"
STATE = RUN / "dns-state.json"


def root_peer(conn):
    # Linux target. Fail closed on unsupported peer authentication.
    try:
        _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == 0
    except (AttributeError, OSError, struct.error):
        return False


def read_message(conn):
    data = bytearray()
    deadline = time.monotonic() + 1
    while len(data) <= 4096:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Control deadline exceeded")
        conn.settimeout(remaining)
        chunk = conn.recv(min(1024, 4097 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if b"\n" in data:
            raw, rest = bytes(data).split(b"\n", 1)
            if rest or len(raw) > 4096:
                raise ValueError("Invalid control frame")
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Invalid control request")
            return value
    raise ValueError("Control frame missing or too large")


def current_context(status, state, now=None):
    now = time.time() if now is None else now
    return (
        isinstance(status, dict)
        and isinstance(state, dict)
        and status.get("state") == "running"
        and -5 <= now - float(status.get("time", 0)) <= 15
        and -1 <= now - float(state.get("time", 0)) <= 5
        and bool(status.get("runtime_id"))
        and bool(status.get("generation"))
        and all(status.get(k) == state.get(k) for k in ("runtime_id", "generation"))
    )


class DNSRuntime:
    def __init__(self, spec, control=CONTROL, state=STATE):
        from native import block_aaaa_for, write_sets
        from dns_classification import ClassificationWriter

        self.control_path, self.state_path = Path(control), Path(state)
        self.identity = {k: spec.get(k) for k in ("runtime_id", "generation")}
        self.identity["process_instance"] = uuid.uuid4().hex
        self.classifier = (
            ClassificationWriter(spec["table_epoch"], write_sets)
            if spec.get("table_epoch")
            else None
        )
        self.started = time.time()
        self.policies = []
        self.lock = threading.Lock()
        self.publish_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.listener = self.thread = None
        self.control_inode = None
        self.control_available = False
        self.last_publish = 0
        self.last_clear = None
        raw_policies = spec.get("policies", [])
        if not 1 <= len(raw_policies) <= 8:
            raise ValueError("DNS policy count outside supported range")
        for index, raw in enumerate(raw_policies):
            policy = dict(raw)
            for key, default in (
                ("dns_foreign", "1.1.1.1"),
                ("dns_domestic", "223.5.5.5"),
                ("transport", "tcp"),
            ):
                policy.setdefault(key, spec.get(key, default))
            policy["block_aaaa"] = block_aaaa_for(raw, spec)
            for key in (
                "custom_direct_domains",
                "custom_proxy_domains",
                "china_domains",
                "mode_domains",
            ):
                policy[key] = frozenset(policy.get(key, []))
            policy["_cache_lock"], policy["_stats_lock"] = threading.Lock(), threading.Lock()
            policy["_cache_epoch"], policy["_stats"] = 0, {}
            policy["_classification_at_send"] = self.classifier is not None
            policy["_classifier"] = self.classifier
            if spec.get("cache", True):
                policy["_cache"] = {}
                policy["_cache_wire_bytes"] = 0
            for name in ("dns_foreign", "dns_domestic"):
                ipaddress.IPv4Address(policy[name])
            policy["port"] = int(policy["port"])
            if not 12100 <= policy["port"] <= 12107 or any(
                p["port"] == policy["port"] for p in self.policies
            ):
                raise ValueError("Invalid or duplicate DNS policy port")
            self.policies.append(policy)

    def snapshot(self):
        items = []
        for index, policy in enumerate(self.policies):
            with policy["_cache_lock"]:
                entries = sum(v[0] > time.monotonic() for v in policy.get("_cache", {}).values())
            with policy["_stats_lock"]:
                stats = {
                    k: policy["_stats"].get(k, 0)
                    for k in (
                        "requests",
                        "cache_hits",
                        "prefetched",
                        "served_stale",
                        "upstream_queries",
                        "errors",
                        "overloaded",
                        "classification_busy",
                        "filtered_aaaa",
                        "coalesced",
                        "upstream_tcp_reused",
                        "udp_queue_full",
                        "udp_queue_expired",
                        "udp_waiters_full",
                    )
                }
            items.append(
                dict(
                    index=index + 1,
                    label=str(policy.get("label") or "受管范围 " + str(index + 1))[:64],
                    port=policy["port"],
                    cache_enabled="_cache" in policy,
                    cache_entries=entries,
                    **stats,
                )
            )
        return dict(
            schema=1,
            state="running",
            time=time.time(),
            started_at=self.started,
            **self.identity,
            control_available=self.control_available,
            table_epoch=self.classifier.epoch if self.classifier else None,
            classification_paused=self.classifier.paused if self.classifier else False,
            upstreams={
                k: self.policies[0][k]
                for k in ("dns_domestic", "dns_foreign", "transport", "proxy_dns_override")
                if k in self.policies[0]
            },
            policies=items,
            last_clear=self.last_clear,
        )

    def clear(self):
        removed = 0
        # One ordering for every clear. Inflight queries retain the old epoch.
        with self.lock, ExitStack() as stack:
            for policy in self.policies:
                stack.enter_context(policy["_cache_lock"])
            for policy in self.policies:
                cache = policy.get("_cache")
                if cache is not None:
                    removed += len(cache)
                    cache.clear()
                    policy["_cache_wire_bytes"] = 0
                policy["_cache_epoch"] += 1
            self.last_clear = dict(request_id=uuid.uuid4().hex, time=time.time(), removed=removed)
        self.publish(force=True)
        return dict(ok=True, **self.identity, **self.last_clear)

    def publish(self, force=False):
        with self.publish_lock:
            if not force and time.monotonic() - self.last_publish < 1:
                return
            self.last_publish = time.monotonic()
            try:
                atomic(self.state_path, json.dumps(self.snapshot()), 0o644)
            except (OSError, ValueError):
                pass  # Observation must not stop packet forwarding.

    def start(self):
        try:
            self.control_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if self.control_path.exists():
                old = self.control_path.lstat()
                if not stat.S_ISSOCK(old.st_mode) or old.st_uid != os.getuid():
                    raise OSError("Unknown control path")
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    try:
                        probe.connect(str(self.control_path))
                    except ConnectionRefusedError:
                        if self.control_path.lstat().st_ino == old.st_ino:
                            self.control_path.unlink()
                    else:
                        raise OSError("Control path is active")
            self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.listener.bind(str(self.control_path))
            self.control_inode = self.control_path.lstat().st_ino
            self.control_path.chmod(0o600)
            self.listener.listen(4)
            self.listener.settimeout(0.3)
            self.control_available = True
            self.thread = threading.Thread(target=self.serve, daemon=True)
            self.thread.start()
        except OSError:
            if self.listener:
                self.listener.close()
            self.listener = None
        self.publish(force=True)

    def serve(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(1)
                try:
                    if not root_peer(conn):
                        continue
                    request = read_message(conn)
                    fields = {"action", "runtime_id", "generation", "process_instance"}
                    action = request.get("action")
                    if action in ("pause-classification", "resume-classification"):
                        fields.add("table_epoch")
                    if action == "resume-classification":
                        fields.add("new_epoch")
                    if set(request) != fields or any(
                        request.get(k) != v for k, v in self.identity.items()
                    ):
                        raise ValueError("DNS process changed")
                    if action == "clear":
                        result = self.clear()
                    elif action == "status":
                        result = dict(ok=True, **self.snapshot())
                    elif self.classifier and action == "pause-classification":
                        self.classifier.pause(request["table_epoch"])
                        self.publish(force=True)
                        result = dict(ok=True, table_epoch=self.classifier.epoch, paused=True)
                    elif self.classifier and action == "resume-classification":
                        new_epoch = request["new_epoch"]
                        if not isinstance(new_epoch, str) or len(new_epoch) != 32:
                            raise ValueError("Invalid DNS table epoch")
                        self.classifier.resume(request["table_epoch"], new_epoch)
                        self.publish(force=True)
                        result = dict(ok=True, table_epoch=self.classifier.epoch, paused=False)
                    else:
                        raise ValueError("Unsupported action")
                    conn.sendall((json.dumps(result) + "\n").encode())
                except (OSError, ValueError, TypeError):
                    try:
                        conn.sendall(
                            b'{"ok":false,"message":"DNS control unavailable or changed"}\n'
                        )
                    except OSError:
                        pass

    def stop(self):
        self.stop_event.set()
        if self.listener:
            self.listener.close()
        if self.thread:
            self.thread.join(timeout=2)
        try:
            if (
                self.control_inode is not None
                and self.control_path.lstat().st_ino == self.control_inode
            ):
                self.control_path.unlink()
        except OSError:
            pass


def classification_control(run, runtime_id, epoch, *, resume=None):
    run = Path(run)
    state = json.loads((run / "dns-state.json").read_text())
    if state.get("runtime_id") != runtime_id or not state.get("control_available"):
        raise ValueError("DNS classification control not current")
    request = dict(
        action="pause-classification" if resume is None else "resume-classification",
        table_epoch=epoch,
        **{k: state[k] for k in ("runtime_id", "generation", "process_instance")},
    )
    if resume is not None:
        request["new_epoch"] = resume
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(1)
        conn.connect(str(run / "dns-control.sock"))
        if not root_peer(conn):
            raise ValueError("Unexpected control peer")
        conn.sendall((json.dumps(request) + "\n").encode())
        result = read_message(conn)
    if (
        not result.get("ok")
        or result.get("table_epoch") != (resume or epoch)
        or result.get("paused") is not (resume is None)
    ):
        raise ValueError("DNS table epoch was not acknowledged")


def request_clear(run=RUN):
    run = Path(run)
    status = json.loads((run / "status.json").read_text())
    state = json.loads((run / "dns-state.json").read_text())
    if not current_context(status, state) or not state.get("control_available"):
        raise ValueError("DNS control not current")
    request = dict(
        action="clear", **{k: state[k] for k in ("runtime_id", "generation", "process_instance")}
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(3)
        conn.connect(str(run / "dns-control.sock"))
        if not root_peer(conn):
            raise ValueError("Unexpected control peer")
        conn.sendall((json.dumps(request) + "\n").encode())
        result = read_message(conn)
    now_status = json.loads((run / "status.json").read_text())
    now_state = json.loads((run / "dns-state.json").read_text())
    if (
        not result.get("ok")
        or not current_context(now_status, now_state)
        or any(
            result.get(k) != now_state.get(k)
            for k in ("runtime_id", "generation", "process_instance")
        )
    ):
        raise ValueError("DNS process changed during cache clear")
    return result
