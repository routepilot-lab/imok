"""Write isolated caller-specific RPC input fixtures, never live paths."""

import hashlib
import json
import uuid
from pathlib import Path


def stage(base, payload=None, identity=None):
    base = Path(base)
    if payload is None:
        raw = base.read_text() if base.exists() else ""
        payload = {"token": raw} if base.name == "auth-input" else json.loads(raw)
        base.unlink(missing_ok=True)
    identity = identity or uuid.uuid4().hex
    text = json.dumps({"request_id": identity, "payload": payload})
    directory = base.parent / "requests"
    directory.mkdir(mode=0o700, exist_ok=True)
    file = directory / (base.name + "-" + identity + ".json")
    file.write_text(text)
    file.chmod(0o600)
    return identity + ":" + hashlib.sha256(text.encode()).hexdigest()
