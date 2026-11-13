"""Persist rule update attempts and bounded retries independently of runtime files."""

import json
import fcntl
from contextlib import contextmanager
import time
from runtime import RULES, RUN, atomic

RETRIES = (300, 1800, 7200)


def hours(main):
    try:
        return max(1, min(720, int(main.get("update_hours", "168"))))
    except (ValueError, TypeError):
        return 168


def read(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Schedule:
    def __init__(self, rules=None, run=None):
        self.rules = rules if rules is not None else RULES
        self.run = run if run is not None else RUN
        self.path = self.rules / "schedule.json"
        self.data = read(self.path)

    def save(self):
        atomic(self.path, json.dumps(self.data, ensure_ascii=False))

    @contextmanager
    def edit(self):
        self.rules.mkdir(parents=True, exist_ok=True)
        with (self.rules / ".schedule.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.data = read(self.path)
            yield
            self.save()

    def resolve(self, now=None):
        """An explicit publication resolves a decline without inventing a download success."""
        with self.edit():
            self.data.update(
                blocked=False,
                failures=0,
                running=False,
                request_id="",
                last_resolution_at=time.time() if now is None else now,
            )

    def metadata(self):
        path = self.rules / "current"
        return read((path if path.is_symlink() else self.rules) / "updated.json")

    def reconcile(self, main, now=None):
        """Recover an interrupted worker once, after the supervisor owns its lock."""
        now = time.time() if now is None else now
        if not self.data.get("running"):
            return
        progress = read(self.run / "update.json")
        meta = self.metadata()
        job = self.data.get("request_id")
        if progress.get("request_id") == job and progress.get("state") in (
            "success",
            "failed",
            "blocked",
        ):
            self.finish(
                main,
                progress["state"] == "success",
                progress["state"] == "blocked",
                now,
                request_id=job,
            )
        elif meta.get("request_id") == job:
            self.finish(main, True, now=now, request_id=job)
        else:
            self.finish(main, False, now=now, request_id=job)

    def plan(self, main, now=None):
        now = time.time() if now is None else now
        interval = hours(main) * 3600
        self.data = read(self.path)
        data = self.data
        last = data.get("last_attempt_at", 0)
        success = data.get("last_success_at", 0)
        if not success:
            meta = self.metadata()
            success = meta.get("downloaded_at", meta.get("time", 0))
        failures = data.get("failures", 0)
        base = max(last, success, data.get("last_resolution_at", 0))
        retry = bool(failures and failures <= len(RETRIES) and not data.get("blocked"))
        if retry:
            due = last + RETRIES[failures - 1]
        elif data.get("blocked"):
            due = 0
        else:
            due = base + interval if base else now
        # Wall clock moved backwards: avoid postponing retries by an arbitrary future date.
        if last > now + interval or success > now + interval:
            due = now
        enabled = main.get("enabled") == "1"
        state = (
            "paused"
            if not enabled
            else "running"
            if data.get("running")
            else "awaiting_confirmation"
            if data.get("blocked")
            else "retry"
            if retry
            else "scheduled"
        )
        messages = {
            "paused": "主线路未开启，自动更新暂停；开启并联网后检查计划",
            "running": "规则正在后台更新",
            "awaiting_confirmation": "规则数量异常下降，保留原版本，等待确认候选",
            "retry": "上次更新失败，将按退避计划重试",
            "scheduled": "按已保存间隔检查更新",
        }
        return dict(
            hours=hours(main),
            last_attempt_at=last,
            last_success_at=success,
            next_due_at=int(due),
            retry_count=min(failures, len(RETRIES)),
            retry_limit=len(RETRIES),
            retry_pending=retry,
            enabled=enabled,
            state=state,
            message=messages[state],
            retries_exhausted=failures > len(RETRIES),
        )

    def due(self, main, now=None):
        now = time.time() if now is None else now
        plan = self.plan(main, now)
        nonce = main.get("update_nonce", "")
        manual = bool(nonce and nonce != self.data.get("last_nonce"))
        return not self.data.get("running") and (
            manual or bool(plan["next_due_at"] and now >= plan["next_due_at"])
        )

    def start(self, main, job, now=None):
        now = time.time() if now is None else now
        with self.edit():
            self.data.update(
                last_attempt_at=now,
                last_nonce=main.get("update_nonce", ""),
                request_id=job,
                running=True,
                blocked=False,
            )

    def finish(self, main, success, blocked=False, now=None, request_id=None):
        now = time.time() if now is None else now
        with self.edit():
            if request_id is not None and (
                self.data.get("request_id") != request_id or not self.data.get("running")
            ):
                return False
            self.data.update(running=False, blocked=blocked)
            if success:
                self.data.update(last_success_at=now, failures=0)
            elif not blocked:
                self.data["failures"] = self.data.get("failures", 0) + 1
        return True


def summary(main, run=None, now=None):
    return Schedule(run=run).plan(main, now)
