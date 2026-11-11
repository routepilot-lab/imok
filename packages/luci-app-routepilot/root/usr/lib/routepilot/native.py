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


class DNSFormatError(ValueError):
    pass


def read_name(packet, offset):
    labels, seen, end, wire_size = [], set(), None, 1
    for _ in range(128):
        if offset >= len(packet) or offset in seen:
            raise DNSFormatError('Invalid DNS name')
        seen.add(offset)
        size = packet[offset]
        if size & 0xc0 == 0xc0:
            if offset + 1 >= len(packet):
                raise DNSFormatError('Invalid DNS pointer')
            target = ((size & 0x3f) << 8) | packet[offset + 1]
            if target >= offset:
                raise DNSFormatError('Invalid DNS pointer')
            if end is None:
                end = offset + 2
            offset = target
            continue
        if size & 0xc0 or size > 63:
            raise DNSFormatError('Invalid DNS label')
        offset += 1
        if not size:
            name = '.'.join(labels).lower()
            return name, end if end is not None else offset
        if offset + size > len(packet):
            raise DNSFormatError('Truncated DNS label')
        wire_size += size + 1
        if wire_size > 255:
            raise DNSFormatError('DNS name too long')
        # RFC 2181 section 11 permits arbitrary label octets, not only hostnames.
        # Escape special octets for internal comparisons; literal dots/backslashes
        # must never become suffix separators or impersonate configured domains.
        raw = packet[offset:offset + size]
        if re.fullmatch(rb'[A-Za-z0-9_*-]+', raw):
            label = raw.decode('ascii').lower()
        else:
            label = ''.join(chr(value).lower() if (65 <= value <= 90 or
                            97 <= value <= 122 or 48 <= value <= 57 or
                            value in (45, 95, 42)) else '\\%03d' % value
                            for value in raw)
        labels.append(label)
        offset += size
    raise DNSFormatError('DNS pointer depth exceeded')


def question(packet, response=False):
    if len(packet) < 12:
        raise DNSFormatError('Truncated DNS header')
    ident, flags, count, _, _, _ = struct.unpack('!6H', packet[:12])
    if count != 1 or bool(flags & 0x8000) != response or flags & 0x7800:
        raise DNSFormatError('Unsupported DNS query')
    name, pos = read_name(packet, 12)
    if pos + 4 > len(packet):
        raise DNSFormatError('Truncated DNS question')
    qtype, qclass = struct.unpack('!HH', packet[pos:pos + 4])
    if qclass != 1:
        raise DNSFormatError('Unsupported DNS class')
    return ident, name, qtype, pos + 4


def answer_addresses(packet, expected_name=None):
    """Return reachable answer A/AAAA records and query/CNAME names.

    Additional-section addresses are deliberately excluded: they do not prove
    that the query should route to these addresses.
    """
    _, name, _, offset = question(packet, True)
    if expected_name is not None and name != expected_name:
        raise DNSFormatError('Mismatched DNS question')
    _, flags, _, answers, authority, additional = struct.unpack('!6H', packet[:12])
    if flags & 0x000f or flags & 0x0200:
        return [], {name}
    records, aliases = [], {}
    for index in range(answers + authority + additional):
        owner, offset = read_name(packet, offset)
        if offset + 10 > len(packet):
            raise DNSFormatError('Truncated DNS record')
        kind, klass, ttl, size = struct.unpack('!HHIH', packet[offset:offset + 10])
        offset += 10
        finish = offset + size
        if finish > len(packet):
            raise DNSFormatError('Truncated DNS data')
        if index < answers and klass == 1:
            if kind == 5:
                alias, consumed = read_name(packet, offset)
                if consumed != finish:
                    raise DNSFormatError('Invalid CNAME data')
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
            raise DNSFormatError('CNAME loop')
        names.add(current)
    else:
        raise DNSFormatError('CNAME depth exceeded')
    return [addr for owner, addr in records if owner in names], names


