"""Supervisor-owned exit and protocol comparisons with bounded measurements."""

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import speed_test
from masque_config import resolve_exit
from public_errors import ControlError
from runtime import RUN, RULES, atomic, read_config
from warp_cli import PROTOCOLS, TunnelStatus, parse_selected_network

REQUEST = RUN / "speed-compare-pending.json"
CANCEL = RUN / "speed-compare-cancel"
RESULT = RUN / "speed-compare.json"
MAX_TARGETS = 16
TARGET_SECONDS = 60
BATCH_SECONDS = 300
TERMINAL = {"done", "cancelled", "config_changed", "timeout", "error", "interrupted"}
TARGET_ERRORS = (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired)


class Interrupted(Exception):
    def __init__(self, state):
        self.state = state


class TargetUnconfirmed(ControlError):
    """A comparison target failed a known check before measurement."""


def load(path):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save(value):
    atomic(RESULT, json.dumps(value, ensure_ascii=False), 0o644)


def config_key(cfg):
    # Include all persistent settings, without exposing them in request/result files.
    raw = dict(cfg)
    raw.pop("rule_overrides", None)
    raw["main"] = {k: v for k, v in cfg["main"].items() if k != "lan_networks"}
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()


@contextmanager
def locks():
    from endpoint_control import locked
    from rules_catalog import operation_lock

    with ExitStack() as stack:
        handle = stack.enter_context(speed_test.LOCK.open("a"))
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stack.enter_context(locked())
        stack.enter_context(operation_lock(RULES))
        yield


