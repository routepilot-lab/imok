"""Group DNS classification writes and coordinate them with table replacement."""

from collections import OrderedDict
import subprocess
import threading
import time

ELEMENT_LIFETIME = 3600
REFRESH_INTERVAL = 0.03
BATCH_WAIT = 0.008
REPLY_WAIT = 1.0
MAX_TRACKED = 16384


class ClassificationBusy(TimeoutError):
    """The response could not be classified within its wait budget."""


class Batch:
    def __init__(self, epoch):
        self.epoch = epoch
        self.ready_at = time.monotonic() + BATCH_WAIT
        self.entries = set()
        self.done = False
        self.error = None


class ClassificationWriter:
    def __init__(self, epoch, write, clock=time.monotonic):
        self.epoch = epoch
        self.write = write
        self.clock = clock
        self.lock = threading.Condition()
        self.paused = False
        self.written = OrderedDict()
        self.pending = set()
        self.batch = None
        self.writing = False
        self.sending = 0

    def pause(self, epoch):
        with self.lock:
            if epoch != self.epoch:
                raise ValueError("DNS table changed")
            self.paused = True
            self.pending.clear()
            self.batch = None
            if not self.lock.wait_for(lambda: not (self.writing or self.sending), timeout=0.75):
                raise TimeoutError("DNS classification write or response did not finish")

    def resume(self, old_epoch, epoch):
        with self.lock:
            if (
                not self.paused
                or self.writing
                or self.sending
                or old_epoch != self.epoch
                or epoch == old_epoch
            ):
                raise ValueError("Unexpected DNS table epoch")
            self.epoch = epoch
            self.written.clear()
            self.pending.clear()
            self.paused = False
            self.lock.notify_all()

    def _commit(self, entries, batch=None, *, timeout=0.65):
        started = self.clock()
        groups = {}
        for name, ip in sorted(entries):
            groups.setdefault(name, []).append(ip)
        error = None
        try:
            self.write(groups, timeout=timeout)
        except subprocess.TimeoutExpired as failure:
            error = ClassificationBusy("DNS classification did not finish") if batch else failure
        except Exception as failure:
            error = failure
        finally:
            with self.lock:
                if error:
                    self.written.clear()
                    self.pending.clear()
                else:
                    self.pending.difference_update(entries)
                    for item in entries:
                        self.written[item] = started + ELEMENT_LIFETIME
                        self.written.move_to_end(item)
                    while len(self.written) > MAX_TRACKED:
                        evicted, _ = self.written.popitem(last=False)
                        self.pending.discard(evicted)
                if batch is not None:
                    batch.done = True
                    batch.error = error
                self.writing = False
                self.lock.notify_all()
        if error:
            raise error

    def deliver(self, entries, send):
        deadline = time.monotonic() + REPLY_WAIT
        joined = None
        while True:
            with self.lock:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ClassificationBusy("DNS classification did not finish")
                if self.paused:
                    self.lock.wait(remaining)
                    continue
                if joined and joined.epoch == self.epoch and joined.done and joined.error:
                    raise joined.error
                now = self.clock()
                missing = set()
                for item in entries:
                    lifetime = self.written.get(item, 0) - now
                    if lifetime <= 1:
                        missing.add(item)
                    elif lifetime <= ELEMENT_LIFETIME / 2:
                        self.pending.add(item)
                if not missing and (joined is None or self.batch is not joined):
                    # Reserve the response while locked. Pause drains active sends
                    # before rebuilding the table, even though socket I/O is outside the lock.
                    self.sending += 1
                    break
                if joined is None or joined.epoch != self.epoch or joined.done:
                    if self.batch is None:
                        self.batch = Batch(self.epoch)
                    joined = self.batch
                    joined.entries.update(missing)
                if self.batch is not joined or self.writing:
                    self.lock.wait(remaining)
                    continue
                gather = joined.ready_at - time.monotonic()
                if gather > 0:
                    self.lock.wait(min(gather, remaining))
                    continue
                self.batch = None
                self.writing = True
            self._commit(joined.entries, joined, timeout=max(0, deadline - time.monotonic()))

        try:
            return send()
        finally:
            with self.lock:
                self.sending -= 1
                self.lock.notify_all()

    def flush(self):
        with self.lock:
            if self.paused or self.writing or self.batch or not self.pending:
                return
            entries = set(self.pending)
            self.writing = True
        self._commit(entries)

    def queue_refresh(self, epoch, entries):
        with self.lock:
            if not self.paused and epoch == self.epoch:
                now = self.clock()
                for item in entries:
                    if len(self.pending) >= MAX_TRACKED:
                        break
                    if self.written.get(item, 0) - now <= ELEMENT_LIFETIME / 2:
                        self.pending.add(item)

    def run(self, stop):
        while not stop.wait(REFRESH_INTERVAL):
            try:
                self.flush()
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                # The next query has to classify synchronously after a failed batch.
                pass