def suffix_match(names, domains):
    domains = domains if isinstance(domains, (set, frozenset)) else set(domains)
    return any('full:' + name in domains for name in names) or any('.'.join(name.split('.')[index:]) in domains
               for name in names for index in range(len(name.split('.'))))


def matching_sets(policy, names):
    """Both custom categories may match; nft precedence remains direct first."""
    result = []
    if suffix_match(names, policy.get('custom_direct_domains', ())):
        result.append(policy['direct_set'])
    if suffix_match(names, policy.get('custom_proxy_domains', ())):
        result.append(policy['proxy_set'])
    if policy.get('mode') in ('gfw', 'whitelist') and suffix_match(names, policy.get('mode_domains', ())):
        result.append(policy['mode_set'])
    return result


def populate_sets(policy, names, addresses):
    sets = matching_sets(policy, names)
    ipv4 = sorted({str(ipaddress.IPv4Address(addr)) for addr in addresses if ':' not in addr})
    if not sets or not ipv4:
        return
    lines = []
    for set_name in sets:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', set_name):
            raise ValueError('Invalid set identifier')
        # nft's userspace grammar has no "update element" command. Ensure
        # existence, then delete/re-add within one transaction to refresh expiry.
        elements = ', '.join(ipv4)
        lines.append('add element inet routepilot %s { %s }' % (set_name, elements))
        lines.append('delete element inet routepilot %s { %s }' % (set_name, elements))
        lines.append('add element inet routepilot %s { %s }' %
                     (set_name, ', '.join(addr + ' timeout 1h' for addr in ipv4)))
    result = subprocess.run(['nft', '-f', '-'], input='\n'.join(lines) + '\n',
                            text=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=5)
    if result.returncode:
        raise RuntimeError('DNS routing classification unavailable')


def _recv_exact(conn, size):
    result = b''
    while len(result) < size:
        part = conn.recv(size - len(result))
        if not part:
            raise DNSFormatError('Truncated DNS stream')
        result += part
    return result


def dns_route(name, policy):
    domestic = suffix_match({name}, policy.get('custom_direct_domains', ())) or (
        not suffix_match({name}, policy.get('custom_proxy_domains', ())) and suffix_match({name}, policy.get('china_domains', ())))
    server = str(ipaddress.IPv4Address(policy.get('dns_domestic', '223.5.5.5') if domestic else policy['dns_foreign']))
    mark = policy['mark']
    mark = int(mark, 0) if isinstance(mark, str) else int(mark)
    return server, 0 if domestic else mark, 'domestic' if domestic else 'proxy'


def count(policy, key):
    if '_stats_lock' in policy:
        with policy['_stats_lock']:
            policy['_stats'][key] = policy['_stats'].get(key, 0) + 1


def backup_doh(query, port=BACKUP_SOCKS_PORT):
    """Fixed, certificate-verified DoH through the backup, never host DNS.

    curl provides a whole-request deadline; existing DNS concurrency/cache bounds
    also apply here. No environment bypass, configurable URL, log or query file.
    """
    if type(port) is not int or not 1024 <= port <= 65535:
        raise DNSFormatError('Invalid local encrypted DNS port')
    result = subprocess.run(['curl', '-q', '-4', '-fsS', '--noproxy', '',
        '--proxy', 'socks5h://127.0.0.1:' + str(port), '--proto', '=https',
        '--connect-timeout', '3', '--max-time', '6', '--max-filesize', '65535',
        '-H', 'Content-Type: application/dns-message', '-H', 'Accept: application/dns-message',
        '--data-binary', '@-', 'https://dns.google/dns-query'], input=query,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=7)
    if result.returncode or not 12 <= len(result.stdout) <= 65535:
        raise DNSFormatError('Backup encrypted DNS unavailable')
    return result.stdout