def start(request_id):
    from endpoint_control import TRANSACTION, PROCESS_BLOCK
    from request_input import claim_request

    with claim_request(RUN / "speed-compare-input", request_id, 2048) as raw:
        if not isinstance(raw, dict) or not {"targets"} <= set(raw) <= {"kind", "targets"}:
            raise ControlError("线路对比请求无效")
        kind = raw.get("kind", "exit")
        targets = raw["targets"]
        if not isinstance(targets, list) or not all(isinstance(target, str) for target in targets):
            raise ControlError("目标列表无效")
        if kind == "exit":
            if not 1 <= len(targets) <= MAX_TARGETS:
                raise ControlError("请选择 1–16 个出口")
            try:
                targets = list(dict.fromkeys(str(uuid.UUID(target)) for target in targets))
            except ValueError:
                raise ControlError("出口编号无效") from None
        elif kind != "protocol" or tuple(targets) != PROTOCOLS:
            raise ControlError("协议测试仅支持 MASQUE 和 WireGuard")
    with locks():
        current = speed_test.status()
        cfg = read_config(timeout=2)
        nonce = cfg["main"].get("apply_nonce", "")
        if (
            current["backend"] != "masque"
            or cfg["main"].get("backend", "masque") != "masque"
            or cfg["main"].get("enabled") != "1"
            or current.get("applied_nonce", "") != nonce
        ):
            raise ControlError("请先启用并应用当前 WARP 主线路")
        if kind == "protocol" and current.get("protocol_managed") is not False:
            raise ControlError("当前协议受管理或管理状态尚未确认")
        if REQUEST.exists() or TRANSACTION.exists() or PROCESS_BLOCK.exists():
            raise ControlError("已有测速或入口事务")
        job = uuid.uuid4().hex
        record = dict(
            id=job,
            kind=kind,
            state="queued",
            targets=targets,
            bytes_per_target=min(speed_test.PATH_BUDGET, speed_test.BATCH_BUDGET // len(targets)),
            started_at=time.time(),
            rows=[],
            history=load(RESULT).get("history", {}),
        )
        CANCEL.unlink(missing_ok=True)
        save(record)
        try:
            atomic(REQUEST, json.dumps(dict(record, digest=config_key(cfg), nonce=nonce)))
        except OSError:
            finish(dict(record, outcome="error"), restored=True)
            raise
    return dict(id=job, state="submitted", request_id=request_id.split(":")[0])


def cancel():
    state = load(RESULT)
    if state.get("state") not in ("queued", "testing"):
        raise ControlError("没有可取消的线路测试")
    atomic(CANCEL, state["id"])
    return dict(id=state["id"], state="submitted")


def expire_queued():
    queued = load(REQUEST)
    if not queued or time.time() - queued["started_at"] <= 30:
        return
    try:
        with locks():
            if load(REQUEST).get("id") != queued["id"]:
                return
            REQUEST.unlink(missing_ok=True)
            public = {k: v for k, v in queued.items() if k not in ("digest", "nonce")}
            finish(dict(public, outcome="timeout"), restored=True)
    except BlockingIOError:
        pass


def restore_protocol_preferences(main):
    from mplane import ROOT
    from warp_control import configure_protocol

    if (ROOT / "var/lib/cloudflare-warp").is_dir():
        configure_protocol(ROOT, main.get("warp_protocol", "auto"), main.get("organization", ""))


def discard_interrupted():
    state = load(RESULT)
    REQUEST.unlink(missing_ok=True)
    CANCEL.unlink(missing_ok=True)
    if state and state.get("state") not in TERMINAL:
        if state.get("kind") == "protocol":
            try:
                restore_protocol_preferences(read_config(timeout=2)["main"])
            except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
                pass  # Normal startup retries with the current saved settings.
        save(dict(state, state="interrupted", finished_at=time.time()))


def finish(record, restored):
    if not record:
        return
    record = {key: value for key, value in record.items() if key != "restore_deadline"}
    state = record["outcome"] if restored else "error"
    history = dict(record.get("history", {}))
    for row in record["rows"]:
        history[row["target"]] = dict(row, batch_state=state)
    latest = sorted(history.items(), key=lambda item: item[1].get("measured_at", 0))[-32:]
    try:
        save(
            dict(
                record,
                state=state,
                history=dict(latest),
                restored=restored,
                finished_at=time.time(),
            )
        )
        CANCEL.unlink(missing_ok=True)
    except OSError:
        pass  # A results file cannot block restoration or take down a working line.


def confirm_target(target, deadline, checkpoint):
    from mplane import cli

    checkpoint()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    selected = parse_selected_network(
        cli("vnet", "--no-paginate", timeout=min(4, remaining)).stdout
    )
    if selected != target:
        raise TargetUnconfirmed("客户端未确认所选出口")


def measure_target(plane, target, deadline, checkpoint):
    from warp_control import ProtocolMismatch

    try:
        state = plane.connect(target, deadline=deadline, checkpoint=checkpoint)
    except ProtocolMismatch:
        raise TargetUnconfirmed("客户端未采用所选协议") from None
    except (TimeoutError, subprocess.TimeoutExpired):
        raise TargetUnconfirmed("等待目标隧道超时") from None
    except TARGET_ERRORS:
        raise TargetUnconfirmed("目标隧道连接失败") from None
    if state is not TunnelStatus.CONNECTED:
        raise TargetUnconfirmed("目标隧道状态未确认")
    try:
        confirm_target(target, deadline, checkpoint)
    except TargetUnconfirmed:
        raise
    except TARGET_ERRORS:
        raise TargetUnconfirmed("无法读取目标出口") from None
    checkpoint()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TargetUnconfirmed("等待严格出口探测超时")
    try:
        address = ipaddress.IPv4Address(plane.probe(strict=True, timeout=min(4, remaining)))
    except (TimeoutError, subprocess.TimeoutExpired):
        raise TargetUnconfirmed("严格出口探测超时") from None
    except TARGET_ERRORS:
        raise TargetUnconfirmed("严格探测未确认公网出口") from None
    if not address.is_global:
        raise TargetUnconfirmed("严格探测未确认公网出口")
    return str(address)


def trial_row(plane, target, deadline, checkpoint, budget):
    row = dict(bytes=0)
    try:
        row["exit_ip"] = measure_target(plane, target, deadline, checkpoint)
    except TargetUnconfirmed as error:
        return dict(row, state="skipped", message=str(error))
    except TARGET_ERRORS:
        return dict(row, state="skipped", message="目标未通过严格出口探测")

    next_check = 0

    def check_measurement():
        nonlocal next_check
        now = time.monotonic()
        if now >= next_check:
            checkpoint()
            next_check = now + speed_test.STATUS_CHECK_SECONDS
        return False

    try:
        row.update(
            speed_test.measure_path(
                "masque",
                "proxy",
                row["exit_ip"],
                deadline,
                budget=budget,
                cancelled=check_measurement,
            )
        )
        if row["state"] == "done":
            confirm_target(target, deadline, checkpoint)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired):
        row.update(state="error", message="目标测速失败或出口未经确认")
    return row


def protocol_trial(args):
    from mplane import Plane, cli
    from warp_control import parse_settings
    from warp_bootstrap_dns import FIELDS

    plane = Plane()
    plane.stop()
    plane.protocol_preference = args["protocol"]
    plane.organization = args["organization"]
    plane.dns_preferences = {key: args["dns"][key] for key in FIELDS if key in args["dns"]}
    try:
        plane.start()
    except TARGET_ERRORS:
        return dict(state="skipped", bytes=0, message="无法启动目标协议的 WARP 实例")
    if time.monotonic() >= args["deadline"]:
        return dict(state="skipped", bytes=0, message="等待目标协议启动超时")
    row = trial_row(plane, args["vnet"], args["deadline"], lambda: None, args["budget"])
    if row["state"] == "done":
        effective = None
        remaining = args["deadline"] - time.monotonic()
        if remaining > 0:
            try:
                effective = parse_settings(cli("settings", timeout=min(2, remaining)).stdout)[
                    "protocol"
                ]
            except TARGET_ERRORS:
                pass  # Retain the bytes even when the protocol proof is unavailable.
        if effective != args["protocol"]:
            row.update(state="invalid", message="测速结束时协议未确认")
    return row


