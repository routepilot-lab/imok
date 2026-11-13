"""WARP entry selection. No registration API, identity export or IP-range scan."""

import fcntl
import ipaddress
import os
import stat
import hashlib
import json
from pathlib import Path
import time
import re
from runtime import RUN, atomic, read_config
from public_errors import ControlError

DIRECTORY = Path("/etc/routepilot")
ROOT = Path("/opt/routepilot/runtime/var/lib/cloudflare-warp")
INPUT = RUN / "endpoint-input"
STATE = RUN / "endpoint-state.json"
CACHE = DIRECTORY / "endpoint-candidates.json"
CONFIG = DIRECTORY / "endpoint-settings.json"
APPLIED = DIRECTORY / "endpoint-applied.json"
TRANSACTION = DIRECTORY / "endpoint-transaction.json"
WARNING = RUN / "endpoint-warning.json"
RECOVERY = DIRECTORY / "endpoint-recovery-block.json"
PROCESS_BLOCK = RUN / "endpoint-process-block.json"


def read(path, default=None):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return default
    with os.fdopen(fd) as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > 65536:
            raise ControlError("入口文件类型、属主或大小无效")
        return json.loads(handle.read(65537))


def address(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ControlError("请输入公网 IP:端口，IPv6 使用 [地址]:端口")
    try:
        host, port = value.rsplit(":", 1)
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        elif ":" in host:
            raise ValueError()
        ip = ipaddress.ip_address(host)
        if (
            "%" in host
            or ip.is_multicast
            or ip.is_reserved
            or not ip.is_global
            or not port.isascii()
            or not port.isdigit()
            or not 1 <= int(port) <= 65535
        ):
            raise ValueError()
    except (ValueError, TypeError):
        raise ControlError("入口必须是公网 IP 与有效端口，不接受域名、私网或命令") from None
    return ("[" + str(ip) + "]" if ip.version == 6 else str(ip)) + ":" + str(int(port))


def validate(data):
    if not isinstance(data, dict) or set(data) - {
        "mode",
        "address",
        "hours",
        "recovery",
        "revision",
        "staged",
    }:
        raise ControlError("入口设置格式无效")
    mode = data.get("mode", "preserve")
    if mode not in ("preserve", "auto", "manual"):
        raise ControlError("入口模式无效")
    hours = str(data.get("hours", 24))
    if hours not in ("0", "6", "12", "24", "72", "168"):
        raise ControlError("入口读取间隔无效")
    recovery = data.get("recovery", False)
    if not isinstance(recovery, bool):
        raise ControlError("故障恢复选项无效")
    if recovery:
        raise ControlError("首批暂不支持自动切换入口，请使用官方自动或手动应用")
    if (
        mode == "manual"
        and ipaddress.ip_address(
            address(data.get("address", "")).rsplit(":", 1)[0].strip("[]")
        ).version
        != 4
    ):
        raise ControlError("本路由器隔离环境只有 IPv4，暂不支持应用 IPv6 入口")
    if not isinstance(data.get("staged", False), bool):
        raise ControlError("入口导入状态无效")
    return dict(
        staged=data.get("staged", False),
        mode=mode,
        address=address(data.get("address", "")) if mode == "manual" else "",
        hours=int(hours),
        recovery=recovery,
        revision=str(data.get("revision", "")),
    )


def settings():
    return validate(read(CONFIG, {}))


def stage_restored_locked():
    """Caller holds both locks. Never replay a firmware-restored request journal."""
    if os.path.lexists(RECOVERY) or os.path.lexists(PROCESS_BLOCK):
        return
    cfg = settings()
    if (
        cfg["revision"]
        and not cfg["staged"]
        and not os.path.lexists(APPLIED)
        and not os.path.lexists(TRANSACTION)
    ):
        cfg.update(revision="", staged=True)
        durable(CONFIG, cfg)


def stage_restored_preferences():
    from worker_backend import guard

    try:
        with guard(), locked():
            stage_restored_locked()
        return True
    except Exception:
        return False  # Supervisor tick retries under the same locks before admitting a request.


def override():
    data = read(ROOT / "settings.json")
    if not isinstance(data, dict):
        raise ControlError("尚不能确认客户端入口设置")
    value = data.get("override_warp_endpoint")
    return address(value) if value else ""


def locked():
    RUN.mkdir(parents=True, exist_ok=True)
    path = RUN / "endpoint.lock"
    if path.is_symlink():
        raise ControlError("入口锁文件无效")
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        handle.close()
        raise
    return handle


def save(request_id):
    from worker_backend import guard
    from request_input import read_request

    with guard(), locked():
        raw = read_request(INPUT, request_id, 2048)
        if not isinstance(raw, dict) or set(raw) - {"mode", "address", "hours", "recovery"}:
            raise ControlError("入口输入字段无效")
        data = validate(raw)
        reject_new_requests()
        if data["mode"] != "preserve":
            if not capability().get("supported"):
                raise ControlError("当前客户端能力未确认，暂不能应用入口")
            override()
        request_id = request_id.split(":")[0]
        data.update(revision=request_id, staged=False)
        if not os.path.lexists(APPLIED):
            durable(APPLIED, {})
        durable(CONFIG, data)
        return dict(
            saved=True,
            request_id=request_id,
            settings=data,
            message="本次入口设置已保存；主线路运行时由后台应用，可能短暂重连",
        )


def save_schedule(request_id):
    from worker_backend import guard
    from request_input import read_request

    with guard(), locked():
        raw = read_request(RUN / "endpoint-schedule-input", request_id, 1024)
        if not isinstance(raw, dict) or set(raw) != {"hours"}:
            raise ControlError("同步间隔输入无效")
        cfg = settings()
        cfg["hours"] = raw["hours"]
        cfg = validate(cfg)
        # Preserve every endpoint preference and transaction revision, including staged imports.
        durable(CONFIG, cfg)
        return dict(
            saved=True,
            request_id=request_id.split(":")[0],
            settings=cfg,
            message="同步间隔已保存；入口及当前连接未改变",
        )


def main_context(main):
    from masque_config import resolve_exit

    # lan_networks is derived by the supervisor; every persisted field matters,
    # including scope/IPv6/rules edits whose writer forgot to rotate a nonce.
    return dict(
        vnet=resolve_exit(main), config={k: v for k, v in main.items() if k != "lan_networks"}
    )


def recovery_context(main):
    block = classify_recovery(main)
    if not block["startup_pause"] or main.get("backend", "masque") != "masque":
        raise ControlError("当前没有可受控恢复的主线路阻断")
    history = read(APPLIED)
    preferences = settings()
    if os.path.lexists(TRANSACTION):
        record = transaction_data()
    elif (
        isinstance(history, dict)
        and history.get("connected") is True
        and history.get("exit_verified") is True
        and history.get("state") == "failed"
        and history.get("rollback_confirmed") is True
    ):
        # finish_verified persists proof before deleting the journal/latch. A
        # power loss between those deletes must still permit explicit recovery.
        record = validate_transaction(dict(history, phase="unknown"))
    else:
        raise ControlError("缺少完整原事务或持久验证证明")
    check_block_owner(record)
    if (
        not isinstance(history, dict)
        or history.get("revision") != record["revision"]
        or preferences["staged"]
        or preferences["revision"] != record["revision"]
    ):
        raise ControlError("历史记录或入口偏好与原事务不一致")
    if record["vnet"] != main_context(main)["vnet"]:
        raise ControlError("原事务出口与当前配置不一致")
    expected = main.get("expected_" + main.get("exit", "default"), "")
    if expected and expected != record["baseline"]:
        raise ControlError("当前预期出口与原事务基线不一致")
    current = override()
    if current not in (record["previous"], record["target"]):
        raise ControlError("客户端入口不属于原事务，拒绝覆盖")
    evidence = {
        name: read(path)
        for name, path in (
            ("transaction", TRANSACTION),
            ("history", APPLIED),
            ("latch", RECOVERY),
            ("process", PROCESS_BLOCK),
            ("preferences", CONFIG),
        )
    }
    context = dict(evidence=evidence, main=main_context(main), override=current)
    digest = hashlib.sha256(
        json.dumps(context, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return record, digest


def recovery_offer(main):
    try:
        record, digest = recovery_context(main)
        return dict(
            available=True,
            revision=record["revision"],
            context=digest,
            message="可显式恢复原事务入口并验证；不会采用新的候选",
        )
    except Exception:
        return dict(
            available=False, message="缺少完整且一致的原事务证据；保持阻断，请按恢复指南人工诊断"
        )


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def save_recovery(request_id):
    from worker_backend import guard
    from request_input import read_request

    with guard(), locked():
        raw = read_request(RUN / "endpoint-recover-input", request_id, 2048)
        if (
            not isinstance(raw, dict)
            or set(raw) != {"confirm", "revision", "context"}
            or raw["confirm"] != "RESTORE"
        ):
            raise ControlError("请输入 RESTORE 确认恢复原事务入口")
        main = read_config()["main"]
        record, digest = recovery_context(main)
        if raw["revision"] != record["revision"] or raw["context"] != digest:
            raise ControlError("恢复证据已变化，请重新读取后确认")
        path = DIRECTORY / "endpoint-recover-request.json"
        if path.exists():
            try:
                old = read(path)
                fresh = old.get("boot") == boot_id() and -5 <= time.time() - old["time"] <= 300
            except Exception:
                fresh = False
            if fresh:
                raise ControlError("已有恢复请求，请读取其结果")
            remove_durable(path)
        durable(
            path,
            dict(
                request_id=request_id.split(":")[0],
                revision=record["revision"],
                context=digest,
                time=time.time(),
                boot=boot_id(),
            ),
        )
        return dict(
            saved=True,
            request_id=request_id.split(":")[0],
            message="受控恢复请求已提交；仍需等待验证结果",
        )


def collect():
    """Read only the client's already downloaded config; never claim network fetch."""
    now = time.time()
    try:
        conf = read(ROOT / "conf.json")
        if (
            not isinstance(conf, dict)
            or conf.get("tunnel_key_data", {}).get("tunnel_type") != "masque"
        ):
            raise ControlError("当前未发现 MASQUE 客户端配置，保留上次候选")
        rows = conf.get("endpoints")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
            raise ControlError("客户端候选格式无效，保留上次候选")
        entries = []
        for row in rows:
            if not isinstance(row, dict):
                raise ControlError("客户端候选格式无效")
            for family in ("v4", "v6"):
                if row.get(family):
                    item = address(row[family])
                    if item not in entries:
                        entries.append(item)
        if len(entries) > 32:
            raise ControlError("客户端候选数量超过限制")
        if not entries:
            raise ControlError("客户端未提供候选，保留上次候选")
        value = dict(
            entries=entries,
            collected_at=now,
            source_mtime=(ROOT / "conf.json").stat().st_mtime,
            source="official_client_cached_config",
            network_fetch=False,
            tested=False,
        )
        atomic(CACHE, json.dumps(value))
        atomic(
            STATE,
            json.dumps(
                dict(
                    state="ready",
                    checked_at=now,
                    message="已读取官方客户端本地候选；未主动向云端拉取，未逐项测速",
                )
            ),
        )
    except Exception:
        atomic(
            STATE,
            json.dumps(
                dict(
                    state="error",
                    checked_at=now,
                    message="候选读取失败，保留最近成功列表；当前连接未改变",
                )
            ),
        )
    return info()


def refresh():
    with locked():
        return collect()


def capability():
    from mplane import cli

    binary = ROOT.parents[2] / "usr/bin/warp-cli"
    try:
        signature = hashlib.sha256(binary.read_bytes()).hexdigest()
        cached = read(RUN / "endpoint-capability.json", {})
        if cached.get("binary") == signature:
            return cached
        result = cli("tunnel", "endpoint", "--help", check=False)
        supported = result.returncode == 0 and all(
            word in result.stdout.split() for word in ("set", "reset")
        )
        value = dict(binary=signature, supported=supported, checked_at=time.time())
        if supported:
            atomic(RUN / "endpoint-capability.json", json.dumps(value))
        return value
    except Exception:
        return dict(supported=False)


def optional(path):
    """Untrusted auxiliary state must not stop an established data plane."""
    try:
        value = read(path, {})
        if not isinstance(value, dict):
            raise ValueError()
        return value, False
    except Exception:
        return {}, True


def generation():
    record, error = optional(APPLIED)
    return record.get("revision", "") if not error else ""


def info():
    warnings = []
    try:
        cfg = settings()
    except Exception:
        cfg = validate({})
        warnings.append("入口偏好无法读取，已暂停新入口操作；原连接保留")
    try:
        current = override()
        observed = True
    except Exception:
        current = ""
        observed = False
    applied, error = optional(APPLIED)
    if error:
        warnings.append("入口历史记录无法读取，已暂停新入口操作；需核对原记录")
    candidates, error = optional(CACHE)
    if error:
        warnings.append("候选缓存无法读取，请重新读取候选")
    collection, error = optional(STATE)
    if error:
        warnings.append("候选读取状态无法读取")
    warning, _ = optional(WARNING)
    transaction = TRANSACTION.exists()
    block = classify_recovery()
    if block["code"] == "TRANSACTION_ACTIVE":
        try:
            main = read_config()["main"]
            from masque_config import resolve_exit

            resolve_exit(main)
        except Exception:
            block = classification(
                "CONTEXT_UNAVAILABLE",
                "暂不能读取当前生效出口，无法确认事务状态；请重新读取状态",
                source="context",
            )
        else:
            block = classify_recovery(main)
    try:
        offer = recovery_offer(read_config()["main"])
    except Exception:
        offer = dict(available=False, message="当前配置无法核对，保持阻断")
    recovery_result, _ = optional(RUN / "endpoint-recover-result.json")
    return dict(
        settings=cfg,
        configured_override=current,
        override_observed=observed,
        candidates=candidates,
        collection=collection,
        application=applied,
        recovery_offer=offer,
        recovery_result=recovery_result,
        warnings=warnings,
        warning=warning,
        recovery_block=block,
        transaction_pending=transaction,
        capability=capability(),
        pending=bool(
            not cfg["staged"] and cfg["revision"] and cfg["revision"] != applied.get("revision")
        ),
        network_fetch_supported=False,
    )


def wait_override(target, timeout=5):
    deadline = time.monotonic() + timeout
    while True:
        try:
            if override() == target:
                return
        except Exception:
            pass
        if time.monotonic() >= deadline:
            raise ControlError("客户端入口设置读回超时")
        time.sleep(0.2)


def set_override(target):
    from mplane import cli

    cli("tunnel", "endpoint", "set", target) if target else cli("tunnel", "endpoint", "reset")
    wait_override(target)


def baseline(service, main):
    selected = main.get("exit", "default")
    expected = main.get("expected_" + selected, "")
    if expected:
        ip = ipaddress.IPv4Address(expected)
        if not ip.is_global:
            raise ControlError("预期出口不是有效公网 IPv4")
        return str(ip), "configured"
    # Probe before changing anything. A stale service.exit_ip is never the sole baseline.
    from mplane import cli

    from warp_cli import TunnelStatus, read_status

    if read_status(cli) is not TunnelStatus.CONNECTED:
        raise ControlError("缺少可信原出口；请先恢复原线路或配置预期出口后再切换")
    observed = ipaddress.IPv4Address(service.plane.probe())
    if not observed.is_global:
        raise ControlError("原出口观测不是有效公网 IPv4")
    if service.exit_ip and str(observed) != service.exit_ip:
        raise ControlError("原出口已变化，请先重新核对线路；未切换入口")
    return str(observed), "fresh_probe"


def verify_connection(service, vnet, expected):
    service.plane.connect(vnet)
    actual = str(ipaddress.IPv4Address(service.plane.probe()))
    if actual != expected:
        raise ControlError("出口与切换前确认的基线不一致")


def transaction_data():
    return validate_transaction(read(TRANSACTION))


def validate_transaction(record):
    if not isinstance(record, dict) or not {
        "revision",
        "previous",
        "target",
        "baseline",
        "vnet",
        "phase",
    } <= set(record):
        raise ControlError("入口事务损坏，不能判定应用结果；请人工恢复")
    if (
        not isinstance(record["revision"], str)
        or not 1 <= len(record["revision"]) <= 128
        or record["phase"] not in ("apply", "verify", "rollback", "unknown")
    ):
        raise ControlError("入口事务状态无效")
    for key in ("previous", "target"):
        if not isinstance(record[key], str) or record[key] and address(record[key]) != record[key]:
            raise ControlError("入口事务地址无效")
    import uuid

    canonical = str(uuid.UUID(record["vnet"]))
    if not ipaddress.IPv4Address(record["baseline"]).is_global:
        raise ControlError("入口事务出口无效")
    return dict(record, vnet=canonical)


def durable(path, value):
    atomic(path, json.dumps(value))
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def classification(code="", reason="", *, pause=False, resume=False, source="", revision=""):
    return dict(
        code=code,
        reason=reason,
        new_request_block=bool(code),
        startup_pause=pause,
        resume_existing=resume,
        terminal=bool(code and code != "TRANSACTION_ACTIVE"),
        source=source,
        revision=revision,
    )


def critical_history(record):
    state = record.get("state")
    return (
        state in ("applying", "verifying", "rolling_back", "unknown")
        or state == "failed"
        and record.get("rollback_confirmed") is not True
    )


def classify_recovery(main=None):
    """Pure classification. Absence of parseable history never clears a durable block."""
    if os.path.lexists(RECOVERY):
        try:
            saved = read(RECOVERY)
            if (
                not isinstance(saved, dict)
                or saved.get("version") != 1
                or not isinstance(saved.get("code"), str)
                or not isinstance(saved.get("source"), str)
                or not isinstance(saved.get("revision"), str)
            ):
                raise ValueError()
            return classification(
                "RECOVERY_LATCHED",
                "已记录的关键恢复阻断尚未验证解除；主线路暂停，需人工核对",
                pause=True,
                source=saved["source"],
                revision=saved["revision"],
            )
        except Exception:
            return classification(
                "RECOVERY_RECORD_INVALID",
                "持久恢复阻断记录无法读取；主线路暂停，需人工核对",
                pause=True,
                source="latch",
            )
    if os.path.lexists(PROCESS_BLOCK):
        try:
            saved = read(PROCESS_BLOCK)
            if (
                not isinstance(saved, dict)
                or saved.get("version") != 1
                or not isinstance(saved.get("revision"), str)
            ):
                raise ValueError()
            return classification(
                "PROCESS_RECOVERY_PAUSED",
                "本进程已记录恢复失败；主线路保持暂停，可能直连，需人工核对",
                pause=True,
                source=saved.get("source", "transaction"),
                revision=saved["revision"],
            )
        except Exception:
            return classification(
                "PROCESS_BLOCK_INVALID",
                "进程恢复阻断记录无法读取；主线路保持暂停，需人工核对",
                pause=True,
                source="runtime",
            )
    if os.path.lexists(TRANSACTION):
        try:
            record = transaction_data()
            if main is not None:
                from masque_config import resolve_exit

                if resolve_exit(main) != record["vnet"]:
                    return classification(
                        "TRANSACTION_VNET_MISMATCH",
                        "未完成事务与当前生效出口不一致；主线路暂停，需人工核对",
                        pause=True,
                        source="transaction",
                        revision=record["revision"],
                    )
            if record["phase"] == "unknown":
                return classification(
                    "TRANSACTION_UNKNOWN",
                    "入口恢复结果未知；主线路暂停，可能直连，需人工核对",
                    pause=True,
                    source="transaction",
                    revision=record["revision"],
                )
            history, bad = optional(APPLIED)
            if (
                not bad
                and critical_history(history)
                and (
                    history.get("revision") != record["revision"]
                    or history.get("state") in ("unknown", "failed")
                )
            ):
                return classification(
                    "RECOVERY_EVIDENCE_CONFLICT",
                    "历史关键状态与可恢复事务不一致；主线路暂停，需人工核对",
                    pause=True,
                    source="transaction",
                    revision=record["revision"],
                )
            return classification(
                "TRANSACTION_ACTIVE",
                "已有入口事务正在处理，拒绝新请求；只允许原事务恢复",
                resume=main is not None,
                source="transaction",
                revision=record["revision"],
            )
        except Exception:
            return classification(
                "TRANSACTION_INVALID",
                "未完成事务无法识别；主线路暂停，需人工核对",
                pause=True,
                source="transaction",
            )
    record, bad = optional(APPLIED)
    if bad:
        return classification(
            "HISTORY_UNREADABLE",
            "历史记录无法读取，新请求未保存；当前连接保留，需核对原记录",
            source="history",
        )
    if not record:
        return classification()
    state = record.get("state")
    revision = record.get("revision", "")
    revision = revision if isinstance(revision, str) else ""
    if critical_history(record):
        return classification(
            "LEGACY_RECOVERY_UNKNOWN",
            "旧版恢复结果未确认，新请求未保存；主线路暂停，需人工核对",
            pause=True,
            source="history",
            revision=revision,
        )
    if not isinstance(state, str) or state not in ("applied", "preserved", "rejected", "failed"):
        return classification(
            "HISTORY_UNREADABLE",
            "历史记录格式无法识别，新请求未保存；当前连接保留，需核对原记录",
            source="history",
        )
    return classification()


def observe_recovery(main=None):
    """Call under shared operation + endpoint locks, before admitting mutations/startup."""
    block = classify_recovery(main)
    if block["startup_pause"] and not os.path.lexists(RECOVERY):
        durable(
            RECOVERY,
            dict(
                version=1,
                code=block["code"],
                source=block["source"],
                revision=block["revision"],
                observed_at=time.time(),
            ),
        )
        return classify_recovery(main)
    return block


class RecoveryBlocked(ControlError):
    def __init__(self, block):
        self.code = block["code"]
        super().__init__("[" + self.code + "] " + block["reason"])


def reject_new_requests():
    block = observe_recovery()
    if block["new_request_block"]:
        raise RecoveryBlocked(block)


def check_block_owner(record):
    for path, source in ((RECOVERY, "transaction"), (PROCESS_BLOCK, None)):
        if not os.path.lexists(path):
            continue
        saved = read(path)
        if (
            not isinstance(saved, dict)
            or saved.get("version") != 1
            or source
            and saved.get("source") != source
            or saved.get("revision") != record.get("revision")
        ):
            raise ControlError("本次验证不能解除其他或损坏的恢复阻断记录")


def remove_durable(path):
    if os.path.lexists(path):
        path.unlink()
    sync_directory(path.parent)


def clear_verified_block(record):
    check_block_owner(record)
    if os.path.lexists(RECOVERY):
        remove_durable(RECOVERY)
    if os.path.lexists(PROCESS_BLOCK):
        remove_durable(PROCESS_BLOCK)


def clean_warning():
    try:
        WARNING.unlink(missing_ok=True)
    except OSError:
        pass  # Auxiliary warning cleanup must never roll back a verified connection.


def pause_reason(main):
    from worker_backend import guard

    try:
        with guard(), locked():
            block = observe_recovery(main)
    except BlockingIOError:
        block = classify_recovery(main)
    except Exception:
        # A persistence failure must never authorize a new client startup.
        return "恢复阻断持久化未确认；主线路保持暂停，请检查存储并人工核对"
    return block["reason"] if block["startup_pause"] else ""


class Manager:
    def __init__(self):
        self.last_check = 0
        self.process_block = None

    def poll(self):
        try:
            cfg = settings()
        except Exception:
            return
        now = time.monotonic()
        if now - self.last_check >= 30:
            self.last_check = now
            state, _ = optional(STATE)
            stamp = state.get("checked_at", 0)
            if cfg["hours"] and (
                not stamp or time.time() - stamp >= cfg["hours"] * 3600 or stamp > time.time()
            ):
                try:
                    refresh()
                except (BlockingIOError, OSError, ValueError):
                    pass

    def halt_recovery(self, record):
        # Set memory first: neither a full persistent filesystem nor tmpfs may authorize another attempt.
        source = (
            "transaction"
            if {"previous", "target", "baseline", "vnet"} <= set(record)
            else "request"
        )
        self.process_block = classification(
            "PROCESS_RECOVERY_PAUSED",
            "恢复或关键记录写入失败；主线路保持暂停，需人工核对",
            pause=True,
            source=source,
            revision=record["revision"],
        )
        value = dict(version=1, source=source, revision=record["revision"], observed_at=time.time())
        try:
            atomic(PROCESS_BLOCK, json.dumps(value))
        except Exception:
            pass
        record.update(state="unknown", rollback_confirmed=False, phase="unknown")
        for path in (TRANSACTION, APPLIED) if source == "transaction" else (APPLIED,):
            try:
                durable(path, record)
            except Exception:
                pass
        self.persist_process_block()

    def persist_process_block(self):
        if not self.process_block:
            return
        try:
            from worker_backend import guard

            with guard(), locked():
                if not os.path.lexists(RECOVERY):
                    durable(
                        RECOVERY,
                        dict(
                            version=1,
                            code=self.process_block["code"],
                            source=self.process_block["source"],
                            revision=self.process_block["revision"],
                            observed_at=time.time(),
                        ),
                    )
        except Exception:
            pass

    def pause_reason(self, main):
        if self.process_block:
            self.persist_process_block()
            return self.process_block["reason"]
        return pause_reason(main)

    def store_ack(self, record):
        try:
            durable(APPLIED, record)
        except Exception:
            self.halt_recovery(record)
            raise ControlError("入口请求结果无法持久确认；主线路暂停，需人工检查") from None

    def recover_requested(self, service, main):
        try:
            return self._recover_requested(service, main)
        except BlockingIOError:
            return False  # Another operation owns the locks; no startup or stop.

    def _recover_requested(self, service, main):
        """Only an explicit, fresh, context-bound operator request may cross the latch."""
        path = DIRECTORY / "endpoint-recover-request.json"
        if not path.exists():
            return False
        from worker_backend import guard
        from mplane import cli

        with guard(), locked():
            request = None
            record = None
            started = False
            try:
                request = read(path)
                if not isinstance(request, dict) or set(request) != {
                    "request_id",
                    "revision",
                    "context",
                    "time",
                    "boot",
                }:
                    raise ControlError("恢复请求格式无效")
                if not isinstance(request["request_id"], str) or not re.fullmatch(
                    "[a-f0-9]{32}", request["request_id"]
                ):
                    raise ControlError("恢复请求编号无效")
                if request["boot"] != boot_id() or not -5 <= time.time() - request["time"] <= 300:
                    raise ControlError("恢复请求已过期或跨重启，请重新确认")
                record, digest = recovery_context(main)
                if digest != request["context"] or record["revision"] != request["revision"]:
                    raise ControlError("恢复上下文已变化，未执行恢复")
                if service.backend != "masque":
                    raise ControlError("当前不在主线路恢复环境")
                before_main = main_context(main)
                archive = DIRECTORY / "recovery-audit"
                archive.mkdir(mode=0o700, exist_ok=True)
                if archive.is_symlink():
                    raise ControlError("恢复证据目录无效")
                sync_directory(DIRECTORY)
                durable(
                    archive / (request["request_id"] + ".json"),
                    dict(
                        request=request,
                        transaction=record,
                        latch=read(RECOVERY),
                        history=read(APPLIED),
                        process=read(PROCESS_BLOCK),
                    ),
                )
                # Consume intent before any mutation: a crash always requires another explicit request.
                remove_durable(path)
                started = True
                if not service.suspend_capture():
                    raise ControlError("未确认接管撤回，未恢复入口")
                service.plane.protocol_preference = main.get("warp_protocol", "auto")
                service.plane.organization = main.get("organization", "")
                service.plane.start()
                record.update(state="rolling_back", phase="rollback", time=time.time())
                durable(TRANSACTION, record)
                durable(APPLIED, record)
                cli("disconnect")
                set_override(record["previous"])
                verify_connection(service, record["vnet"], record["baseline"])
                if (
                    main_context(read_config()["main"]) != before_main
                    or override() != record["previous"]
                ):
                    raise ControlError("验证期间配置或入口发生变化")
                record.update(
                    state="failed",
                    phase="rolled_back",
                    connected=True,
                    exit_verified=True,
                    rollback_confirmed=True,
                    operator_recovered=True,
                    time=time.time(),
                )
                self.finish_verified(record)
            except Exception:
                if started and record is not None:
                    self.halt_recovery(record)
                    try:
                        service.stop(restore=False)
                    except Exception:
                        pass
                # Invalid requests must not leave an automatic retry queued.
                if path.exists():
                    remove_durable(path)
                atomic(
                    RUN / "endpoint-recover-result.json",
                    json.dumps(
                        dict(
                            request_id=request.get("request_id", "")
                            if isinstance(request, dict)
                            else "",
                            state="failed",
                            time=time.time(),
                            message="受控恢复未通过；阻断保留，请重新核对证据和网络",
                        )
                    ),
                )
                return False
            atomic(
                RUN / "endpoint-recover-result.json",
                json.dumps(
                    dict(
                        request_id=request["request_id"],
                        state="done",
                        time=time.time(),
                        message="原入口及原预期出口已验证，恢复阻断已解除",
                    )
                ),
            )
            return True

    def finish_verified(self, record):
        check_block_owner(record)
        if self.process_block and self.process_block["revision"] != record["revision"]:
            raise ControlError("本次验证不能解除其他进程内恢复阻断")
        # Commit proof first. A crash before deletion can only replay the conservative old-path rollback.
        durable(APPLIED, record)
        remove_durable(TRANSACTION)
        clear_verified_block(record)
        self.process_block = None

    def rollback(self, service, record):
        from mplane import cli

        check_block_owner(record)
        try:
            record.update(state="rolling_back", phase="rollback", time=time.time())
            durable(TRANSACTION, record)
            durable(APPLIED, record)
            cli("disconnect")
            set_override(record["previous"])
            verify_connection(service, record["vnet"], record["baseline"])
            record.update(
                state="failed",
                rollback_confirmed=True,
                connected=True,
                exit_verified=True,
                phase="rolled_back",
            )
            self.finish_verified(record)
        except Exception:
            self.halt_recovery(record)
            raise ControlError(
                "原路径恢复或持久记录未确认；主线路保持暂停，可能直连，请人工检查"
            ) from None

    def tick(self, service, main):
        if service.backend == "worker" or not service.plane.owned:
            return False
        if self.process_block:
            raise RecoveryBlocked(self.process_block)
        from worker_backend import guard
        from supervisor import output
        from masque_config import resolve_exit

        with guard(), locked():
            block = observe_recovery(main)
            if block["startup_pause"]:
                raise RecoveryBlocked(block)
            if block["resume_existing"]:
                record = transaction_data()
                try:
                    if not service.suspend_capture():
                        raise ControlError("接管撤回未确认，未恢复入口事务")
                    output(
                        "reconnecting",
                        "正在恢复未完成的入口事务；"
                        + (
                            "受保护设备保持断网"
                            if service.egress_guard.selectors
                            else "可能短暂直连"
                        ),
                        endpoint_generation=record["revision"],
                    )
                    self.rollback(service, record)
                except Exception:
                    if not self.process_block:
                        self.halt_recovery(record)
                    raise ControlError("原事务恢复未确认；主线路暂停，需人工检查") from None
                return True
            if block["new_request_block"]:
                atomic(WARNING, json.dumps(dict(message=block["reason"], time=time.time())))
                return False
            try:
                stage_restored_locked()
                cfg = settings()
                record = read(APPLIED, {})
            except Exception:
                atomic(
                    WARNING,
                    json.dumps(
                        dict(
                            message="入口辅助记录异常，已暂停新入口操作；原连接保留",
                            time=time.time(),
                        )
                    ),
                )
                return False
            if cfg["staged"]:
                return False
            pending = bool(cfg["revision"] and cfg["revision"] != record.get("revision"))
            if not pending:
                return False
            if cfg["mode"] == "preserve":
                self.store_ack(dict(revision=cfg["revision"], state="preserved", time=time.time()))
                clean_warning()
                return False
            if not getattr(service, "compiled", None):
                atomic(
                    WARNING,
                    json.dumps(
                        dict(
                            message="入口请求等待原线路建立并确认出口基线；尚未消费本次请求",
                            time=time.time(),
                        )
                    ),
                )
                return False
            # Preflight errors are local operation failures, not data-plane failures.
            try:
                if not capability().get("supported"):
                    raise ControlError("客户端入口能力未确认")
                target = "" if cfg["mode"] == "auto" else cfg["address"]
                previous = override()
                expected, source = baseline(service, main)
                vnet = resolve_exit(main)
            except Exception:
                if self.process_block:
                    raise RecoveryBlocked(self.process_block)
                self.store_ack(
                    dict(
                        revision=cfg["revision"],
                        state="rejected",
                        time=time.time(),
                        message="入口应用前校验未通过：请核对能力、当前入口和原出口基线；当前连接未改变",
                    )
                )
                return False
            record = dict(
                revision=cfg["revision"],
                state="applying",
                phase="apply",
                target=target,
                previous=previous,
                baseline=expected,
                baseline_source=source,
                vnet=vnet,
                time=time.time(),
            )
            try:
                # Directory-synced intent must exist before withdrawal or endpoint mutation.
                durable(TRANSACTION, record)
                durable(APPLIED, record)
                output(
                    "reconnecting",
                    "正在应用入口；"
                    + (
                        "将暂停受保护设备的外网访问"
                        if service.egress_guard.selectors
                        else "将短暂撤回接管并允许宽带直连"
                    )
                    + "，恢复后重新验证原出口",
                    endpoint_generation=cfg["revision"],
                )
                if not service.suspend_capture():
                    raise ControlError("接管撤回未确认，未切换入口")
            except Exception:
                self.halt_recovery(record)
                raise ControlError("关键入口意图无法持久保存；未切换入口，主线路暂停") from None
            try:
                from mplane import cli

                cli("disconnect")
                set_override(target)
                record.update(state="verifying", phase="verify")
                durable(APPLIED, record)
                verify_connection(service, vnet, expected)
                record.update(
                    state="applied",
                    phase="done",
                    connected=True,
                    exit_verified=True,
                    time=time.time(),
                )
            except Exception:
                self.rollback(service, record)
            else:
                try:
                    self.finish_verified(record)
                except Exception:
                    self.halt_recovery(record)
                    raise ControlError(
                        "连接已验证但持久确认未完成；主线路暂停，需人工检查"
                    ) from None
                clean_warning()
            return True