def upstream(query, policy, tcp=False):
    ident, name, qtype, finish = question(query)
    count(policy, 'requests')
    if qtype == 28 and policy.get('block_aaaa', False):
        count(policy, 'filtered_aaaa')
        flags = 0x8080 | (struct.unpack('!H', query[2:4])[0] & 0x0100)
        return struct.pack('!6H', ident, flags, 1, 0, 0, 0) + query[12:finish]
    key = query[2:]
    cache = policy.get('_cache')
    epoch = None
    if cache is not None:
        with policy['_cache_lock']:
            epoch = policy.get('_cache_epoch', 0)
            hit = cache.get(key)
            if hit and hit[0] <= time.monotonic():
                cache.pop(key, None)
                hit = None
        if hit:
            packet = query[:2] + hit[1][2:]
            addresses, names = answer_addresses(packet, name)
            populate_sets(policy, names, addresses)
            # Classification may take time. Never renew or return an expired hit.
            if time.monotonic() < hit[0]:
                count(policy, 'cache_hits')
                return age_response(packet, math.ceil(time.monotonic() - hit[2]))
    server, mark, _ = dns_route(name, policy)
    tcp = tcp or policy.get('transport') == 'tcp'
    count(policy, 'upstream_queries')
    if mark and policy.get('proxy_dns_override') == 'https://dns.google/dns-query':
        response = backup_doh(query)
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
            sock.settimeout(6)
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, 'SO_MARK', 36), mark)
            sock.connect((server, 53))
            if tcp:
                sock.sendall(struct.pack('!H', len(query)) + query)
                size = struct.unpack('!H', _recv_exact(sock, 2))[0]
                response = _recv_exact(sock, size)
            else:
                sock.send(query)
                response = sock.recv(65535)
    received = time.monotonic()
    rid, rname, rtype, _ = question(response, True)
    if (ident, name, qtype) != (rid, rname, rtype):
        raise DNSFormatError('Mismatched DNS response')
    response = limit_response_ttl(response)
    addresses, names = answer_addresses(response, name)
    populate_sets(policy, names, addresses)
    ttl = response_ttl(response)
    if cache is not None and ttl > 0 and received + ttl > time.monotonic() and not (struct.unpack('!H', response[2:4])[0] & 0x020f):
        with policy['_cache_lock']:
            if epoch == policy.get('_cache_epoch', 0):
                # Remove expired entries before bounded eviction; no full-cache flush.
                now = time.monotonic()
                if len(cache) >= 1024:
                    for old in [k for k, v in cache.items() if v[0] <= now]:
                        cache.pop(old, None)
                    if len(cache) >= 1024:
                        cache.pop(min(cache, key=lambda k: cache[k][0]))
                cache[key] = (received + ttl, response, received)
    return age_response(response, math.ceil(time.monotonic() - received))


def ttl_records(packet):
    _, _, _, pos = question(packet, True)
    _, _, _, an, ns, ar = struct.unpack('!6H', packet[:12])
    for _ in range(an + ns + ar):
        owner, pos = read_name(packet, pos)
        if pos + 10 > len(packet):
            raise DNSFormatError('Truncated record')
        kind, klass, ttl, length = struct.unpack('!HHIH', packet[pos:pos + 10])
        if kind != 41:
            yield pos + 4, ttl, (owner, kind, klass)
        pos += 10 + length
        if pos > len(packet):
            raise DNSFormatError('Truncated record')


def limit_response_ttl(packet):
    records = list(ttl_records(packet))
    bounds = {}
    for _, ttl, key in records:
        bounds[key] = min(ttl, bounds.get(key, MAX_RESPONSE_TTL))
    result = bytearray(packet)
    for pos, _, key in records:
        struct.pack_into('!I', result, pos, bounds[key])
    return bytes(result)


def ttl_fields(packet):
    for pos, ttl, _ in ttl_records(packet):
        yield pos, ttl


def response_ttl(packet):
    return min((ttl for _, ttl in ttl_fields(packet)), default=0)


def age_response(packet, elapsed):
    result = bytearray(packet)
    for pos, ttl in ttl_fields(packet):
        struct.pack_into('!I', result, pos, max(0, ttl - max(0, elapsed)))
    return bytes(result)


