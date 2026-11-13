"""Read the official client's account network directory without changing selection."""

import os
import signal
import subprocess
import time

from warp_cli import parse_directory


def parse_networks(text):
    return parse_directory(text).networks


def read_networks():
    # Separate read-only process: no registration, UCI, connect, or selected-vnet writes.
    from mplane import NS, ROOT

    args = [
        "ip",
        "netns",
        "exec",
        NS,
        "chroot",
        str(ROOT),
        "/usr/bin/warp-cli",
        "--accept-tos",
        "vnet",
        "--no-paginate",
    ]
    child = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    try:
        text, _ = child.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.communicate()
        raise RuntimeError("读取出口目录超时，请稍后刷新") from None
    if child.returncode:
        raise RuntimeError("未能读取出口目录，请检查登录环境")
    return dict(available=True, networks=parse_networks(text), time=int(time.time()))
