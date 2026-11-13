"""Independent, marked DNS forwarding process for plugin-owned policies.

Only this child process starts worker threads. The supervisor never gains threads.
No query names, configuration or upstream exceptions are logged.
"""

import concurrent.futures
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import ctypes
import math

MAX_RESPONSE_TTL = 300
BACKUP_SOCKS_PORT = 15581
# Allow a short burst while TCP connections are still warming up. The
# admission limit includes running requests across all device policies.
UDP_WORKERS = 48
UDP_MAX_PENDING = 256
UDP_MAX_QUEUE_AGE = 3.0
UDP_MAX_WAITERS = 32
TCP_MAX_CONNECTIONS = 16
UPSTREAM_TCP_IDLE = 30.0
# About 32 MiB at 1 KiB per entry and eight policies. The wire-byte cap also bounds large answers.
DNS_CACHE_ENTRIES = 4096
DNS_CACHE_WIRE_BYTES = 4 * 1024 * 1024
STALE_GRACE = 60
STALE_WAIT = 0.4
STALE_TTL = 5
REFRESH_WORKERS = 4
UPSTREAM_TCP_IDLE_LIMIT = 32
UPSTREAM_TIMEOUT = 6.0


class DNSFormatError(ValueError):
    pass


def read_name(packet, offset):
    labels, seen, end, wire_size = [], set(), None, 1
    for _ in range(128):
        if offset >= len(packet) or offset in seen:
            raise DNSFormatError("Invalid DNS name")
        seen.add(offset)
        size = packet[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise DNSFormatError("Invalid DNS pointer")
            target = ((size & 0x3F) << 8) | packet[offset + 1]
            if target >= offset:
                raise DNSFormatError("Invalid DNS pointer")
            if end is None:
                end = offset + 2
            offset = target
            continue
        if size & 0xC0 or size > 63:
            raise DNSFormatError("Invalid DNS label")
        offset += 1
        if not size:
            name = ".".join(labels).lower()
            return name, end if end is not None else offset
        if offset + size > len(packet):
            raise DNSFormatError("Truncated DNS label")
        wire_size += size + 1
        if wire_size > 255:
            raise DNSFormatError("DNS name too long")
        # RFC 2181 section 11 permits arbitrary label octets, not only hostnames.
        # Escape special octets for internal comparisons; literal dots/backslashes
        # must never become suffix separators or impersonate configured domains.
        raw = packet[offset : offset + size]
        if re.fullmatch(rb"[A-Za-z0-9_*-]+", raw):
            label = raw.decode("ascii").lower()
        else:
            label = "".join(
                chr(value).lower()
                if (
                    65 <= value <= 90
                    or 97 <= value <= 122
                    or 48 <= value <= 57
                    or value in (45, 95, 42)
                )
                else "\\%03d" % value
                for value in raw
            )
        labels.append(label)
        offset += size
    raise DNSFormatError("DNS pointer depth exceeded")


def question(packet, response=False):
    if len(packet) < 12:
        raise DNSFormatError("Truncated DNS header")
    ident, flags, count, _, _, _ = struct.unpack("!6H", packet[:12])
    if count != 1 or bool(flags & 0x8000) != response or flags & 0x7800:
        raise DNSFormatError("Unsupported DNS query")
    name, pos = read_name(packet, 12)
    if pos + 4 > len(packet):
        raise DNSFormatError("Truncated DNS question")
    qtype, qclass = struct.unpack("!HH", packet[pos : pos + 4])
    if qclass != 1:
        raise DNSFormatError("Unsupported DNS class")
    return ident, name, qtype, pos + 4


def answer_addresses(packet, expected_name=None):
    """Return reachable answer A/AAAA records and query/CNAME names.

    Additional-section addresses are deliberately excluded: they do not prove
    that the query should route to these addresses.
    """
    _, name, _, offset = question(packet, True)
    if expected_name is not None and name != expected_name:
        raise DNSFormatError("Mismatched DNS question")
    _, flags, _, answers, authority, additional = struct.unpack("!6H", packet[:12])
    if flags & 0x000F or flags & 0x0200:
        return [], {name}
    records, aliases = [], {}
    for index in range(answers + authority + additional):
        owner, offset = read_name(packet, offset)
        if offset + 10 > len(packet):
            raise DNSFormatError("Truncated DNS record")
        kind, klass, ttl, size = struct.unpack("!HHIH", packet[offset : offset + 10])
        offset += 10
        finish = offset + size
        if finish > len(packet):
            raise DNSFormatError("Truncated DNS data")
        if index < answers and klass == 1:
            if kind == 5:
                alias, consumed = read_name(packet, offset)
                if consumed != finish:
                    raise DNSFormatError("Invalid CNAME data")
                aliases[owner] = alias
            elif kind == 1 and size == 4:
                records.append((owner, str(ipaddress.IPv4Address(packet[offset:finish]))))
            elif kind == 28 and size == 16:
                records.append((owner, str(ipaddress.IPv6Address(packet[offset:finish]))))
        offset = finish
    names = {name}
    current = name
    for _ in range(128):
        if current not in aliases:
            break
        current = aliases[current]
        if current in names:
            raise DNSFormatError("CNAME loop")
        names.add(current)
    else:
        raise DNSFormatError("CNAME depth exceeded")
    return [addr for owner, addr in records if owner in names], names


def suffix_match(names, domains):
    domains = domains if isinstance(domains, (set, frozenset)) else set(domains)
    return any("full:" + name in domains for name in names) or any(
        ".".join(name.split(".")[index:]) in domains
        for name in names
        for index in range(len(name.split(".")))
    )


def matching_sets(policy, names):
    """Both custom categories may match; nft precedence remains direct first."""
    result = []
    if suffix_match(names, policy.get("custom_direct_domains", ())):
        result.append(policy["direct_set"])
    if suffix_match(names, policy.get("custom_proxy_domains", ())):
        result.append(policy["proxy_set"])
    if policy.get("mode") in ("gfw", "whitelist") and suffix_match(
        names, policy.get("mode_domains", ())
    ):
        result.append(policy["mode_set"])
    return result


def populate_sets(policy, names, addresses):
    sets = matching_sets(policy, names)
    ipv4 = sorted({str(ipaddress.IPv4Address(addr)) for addr in addresses if ":" not in addr})
    if sets and ipv4:
        write_sets({name: ipv4 for name in sets})


def classification_entries(policy, names, addresses):
    return [
        (set_name, ip)
        for set_name in matching_sets(policy, names)
        for ip in addresses
        if ":" not in ip
    ]


def write_sets(groups, *, timeout=5):
    lines = []
    for set_name, ipv4 in groups.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", set_name):
            raise ValueError("Invalid set identifier")
        # nft's userspace grammar has no "update element" command. Ensure
        # existence, then delete/re-add within one transaction to refresh expiry.
        elements = ", ".join(ipv4)
        lines.append("add element inet routepilot %s { %s }" % (set_name, elements))
        lines.append("delete element inet routepilot %s { %s }" % (set_name, elements))
        lines.append(
            "add element inet routepilot %s { %s }"
            % (set_name, ", ".join(addr + " timeout 1h" for addr in ipv4))
        )
    result = subprocess.run(
        ["nft", "-f", "-"],
        input="\n".join(lines) + "\n",
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("DNS routing classification unavailable")


def remaining_time(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("DNS upstream deadline exceeded")
    return remaining


def _recv_exact(conn, size, deadline=None):
    result = b""
    while len(result) < size:
        if deadline is not None:
            conn.settimeout(remaining_time(deadline))
        part = conn.recv(size - len(result))
        if not part:
            raise DNSFormatError("Truncated DNS stream")
        result += part
    return result


def read_tcp_response(conn, query, deadline=None):
    if deadline is not None:
        conn.settimeout(remaining_time(deadline))
    conn.sendall(struct.pack("!H", len(query)) + query)
    (size,) = struct.unpack("!H", _recv_exact(conn, 2, deadline))
    return _recv_exact(conn, size, deadline)


class UpstreamTCP:
    """Idle sockets are keyed by DNS address and routing mark. One query owns a socket at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.idle = []
        self.closed = False

    def reap(self):
        with self.lock:
            now = time.monotonic()
            while self.idle and self.idle[0][0] <= now:
                self.idle.pop(0)[2].close()

    def take(self, key):
        with self.lock:
            now = time.monotonic()
            for index in range(len(self.idle) - 1, -1, -1):
                until, route, conn = self.idle[index]
                if until > now and route == key:
                    self.idle.pop(index)
                    return conn
        return None

    def release(self, key, conn):
        with self.lock:
            if not self.closed:
                if len(self.idle) >= UPSTREAM_TCP_IDLE_LIMIT:
                    self.idle.pop(0)[2].close()
                self.idle.append((time.monotonic() + UPSTREAM_TCP_IDLE, key, conn))
                return
        conn.close()

    def exchange(self, query, expected, server, mark):
        key = (server, mark)
        deadline = time.monotonic() + UPSTREAM_TIMEOUT
        conn = self.take(key)
        while True:
            reused = conn is not None
            if not reused:
                conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if not reused:
                    conn.settimeout(remaining_time(deadline))
                    conn.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), mark)
                    conn.connect((server, 53))
                response = read_tcp_response(conn, query, deadline)
                if question(response, True)[:3] != expected:
                    raise DNSFormatError("Mismatched DNS response")
            except (OSError, DNSFormatError):
                conn.close()
                if not reused or time.monotonic() >= deadline:
                    raise
                # An upstream can close an idle socket between our checks.
                conn = None
                continue
            self.release(key, conn)
            return response, reused

    def close(self):
        with self.lock:
            self.closed = True
            for _, _, conn in self.idle:
                conn.close()
            self.idle.clear()


def dns_route(name, policy):
    domestic = suffix_match({name}, policy.get("custom_direct_domains", ())) or (
        not suffix_match({name}, policy.get("custom_proxy_domains", ()))
        and suffix_match({name}, policy.get("china_domains", ()))
    )
    if policy.get("guarded_global"):
        domestic = False
    server = str(
        ipaddress.IPv4Address(
            policy.get("dns_domestic", "223.5.5.5") if domestic else policy["dns_foreign"]
        )
    )
    mark = policy["mark"]
    mark = int(mark, 0) if isinstance(mark, str) else int(mark)
    return server, 0 if domestic else mark, "domestic" if domestic else "proxy"


def count(policy, key, amount=1):
    if amount and "_stats_lock" in policy:
        with policy["_stats_lock"]:
            policy["_stats"][key] = policy["_stats"].get(key, 0) + amount


def backup_doh(query, port=BACKUP_SOCKS_PORT):
    """Fixed, certificate-verified DoH through the backup, never host DNS.

    curl provides a whole-request deadline; existing DNS concurrency/cache bounds
    also apply here. No environment bypass, configurable URL, log or query file.
    """
    if type(port) is not int or not 1024 <= port <= 65535:
        raise DNSFormatError("Invalid local encrypted DNS port")
    result = subprocess.run(
        [
            "curl",
            "-q",
            "-4",
            "-fsS",
            "--noproxy",
            "",
            "--proxy",
            "socks5h://127.0.0.1:" + str(port),
            "--proto",
            "=https",
            "--connect-timeout",
            "3",
            "--max-time",
            "6",
            "--max-filesize",
            "65535",
            "-H",
            "Content-Type: application/dns-message",
            "-H",
            "Accept: application/dns-message",
            "--data-binary",
            "@-",
            "https://dns.google/dns-query",
        ],
        input=query,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=7,
    )
    if result.returncode or not 12 <= len(result.stdout) <= 65535:
        raise DNSFormatError("Backup encrypted DNS unavailable")
    return result.stdout


def blocked_aaaa_response(query, ident, finish):
    flags = 0x8080 | (struct.unpack("!H", query[2:4])[0] & 0x0100)
    return struct.pack("!6H", ident, flags, 1, 0, 0, 0) + query[12:finish]


def query_opt(query, finish):
    if query[10:12] != b"\0\1" or len(query) < finish + 11 or query[finish] != 0:
        return None
    kind, udp_size, flags, length = struct.unpack_from("!HHIH", query, finish + 1)
    if kind == 41 and len(query) == finish + 11 + length:
        return max(512, min(udp_size, 65507)), flags, length
    return None


def ordinary_query(query):
    _, _, _, finish = question(query)
    _, flags, qd, answers, authority, extra = struct.unpack("!6H", query[:12])
    if flags & ~0x0110 or qd != 1 or answers or authority or extra not in (0, 1):
        return None
    offset = 12
    while offset < finish - 4 and 0 < query[offset] <= 63:
        offset += 1 + query[offset]
    if offset != finish - 5 or query[offset] != 0:
        return None  # Leave compressed or otherwise unusual questions on the wire key.
    if not extra:
        return (finish, None) if len(query) == finish else None
    opt = query_opt(query, finish)
    if opt is None or opt[1] & ~0x8000 or opt[2]:
        return None
    return finish, bool(opt[1] & 0x8000)


def cache_key(query):
    ordinary = ordinary_query(query)
    if ordinary is None:
        return query[2:]
    finish, dnssec = ordinary
    return query[2:4], query[12:finish].lower(), dnssec


def client_response(query, response, *, tcp=False):
    ordinary = ordinary_query(query)
    packet = query[:2] + response[2:]
    if ordinary is not None:
        finish, _ = ordinary
        _, _, _, end = question(response, True)
        if end == finish and response[12:end].lower() == query[12:finish].lower():
            packet = packet[:12] + query[12:finish] + packet[finish:]
        elif response[:2] != query[:2]:
            return failure(query)
    if not tcp and len(packet) > 512:
        ident, _, _, finish = question(query)
        opt = query_opt(query, finish)
        if len(packet) > (opt[0] if opt else 512):
            flags = struct.unpack_from("!H", packet, 2)[0] | 0x0200
            # A truncated EDNS reply still carries an empty OPT record.
            extra = b"\0" + struct.pack("!HHIH", 41, opt[0], opt[1] & 0x8000, 0) if opt else b""
            return (
                struct.pack("!6H", ident, flags, 1, 0, 0, int(bool(opt))) + query[12:finish] + extra
            )
    return packet


def remember_response(policy, key, epoch, response, received):
    flags = struct.unpack_from("!H", response, 2)[0]
    if flags & 0x0200 or (flags & 15) not in (0, 3):
        return
    ttl = response_ttl(response)
    with policy["_cache_lock"]:
        if epoch != policy.get("_cache_epoch", 0):
            return
        cache = policy["_cache"]
        wire_bytes = policy.get("_cache_wire_bytes")
        if wire_bytes is None:
            wire_bytes = sum(len(entry[1]) for entry in cache.values())
        replaced = cache.pop(key, None)
        if replaced is not None:
            wire_bytes -= len(replaced[1])
        now = time.monotonic()
        if (flags & 15) == 3 or received + ttl <= now:
            # An NXDOMAIN or a valid zero-TTL reply invalidates older data.
            policy["_cache_wire_bytes"] = wire_bytes
            return
        while cache and (
            len(cache) >= DNS_CACHE_ENTRIES or wire_bytes + len(response) > DNS_CACHE_WIRE_BYTES
        ):
            oldest = min(cache, key=lambda candidate: cache[candidate][0])
            wire_bytes -= len(cache.pop(oldest)[1])
        if received + ttl > now:
            cache[key] = (received + ttl, response, received, False)
            wire_bytes += len(response)
        policy["_cache_wire_bytes"] = wire_bytes


def fetch_response(query, policy, tcp):
    ident, name, qtype, _ = question(query)
    server, mark, _ = dns_route(name, policy)
    tcp = tcp or policy.get("transport") == "tcp"
    count(policy, "upstream_queries")
    pool = policy.get("_upstream_tcp")
    if mark and policy.get("proxy_dns_override") == "https://dns.google/dns-query":
        response = backup_doh(query)
    elif tcp and pool is not None:
        response, reused = pool.exchange(query, (ident, name, qtype), server, mark)
        count(policy, "upstream_tcp_reused", int(reused))
    else:
        with socket.socket(
            socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
        ) as sock:
            sock.settimeout(UPSTREAM_TIMEOUT)
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_MARK", 36), mark)
            sock.connect((server, 53))
            if tcp:
                response = read_tcp_response(sock, query)
            else:
                sock.send(query)
                response = sock.recv(65535)
    received = time.monotonic()
    rid, rname, rtype, _ = question(response, True)
    if (ident, name, qtype) != (rid, rname, rtype):
        raise DNSFormatError("Mismatched DNS response")
    response = limit_response_ttl(response)
    addresses, names = answer_addresses(response, name)
    if not policy.get("_classification_at_send"):
        populate_sets(policy, names, addresses)
    return response, received, names, addresses


class CacheResponse(bytes):
    def __new__(cls, packet, epoch):
        response = super().__new__(cls, packet)
        response.cache_epoch = epoch
        return response


def refresh_response(query, policy, tcp, key, epoch, *, prefetch=False):
    pool = policy.get("_refresh_pool")
    if pool is None:
        return None
    refreshes = policy["_refreshes"]
    with policy["_cache_lock"]:
        if epoch != policy["_cache_epoch"]:
            return None
        identity = (key, epoch)
        if identity in refreshes:
            return refreshes[identity]
        if len(refreshes) >= 8 or not policy["_refresh_slots"].acquire(False):
            return None
        classifier = policy.get("_classifier")
        table_epoch = classifier.epoch if classifier else None

        def refresh():
            response, received, names, addresses = fetch_response(query, policy, tcp)
            remember_response(policy, key, epoch, response, received)
            if classifier is not None and not (struct.unpack_from("!H", response, 2)[0] & 0x020F):
                classifier.queue_refresh(
                    table_epoch, classification_entries(policy, names, addresses)
                )
            return response, received

        try:
            future = pool.submit(refresh)
        except RuntimeError:
            policy["_refresh_slots"].release()
            return None
        refreshes[identity] = future
        count(policy, "prefetched", int(prefetch))

    def finished(_):
        with policy["_cache_lock"]:
            refreshes.pop(identity, None)
        policy["_refresh_slots"].release()

    future.add_done_callback(finished)
    return future


def upstream(query, policy, tcp=False):
    ident, name, qtype, finish = question(query)
    count(policy, "requests")
    if qtype == 28 and policy.get("block_aaaa", False):
        count(policy, "filtered_aaaa")
        return blocked_aaaa_response(query, ident, finish)
    # UDP fanout applies each waiter’s advertised limit after classification.
    full_response = tcp or policy.get("_classification_at_send", False)
    key = cache_key(query)
    cache = policy.get("_cache")
    epoch = None
    if cache is not None:
        with policy["_cache_lock"]:
            epoch = policy.get("_cache_epoch", 0)
            hit = cache.get(key)
            now = time.monotonic()
            if hit and now < hit[0] and not hit[3]:
                cache[key] = (*hit[:3], True)
        if hit:
            packet = client_response(query, hit[1], tcp=full_response)
            if now < hit[0]:
                if not policy.get("_classification_at_send"):
                    addresses, names = answer_addresses(packet, name)
                    populate_sets(policy, names, addresses)
                if time.monotonic() < hit[0]:
                    if hit[3] and hit[0] - now <= (hit[0] - hit[2]) / 10:
                        refresh_response(query, policy, tcp, key, epoch, prefetch=True)
                    count(policy, "cache_hits")
                    return CacheResponse(
                        age_response(packet, math.ceil(time.monotonic() - hit[2])), epoch
                    )
            if policy.get("_refresh_pool") is not None and 0 <= now - hit[0] <= STALE_GRACE:
                future = refresh_response(query, policy, tcp, key, epoch)
                if future:
                    try:
                        response, received = future.result(timeout=STALE_WAIT)
                    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                        pass
                    else:
                        with policy["_cache_lock"]:
                            current = epoch == policy["_cache_epoch"]
                        flags = struct.unpack_from("!H", response, 2)[0]
                        if current and not flags & 0x0200 and (flags & 15) in (0, 3):
                            packet = client_response(
                                query,
                                age_response(response, math.ceil(time.monotonic() - received)),
                                tcp=full_response,
                            )
                            return CacheResponse(packet, epoch)
                with policy["_cache_lock"]:
                    if (
                        epoch == policy.get("_cache_epoch", 0)
                        and time.monotonic() - hit[0] <= STALE_GRACE
                    ):
                        stale = bytearray(packet)
                        # A stale response must not assert current DNSSEC authentication.
                        stale[3] &= ~0x20
                        for pos, _ in ttl_fields(stale):
                            struct.pack_into("!I", stale, pos, STALE_TTL)
                        count(policy, "served_stale")
                        return CacheResponse(stale, epoch)
                    # This is a new upstream request if the old cache was cleared.
                    epoch = policy["_cache_epoch"]
    response, received, _, _ = fetch_response(query, policy, tcp)
    if cache is not None:
        remember_response(policy, key, epoch, response, received)
    return client_response(
        query, age_response(response, math.ceil(time.monotonic() - received)), tcp=full_response
    )


def ttl_records(packet):
    _, _, _, pos = question(packet, True)
    _, _, _, an, ns, ar = struct.unpack("!6H", packet[:12])
    for _ in range(an + ns + ar):
        owner, pos = read_name(packet, pos)
        if pos + 10 > len(packet):
            raise DNSFormatError("Truncated record")
        kind, klass, ttl, length = struct.unpack("!HHIH", packet[pos : pos + 10])
        if kind != 41:
            yield pos + 4, ttl, (owner, kind, klass)
        pos += 10 + length
        if pos > len(packet):
            raise DNSFormatError("Truncated record")


def limit_response_ttl(packet):
    records = list(ttl_records(packet))
    bounds = {}
    for _, ttl, key in records:
        bounds[key] = min(ttl, bounds.get(key, MAX_RESPONSE_TTL))
    result = bytearray(packet)
    for pos, _, key in records:
        struct.pack_into("!I", result, pos, bounds[key])
    return bytes(result)


def ttl_fields(packet):
    for pos, ttl, _ in ttl_records(packet):
        yield pos, ttl


def response_ttl(packet):
    return min((ttl for _, ttl in ttl_fields(packet)), default=0)


def age_response(packet, elapsed):
    result = bytearray(packet)
    for pos, ttl in ttl_fields(packet):
        struct.pack_into("!I", result, pos, max(0, ttl - max(0, elapsed)))
    return bytes(result)


def failure(query):
    try:
        ident, _, _, finish = question(query)
        flags = struct.unpack("!H", query[2:4])[0]
        return (
            struct.pack("!6H", ident, 0x8000 | (flags & 0x0100) | 0x0080 | 2, 1, 0, 0, 0)
            + query[12:finish]
        )
    except Exception:
        return None


def block_aaaa_for(policy, spec):
    value = policy.get("block_aaaa", spec.get("block_aaaa", True))
    if not isinstance(value, bool):
        raise ValueError("DNS AAAA policy must be boolean")
    return value


def run_dns(spec):
    from dns_runtime import DNSRuntime

    runtime = DNSRuntime(spec)
    upstream_tcp = UpstreamTCP()
    for policy in runtime.policies:
        policy["_upstream_tcp"] = upstream_tcp
    stop = threading.Event()
    refresh_pool = None
    if runtime.classifier is not None:
        refresh_pool = concurrent.futures.ThreadPoolExecutor(max_workers=REFRESH_WORKERS)
        refresh_slots = threading.BoundedSemaphore(REFRESH_WORKERS * 2)
        for policy in runtime.policies:
            policy.update(_refresh_pool=refresh_pool, _refresh_slots=refresh_slots, _refreshes={})
    sockets = []
    # A persistent TCP connection may wait for another frame for several
    # seconds. Keep those sessions out of the UDP worker pool.
    udp_pool = concurrent.futures.ThreadPoolExecutor(max_workers=UDP_WORKERS)
    tcp_pool = concurrent.futures.ThreadPoolExecutor(max_workers=TCP_MAX_CONNECTIONS)
    udp_slots = threading.BoundedSemaphore(UDP_MAX_PENDING)
    tcp_slots = threading.BoundedSemaphore(TCP_MAX_CONNECTIONS)

    def halt(*_):
        stop.set()

    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)

    def submit(pool, slots, fn, *args):
        if not slots.acquire(False):
            return False

        def task():
            try:
                fn(*args)
            finally:
                slots.release()

        pool.submit(task)
        return True

    def send_udp(listener, client, response):
        if response and not stop.is_set():
            try:
                listener.sendto(response, client)
            except OSError:
                pass

    def deliver(policy, query, response, send):
        if not isinstance(response, CacheResponse):
            send_classified(policy, query, response, send)
            return

        def send_current(packet):
            with policy["_cache_lock"]:
                send(packet if response.cache_epoch == policy["_cache_epoch"] else failure(query))

        send_classified(policy, query, response, send_current)

    from dns_classification import ClassificationBusy

    def send_classified(policy, query, response, send):
        if not response:
            return
        if runtime.classifier is None or response[3] & 15:
            send(response)
            return
        sending = False

        def send_classified():
            nonlocal sending
            sending = True
            return send(response)

        try:
            _, name, _, _ = question(query)
            addresses, names = answer_addresses(response, name)
            runtime.classifier.deliver(
                classification_entries(policy, names, addresses), send_classified
            )
        except ClassificationBusy:
            count(policy, "classification_busy")
            count(policy, "errors")
            send(failure(query))
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired):
            if sending:
                raise
            count(policy, "errors")
            send(failure(query))

    def refuse_udp(listener, query, client, policy, reason):
        count(policy, "overloaded")
        count(policy, reason)
        send_udp(listener, client, failure(query))

    def receive_udp(listener, policy):
        pending = {}
        pending_lock = threading.Lock()

        def resolve(key, query, queued_at):
            if stop.is_set():
                return
            expired = time.monotonic() - queued_at >= UDP_MAX_QUEUE_AGE
            failed = False
            try:
                response = failure(query) if expired else upstream(query, policy)
            except Exception:
                failed = True
                response = failure(query)
            with pending_lock:
                waiters = pending.pop(key)
            if expired:
                count(policy, "overloaded", len(waiters))
                count(policy, "udp_queue_expired", len(waiters))
            else:
                count(policy, "requests", len(waiters) - 1)
                count(policy, "coalesced", len(waiters) - 1)
                if failed:
                    count(policy, "errors", len(waiters))

            def send_all(packet):
                for waiter, client in waiters:
                    send_udp(listener, client, client_response(waiter, packet))

            deliver(policy, query, response, send_all)

        while not stop.is_set():
            try:
                query, client = listener.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                ident, _, qtype, finish = question(query)
            except DNSFormatError:
                count(policy, "errors")
                continue
            if qtype == 28 and policy["block_aaaa"]:
                count(policy, "requests")
                count(policy, "filtered_aaaa")
                send_udp(listener, client, blocked_aaaa_response(query, ident, finish))
                continue

            # Preserve wire matching for other queries. Unrecognized additional
            # records may carry EDNS options, so give those queries their own work.
            key = cache_key(query)
            if isinstance(key, bytes) and query[10:12] != b"\0\0":
                key = object()
            waiter = (query, client)
            with pending_lock:
                waiters = pending.get(key)
                if waiters is None:
                    pending[key] = [waiter]
                    if submit(udp_pool, udp_slots, resolve, key, query, time.monotonic()):
                        continue
                    pending.pop(key)
                    reason = "udp_queue_full"
                elif waiter in waiters:
                    count(policy, "requests")
                    count(policy, "coalesced")
                    continue
                elif len(waiters) < UDP_MAX_WAITERS:
                    waiters.append(waiter)
                    continue
                else:
                    reason = "udp_waiters_full"
            refuse_udp(listener, query, client, policy, reason)

    def tcp_client(conn, policy):
        with conn:
            conn.settimeout(7)
            # Bound queries on a single connection to prevent worker starvation.
            for _ in range(32):
                if stop.is_set():
                    return
                try:
                    size = struct.unpack("!H", _recv_exact(conn, 2))[0]
                    query = _recv_exact(conn, size)
                except Exception:
                    return
                try:
                    response = upstream(query, policy, True)
                except Exception:
                    count(policy, "errors")
                    response = failure(query)
                if not response:
                    return

                def send(packet):
                    conn.settimeout(0.25)
                    try:
                        conn.sendall(struct.pack("!H", len(packet)) + packet)
                    finally:
                        conn.settimeout(7)

                try:
                    deliver(policy, query, response, send)
                except OSError:
                    return

    def receive_tcp(listener, policy):
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
                if not submit(tcp_pool, tcp_slots, tcp_client, conn, policy):
                    count(policy, "overloaded")
                    conn.close()
            except socket.timeout:
                continue
            except OSError:
                return

    threads = []
    try:
        for policy in runtime.policies:
            port = policy["port"]
            for tcp in (False, True):
                listener = socket.socket(
                    socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
                )
                sockets.append(listener)
                if tcp:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.settimeout(0.5)
                listener.bind(("0.0.0.0", port))
                if tcp:
                    listener.listen(32)
                thread = threading.Thread(
                    target=receive_tcp if tcp else receive_udp, args=(listener, policy), daemon=True
                )
                thread.start()
                threads.append(thread)
        runtime.start()
        if runtime.classifier is not None:
            threading.Thread(target=runtime.classifier.run, args=(stop,), daemon=True).start()
        while not stop.wait(0.5):
            upstream_tcp.reap()
            runtime.publish()
            if any(not thread.is_alive() for thread in threads):
                raise RuntimeError("DNS listener stopped")
    finally:
        stop.set()
        runtime.stop()
        if refresh_pool:
            refresh_pool.shutdown(wait=True, cancel_futures=True)
        upstream_tcp.close()
        for listener in sockets:
            listener.close()
        for thread in threads:
            thread.join(timeout=1)
        udp_pool.shutdown(wait=True, cancel_futures=True)
        # Let accepted connections run their context manager to close the socket.
        tcp_pool.shutdown(wait=True)


class NativeDNS:
    """Process wrapper; spec path must already be mode 0600, not a CLI secret."""

    def __init__(self):
        self.child = None

    def start(self, spec_path):
        self.stop()
        path = Path(spec_path)
        if path.stat().st_mode & 0o077:
            raise ValueError("DNS specification must be private")
        parent = os.getpid()

        def die_with_parent():
            if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
                os._exit(1)

        self.child = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=die_with_parent,
        )

    def stop(self):
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()
        self.child = None

    def alive(self):
        return self.child is not None and self.child.poll() is None


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Invalid arguments")
        os.umask(0o077)
        with open(sys.argv[1]) as handle:
            run_dns(json.load(handle))
    except Exception:
        raise SystemExit(1)
