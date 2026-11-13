"""Bind shared LuCI staging files to their caller; never delete another request."""

import json
import hashlib
import os
import re
import stat
import time
import uuid
import fcntl
from contextlib import contextmanager
from pathlib import Path
from public_errors import ControlError


def read_request(path, request_id, limit):
    if not isinstance(request_id, str) or not re.fullmatch("[a-f0-9]{32}:[a-f0-9]{64}", request_id):
        raise ControlError("请求标识无效，请刷新页面后重新提交")
    request_id, digest = request_id.split(":")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise ControlError("请求输入不可用，请重新提交") from None
    with os.fdopen(fd) as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > limit:
            raise ControlError("请求输入大小、类型或属主无效")
        raw = handle.read(limit + 1)
    if len(raw.encode()) > limit:
        raise ControlError("请求输入过大")
    if hashlib.sha256(raw.encode()).hexdigest() != digest:
        raise ControlError("提交内容已变化，本次请求未执行；请重新提交")
    try:
        document = json.loads(raw)
    except (ValueError, TypeError):
        raise ControlError("请求输入不完整，请重新提交") from None
    if (
        not isinstance(document, dict)
        or set(document) != {"request_id", "payload"}
        or document.get("request_id") != request_id
    ):
        raise ControlError("另一个页面已更新提交内容，本次请求未执行；请重新提交")
    # Parse once, then use the immutable snapshot. Deliberately do not unlink:
    # another fs.write can arrive at any time, even after a cleanup hash check.
    # Only the latest bounded 0600 payload is retained in volatile /var/run.
    return document["payload"]


def prepare_requests(run):
    """Only the public slots are RPC writable; claimed inputs are private."""
    for name in ("requests", "claimed"):
        directory = Path(run) / name
        directory.mkdir(mode=0o700, exist_ok=True)
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
            raise ControlError("请求目录无效")
        directory.chmod(0o700)


def reap_requests(run, now=None):
    """Expire abandoned credentials; never touch a live claimed operation."""
    now = time.time() if now is None else now
    prepare_requests(run)
    directory = Path(run) / "requests"
    for path in directory.iterdir():
        if not re.fullmatch(
            r"(auth-input|backup-input|cloud-input|relay-input|organization-input|rules-input|client-input|runtime-input)-[a-f0-9]{32}\.json",
            path.name,
        ):
            continue
        try:
            age = now - path.lstat().st_mtime
            if age > 300 or age < -5:
                path.unlink()
        except FileNotFoundError:
            pass
    # Claimed files can survive SIGKILL. An operation holds this lock while live;
    # only expired, unlocked crash remnants may be removed.
    for path in (Path(run) / "claimed").iterdir():
        try:
            age = now - path.lstat().st_mtime
            if not re.fullmatch("[a-f0-9]{32}", path.name) or -5 <= age <= 900:
                continue
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                path.unlink(missing_ok=True)
            finally:
                os.close(fd)
        except (FileNotFoundError, BlockingIOError):
            pass


@contextmanager
def claim_request(base, request_id, limit):
    """Atomically own a caller-specific slot before parsing or deleting it.

    New writes, including a recreation of the same slot, cannot be removed by
    this operation's cleanup. Never implement hash-check followed by unlink.
    """
    if not isinstance(request_id, str) or not re.fullmatch("[a-f0-9]{32}:[a-f0-9]{64}", request_id):
        raise ControlError("请求标识无效，请刷新页面后重新提交")
    base = Path(base)
    prepare_requests(base.parent)
    slot = base.parent / "requests" / (base.name + "-" + request_id.split(":")[0] + ".json")
    claimed = base.parent / "claimed" / uuid.uuid4().hex
    try:
        os.rename(slot, claimed)
    except OSError:
        raise ControlError("请求已处理或不可用，请重新提交") from None
    lock_fd = None
    try:
        st = claimed.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.geteuid():
            raise ControlError("请求输入类型、权限或属主无效")
        if not -5 <= time.time() - st.st_mtime <= 300:
            raise ControlError("请求已过期，请重新提交")
        lock_fd = os.open(claimed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield read_request(claimed, request_id, limit)
    finally:
        claimed.unlink(missing_ok=True)
        if lock_fd is not None:
            os.close(lock_fd)
