"""Keep bounded connection and protection histories in the existing LuCI feed."""

import fcntl
import json
import time

from runtime import atomic

LIMIT = 40


def record(run, stream, event):
    if stream == "protection" and event["state"] == "off" and event["reason"] == "startup":
        return
    path = run / "connection-events.json"
    try:
        run.mkdir(parents=True, exist_ok=True)
        with (run / "connection-events.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                history = json.loads(path.read_text())
            except (FileNotFoundError, ValueError):
                history = {}
            if not isinstance(history, dict):
                history = {}
            rows = history.get(stream)
            if not isinstance(rows, list):
                rows = []
            if stream == "protection":
                if (
                    rows
                    and isinstance(rows[-1], dict)
                    and all(rows[-1].get(key) == value for key, value in event.items())
                ):
                    return
            history[stream] = (rows + [dict(event, time=int(time.time()))])[-LIMIT:]
            atomic(path, json.dumps(history), 0o644)
    except OSError:
        pass  # History is diagnostic and must never interrupt the firewall.