def failure(query):
    try:
        ident, _, _, finish = question(query)
        flags = struct.unpack('!H', query[2:4])[0]
        return struct.pack('!6H', ident, 0x8000 | (flags & 0x0100) | 0x0080 | 2, 1, 0, 0, 0) + query[12:finish]
    except Exception:
        return None


def block_aaaa_for(policy, spec):
    value = policy.get('block_aaaa', spec.get('block_aaaa', True))
    if not isinstance(value, bool):
        raise ValueError('DNS AAAA policy must be boolean')
    return value


def run_dns(spec):
    from dns_runtime import DNSRuntime
    runtime = DNSRuntime(spec)
    stop = threading.Event()
    sockets = []
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=16)
    slots = threading.BoundedSemaphore(64)
    def halt(*_):
        stop.set()
    signal.signal(signal.SIGTERM, halt)
    signal.signal(signal.SIGINT, halt)
    def submit(fn, *args):
        if not slots.acquire(False):
            return False
        def task():
            try:
                fn(*args)
            finally:
                slots.release()
        pool.submit(task)
        return True
    def udp_client(listener, query, client, policy):
        try:
            response = upstream(query, policy)
        except Exception:
            count(policy, 'errors')
            response = failure(query)
        if response and not stop.is_set():
            try:
                listener.sendto(response, client)
            except OSError:
                pass
    def tcp_client(conn, policy):
        with conn:
            conn.settimeout(7)
            # Bound queries on a single connection to prevent worker starvation.
            for _ in range(32):
                if stop.is_set():
                    return
                try:
                    size = struct.unpack('!H', _recv_exact(conn, 2))[0]
                    query = _recv_exact(conn, size)
                except Exception:
                    return
                try:
                    response = upstream(query, policy, True)
                except Exception:
                    count(policy, 'errors')
                    response = failure(query)
                if not response:
                    return
                try:
                    conn.sendall(struct.pack('!H', len(response)) + response)
                except OSError:
                    return
    def receive(listener, policy, tcp):
        while not stop.is_set():
            try:
                if tcp:
                    conn, _ = listener.accept()
                    if not submit(tcp_client, conn, policy):
                        count(policy, 'overloaded')
                        conn.close()
                else:
                    query, client = listener.recvfrom(65535)
                    if not submit(udp_client, listener, query, client, policy):
                        count(policy, 'overloaded')
            except socket.timeout:
                continue
            except OSError:
                return
    threads = []
    try:
        for policy in runtime.policies:
            port = policy['port']
            for tcp in (False, True):
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM)
                sockets.append(listener)
                if tcp:
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.settimeout(0.5)
                listener.bind(('0.0.0.0', port))
                if tcp:
                    listener.listen(32)
                thread = threading.Thread(target=receive, args=(listener, policy, tcp), daemon=True)
                thread.start()
                threads.append(thread)
        runtime.start()
        while not stop.wait(0.5):
            runtime.publish()
            if any(not thread.is_alive() for thread in threads):
                raise RuntimeError('DNS listener stopped')
    finally:
        stop.set()
        runtime.stop()
        for listener in sockets:
            listener.close()
        for thread in threads:
            thread.join(timeout=1)
        pool.shutdown(wait=True, cancel_futures=True)


class NativeDNS:
    """Process wrapper; spec path must already be mode 0600, not a CLI secret."""
    def __init__(self):
        self.child = None

    def start(self, spec_path):
        self.stop()
        path = Path(spec_path)
        if path.stat().st_mode & 0o077:
            raise ValueError('DNS specification must be private')
        parent = os.getpid()
        def die_with_parent():
            if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
                os._exit(1)
        self.child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(path)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, preexec_fn=die_with_parent)

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


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('Invalid arguments')
        os.umask(0o077)
        with open(sys.argv[1]) as handle:
            run_dns(json.load(handle))
    except Exception:
        raise SystemExit(1)
