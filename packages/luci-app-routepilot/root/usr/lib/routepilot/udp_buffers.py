"""Tune only the dedicated client's connected Cloudflare UDP sockets.

pidfd duplicates refer to the same socket, so reconnects and global sysctl
changes are unnecessary. Failure leaves the existing tunnel working.
"""

import ctypes
import ipaddress
import os
from pathlib import Path
import socket

BUFFER_BYTES = 4 * 1024 * 1024
INGRESS = tuple(
    ipaddress.ip_network(n) for n in ("162.159.192.0/24", "162.159.193.0/24", "162.159.197.0/24")
)


def tune(pid, target=BUFFER_BYTES):
    if target < 212992 or target > 8 * 1024 * 1024:
        raise ValueError("Invalid UDP buffer target")
    libc = ctypes.CDLL(None, use_errno=True)
    pidfd = libc.syscall(434, int(pid), 0)  # pidfd_open on Linux arm64/x86_64
    if pidfd < 0:
        return []
    changed = []
    try:
        for path in Path("/proc/%d/fd" % pid).iterdir():
            try:
                if not os.readlink(path).startswith("socket:"):
                    continue
                duplicate = libc.syscall(438, pidfd, int(path.name), 0)  # pidfd_getfd
                if duplicate < 0:
                    continue
                with socket.socket(fileno=duplicate) as sock:
                    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_DGRAM:
                        continue
                    peer = ipaddress.ip_address(sock.getpeername()[0])
                    if not any(peer in network for network in INGRESS):
                        continue
                    before = [
                        sock.getsockopt(socket.SOL_SOCKET, opt)
                        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF)
                    ]
                    if all(value >= target for value in before):
                        continue
                    # Linux reports twice the requested size. FORCE uses our existing
                    # CAP_NET_ADMIN without increasing host defaults or limits.
                    sock.setsockopt(
                        socket.SOL_SOCKET, 33, max(before[1], target) // 2
                    )  # SO_SNDBUFFORCE
                    sock.setsockopt(
                        socket.SOL_SOCKET, 32, max(before[0], target) // 2
                    )  # SO_RCVBUFFORCE
                    after = [
                        sock.getsockopt(socket.SOL_SOCKET, opt)
                        for opt in (socket.SO_RCVBUF, socket.SO_SNDBUF)
                    ]
                    changed.append({"fd": int(path.name), "before": before, "after": after})
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    finally:
        os.close(pidfd)
    return changed
