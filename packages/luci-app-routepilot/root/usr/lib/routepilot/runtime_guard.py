"""Coordinate runtime replacement with the existing namespace lifecycle."""
from contextlib import contextmanager
from functools import wraps
import fcntl
import os
from pathlib import Path


def open_lock(run, kind):
    Path(run).mkdir(parents=True, exist_ok=True)
    fd = os.open(Path(run) / '.runtime-access.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, kind | fcntl.LOCK_NB)
    except Exception:
        os.close(fd)
        raise RuntimeError('WARP 环境正在使用或维护，请稍后重试') from None
    return fd


def release_plane(plane):
    fd = getattr(plane, '_runtime_lease', None)
    if fd is not None:
        os.close(fd)
        plane._runtime_lease = None


def guarded_start(start):
    @wraps(start)
    def wrapped(self, *args, **kwargs):
        if getattr(self, '_runtime_lease', None) is None:
            run, root = start.__globals__['RUN'], start.__globals__['ROOT']
            self._runtime_lease = open_lock(run, fcntl.LOCK_SH)
            if (root.parent / '.runtime-transaction.json').exists() or (root.parent / '.runtime-transaction.json').is_symlink():
                release_plane(self)
                raise RuntimeError('WARP 上次维护尚未完成，请到“WARP 安装与更新”核对恢复状态')
        try:
            return start(self, *args, **kwargs)
        except Exception:
            release_plane(self)
            raise
    return wrapped


@contextmanager
def maintenance(run, base):
    fd = open_lock(run, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)