def run_protocol_trial(main, protocol, target_deadline, checkpoint, budget):
    from warp_bootstrap_dns import FIELDS

    args = json.dumps(
        dict(
            parent_pid=os.getpid(),
            protocol=protocol,
            organization=main.get("organization", ""),
            dns={key: main[key] for key in FIELDS if key in main},
            vnet=resolve_exit(main),
            deadline=target_deadline,
            budget=budget,
        )
    )
    child = subprocess.Popen(
        [sys.executable, "-B", __file__, "--protocol-trial"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        while True:
            checkpoint()
            remaining = target_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                output, _ = child.communicate(input=args, timeout=min(0.75, remaining))
            except subprocess.TimeoutExpired:
                args = None  # communicate() retains the original input across a timeout.
                continue
            if child.returncode:
                raise ControlError("无法确认目标协议")
            result = json.loads(output)
            if not isinstance(result, dict) or not isinstance(result.get("bytes"), int):
                raise ValueError("Invalid protocol measurement")
            return result
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()


def execute(service, cfg, stopped, progress):
    """Return a record for the supervisor to finish after applying saved settings."""
    from endpoint_control import TRANSACTION, PROCESS_BLOCK
    from mplane import cli
    from warp_control import parse_settings

    queued = load(REQUEST)
    if not queued:
        return None
    deadline = time.monotonic() + min(BATCH_SECONDS, len(queued["targets"]) * TARGET_SECONDS)
    record = {k: v for k, v in queued.items() if k not in ("digest", "nonce")}
    claimed = False
    changed = False
    config_invalid = False
    protocol = queued.get("kind") == "protocol"
    protocol_started = False

    def checkpoint():
        if stopped():
            raise Interrupted("interrupted")
        try:
            cancelled = CANCEL.read_text() == queued["id"]
        except FileNotFoundError:
            cancelled = False
        if cancelled:
            raise Interrupted("cancelled")
        if time.monotonic() >= deadline:
            raise Interrupted("timeout")
        try:
            current = read_config(timeout=1)
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
            raise Interrupted("config_changed") from None
        if config_key(current) != queued["digest"]:
            raise Interrupted("config_changed")

    try:
        with locks():
            if load(REQUEST).get("id") != queued["id"]:
                return None
            REQUEST.unlink(missing_ok=True)
            claimed = True
            if service.update_job or TRANSACTION.exists() or PROCESS_BLOCK.exists():
                raise Interrupted("error")
            checkpoint()
            if config_key(cfg) != queued["digest"] or service.compiled["vnet"] != resolve_exit(
                cfg["main"]
            ):
                raise Interrupted("config_changed")
            if protocol and parse_settings(cli("settings", timeout=3).stdout)["managed"]:
                raise Interrupted("error")
            checkpoint()
            changed = True
            if not service.suspend_capture(protection_reason="reconfigure"):
                raise Interrupted("error")
            for index, target in enumerate(queued["targets"], 1):
                checkpoint()
                progress(record["id"], target, index, len(queued["targets"]))
                save(dict(record, state="testing", target=target, index=index))
                target_deadline = min(deadline, time.monotonic() + TARGET_SECONDS)

                if protocol:
                    protocol_started = True
                    try:
                        trial = run_protocol_trial(
                            cfg["main"],
                            target,
                            target_deadline,
                            checkpoint,
                            record["bytes_per_target"],
                        )
                    except TimeoutError:
                        trial = dict(state="skipped", bytes=0, message="等待目标协议测速超时")
                    except TARGET_ERRORS:
                        trial = dict(state="skipped", bytes=0, message="目标协议未通过严格出口探测")
                else:
                    trial = trial_row(
                        service.plane,
                        target,
                        target_deadline,
                        checkpoint,
                        record["bytes_per_target"],
                    )
                row = dict(trial, target=target, id=record["id"])
                row["measured_at"] = time.time()
                record["rows"].append(row)
                save(dict(record, state="testing", target=target, index=index))
            checkpoint()
            record["outcome"] = "done"
    except Interrupted as error:
        record["outcome"] = error.state
        config_invalid = error.state == "config_changed"
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired):
        if not claimed:
            # Submission may still hold the shared lock after publishing the request.
            return None
        record["outcome"] = "error"
    if protocol_started:
        # The supervisor restores from saved UCI after removing any temporary instance.
        try:
            if not service.stop(restore=False)["direct"]:
                record["outcome"] = "error"
            restore_protocol_preferences(read_config(timeout=2)["main"])
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            record["outcome"] = "error"
    record["bytes"] = sum(row["bytes"] for row in record["rows"])
    if changed or config_invalid:
        try:
            save(dict(record, state="restoring"))
        except OSError:
            pass  # Reporting failure must not prevent the normal apply path.

        return record
    finish(record, restored=True)
    return None


def stop_with_supervisor(parent_pid):
    # A killed supervisor must not leave a helper changing its replacement's tunnel.
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != parent_pid:
        raise RuntimeError("Supervisor is unavailable")


if __name__ == "__main__" and sys.argv[1:] == ["--protocol-trial"]:
    try:
        args = json.load(sys.stdin)
        stop_with_supervisor(args["parent_pid"])
        print(json.dumps(protocol_trial(args), ensure_ascii=False))
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired):
        raise SystemExit(1) from None
